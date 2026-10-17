"""Polyglot static and forensic analysis of a single student submission.

Everything here is *evidence gathering* across C, C++, Python, Java, JavaScript/TypeScript, Rust, Go,
SQL, R, Bash, and general text/markdown submissions. Verdicts are decided in triage.py and llm_evaluator.py.
"""
import ast
import json
import re
from typing import Dict, Iterator, List, Optional, Set, Tuple

from app.analyzer.zip_ingestor import detect_language
from app.schemas import QuestionSpec, StaticAnalysisResult

# --------------------------------------------------------------------------- constants
PROMPT_INJECTION_PATTERNS = [
    r"ignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier|preceding)\s+(?:instructions?|rules?|prompts?|directions?|guidelines?)",
    r"disregard\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier|preceding|rubric|grading|instructions?|rules?|directions?|criteria)",
    r"system\s*(?:prompt|instruction|override)",
    r"mark\s+(?:this|my|the)\s+(?:code|submission|answer|solution|program|file|response)\s+(?:as\s+)?(?:clean|green|correct|valid|passing|perfect|safe)",
    r"(?:classify|grade|rate|label|score)\s+(?:this|my|the)\s+(?:code|submission|answer|solution|program)",
    r"as\s+(?:clean|green)\s+and\s+(?:valid|correct)",
    r"you\s+are\s+(?:now\s+)?(?:an?\s+)?(?:ai|llm|language\s+model|assistant|grader|grading|evaluator|judge)",
    r"confidence_score\s*[:=]\s*\d+",
    r"tab_category",
    r"verdict_tab",
    r"do\s+not\s+flag",
    r"(?:award|give|assign|grant)\s+(?:me\s+|this\s+)?(?:full|maximum|max|100|top)\s*(?:marks?|score|points?|credit)",
    r"new\s+instructions?\s*:",
]

# name -> description. Names the student defines themselves are skipped (e.g. a union-find connect()).
DANGEROUS_CALLS: Dict[str, str] = {
    "system": "Invokes system() shell command",
    "popen": "Invokes popen() process pipe",
    "fork": "Invokes fork() process creation",
    "vfork": "Invokes vfork() process creation",
    "execl": "Invokes exec() family syscall",
    "execlp": "Invokes exec() family syscall",
    "execle": "Invokes exec() family syscall",
    "execv": "Invokes exec() family syscall",
    "execvp": "Invokes exec() family syscall",
    "execve": "Invokes exec() family syscall",
    "socket": "Invokes socket() network call",
    "connect": "Invokes connect() network call",
    "listen": "Invokes listen() network call",
    "getenv": "Reads environment variables via getenv()",
    "secure_getenv": "Reads environment variables via secure_getenv()",
    "setenv": "Mutates environment variables via setenv()",
    "putenv": "Mutates environment variables via putenv()",
    "ptrace": "Invokes ptrace() process inspection",
    "dlopen": "Loads shared libraries via dlopen()",
    "dlsym": "Resolves symbols via dlsym()",
    "syscall": "Invokes raw syscall()",
    "kill": "Invokes kill() on other processes",
}

POLYGLOT_DANGEROUS_PATTERNS: List[Tuple[str, str]] = [
    (r"\bos\s*\.\s*(?:system|popen|spawn\w*|exec\w*|fork|kill|remove|unlink|rmdir)\s*\(", "Invokes dangerous Python os process/filesystem call"),
    (r"\bsubprocess\s*\.\s*(?:Popen|run|call|check_output|check_call|getoutput|getstatusoutput)\s*\(", "Invokes Python subprocess execution"),
    (r"\bpty\s*\.\s*spawn\s*\(", "Invokes pty.spawn() shell escape"),
    (r"\bshutil\s*\.\s*rmtree\s*\(", "Invokes destructive shutil.rmtree()"),
    (r"\b__import__\s*\(\s*['\"](?:os|subprocess|sys|socket|pty|ctypes|shutil)['\"]", "Dynamic import of restricted system module"),
    (r"\bos\s*\.\s*environ\b", "Reads or mutates process environment via os.environ"),
    (r"\bRuntime\s*\.\s*getRuntime\s*\(\s*\)\s*\.\s*exec\s*\(", "Invokes Java Runtime.exec() process execution"),
    (r"\bnew\s+ProcessBuilder\s*\(", "Invokes Java ProcessBuilder"),
    (r"\bchild_process\b|\bexecSync\s*\(|\bspawnSync\s*\(", "Invokes Node.js child_process execution"),
    (r"\bprocess\s*\.\s*env\b", "Accesses Node.js process.env"),
]

RESTRICTED_PATH_RE = re.compile(r"^\s*/(?:etc|proc|sys|root|home|app|mnt|dev|var|usr|tmp|run|boot)\b")
TROJAN_BIDI_RE = re.compile(r"[\u202a-\u202e\u2066-\u2069\u061c]")
ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200d\u2060]")

