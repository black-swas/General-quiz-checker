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
import math
import os
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Tuple, Union

try:
    import resource
except ImportError:  # pragma: no cover - only on non-POSIX development machines
    resource = None  # type: ignore[assignment]

from app.config import settings
from app.schemas import GccTestResult, TestCase

SAFE_ENV = {"PATH": "/usr/bin:/bin:/usr/local/bin", "LC_ALL": "C", "LANG": "C", "PYTHONDONTWRITEBYTECODE": "1"}
MAX_STDIN_BYTES = 49152  # stays below the 64 KiB pipe buffer so writing never blocks
_PATH_RE = re.compile(r"(?<![\w\"'.])(?:/[\w.\-]+){2,}")


def gcc_available() -> bool:
    return shutil.which("gcc") is not None


def sandbox_runtimes() -> Dict[str, bool]:
    return {
        "c": shutil.which("gcc") is not None,
        "cpp": (shutil.which("g++") or shutil.which("gcc")) is not None,
        "python": shutil.which("python3") is not None,
        "bash": shutil.which("bash") is not None,
        "javascript": shutil.which("node") is not None,
    }


def runtime_available_for(language: str) -> bool:
    return sandbox_runtimes().get((language or "c").lower(), False)


def normalize_output(text: str) -> str:
    return "\n".join(line.strip() for line in text.strip().splitlines() if line.strip())


def outputs_match(expected: str, actual: str) -> bool:
    """Exact normalized match, plus 1e-6 tolerance when tokens are floating-point numbers (e.g. 3.141590 vs 3.14159)."""
    if expected == actual:
        return True
    exp_lines = expected.splitlines()
    act_lines = actual.splitlines()
    if len(exp_lines) != len(act_lines) or not exp_lines:
        return False
    has_float = False
    for el, al in zip(exp_lines, act_lines):
        etoks = el.split()
        atoks = al.split()
        if len(etoks) != len(atoks) or not etoks:
            return False
        for et, at in zip(etoks, atoks):
            if et == at:
                continue
            if "." in et or "." in at:
                try:
                    ef, af = float(et), float(at)
                    if math.isclose(ef, af, rel_tol=1e-6, abs_tol=1e-6):
                        has_float = True
                        continue
                except ValueError:
                    pass
            return False
    return has_float


def sanitize_compiler_errors(stderr_text: str, tmpdir: str) -> str:
    """Strip sandbox paths and cap the length. (Dangerous #includes are rejected before compiling.)"""
    if not stderr_text:
        return ""
    cleaned = stderr_text.replace(tmpdir + "/", "").replace(tmpdir, "")
    return "\n".join(_PATH_RE.sub("[path]", line) for line in cleaned.splitlines()[:30])


