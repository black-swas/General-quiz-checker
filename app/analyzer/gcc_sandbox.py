"""Compile and run one student submission under OS-level limits (POSIX) across supported runtimes.

Supports C (gcc), C++ (g++), Python (python3 -I -B), Bash (bash), and JavaScript (node when installed).
For languages without a local sandbox runtime (e.g. Java, Rust, SQL, R, Markdown/Text), returns a clean
unverified result so static analysis, plagiarism checks, and the 4-way LLM evaluator handle grading.

Isolation model (no root, no containers needed, so it works inside a Render web service):
  * scrubbed environment for compiler and program (no secrets are ever passed to children)
  * RLIMIT_CPU / AS / FSIZE / CORE / NPROC on the program, CPU / AS / FSIZE on the compiler
  * own session + process group; the whole group is SIGKILLed on exit, timeout or output flood
  * stdout is read incrementally and capped, so an endless puts()/print() loop cannot exhaust memory
  * the server process itself is non-dumpable (see app.security.harden_process), so a student
    program cannot read the server's environment through /proc/<ppid>/environ
Static pre-checks (c_static_analyzer) refuse to run submissions that try obvious escapes.
"""
import copy
import functools
import hashlib
import math
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Tuple, Union

try:
    import resource
except ImportError:  # pragma: no cover - only on non-POSIX development machines
    resource = None  # type: ignore[assignment]

from app.config import settings
from app.schemas import GccTestResult, TestCase

_VERIFICATION_CACHE: OrderedDict[Tuple[str, str, str, float, str], GccTestResult] = OrderedDict()
_CACHE_MAX_ENTRIES = 2048


def clear_verification_cache() -> None:
    _VERIFICATION_CACHE.clear()


def _cache_put(key: Tuple[str, str, str, float, str], result: GccTestResult, max_entries: int = _CACHE_MAX_ENTRIES) -> None:
    _VERIFICATION_CACHE[key] = result
    _VERIFICATION_CACHE.move_to_end(key)
    if len(_VERIFICATION_CACHE) > max_entries:
        _VERIFICATION_CACHE.popitem(last=False)


def _test_cases_fingerprint(test_cases: List[TestCase]) -> str:
    h = hashlib.sha256()
    for tc in test_cases:
        h.update(tc.input_data.encode("utf-8", errors="replace"))
        h.update(b"-->")
        h.update(tc.expected_output.encode("utf-8", errors="replace"))
        h.update(b"|sample=" if tc.is_sample else b"|hidden=")
    return h.hexdigest()


if os.name == "nt":
    SAFE_ENV = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", "C:\\Windows"),
        "COMSPEC": os.environ.get("COMSPEC", "cmd.exe"),
        "TEMP": os.environ.get("TEMP", os.environ.get("TMP", os.path.expanduser("~\\AppData\\Local\\Temp"))),
        "TMP": os.environ.get("TMP", os.environ.get("TEMP", os.path.expanduser("~\\AppData\\Local\\Temp"))),
        "USERPROFILE": os.environ.get("USERPROFILE", ""),
        "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", ""),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
else:
    SAFE_ENV = {"PATH": "/usr/bin:/bin:/usr/local/bin", "LC_ALL": "C", "LANG": "C", "PYTHONDONTWRITEBYTECODE": "1"}


def get_safe_env(language: str = "c", tmpdir: Optional[str] = None) -> Dict[str, str]:
    """Returns a scrubbed environment dictionary.
    
    On Windows, prepends the compiler directory (e.g. MinGW/MSYS2 bin) to PATH so Windows
    always loads the correct runtime DLLs (libwinpthread-1.dll, libgcc_s_seh-1.dll, etc.)
    and prevents DLL collision with third-party software installed earlier in system PATH.
    """
    env = dict(SAFE_ENV)
    if os.name == "nt":
        extra_paths: List[str] = []
        for bin_name in ("gcc", "g++", "clang", "node", "go", "javac", "java"):
            target_bin = shutil.which(bin_name)
            if target_bin:
                b_dir = os.path.dirname(os.path.abspath(target_bin))
                if b_dir not in extra_paths and os.path.isdir(b_dir):
                    extra_paths.append(b_dir)
        py_dir = os.path.dirname(os.path.abspath(sys.executable))
        if py_dir not in extra_paths and os.path.isdir(py_dir):
            extra_paths.append(py_dir)

        # Standard Windows installation paths for MSYS2 MinGW, Go, OpenJDK, and Node.js
        for fallback_dir in (
            r"C:\msys64\mingw64\bin",
            r"C:\Program Files\Go\bin",
            r"C:\Program Files\Microsoft\jdk-17.0.20.101-hotspot\bin",
            r"C:\Program Files\nodejs",
        ):
            if os.path.isdir(fallback_dir) and fallback_dir not in extra_paths:
                extra_paths.append(fallback_dir)

        if extra_paths:
            current_path = env.get("PATH", "")
            env["PATH"] = os.pathsep.join(extra_paths) + os.pathsep + current_path

        if tmpdir:
            env["TEMP"] = tmpdir
            env["TMP"] = tmpdir
            env["TMPDIR"] = tmpdir
            env["HOME"] = tmpdir
        if language in ("go", "golang"):
            env["GO111MODULE"] = "off"
    elif tmpdir:
        env["TMPDIR"] = tmpdir
        env["HOME"] = tmpdir
        if language in ("go", "golang"):
            env["GO111MODULE"] = "off"
    return env