C_KEYWORDS = {
    "auto", "break", "case", "char", "const", "continue", "default", "do",
    "double", "else", "enum", "extern", "float", "for", "goto", "if",
    "inline", "int", "long", "register", "restrict", "return", "short",
    "signed", "sizeof", "static", "struct", "switch", "typedef", "union",
    "unsigned", "void", "volatile", "while", "bool", "true", "false",
    "size_t", "FILE", "NULL", "main", "printf", "scanf", "fprintf", "fscanf",
    "sprintf", "sscanf", "getchar", "putchar", "puts", "gets", "fgets",
    "malloc", "calloc", "realloc", "free", "exit", "abs", "labs", "pow",
    "sqrt", "sin", "cos", "tan", "log", "exp", "floor", "ceil", "strlen",
    "strcpy", "strncpy", "strcat", "strcmp", "strncmp", "memcpy", "memset",
    "qsort", "bsearch", "INT_MIN", "INT_MAX", "LONG_MIN", "LONG_MAX",
    "tolower", "toupper", "isalpha", "isdigit", "isalnum", "isspace",
    "def", "class", "import", "from", "as", "pass", "None", "True", "False",
    "and", "or", "not", "in", "is", "lambda", "yield", "with", "try", "except",
    "finally", "raise", "elif", "print", "input", "len", "range", "enumerate",
    "zip", "map", "filter", "sorted", "sum", "min", "max", "list", "dict", "set",
    "tuple", "str", "int", "float", "bool", "public", "private", "protected",
    "final", "System", "out", "println", "Scanner", "String", "let", "var",
    "function", "console", "log", "fn", "mut", "impl", "pub", "use", "func",
    "package", "SELECT", "FROM", "WHERE", "GROUP", "BY", "ORDER", "HAVING",
    "JOIN", "LEFT", "RIGHT", "INNER", "LIMIT", "INSERT", "UPDATE", "DELETE",
    "cout", "cin", "endl", "std", "vector", "string",
}

HASH_COMMENT_LANGS = {"python", "r", "bash", "ruby", "perl", "julia", "yaml"}
DASH_COMMENT_LANGS = {"sql", "lua", "haskell", "vhdl"}
PERCENT_COMMENT_LANGS = {"matlab", "latex"}
PROSE_LANGS = {"text", "markdown", "latex"}

_BASE_CASE_NUMBERS = {"-1", "0", "1", "2", "3"}
_CONTROL_RE = re.compile(r"\b(?:if|elif|else|while|for|do|switch|case|default|match|when)\b|\?")
_FUNC_RE = re.compile(
    r"\b(?:static\s+|inline\s+|extern\s+|const\s+|public\s+|private\s+|protected\s+)*(?:unsigned\s+|signed\s+)?"
    r"(?:long\s+long|long|int|void|float|double|char|short|size_t|bool|_Bool|string|String|auto)"
    r"\b\s*\**\s*([A-Za-z_]\w*)\s*\(([^;{}]*)\)\s*([{;])"
)
_PY_FUNC_RE = re.compile(r"^([ \t]*)def\s+([A-Za-z_]\w*)\s*\(([^)]*)\)\s*(?:->[^:]+)?\s*:", re.M)
_JS_RS_GO_FUNC_RE = re.compile(r"\b(?:function|fn|func)\s+([A-Za-z_]\w*)\s*\(([^)]*)\)\s*(?:->[^{]+)?\s*\{")
_DIRECTIVE_RE = re.compile(r"^[ \t]*(?:#|%:)[ \t]*([A-Za-z_]+)([^\n]*)", re.M)
_DEREF_RE = re.compile(r"(?:^|[=(,;{}!&|<>+\-*/%?:\[]|\breturn\b)[ \t]*\*+[ \t]*[A-Za-z_(]", re.M)