def diagnose_case_failure(expected: str, actual: str, rc: int, timed_out: bool, truncated: bool) -> str:
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
    if outputs_match(expected, actual):
        return "PASS"
    try:
        exp_num = int(expected.strip())
        act_num = int(actual.strip())
        if abs(exp_num - act_num) == 1:
            return "OFF_BY_ONE"
        if exp_num > 2_147_483_647 and act_num < exp_num:
            return "INTEGER_OVERFLOW"
    except ValueError:
        pass
    if expected.lower().replace(" ", "") == actual.lower().replace(" ", ""):
        return "CASE_OR_FORMAT_MISMATCH"
    if expected.strip() and actual.strip().endswith(expected.strip()) and re.search(r"(?:enter|input|output|answer|result|sum|count)\b", actual, re.I):
        return "CASE_OR_FORMAT_MISMATCH"
    return "WRONG_ANSWER"


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
    """SIGKILL the whole process group (the child is its own session leader, so pgid == pid)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _compile_or_check(src_path: str, bin_path: str, tmpdir: str, language: str = "c") -> Tuple[int, str, bool, List[str], str]:
    """Returns (returncode, stderr, timed_out, run_argv, runtime_label)."""
    lang = (language or "c").lower()
    if lang == "c":
        cmd = ["gcc", "-O0", "-Wall", "-Wextra", "-fstack-protector-strong", src_path, "-o", bin_path, "-lm"]
        run_argv = [bin_path]
        label = "gcc"
    elif lang == "cpp":
        cxx = shutil.which("g++")
        if cxx:
            cmd = [cxx, "-O0", "-Wall", "-Wextra", "-std=c++17", "-fstack-protector-strong", src_path, "-o", bin_path, "-lm"]
        else:
            cmd = ["gcc", "-x", "c++", "-O0", "-Wall", "-Wextra", "-std=c++17", src_path, "-o", bin_path, "-lstdc++", "-lm"]
        run_argv = [bin_path]
        label = "g++"
    elif lang == "python":
        py = shutil.which("python3") or "python3"
        cmd = [py, "-I", "-B", "-m", "py_compile", src_path]
        run_argv = [py, "-I", "-B", src_path]
        label = "python3"
    elif lang == "bash":
        sh = shutil.which("bash") or "bash"
        cmd = [sh, "-n", src_path]
        run_argv = [sh, src_path]
        label = "bash"
    elif lang == "javascript":
        node = shutil.which("node") or "node"
        cmd = [node, "--check", src_path]
        run_argv = [node, src_path]
        label = "node"
    else:
        return 0, "", False, [], "none"

    cpu = int(math.ceil(settings.compile_timeout_seconds)) + 2
    argv, preexec = _with_limits(cmd, cpu, 1536 * 1024 * 1024, 64 * 1024 * 1024)
    proc = subprocess.Popen(
        argv,
        cwd=tmpdir,
        env=dict(SAFE_ENV, TMPDIR=tmpdir),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=preexec,
        start_new_session=True,
    )
    try:
        _, err = proc.communicate(timeout=settings.compile_timeout_seconds)
    except subprocess.TimeoutExpired:
        _kill_group(proc)   # kills compiler AND its children (same process group)
        try:
            proc.communicate(timeout=2)
        except Exception:
            pass
        return -1, "", True, run_argv, label
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
    mem_limit = settings.rlimit_as_bytes if lang in ("c", "cpp") else max(settings.rlimit_as_bytes, 512 * 1024 * 1024)
    nproc_limit = 1 if lang in ("c", "cpp") else max(1, settings.rlimit_nproc)
    argv, preexec = _with_limits(
        cmd_list, cpu, mem_limit, max(0, settings.rlimit_fsize_bytes), nproc_limit
    )
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmpdir,
        env=dict(SAFE_ENV, HOME=tmpdir, TMPDIR=tmpdir),
        bufsize=0,
        preexec_fn=preexec,
        start_new_session=True,
    )

    data = input_data.encode("utf-8", errors="replace")[:MAX_STDIN_BYTES]
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
    finally:
        _kill_group(proc)
        sel.close()
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
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
) -> GccTestResult:
    lang = (language or "c").lower()
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

    ext_map = {"c": ".c", "cpp": ".cpp", "python": ".py", "bash": ".sh", "javascript": ".js"}
    src_ext = ext_map.get(lang, ".c")

    with tempfile.TemporaryDirectory(prefix="iiserb_c_sandbox_") as tmpdir:
        src_path = os.path.join(tmpdir, f"submission{src_ext}")
        bin_path = os.path.join(tmpdir, "submission.out")
        with open(src_path, "w", encoding="utf-8", newline="") as fh:
            fh.write(raw_code)

        try:
            returncode, stderr, compile_timed_out, run_argv, runtime_label = _compile_or_check(src_path, bin_path, tmpdir, lang)
        except OSError as exc:
            return GccTestResult(compiled=False, runtime_used=lang, compiler_errors=f"Compiler invocation error: {exc}", **base)

        if compile_timed_out:
            return GccTestResult(
                compiled=False,
                runtime_used=runtime_label,
                compiler_errors=f"Compilation timed out (> {settings.compile_timeout_seconds:g}s).",
                **base,
            )
        if returncode != 0:
            return GccTestResult(
                compiled=False,
                runtime_used=runtime_label,
                compiler_errors=sanitize_compiler_errors(stderr, tmpdir)[:1500] or "Compilation or syntax check failed.",
                **base,
            )

        warnings = sanitize_compiler_errors(stderr, tmpdir)[:600] if stderr else ""
        passed = sample_passed = hidden_passed = 0
        timed_out_any = runtime_err_any = False
        failure_modes: List[str] = []
        details = []

        def run_case(tc: TestCase) -> Tuple[str, str, int, bool, bool]:
            return execute_binary_capped(run_argv, tc.input_data, timeout_sec, tmpdir, settings.max_output_bytes, lang)

        workers = max(1, min(settings.sandbox_test_workers, len(test_cases)))
        if workers == 1:
            outcomes = [run_case(tc) for tc in test_cases]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                outcomes = list(pool.map(run_case, test_cases))

        for idx, (tc, (out, _err, rc, timed_out, truncated)) in enumerate(zip(test_cases, outcomes), start=1):
            actual = normalize_output(out[:2048])
            expected = normalize_output(tc.expected_output)
            ok = (not timed_out) and (not truncated) and rc == 0 and outputs_match(expected, actual)
            fmode = diagnose_case_failure(expected, actual, rc, timed_out, truncated)
            if fmode != "PASS" and fmode not in failure_modes:
                failure_modes.append(fmode)

            if timed_out:
                timed_out_any = True
                actual = f"[TIMEOUT > {timeout_sec:g}s]"
            elif truncated:
                actual = "[OUTPUT TRUNCATED: > 64 KB]"
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
                "actual": actual[:160],
                "returncode": rc,
            })

        return GccTestResult(
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