MAX_STDIN_BYTES = 49152  # stays below the 64 KiB pipe buffer so writing never blocks
_PATH_RE = re.compile(r"(?<![\w\"'.])(?:/[\w.\-]+){2,}")


@functools.lru_cache(maxsize=1)
def sandbox_runtimes() -> Dict[str, bool]:
    py_found = bool(shutil.which("python3") or shutil.which("python"))
    gcc_found = bool(shutil.which("gcc") or shutil.which("clang") or shutil.which("cl") or os.path.exists(r"C:\msys64\mingw64\bin\gcc.exe"))
    gpp_found = bool(shutil.which("g++") or shutil.which("clang++") or gcc_found or os.path.exists(r"C:\msys64\mingw64\bin\g++.exe"))
    node_found = bool(shutil.which("node") or os.path.exists(r"C:\Program Files\nodejs\node.exe"))
    go_found = bool(shutil.which("go") or os.path.exists(r"C:\Program Files\Go\bin\go.exe"))
    javac_found = bool(shutil.which("javac") or os.path.exists(r"C:\Program Files\Microsoft\jdk-17.0.20.101-hotspot\bin\javac.exe"))
    java_found = bool(shutil.which("java") or os.path.exists(r"C:\Program Files\Microsoft\jdk-17.0.20.101-hotspot\bin\java.exe"))
    return {
        "c": gcc_found,
        "cpp": gpp_found,
        "python": py_found,
        "bash": shutil.which("bash") is not None,
        "javascript": node_found,
        "go": go_found,
        "java": javac_found and java_found,
    }


def reset_runtime_cache() -> None:
    sandbox_runtimes.cache_clear()


def gcc_available() -> bool:
    r = sandbox_runtimes()
    return r.get("c", False) or r.get("cpp", False)



def runtime_available_for(language: str) -> bool:
    lang = (language or "c").lower()
    alias_map = {"js": "javascript", "golang": "go", "c++": "cpp"}
    canonical = alias_map.get(lang, lang)
    return sandbox_runtimes().get(canonical, False)


def normalize_output(text: str) -> str:
    return "\n".join(line.strip() for line in text.strip().splitlines() if line.strip())


def _try_float(s: str) -> Optional[float]:
    s_clean = s.strip()
    # Do not coerce integers with leading zeros (e.g. '007', '01') into float matches
    if re.match(r"^[+-]?0\d+", s_clean):
        return None
    # Do not coerce word variations of infinity into floats
    if s_clean.lower() in ("inf", "-inf", "+inf", "infinity", "-infinity", "+infinity"):
        return None
    try:
        val = float(s_clean)
        if math.isnan(val) or math.isinf(val):
            return None
        return val
    except (ValueError, TypeError):
        return None