# --------------------------------------------------------------------------- lexical helpers
def extract_and_strip_comments(code: str, language: str = "c") -> Tuple[str, List[str]]:
    """Remove comments (string/char aware) across C-family, Python/Bash/R (#), SQL/Lua (--), and MATLAB/LaTeX (%).
    Line numbering is strictly preserved.
    """
    lang = (language or "c").lower()
    if lang in PROSE_LANGS and lang != "latex":
        return code, []

    use_hash = lang in HASH_COMMENT_LANGS
    use_dash = lang in DASH_COMMENT_LANGS
    use_percent = lang in PERCENT_COMMENT_LANGS
    use_c_style = lang not in HASH_COMMENT_LANGS or lang in ("php", "r", "julia")

    comments: List[str] = []
    out: List[str] = []
    i, n = 0, len(code)
    in_str = in_char = False
    while i < n:
        ch = code[i]
        nxt = code[i + 1] if i + 1 < n else ""
        nxt3 = code[i:i + 3]

        if use_hash and not in_str and not in_char and nxt3 in ('"""', "'''"):
            line_start = code.rfind("\n", 0, i) + 1
            prefix = code[line_start:i].strip()
            end = code.find(nxt3, i + 3)
            if end == -1:
                end = n
            body = code[i + 3:end]
            if prefix == "":
                comments.append(body.strip())
                out.append("\n" * body.count("\n"))
            else:
                comments.append(body.strip())
                flat_body = body.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
                out.append(f'"{flat_body}"' + ("\n" * body.count("\n")))
            i = end + 3 if end < n else n
            continue

        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(code[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
        elif in_char:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(code[i + 1])
                i += 2
                continue
            if ch == "'":
                in_char = False
            i += 1
        elif ch == '"':
            in_str = True
            out.append(ch)
            i += 1
        elif ch == "'":
            in_char = True
            out.append(ch)
            i += 1
        elif use_c_style and ch == "/" and nxt == "/":
            end = code.find("\n", i + 2)
            if end == -1:
                end = n
            comments.append(code[i + 2:end].strip())
            i = end
        elif (use_c_style or use_dash) and ch == "/" and nxt == "*":
            end = code.find("*/", i + 2)
            if end == -1:
                end = n
            body = code[i + 2:end]
            comments.append(body.strip())
            out.append("\n" * body.count("\n"))
            i = end + 2 if end < n else n
        elif use_hash and ch == "#":
            end = code.find("\n", i + 1)
            if end == -1:
                end = n
            comments.append(code[i + 1:end].strip())
            i = end
        elif use_dash and ch == "-" and nxt == "-":
            end = code.find("\n", i + 2)
            if end == -1:
                end = n
            comments.append(code[i + 2:end].strip())
            i = end
        elif use_percent and ch == "%":
            end = code.find("\n", i + 1)
            if end == -1:
                end = n
            comments.append(code[i + 1:end].strip())
            i = end
        else:
            out.append(ch)
            i += 1
    return "".join(out), comments


def mask_literals(code: str) -> str:
    """Replace the *contents* of string/char literals by 'x' (length and newlines preserved)."""
    out: List[str] = []
    i, n = 0, len(code)
    while i < n:
        ch = code[i]
        if ch in "\"'":
            quote = ch
            out.append(ch)
            i += 1
            while i < n and code[i] != quote and code[i] != "\n":
                if code[i] == "\\" and i + 1 < n and code[i + 1] != "\n":
                    out.append("xx")
                    i += 2
                else:
                    out.append("x")
                    i += 1
            if i < n and code[i] == quote:
                out.append(quote)
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def string_literals(code: str) -> List[str]:
    lits: List[str] = []
    i, n = 0, len(code)
    while i < n:
        ch = code[i]
        if ch == '"':
            j, buf = i + 1, []
            while j < n and code[j] != '"' and code[j] != "\n":
                if code[j] == "\\" and j + 1 < n:
                    buf.append(code[j:j + 2])
                    j += 2
                else:
                    buf.append(code[j])
                    j += 1
            lits.append("".join(buf))
            i = j + 1
        elif ch == "'":
            j, buf = i + 1, []
            while j < n and code[j] != "'" and code[j] != "\n":
                if code[j] == "\\" and j + 1 < n:
                    buf.append(code[j:j + 2])
                    j += 2
                else:
                    buf.append(code[j])
                    j += 1
            if len("".join(buf)) > 1:
                lits.append("".join(buf))
            i = j + 1
        else:
            i += 1
    return lits


def add_line_numbers(code: str) -> str:
    lines = code.splitlines()
    width = max(2, len(str(len(lines))))
    return "\n".join(f"{idx:0{width}d}: {line}" for idx, line in enumerate(lines, start=1))


# --------------------------------------------------------------------------- structure helpers
def _match_brace(code: str, open_idx: int) -> int:
    depth = 0
    for idx in range(open_idx, len(code)):
        if code[idx] == "{":
            depth += 1
        elif code[idx] == "}":
            depth -= 1
            if depth == 0:
                return idx
    return -1


def defined_function_names(masked: str, language: str = "c") -> Set[str]:
    """Names of functions the student actually *defines* (a bare prototype does not count)."""
    names = {m.group(1) for m in _FUNC_RE.finditer(masked) if m.group(3) == "{"}
    if language == "python":
        names.update(m.group(2) for m in _PY_FUNC_RE.finditer(masked))
    else:
        names.update(m.group(1) for m in _JS_RS_GO_FUNC_RE.finditer(masked))
    return names


def detect_defined_functions(masked: str, language: str = "c") -> List[Tuple[str, str, int, int]]:
    """Returns (name, body, start_line, end_line) for every function *definition*."""
    results: List[Tuple[str, str, int, int]] = []
    if language == "python":
        lines = masked.splitlines()
        for m in _PY_FUNC_RE.finditer(masked):
            indent = len(m.group(1).expandtabs(4))
            name = m.group(2)
            start_line = masked[:m.start()].count("\n") + 1
            body_lines: List[str] = []
            end_line = start_line
            for idx in range(start_line, len(lines)):
                ln = lines[idx]
                if not ln.strip():
                    body_lines.append("")
                    continue
                cur_indent = len(ln) - len(ln.lstrip())
                if cur_indent <= indent:
                    break
                body_lines.append(ln)
                end_line = idx + 1
            results.append((name, "\n".join(body_lines), start_line, end_line))
        return results

    for m in _FUNC_RE.finditer(masked):
        if m.group(3) != "{":
            continue
        name = m.group(1)
        if name in ("if", "for", "while", "switch", "catch"):
            continue
        brace_start = m.end() - 1
        end = _match_brace(masked, brace_start)
        if end == -1:
            continue
        results.append((
            name,
            masked[brace_start + 1:end],
            masked[:m.start()].count("\n") + 1,
            masked[:end].count("\n") + 1,
        ))
    for m in _JS_RS_GO_FUNC_RE.finditer(masked):
        name = m.group(1)
        brace_start = m.end() - 1
        end = _match_brace(masked, brace_start)
        if end == -1:
            continue
        results.append((
            name,
            masked[brace_start + 1:end],
            masked[:m.start()].count("\n") + 1,
            masked[:end].count("\n") + 1,
        ))
    return results


def check_is_empty_or_skeleton_stub(masked: str, language: str = "c") -> bool:
    """True for empty files and skeleton stubs; short real solutions are NOT stubs."""
    lang = (language or "c").lower()
    code_lines = [
        ln.strip() for ln in masked.splitlines()
        if ln.strip() and not ln.strip().startswith("#") and not re.match(r"^(?:import|from|package|using)\b", ln.strip())
    ]
    code_text = " ".join(code_lines)
    if not code_text.strip():
        return True
    if lang in PROSE_LANGS:
        cleaned = re.sub(r"\b(?:todo|tbd|n/a|none|unattempted|skip)\b", "", code_text, flags=re.I).strip()
        return len(cleaned) < 5
    if lang == "python":
        funcs = detect_defined_functions(masked, "python")
        non_func_lines = [
            ln for ln in code_lines
            if not re.match(r"^(?:def\s+\w+|pass\b|\.\.\.|return\s*(?:None|0)?\s*$|raise\s+NotImplementedError)", ln)
        ]
        if funcs and not non_func_lines:
            for _, body, _, _ in funcs:
                cleaned_body = re.sub(r"\b(?:pass|return\s*(?:None|0)?|raise\s+NotImplementedError(?:\([^)]*\))?)\b", "", body)
                if re.sub(r"[\s.:;]", "", cleaned_body):
                    return False
            return True
        cleaned_all = re.sub(r"\b(?:pass|raise\s+NotImplementedError(?:\([^)]*\))?)\b", "", code_text)
        return len(re.sub(r"[\s.:;]", "", cleaned_all)) < 3

    funcs = detect_defined_functions(masked, lang)
    if not funcs:
        return len(code_text) < 12
    for _, body, _, _ in funcs:
        body = re.sub(r"\breturn\s*(?:0|EXIT_SUCCESS|null|None)?\s*;", "", body)
        if re.sub(r"[;{}\s]", "", body):
            return False
    return True


def extract_all_loop_blocks(masked: str, language: str = "c") -> List[Tuple[str, str, str, int]]:
    """(loop_type, header, body, start_line); works across C-brace loops and Python/colon loops."""
    loops: List[Tuple[str, str, str, int]] = []
    if language == "python":
        lines = masked.splitlines()
        for idx, ln in enumerate(lines):
            m = re.match(r"^([ \t]*)(?:async\s+)?(for|while)\b(.*):\s*$", ln)
            if m:
                indent = len(m.group(1).expandtabs(4))
                ltype, header = m.group(2), m.group(3).strip()
                body_lines: List[str] = []
                for j in range(idx + 1, len(lines)):
                    sub = lines[j]
                    if not sub.strip():
                        continue
                    if len(sub) - len(sub.lstrip()) <= indent:
                        break
                    body_lines.append(sub)
                loops.append((ltype, header, "\n".join(body_lines), idx + 1))
        for m in re.finditer(r"[\[\(\{][^\]\)\}\n]*\bfor\s+[A-Za-z_]\w*\s+in\b[^\]\)\}\n]*[\]\)\}]", masked):
            line = masked[:m.start()].count("\n") + 1
            loops.append(("comprehension", m.group(0), m.group(0), line))
        return loops

    for m in re.finditer(r"\b(for|while|foreach)\s*\(", masked):
        paren_start = m.end() - 1
        depth, header_end = 0, -1
        for idx in range(paren_start, len(masked)):
            if masked[idx] == "(":
                depth += 1
            elif masked[idx] == ")":
                depth -= 1
                if depth == 0:
                    header_end = idx
                    break
        if header_end == -1:
            continue
        header = masked[paren_start + 1:header_end]
        rest = masked[header_end + 1:]
        start_line = masked[:m.start()].count("\n") + 1
        brace_m = re.match(r"\s*\{", rest)
        if brace_m:
            brace_start = header_end + 1 + brace_m.end() - 1
            end = _match_brace(masked, brace_start)
            if end != -1:
                loops.append((m.group(1), header, masked[brace_start + 1:end], start_line))
        else:
            semi = rest.find(";")
            if semi != -1:
                loops.append((m.group(1), header, rest[:semi + 1].strip(), start_line))
    for m in re.finditer(r"\b(do|loop)\s*\{", masked):
        brace_start = m.end() - 1
        end = _match_brace(masked, brace_start)
        if end != -1:
            ltype = "do-while" if m.group(1) == "do" else "loop"
            loops.append((ltype, "", masked[brace_start + 1:end], masked[:m.start()].count("\n") + 1))
    for m in re.finditer(r"\b(for|while)\s+([^{;\n]+)\{", masked):
        brace_start = m.end() - 1
        end = _match_brace(masked, brace_start)
        if end != -1:
            loops.append((m.group(1), m.group(2).strip(), masked[brace_start + 1:end], masked[:m.start()].count("\n") + 1))
    return loops


def _context_at(masked: str, pos: int) -> Tuple[str, List[str]]:
    """Text of the current statement up to pos, plus the headers of all enclosing blocks."""
    stack: List[str] = []
    seg_start = 0
    pdepth = 0
    for idx in range(pos):
        ch = masked[idx]
        if ch == "(":
            pdepth += 1
        elif ch == ")":
            pdepth = max(0, pdepth - 1)
        elif pdepth == 0:
            if ch == "{":
                stack.append(masked[seg_start:idx])
                seg_start = idx + 1
            elif ch == "}":
                if stack:
                    stack.pop()
                seg_start = idx + 1
            elif ch == ";":
                seg_start = idx + 1
    return masked[seg_start:pos], stack


def _is_guarded(masked: str, pos: int, language: str = "c") -> bool:
    """True if the statement at pos is under an if/else/loop/switch/ternary."""
    if language == "python":
        lines = masked[:pos].splitlines()
        if not lines:
            return False
        cur_line = lines[-1]
        if re.search(r"\bif\b.*\belse\b", cur_line):
            return True
        cur_indent = len(cur_line) - len(cur_line.lstrip())
        if cur_indent > 0:
            for prev in reversed(lines[:-1]):
                if not prev.strip():
                    continue
                prev_indent = len(prev) - len(prev.lstrip())
                if prev_indent < cur_indent and re.match(r"^[ \t]*(?:if|elif|else|for|while|match|case|try|except)\b", prev):
                    return True
        return False

    segment, headers = _context_at(masked, pos)
    if _CONTROL_RE.search(segment):
        return True
    return any(re.search(r"\b(?:if|else|while|for|do|switch|match|when)\b", h) for h in headers)


def _extract_calls(masked: str, original: str, name: str) -> Iterator[Tuple[int, str]]:
    for m in re.finditer(rf"\b{name}\s*\(", masked):
        open_idx = m.end() - 1
        depth, j, closed = 0, open_idx, False
        while j < len(masked):
            if masked[j] == "(":
                depth += 1
            elif masked[j] == ")":
                depth -= 1
                if depth == 0:
                    closed = True
                    break
            j += 1
        if closed:
            yield m.start(), original[open_idx + 1:j]


def _split_args(text: str) -> List[str]:
    args: List[str] = []
    buf: List[str] = []
    depth = 0
    in_str = in_chr = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if in_str or in_chr:
            buf.append(ch)
            if ch == "\\" and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            if (in_str and ch == '"') or (in_chr and ch == "'"):
                in_str = in_chr = False
        elif ch == '"':
            in_str = True
            buf.append(ch)
        elif ch == "'":
            in_chr = True
            buf.append(ch)
        elif ch in "([{":
            depth += 1
            buf.append(ch)
        elif ch in ")]}":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            args.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail or args:
        args.append(tail)
    return args


def _looks_like_prompt(fmt: str) -> bool:
    low = fmt.replace("\\n", "").replace("\\t", "").strip().lower()
    return low == "" or low.endswith(":") or "enter" in low or "input" in low or "please" in low


def analyze_output_calls(stripped: str, masked: str, language: str = "c") -> Tuple[List[int], Set[str], int]:
    """Returns (unguarded constant-output lines, variables that reach the output, total output call count)."""
    constant_lines: Set[int] = set()
    dynamic_vars: Set[str] = set()
    output_count = 0

    call_names = ("printf", "puts", "putchar", "print", "println", "echo")
    for name in call_names:
        for start, argtext in _extract_calls(masked, stripped, name):
            output_count += 1
            args = _split_args(argtext)
            if not args:
                continue
            line = stripped.count("\n", 0, start) + 1
            first = args[0]

            if language == "python":
                if re.match(r"^[fF][\"']", first):
                    f_vars = re.findall(r"\{([^{}]+)\}", first)
                    ids = {t for expr in f_vars for t in re.findall(r"[A-Za-z_]\w*", expr) if t not in C_KEYWORDS}
                    if ids:
                        dynamic_vars.update(ids)
                        continue
                all_const = True
                for a in args:
                    if re.match(r"^[A-Za-z_]\w*\s*=", a):
                        continue
                    is_str = (len(a) >= 2 and a[0] in "\"'" and a[-1] == a[0])
                    is_num = bool(re.fullmatch(r"[-+]?\d+(?:\.\d+)?", a))
                    if not (is_str or is_num):
                        all_const = False
                        dynamic_vars.update(t for t in re.findall(r"[A-Za-z_]\w*", a) if t not in C_KEYWORDS)
                if all_const:
                    if not _is_guarded(masked, start, language):
                        constant_lines.add(line)
                continue

            first_is_literal = (len(first) >= 2 and first.startswith('"') and first.endswith('"')) or (
                name == "putchar" and first.startswith("'")
            )
            if not first_is_literal:
                if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", first) and len(args) == 1:
                    if not _is_guarded(masked, start, language):
                        constant_lines.add(line)
                    continue
                dynamic_vars.update(t for t in re.findall(r"[A-Za-z_]\w*", argtext) if t not in C_KEYWORDS)
                continue
            fmt = first[1:-1]
            has_conversion = name == "printf" and "%" in fmt.replace("%%", "")
            rest = args[1:]
            if has_conversion:
                ids = {t for t in re.findall(r"[A-Za-z_]\w*", " ".join(rest)) if t not in C_KEYWORDS}
                all_numeric = bool(rest) and all(re.fullmatch(r"[-+]?\d+(?:\.\d+)?", a) for a in rest)
                if ids:
                    dynamic_vars.update(ids)
                    continue
                if not all_numeric:
                    continue
            elif _looks_like_prompt(fmt):
                continue
            if not _is_guarded(masked, start, language):
                constant_lines.add(line)

    for m in re.finditer(r"\b(?:std::)?cout\s*<<([^;]+);", stripped):
        output_count += 1
        expr = m.group(1)
        line = stripped.count("\n", 0, m.start()) + 1
        ids = {t for t in re.findall(r"[A-Za-z_]\w*", mask_literals(expr)) if t not in C_KEYWORDS and t != "x"}
        if ids:
            dynamic_vars.update(ids)
        elif not _is_guarded(masked, m.start(), language):
            constant_lines.add(line)

    return sorted(constant_lines), dynamic_vars, output_count


# --------------------------------------------------------------------------- security / policy / forensic checks
def detect_trojan_and_homoglyphs(raw_code: str) -> Tuple[List[str], List[str]]:
    """Returns (security_violations, warnings) for Unicode BiDi overrides, zero-width chars, and confusable homoglyphs."""
    sec: List[str] = []
    warnings: List[str] = []
    if TROJAN_BIDI_RE.search(raw_code):
        sec.append("Contains Unicode bidirectional control characters (Trojan Source CVE-2021-42574)")
        warnings.append("Unicode bidirectional override characters detected")
    if ZERO_WIDTH_RE.search(raw_code):
        warnings.append("Contains invisible zero-width Unicode characters")
    for tok in re.findall(r"[^\W\d_]{2,}", raw_code):
        has_ascii = any("a" <= c.lower() <= "z" for c in tok)
        has_non_ascii = any(ord(c) > 127 for c in tok)
        if has_ascii and has_non_ascii:
            warnings.append(f"Mixed-script homoglyph identifier '{tok[:30]}' detected")
            break
    return sec, warnings


def analyze_includes(stripped: str, language: str = "c") -> Tuple[List[str], List[str]]:
    """Returns (security violations, harmless notes) for preprocessor directives."""
    if language not in ("c", "cpp"):
        return [], []
    joined = stripped.replace("\\\r\n", "").replace("\\\n", "")
    violations: List[str] = []
    notes: List[str] = []
    for m in _DIRECTIVE_RE.finditer(joined):
        directive = m.group(1).lower()
        rest = m.group(2).strip()
        if directive in {"include_next", "import", "embed"}:
            violations.append(f"Forbidden preprocessor directive #{directive}")
            continue
        if directive != "include":
            continue
        target = re.match(r'^<([^>]*)>|^"([^"]*)"', rest)
        if not target:
            violations.append("Computed #include (macro-expanded path) is not permitted")
            continue
        quoted = target.group(2) is not None
        name = target.group(2) if quoted else target.group(1)
        if name.startswith("/") or ".." in name or (quoted and "/" in name):
            violations.append(f"Forbidden path in #include: {name}")
        elif quoted:
            notes.append(f'Local header "{name}" is not available in single-file submissions')
    return violations, notes


def find_security_violations(stripped: str, masked: str, own_names: Set[str], language: str = "c") -> List[str]:
    found: List[str] = []
    if language not in PROSE_LANGS:
        for name, description in DANGEROUS_CALLS.items():
            if name in own_names:
                continue
            direct_call = rf"\b{name}\s*\("
            paren_call = rf"\(\s*{name}\s*\)\s*\("          # (system)("ls")
            pointer_alias = rf"=\s*&?\s*{name}\s*[;,)]"     # fp = system;
            macro_alias = rf"^[ \t]*#[ \t]*define[^\n]*\b{name}\b"
            if (re.search(direct_call, masked) or re.search(paren_call, masked)
                    or re.search(pointer_alias, masked) or (language in ("c", "cpp") and re.search(macro_alias, masked, re.M))):
                if description not in found:
                    found.append(description)
        for pattern, description in POLYGLOT_DANGEROUS_PATTERNS:
            if re.search(pattern, stripped):
                if description not in found:
                    found.append(description)
        if language in ("c", "cpp"):
            if re.search(r"\b(?:__asm__|__asm|asm)\b", masked):
                found.append("Inline assembly is not permitted")
            if "##" in masked:
                found.append("Token-pasting preprocessor operator (##) is not permitted")
        for lit in string_literals(stripped):
            if RESTRICTED_PATH_RE.match(lit):
                found.append("References a restricted system path")
                break
        if any("../" in lit for lit in string_literals(stripped)):
            found.append("Path traversal sequence in a string literal")
        if re.search(r'\b(?:fopen|freopen|open)\s*\([^;)\n]*,\s*["\'][^"\']*[wa+][^"\']*["\']', stripped):
            found.append("Attempts to create or modify files")
    return list(dict.fromkeys(found))


def detect_prompt_injection(stripped: str, comments: List[str]) -> List[str]:
    haystacks = [
        "\n".join(comments),
        "\n".join(string_literals(stripped)),
        re.sub(r"_+", " ", stripped),
    ]
    snippets: List[str] = []
    for pattern in PROMPT_INJECTION_PATTERNS:
        for text in haystacks:
            m = re.search(pattern, text, flags=re.IGNORECASE)
            if m:
                snippets.append(m.group(0)[:80])
                break
    return snippets


def check_syntax_errors(raw_code: str, masked: str, language: str = "c") -> List[str]:
    """Lightweight in-memory syntax validation for languages where AST/delimiter checks apply."""
    lang = (language or "c").lower()
    if not raw_code.strip():
        return []
    if lang == "python":
        try:
            ast.parse(raw_code)
        except SyntaxError as exc:
            lineno = exc.lineno or 1
            msg = exc.msg or "invalid syntax"
            return [f"Python SyntaxError at line {lineno}: {msg}"]
        return []
    if lang == "json":
        try:
            json.loads(raw_code)
        except ValueError as exc:
            return [f"Invalid JSON: {exc}"]
        return []
    if lang in ("java", "javascript", "typescript", "rust", "go", "csharp", "kotlin", "swift", "scala"):
        open_b, close_b = masked.count("{"), masked.count("}")
        open_p, close_p = masked.count("("), masked.count(")")
        errors = []
        if open_b != close_b:
            errors.append(f"Unbalanced curly braces: {open_b} '{{' vs {close_b} '}}'")
        if open_p != close_p:
            errors.append(f"Unbalanced parentheses: {open_p} '(' vs {close_p} ')'")
        return errors
    return []


def _find_spoof_lines(stripped: str, masked: str, question: Optional[QuestionSpec], language: str = "c") -> List[int]:
    if not question:
        return []
    sample_numbers: Set[str] = set()
    sample_words: Set[str] = set()
    for tc in question.test_cases:
        if not tc.is_sample:
            continue
        sample_numbers.update(re.findall(r"-?\d+", tc.input_data))
        sample_words.update(tok for tok in tc.input_data.split() if not re.fullmatch(r"-?\d+", tok))
    if not sample_numbers and not sample_words:
        return []
    lines_s = stripped.splitlines()
    lines_m = masked.splitlines()
    spoof: List[int] = []
    for idx, line_m in enumerate(lines_m):
        if not re.search(r"\b(?:if|elif|case|when)\b", line_m) or re.search(r"%\s*\d+\s*==", line_m):
            continue
        eq_nums = [n for n in re.findall(r"(?:==|\bcase)\s*(-?\d+)", line_m) if n in sample_numbers]
        nums = [n for n in eq_nums if n not in _BASE_CASE_NUMBERS] or (
            eq_nums if len(eq_nums) >= 2 and re.search(r"&&|\band\b", line_m) else []
        )
        words = [w for w in re.findall(r'strcmp\s*\([^;]*?"([^"]*)"', lines_s[idx]) if w in sample_words]
        words.extend(w for w in re.findall(r'==\s*["\']([^"\']+)["\']', lines_s[idx]) if w in sample_words)
        window_m = "\n".join(lines_m[idx:idx + 4])
        window_s = "\n".join(lines_s[idx:idx + 4])
        if (nums or words) and re.search(r"\b(?:printf|puts|print|println|cout|echo|return)\b", window_m):
            # Exclude windows where printf/print only prints a computed variable (e.g. printf("%d", sum))
            if not re.search(r'\bprintf\s*\(\s*"[^"]*%[^"]*"\s*,\s*[A-Za-z_]', window_s):
                spoof.append(idx + 1)
    return spoof


def estimate_time_complexity(
    masked: str,
    loop_blocks: List[Tuple[str, str, str, int]],
    has_recursion: bool,
    recursive_funcs: List[str],
    func_defs: List[Tuple[str, str, int, int]],
    is_stub: bool,
    question: Optional[QuestionSpec] = None,
) -> Tuple[str, Optional[str]]:
    """Heuristic time-complexity estimator + comparison against question.expected_complexity."""
    if is_stub:
        return "O(1)", None

    max_depth = 0
    has_log_loop = False
    for ltype, header, body, _ in loop_blocks:
        if ltype == "comprehension":
            depth = max(1, len(re.findall(r"\bfor\s+", body)))
        else:
            nested = len(re.findall(r"\b(?:for|while|foreach)\b", body))
            depth = 1 + nested
        max_depth = max(max_depth, depth)
        combined = f"{header} {body}"
        if re.search(r"(?:>>=|<<=|/=|\*=|//=)\s*2\b|\bn\s*&\s*\(\s*n\s*-\s*1\s*\)", combined):
            has_log_loop = True

    has_sort = bool(re.search(r"\b(?:qsort|sort|sorted|Arrays\.sort|Collections\.sort)\s*\(", masked))

    if has_recursion:
        branching = 1
        for fname, fbody, _, _ in func_defs:
            if fname in recursive_funcs:
                calls = len(re.findall(rf"\b{re.escape(fname)}\s*\(", fbody))
                branching = max(branching, calls)
                if re.search(r"/\s*2|>>\s*1|//\s*2", fbody) and calls == 1:
                    has_log_loop = True
        if branching >= 2 and not re.search(r"\b(?:memo|dp|cache|lru_cache)\b", masked):
            est = "O(2^N)"
        elif has_log_loop and max_depth == 0:
            est = "O(log N)"
        else:
            est = "O(N)"
    elif max_depth == 0:
        est = "O(N log N)" if has_sort else "O(1)"
    elif max_depth == 1:
        if has_sort:
            est = "O(N log N)"
        elif has_log_loop:
            est = "O(log N)"
        else:
            est = "O(N)"
    elif max_depth == 2:
        est = "O(N^2)"
    else:
        est = f"O(N^{max_depth})"

    warning: Optional[str] = None
    if question and question.expected_complexity:
        exp = question.expected_complexity.upper()
        rank = {"O(1)": 0, "O(LOG": 1, "O(STEPS)": 2, "O(L)": 2, "O(N)": 2, "O(R * C)": 3, "O(N LOG": 3, "O(N^2)": 4, "O(N^3)": 5, "O(2^N)": 6}
        exp_ranks = [v for k, v in rank.items() if k in exp]
        est_ranks = [v for k, v in rank.items() if est.upper().startswith(k)]
        if exp_ranks and est_ranks and est_ranks[0] > max(exp_ranks) + 1:
            warning = f"Estimated time complexity {est} exceeds target {question.expected_complexity}"
    return est, warning


# --------------------------------------------------------------------------- main entry point
def analyze_c_submission(
    raw_code: str,
    question: Optional[QuestionSpec] = None,
    language: str = "auto",
) -> StaticAnalysisResult:
    lang = language if (language and language != "auto") else (
        question.language if (question and question.language and question.language != "auto") else detect_language("submission.c", raw_code)
    )
    lang = lang.lower()

    stripped, comments = extract_and_strip_comments(raw_code, lang)
    masked = mask_literals(stripped)
    tags = {t.lower() for t in question.topic_tags} if question else set()

    # ---- integrity / policy / forensics
    trojan_sec, trojan_warn = detect_trojan_and_homoglyphs(raw_code)
    injection = detect_prompt_injection(stripped, comments)
    include_violations, include_notes = analyze_includes(stripped, lang)
    own_names = defined_function_names(masked, lang)
    security = list(dict.fromkeys(trojan_sec + include_violations + find_security_violations(stripped, masked, own_names, lang)))
    syntax_errors = check_syntax_errors(raw_code, masked, lang)

    forbidden_hits: List[str] = []
    missing_required_hits: List[str] = []
    if question:
        for name in question.forbidden_constructs:
            haystack = stripped if re.search(r"[./]", name) else masked
            if name and re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", haystack):
                forbidden_hits.append(name)
        for req in getattr(question, "required_constructs", []) or []:
            haystack = stripped if re.search(r"[./]", req) else masked
            if req and not re.search(rf"(?<![A-Za-z0-9_]){re.escape(req)}(?![A-Za-z0-9_])", haystack):
                missing_required_hits.append(req)

    is_stub = check_is_empty_or_skeleton_stub(masked, lang)

    # ---- control flow
    loop_blocks = extract_all_loop_blocks(masked, lang)
    loop_types = sorted({lb[0] for lb in loop_blocks})
    has_loops = bool(loop_blocks)
    func_defs = detect_defined_functions(masked, lang)
    recursive_funcs = [
        name for name, body, _, _ in func_defs
        if name != "main" and re.search(rf"\b{re.escape(name)}\s*\(", body)
    ]
    has_recursion = bool(recursive_funcs)

    # ---- constructs
    code_no_scanf = re.sub(r"\bscanf\s*\([^;{}()]*(?:\([^()]*\)[^;{}()]*)*\)", "", masked)
    has_pointers = bool(
        lang in ("c", "cpp", "rust", "go") and (
            _DEREF_RE.search(code_no_scanf)
            or re.search(r"\b(?:int|char|long|float|double|void|unsigned|short|size_t)\s*\*+\s*[A-Za-z_]", code_no_scanf)
            or re.search(r"->\s*[A-Za-z_]", code_no_scanf)
        )
    )
    no_logical = re.sub(r"&&|\|\|", "  ", code_no_scanf)
    has_bitwise = bool(
        lang not in PROSE_LANGS and (
            re.search(r"<<|>>|\^|~|&=|\|=", no_logical)
            or re.search(r"[A-Za-z0-9_\)\]][ \t]*[&|][ \t]*[A-Za-z0-9_\(]", no_logical)
        )
    )
    has_ternary = bool(
        re.search(r"\?[^?:;{}]*:", masked)
        or (lang == "python" and re.search(r"\b\w+\s+if\s+[^:\n]+\s+else\s+\w+", masked))
    )
    has_goto = bool(re.search(r"\bgoto\s+[A-Za-z_]", masked))

    # ---- output & input behaviour
    constant_lines, dynamic_vars, out_count = analyze_output_calls(stripped, masked, lang)
    printf_count = len(re.findall(r"\bprintf\s*\(", masked)) if lang == "c" else out_count
    scanf_count = (
        len(re.findall(r"\bscanf\s*\(", masked))
        if lang == "c"
        else len(re.findall(r"\b(?:scanf|cin|input|stdin|readLine|Scanner|readFileSync|readline)\b", masked))
    )
    spoof_lines = _find_spoof_lines(stripped, masked, question, lang)
    dead_loop_suspected = bool(loop_blocks) and bool(constant_lines) and not dynamic_vars
    dead_loop_lines = sorted({lb[3] for lb in loop_blocks}) if dead_loop_suspected else []

    is_function_only = bool(
        not is_stub
        and func_defs
        and "main" not in own_names
        and scanf_count == 0
        and out_count == 0
    )

    est_complexity, complexity_warning = estimate_time_complexity(
        masked, loop_blocks, has_recursion, recursive_funcs, func_defs, is_stub, question
    )

    # ---- approach notes (used for the BLUE tab). Ternary operators are deliberately NOT notable.
    notes: List[str] = []
    if has_recursion and "recursion" not in tags:
        suffix = "instead of iterative loops" if not has_loops else "mixed with loops"
        notes.append(f"Recursive formulation ({', '.join(recursive_funcs)}) {suffix}")
    if has_bitwise:
        notes.append("Uses bitwise operations (&, |, ^, <<, >>) for the core logic")
    if has_pointers and "pointers" not in tags:
        notes.append("Uses pointer dereferencing / pointer arithmetic")
    if has_goto:
        notes.append("Uses explicit label/goto control flow")
    if lang == "python":
        if re.search(r"\byield\b", masked):
            notes.append("Uses generator / lazy evaluation (yield)")
        if re.search(r"@(?:functools\.)?(?:lru_cache|cache)\b", stripped):
            notes.append("Uses memoization decorator (@lru_cache / @cache)")
    if lang == "sql" and re.search(r"\bOVER\s*\(\s*(?:PARTITION\s+BY|ORDER\s+BY)\b", stripped, re.I):
        notes.append("Uses SQL window functions (OVER PARTITION/ORDER BY)")
    if (not has_loops and not has_recursion and not is_stub and not constant_lines and dynamic_vars
            and any("loop" in t for t in tags)):
        notes.append("Closed-form expression without iteration")

    reasons: List[str] = []
    if security:
        reasons.append("Security violation: " + "; ".join(security))
    if trojan_warn:
        reasons.extend(trojan_warn)
    if injection:
        reasons.append("Text addressed to an automated grader found in comments/strings")
    if syntax_errors:
        reasons.append("Syntax error: " + "; ".join(syntax_errors))
    if forbidden_hits:
        reasons.append("Uses forbidden construct(s): " + ", ".join(forbidden_hits))
    if missing_required_hits:
        reasons.append("Missing required construct(s): " + ", ".join(missing_required_hits))
    if is_stub:
        reasons.append("Empty file or unattempted stub")
    if spoof_lines:
        reasons.append(f"Equality checks against sample inputs followed by output (lines {spoof_lines})")
    if constant_lines and not has_loops and not has_recursion:
        reasons.append(f"Unconditional constant output (lines {constant_lines})")
    if dead_loop_suspected:
        reasons.append(f"Loop(s) at line(s) {dead_loop_lines} never influence the printed output")
    if complexity_warning:
        reasons.append(complexity_warning)

    return StaticAnalysisResult(
        language=lang,
        line_count=len(stripped.splitlines()),
        has_loops=has_loops,
        loop_types=loop_types,
        has_recursion=has_recursion,
        has_pointers=has_pointers,
        has_bitwise=has_bitwise,
        has_ternary_or_goto=has_ternary or has_goto,
        printf_count=printf_count,
        scanf_count=scanf_count,
        constant_printf_lines=constant_lines,
        sample_spoof_lines=spoof_lines,
        dead_loop_suspected=dead_loop_suspected,
        dead_loop_lines=dead_loop_lines,
        prompt_injection_detected=bool(injection),
        prompt_injection_snippets=injection,
        security_violations=security,
        forbidden_hits=forbidden_hits,
        missing_required_hits=missing_required_hits,
        syntax_errors=syntax_errors,
        trojan_or_homoglyph_warnings=trojan_warn,
        estimated_complexity=est_complexity,
        complexity_warning=complexity_warning,
        include_notes=include_notes,
        is_empty_or_stub=is_stub,
        is_function_only=is_function_only,
        uncommon_construct_notes=notes,
        static_suspicion_reasons=reasons,
    )