def outputs_match(
    expected: str,
    actual: str,
    rel_tol: Optional[float] = None,
    abs_tol: Optional[float] = None,
    match_mode: str = "exact",
) -> bool:
    """Exact normalized match, plus float tolerance when tokens are numbers (including scientific notation).
    Supports match_mode: 'exact' (default), 'sorted_tokens', 'unordered_set', or 'regex'.
    """
    if expected == actual:
        return True

    mode = (match_mode or "exact").lower()
    if mode == "regex":
        try:
            return bool(re.search(expected.strip(), actual.strip()))
        except re.error:
            pass
    elif mode == "sorted_tokens":
        if sorted(expected.split()) == sorted(actual.split()):
            return True
    elif mode == "unordered_set":
        exp_lines_set = {line.strip() for line in expected.splitlines() if line.strip()}
        act_lines_set = {line.strip() for line in actual.splitlines() if line.strip()}
        if exp_lines_set == act_lines_set and exp_lines_set:
            return True
        exp_tok_set = set(expected.split())
        act_tok_set = set(actual.split())
        if exp_tok_set == act_tok_set and exp_tok_set:
            return True

    rel_tolerance = float(rel_tol if rel_tol is not None else settings.float_rel_tol)
    abs_tolerance = float(abs_tol if abs_tol is not None else settings.float_abs_tol)

    exp_lines = expected.splitlines()
    act_lines = actual.splitlines()

    # Strategy 1: line-by-line token comparison
    if len(exp_lines) == len(act_lines) and exp_lines:
        lines_match = True
        has_float = False
        for el, al in zip(exp_lines, act_lines):
            etoks = el.split()
            atoks = al.split()
            if len(etoks) != len(atoks) or not etoks:
                lines_match = False
                break
            for e_tok, a_tok in zip(etoks, atoks):
                if e_tok == a_tok:
                    continue
                if e_tok.lower() == a_tok.lower() and e_tok.lower() in ("true", "false", "yes", "no", "null", "none"):
                    continue
                ef, af = _try_float(e_tok), _try_float(a_tok)
                if ef is not None and af is not None:
                    if math.isinf(ef) and math.isinf(af) and (ef > 0) == (af > 0):
                        continue
                    if math.isclose(ef, af, rel_tol=rel_tolerance, abs_tol=abs_tolerance):
                        has_float = True
                        continue
                lines_match = False
                break
            if not lines_match:
                break
        if lines_match:
            return True

    # Strategy 2: whole token stream comparison (handles line wrapping differences)
    exp_all_toks = expected.split()
    act_all_toks = actual.split()
    if len(exp_all_toks) == len(act_all_toks) and exp_all_toks:
        all_match = True
        for e_tok, a_tok in zip(exp_all_toks, act_all_toks):
            if e_tok == a_tok:
                continue
            if e_tok.lower() == a_tok.lower() and e_tok.lower() in ("true", "false", "yes", "no", "null", "none"):
                continue
            ef, af = _try_float(e_tok), _try_float(a_tok)
            if ef is not None and af is not None:
                if math.isinf(ef) and math.isinf(af) and (ef > 0) == (af > 0):
                    continue
                if math.isclose(ef, af, rel_tol=rel_tolerance, abs_tol=abs_tolerance):
                    continue
            all_match = False
            break
        if all_match:
            return True

    return False


def sanitize_compiler_errors(stderr_text: str, tmpdir: str) -> str:
    """Strip sandbox paths and cap the length. (Dangerous #includes are rejected before compiling.)"""
    if not stderr_text:
        return ""
    cleaned = stderr_text.replace(tmpdir + "/", "").replace(tmpdir + "\\", "").replace(tmpdir, "")
    return "\n".join(_PATH_RE.sub("[path]", line) for line in cleaned.splitlines()[:30])


def diagnose_case_failure(
    expected: str,
    actual: str,
    rc: int,
    timed_out: bool,
    truncated: bool,
    match_mode: str = "exact",
    rel_tol: float = 1e-4,
    abs_tol: float = 1e-4,
) -> str:
    """Classify why a single test case failed to give TAs immediate diagnostic context."""
    if timed_out:
        return "TIMEOUT"
    if truncated:
        return "OUTPUT_TRUNCATED"
    if rc != 0:
        sig = -rc if rc < 0 else (rc - 128 if rc > 128 else 0)
        if sig == 11:
            return "SEGFAULT"
        if sig == 8:
            return "FLOATING_POINT_EXCEPTION"
        if sig == 6:
            return "ABORTED"
        return "RUNTIME_ERROR"
    if outputs_match(expected, actual, match_mode=match_mode, rel_tol=rel_tol, abs_tol=abs_tol):
        return "PASS"

    exp_toks = expected.split()
    act_toks = actual.split()

    # 1. Single scalar comparison
    try:
        exp_num = int(expected.strip())
        act_num = int(actual.strip())
        if abs(exp_num - act_num) == 1:
            return "OFF_BY_ONE"
        if (exp_num > 2_147_483_647 and act_num < exp_num) or (exp_num > 0 and act_num < 0 and exp_num > 2_000_000_000):
            return "INTEGER_OVERFLOW"
    except ValueError:
        pass

    # 2. Multi-token sequence with exactly one mismatch
    if len(exp_toks) == len(act_toks) and exp_toks:
        mismatches = [(e, a) for e, a in zip(exp_toks, act_toks) if e != a]
        if len(mismatches) == 1:
            e_str, a_str = mismatches[0]
            try:
                e_val, a_val = int(e_str), int(a_str)
                if abs(e_val - a_val) == 1:
                    return "OFF_BY_ONE"
                if (e_val > 2_147_483_647 and a_val < e_val) or (e_val > 0 and a_val < 0 and e_val > 2_000_000_000):
                    return "INTEGER_OVERFLOW"
            except ValueError:
                pass

    prompt_words_re = r"(?:enter|input|output|answer|result|sum|count|total|val)\b"
    if expected.lower().replace(" ", "") == actual.lower().replace(" ", ""):
        return "CASE_OR_FORMAT_MISMATCH"
    if expected.strip() and actual.strip().endswith(expected.strip()) and re.search(prompt_words_re, actual, re.I):
        return "CASE_OR_FORMAT_MISMATCH"
    if actual.strip() and expected.strip().endswith(actual.strip()) and re.search(prompt_words_re, expected, re.I):
        return "CASE_OR_FORMAT_MISMATCH"
    if exp_toks and all(tok in act_toks for tok in exp_toks) and any(re.search(prompt_words_re, t, re.I) for t in act_toks):
        return "CASE_OR_FORMAT_MISMATCH"
    if act_toks and all(tok in exp_toks for tok in act_toks) and any(re.search(prompt_words_re, t, re.I) for t in exp_toks):
        return "CASE_OR_FORMAT_MISMATCH"

    return "WRONG_ANSWER"


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
    _JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
    _JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
    _JobObjectExtendedLimitInformation = 9


def _assign_windows_job(proc: subprocess.Popen, mem_limit_bytes: int) -> Optional[int]:
    """Applies Windows Job Object hard memory limits and kill-on-close to proc."""
    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes.windll.kernel32
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | _JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | _JOB_OBJECT_LIMIT_JOB_MEMORY
            | _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        )
        info.BasicLimitInformation.ActiveProcessLimit = 16
        info.ProcessMemoryLimit = mem_limit_bytes
        info.JobMemoryLimit = mem_limit_bytes
        res = kernel32.SetInformationJobObject(
            job,
            _JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        proc_handle = getattr(proc, "_handle", None)
        if proc_handle and res:
            kernel32.AssignProcessToJobObject(job, int(proc_handle))
        return job
    except Exception:
        return None


def _close_windows_job(job_handle: Optional[int]) -> None:
    if os.name == "nt" and job_handle:
        try:
            ctypes.windll.kernel32.CloseHandle(job_handle)
        except Exception:
            pass


def _with_limits(
    cmd: List[str], cpu: int, mem: int, fsize: int, nproc: Optional[int] = None
) -> Tuple[List[str], Optional[Callable[[], None]]]:
    """Returns (argv, preexec_fn) applying resource limits to `cmd`."""
    exe = shutil.which("prlimit")
    if exe:
        argv = [exe, f"--cpu={cpu}", f"--as={mem}", f"--fsize={fsize}", "--core=0"]
        if nproc is not None:
            argv.append(f"--nproc={nproc}")
        return argv + ["--"] + cmd, None
    if resource is None:  # pragma: no cover - development machines without POSIX limits
        return cmd, None

    def _preexec() -> None:
        pairs = [(resource.RLIMIT_CPU, cpu), (resource.RLIMIT_AS, mem), (resource.RLIMIT_FSIZE, fsize), (resource.RLIMIT_CORE, 0)]
        if nproc is not None:
            pairs.append((resource.RLIMIT_NPROC, nproc))
        for res, value in pairs:
            try:
                resource.setrlimit(res, (value, value))
            except (ValueError, OSError):
                pass

    return cmd, _preexec


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the process or process group safely on both POSIX and Windows."""
    if os.name == "nt":
        try:
            # On Windows, kill the entire process tree (/T) forcefully (/F) to prevent orphaned compiler/child processes
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=5)
        except Exception:
            try:
                proc.kill()
            except OSError:
                pass
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError, AttributeError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _compile_or_check(src_path: str, bin_path: str, tmpdir: str, language: str = "c") -> Tuple[int, str, bool, List[str], str]:
    """Returns (returncode, stderr, timed_out, run_argv, runtime_label)."""
    lang = (language or "c").lower()
    if lang == "c":
        gcc = shutil.which("gcc") or shutil.which("clang") or "gcc"
        cmd = [gcc, "-O0", "-Wall", "-Wextra", "-fstack-protector-strong", src_path, "-o", bin_path, "-lm"]
        run_argv = [bin_path]
        label = "gcc"
    elif lang == "cpp":
        cxx = shutil.which("g++") or shutil.which("clang++")
        if cxx:
            cmd = [cxx, "-O0", "-Wall", "-Wextra", "-std=c++17", "-fstack-protector-strong", src_path, "-o", bin_path, "-lm"]
        else:
            gcc = shutil.which("gcc") or "gcc"
            cmd = [gcc, "-x", "c++", "-O0", "-Wall", "-Wextra", "-std=c++17", src_path, "-o", bin_path, "-lstdc++", "-lm"]
        run_argv = [bin_path]
        label = "g++"
    elif lang == "python":
        py = sys.executable or shutil.which("python3") or shutil.which("python") or "python3"
        run_argv = [py, "-I", "-B", src_path]
        label = "python3"
        try:
            with open(src_path, "r", encoding="utf-8", errors="replace") as fh:
                py_source = fh.read()
            compile(py_source, "submission.py", "exec")
            return 0, "", False, run_argv, label
        except SyntaxError as exc:
            line_no = exc.lineno or 1
            code_line = (exc.text or "").strip()
            msg = f'File "submission.py", line {line_no}\n    {code_line}\nSyntaxError: {exc.msg}'
            return 1, msg, False, run_argv, label
    elif lang == "bash":
        sh = shutil.which("bash") or "bash"
        cmd = [sh, "-n", src_path]
        run_argv = [sh, src_path]
        label = "bash"
    elif lang in ("javascript", "js"):
        node = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"
        cmd = [node, "--check", src_path]
        run_argv = [node, src_path]
        label = "node"
    elif lang in ("go", "golang"):
        go = shutil.which("go") or r"C:\Program Files\Go\bin\go.exe"
        bin_target = bin_path if bin_path.endswith(".exe") else (bin_path + ".exe" if os.name == "nt" else bin_path)
        cmd = [go, "build", "-o", bin_target, src_path]
        run_argv = [bin_target]
        label = "go"
    elif lang == "java":
        javac = shutil.which("javac") or r"C:\Program Files\Microsoft\jdk-17.0.20.101-hotspot\bin\javac.exe"
        java = shutil.which("java") or r"C:\Program Files\Microsoft\jdk-17.0.20.101-hotspot\bin\java.exe"
        cmd = [javac, "-encoding", "UTF-8", "-d", tmpdir, src_path]
        main_class = "Main"
        try:
            with open(src_path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
                m = re.search(r"\bpublic\s+class\s+([A-Za-z_]\w*)", content)
                if m:
                    main_class = m.group(1)
                else:
                    classes = list(re.finditer(r"\bclass\s+([A-Za-z_]\w*)", content))
                    main_match = re.search(r"\bpublic\s+static\s+void\s+main\b", content)
                    if classes and main_match:
                        main_pos = main_match.start()
                        valid_classes = [c for c in classes if c.start() < main_pos]
                        if valid_classes:
                            main_class = valid_classes[-1].group(1)
                        else:
                            main_class = classes[0].group(1)
                    elif classes:
                        main_class = classes[0].group(1)
        except Exception:
            pass
        run_argv = [java, "-cp", tmpdir, main_class]
        label = "java"
    else:
        return 0, "", False, [], "none"

    cpu = int(math.ceil(settings.compile_timeout_seconds)) + 2
    comp_mem = 384 * 1024 * 1024 if settings.is_low_memory else 1536 * 1024 * 1024
    argv, preexec = _with_limits(cmd, cpu, comp_mem, 64 * 1024 * 1024)

    popen_kwargs: Dict[str, Any] = {
        "cwd": tmpdir,
        "env": get_safe_env(lang, tmpdir),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["preexec_fn"] = preexec
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(argv, **popen_kwargs)
    job = _assign_windows_job(proc, comp_mem)
    try:
        _, err = proc.communicate(timeout=settings.compile_timeout_seconds)
    except subprocess.TimeoutExpired:
        _kill_group(proc)   # kills compiler AND its children
        try:
            proc.communicate(timeout=2)
        except Exception:
            pass
        return -1, "", True, run_argv, label
    finally:
        _close_windows_job(job)
    return proc.returncode, err.decode("utf-8", errors="replace"), False, run_argv, label


def execute_binary_capped(
    bin_path: Union[str, List[str]],
    input_data: str,
    timeout_sec: float,
    tmpdir: str,
    max_output_bytes: int = 65536,
    language: str = "c",
) -> Tuple[str, str, int, bool, bool]:
    """Run the compiled program or script once. Returns (stdout, stderr, returncode, timed_out, truncated)."""
    cpu = max(settings.rlimit_cpu_seconds, int(math.ceil(timeout_sec)) + 1)  # wall clock must fire first
    cmd_list = [bin_path] if isinstance(bin_path, str) else list(bin_path)
    lang = (language or "c").lower()
    if lang in ("c", "cpp"):
        mem_limit = settings.rlimit_as_bytes
    elif settings.is_low_memory:
        mem_limit = min(settings.rlimit_as_bytes * 3 // 2, 384 * 1024 * 1024)
    else:
        mem_limit = max(settings.rlimit_as_bytes, 768 * 1024 * 1024)
    nproc_limit = 1 if lang in ("c", "cpp") else max(1, settings.rlimit_nproc)
    argv, preexec = _with_limits(
        cmd_list, cpu, mem_limit, max(0, settings.rlimit_fsize_bytes), nproc_limit
    )


    data = input_data.encode("utf-8", errors="replace")[:MAX_STDIN_BYTES]

    popen_kwargs: Dict[str, Any] = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "cwd": tmpdir,
        "env": get_safe_env(lang, tmpdir),
        "bufsize": 0,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["preexec_fn"] = preexec
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(argv, **popen_kwargs)
    job = _assign_windows_job(proc, mem_limit)

    # Windows handling: select() does not support pipe file descriptors on Windows.
    # We use stream reader threads to cap output at max_output_bytes and terminate runaway flood loops early.
    if os.name == "nt":
        import threading
        timed_out = truncated = False
        out_chunks: List[bytes] = []
        err_chunks: List[bytes] = []
        out_len = 0

        def _write_stdin():
            if data:
                try:
                    proc.stdin.write(data)
                except OSError:
                    pass
            try:
                proc.stdin.close()
            except OSError:
                pass

        stdin_thread = threading.Thread(target=_write_stdin, daemon=True)
        stdin_thread.start()

        def _read_out():
            nonlocal out_len, truncated
            try:
                while True:
                    chunk = proc.stdout.read(4096)
                    if not chunk:
                        break
                    out_len += len(chunk)
                    if out_len > max_output_bytes:
                        truncated = True
                        _kill_group(proc)
                        break
                    out_chunks.append(chunk)
            except Exception:
                pass

        def _read_err():
            try:
                err_len = 0
                while True:
                    chunk = proc.stderr.read(4096)
                    if not chunk:
                        break
                    err_len += len(chunk)
                    if err_len <= 16384:
                        err_chunks.append(chunk)
            except Exception:
                pass

        t_out = threading.Thread(target=_read_out, daemon=True)
        t_err = threading.Thread(target=_read_err, daemon=True)
        t_out.start()
        t_err.start()

        try:
            proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            if not truncated:
                timed_out = True
            _kill_group(proc)
            try:
                proc.wait(timeout=0.1)
            except Exception:
                pass
        finally:
            _close_windows_job(job)

        t_out.join(timeout=0.3)
        t_err.join(timeout=0.3)

        out_b = b"".join(out_chunks)[:max_output_bytes]
        err_b = b"".join(err_chunks)[:16384]

        stdout_text = out_b.decode("utf-8", errors="replace")
        stderr_text = err_b.decode("utf-8", errors="replace")
        returncode = proc.returncode if proc.returncode is not None else -1
        return stdout_text, stderr_text, returncode, timed_out, truncated

    # POSIX handling: non-blocking selectors streaming loop
    try:
        view = memoryview(data)
        while view:
            written = proc.stdin.write(view)
            if not written:
                break
            view = view[written:]
    except OSError:
        pass  # program exited before reading everything
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass

    out_chunks: List[bytes] = []
    err_chunks: List[bytes] = []
    out_len = err_len = 0
    truncated = timed_out = False
    deadline = time.monotonic() + timeout_sec

    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ, "out")
    sel.register(proc.stderr, selectors.EVENT_READ, "err")
    try:
        while sel.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            for key, _ in sel.select(timeout=min(remaining, 0.1)):
                chunk = os.read(key.fd, 4096)  # returns whatever is available; never blocks after select()
                if not chunk:
                    sel.unregister(key.fileobj)
                    continue
                if key.data == "out":
                    out_len += len(chunk)
                    if out_len > max_output_bytes:
                        truncated = True
                        break
                    out_chunks.append(chunk)
                elif err_len < 16384:
                    err_chunks.append(chunk)
                    err_len += len(chunk)
            if truncated:
                break
        if not timed_out and not truncated and proc.poll() is None:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                timed_out = True
    finally:
        _close_windows_job(job)
        if proc.poll() is None:
            _kill_group(proc)
            try:
                proc.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
        sel.close()
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass

    stdout_text = b"".join(out_chunks).decode("utf-8", errors="replace")
    stderr_text = b"".join(err_chunks).decode("utf-8", errors="replace")
    returncode = proc.returncode if proc.returncode is not None else -1
    return stdout_text, stderr_text, returncode, timed_out, truncated


def run_gcc_verification(
    raw_code: str,
    test_cases: List[TestCase],
    timeout_sec: Optional[float] = None,
    language: str = "c",
    match_mode: str = "exact",
    float_rel_tol: float = 1e-4,
    float_abs_tol: float = 1e-4,
) -> GccTestResult:
    lang = (language or "c").lower()
    if lang == "auto" or not lang:
        from app.analyzer.zip_ingestor import detect_language
        lang = detect_language("submission", raw_code).lower()
    timeout_sec = timeout_sec or settings.test_timeout_seconds
    sample_total = sum(1 for t in test_cases if t.is_sample)
    hidden_total = len(test_cases) - sample_total
    base = dict(language=lang, total_count=len(test_cases), sample_total=sample_total, hidden_total=hidden_total)

    if not test_cases:
        return GccTestResult(compiled=True, runtime_used="none", skipped_reason="No test cases configured", **base)

    if not runtime_available_for(lang):
        return GccTestResult(
            language=lang,
            runtime_used="none",
            compiled=True,
            total_count=0,
            sample_total=sample_total,
            hidden_total=hidden_total,
            skipped_reason=f"No local {lang} sandbox runtime; evaluated via static analysis and AI review",
        )

    cache_key = (
        lang,
        hashlib.sha256(raw_code.encode("utf-8", errors="replace")).hexdigest(),
        _test_cases_fingerprint(test_cases),
        round(float(timeout_sec), 3),
        match_mode,
        round(float(float_rel_tol), 6),
        round(float(float_abs_tol), 6),
    )
    if cache_key in _VERIFICATION_CACHE:
        _VERIFICATION_CACHE.move_to_end(cache_key)
        return _VERIFICATION_CACHE[cache_key].model_copy(deep=True)

    ext_map = {
        "c": ".c",
        "cpp": ".cpp",
        "python": ".py",
        "bash": ".sh",
        "javascript": ".js",
        "js": ".js",
        "go": ".go",
        "golang": ".go",
        "java": ".java",
    }
    src_ext = ext_map.get(lang, ".c")
    bin_ext = ".exe" if os.name == "nt" and lang in ("c", "cpp", "go", "golang") else ".out"

    shm_dir = "/dev/shm" if os.path.isdir("/dev/shm") else None
    with tempfile.TemporaryDirectory(prefix="iiserb_c_sandbox_", dir=shm_dir) as tmpdir:
        if lang == "java":
            m_cls = re.search(r"\bpublic\s+class\s+([A-Za-z_]\w*)", raw_code)
            fname = f"{m_cls.group(1)}.java" if m_cls else "Main.java"
            src_path = os.path.join(tmpdir, fname)
        else:
            src_path = os.path.join(tmpdir, f"submission{src_ext}")
        bin_path = os.path.join(tmpdir, f"submission{bin_ext}")
        with open(src_path, "w", encoding="utf-8", newline="") as fh:
            fh.write(raw_code)

        try:
            returncode, stderr, compile_timed_out, run_argv, runtime_label = _compile_or_check(src_path, bin_path, tmpdir, lang)
        except OSError as exc:
            return GccTestResult(compiled=False, runtime_used=lang, compiler_errors=f"Compiler invocation error: {exc}", **base)

        if compile_timed_out:
            res = GccTestResult(
                compiled=False,
                runtime_used=runtime_label,
                compiler_errors=f"Compilation timed out (> {settings.compile_timeout_seconds:g}s).",
                **base,
            )
            _cache_put(cache_key, res)
            return res.model_copy(deep=True)
        if returncode != 0:
            res = GccTestResult(
                compiled=False,
                runtime_used=runtime_label,
                compiler_errors=sanitize_compiler_errors(stderr, tmpdir)[:1500] or "Compilation or syntax check failed.",
                **base,
            )
            _cache_put(cache_key, res)
            return res.model_copy(deep=True)

        warnings = sanitize_compiler_errors(stderr, tmpdir)[:600] if stderr else ""
        passed = sample_passed = hidden_passed = 0
        timed_out_any = runtime_err_any = False
        failure_modes: List[str] = []
        details = []

        def run_case(item: Tuple[int, TestCase]) -> Tuple[str, str, int, bool, bool]:
            idx, tc = item
            case_tmpdir = os.path.join(tmpdir, f"case_{idx}")
            os.makedirs(case_tmpdir, exist_ok=True)
            return execute_binary_capped(run_argv, tc.input_data, timeout_sec, case_tmpdir, settings.max_output_bytes, lang)

        workers = max(1, min(settings.sandbox_test_workers, len(test_cases)))
        items = list(enumerate(test_cases, start=1))
        if workers == 1:
            outcomes = []
            consecutive_timeouts = 0
            for item in items:
                if settings.early_abort_on_timeout and consecutive_timeouts >= 2:
                    outcomes.append(("", "", -1, True, False))
                    continue
                outcome = run_case(item)
                outcomes.append(outcome)
                if outcome[3]:  # timed_out
                    consecutive_timeouts += 1
                else:
                    consecutive_timeouts = 0
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                outcomes = list(pool.map(run_case, items))

        # A4: Single timeout de-flaking rerun for shared/noisy hosts
        if settings.enable_timeout_rerun and len(test_cases) > 1:
            to_indices = [i for i, (_, _, _, to, _) in enumerate(outcomes) if to]
            if len(to_indices) == 1:
                t_idx = to_indices[0]
                retry_out = run_case((t_idx + 1, test_cases[t_idx]))
                if not retry_out[3]:  # cleared timeout on rerun
                    outcomes[t_idx] = retry_out

        rel_tol = float_rel_tol
        abs_tol = float_abs_tol

        for idx, (tc, (out, _err, rc, timed_out, truncated)) in enumerate(zip(test_cases, outcomes), start=1):
            actual = normalize_output(out)
            expected = normalize_output(tc.expected_output)
            ok = (not timed_out) and (not truncated) and rc == 0 and outputs_match(
                expected, actual, match_mode=match_mode, rel_tol=rel_tol, abs_tol=abs_tol
            )
            fmode = diagnose_case_failure(
                expected, actual, rc, timed_out, truncated, match_mode=match_mode, rel_tol=rel_tol, abs_tol=abs_tol
            )
            if fmode != "PASS" and fmode not in failure_modes:
                failure_modes.append(fmode)

            actual_display = actual[:160]
            if timed_out:
                timed_out_any = True
                actual_display = f"[TIMEOUT > {timeout_sec:g}s]"
            elif truncated:
                actual_display = "[OUTPUT TRUNCATED: > 64 KB]"
            elif rc != 0:
                runtime_err_any = True

            if ok:
                passed += 1
                if tc.is_sample:
                    sample_passed += 1
                else:
                    hidden_passed += 1
            details.append({
                "case_index": idx,
                "is_sample": tc.is_sample,
                "passed": ok,
                "failure_mode": fmode,
                "input": tc.input_data[:200],
                "expected": expected[:160],
                "actual": actual_display,
                "returncode": rc,
            })

        res = GccTestResult(
            compiled=True,
            runtime_used=runtime_label,
            compiler_warnings=warnings,
            passed_count=passed,
            sample_passed=sample_passed,
            hidden_passed=hidden_passed,
            timed_out=timed_out_any,
            runtime_error=runtime_err_any,
            failure_modes=failure_modes,
            case_details=details,
            **base,
        )
        _cache_put(cache_key, res)
        return res.model_copy(deep=True)

