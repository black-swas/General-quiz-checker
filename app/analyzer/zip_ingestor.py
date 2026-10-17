"""Turn uploaded ZIPs / loose submission files into {student: {question: {filename, code, language}}} with strict limits.

Works across programming languages (.c, .cpp, .py, .java, .js, .ts, .rs, .go, .sql, .r, .sh, .jl, .m, .kt, .swift,
.rb, .php, .cs, .scala, .hs, .lua, .pl, .ipynb, ...) and general written/markup tests (.txt, .md, .tex, ...).
Student identity and question number are recovered from whatever naming students used:
25104_que1.py, que1_25104.c, Que 1.java, 25104_que1 (1).cpp, 25104_que1_final.sql, answer1.txt, 1.py, alice-q2.md,
batch/25104/que1.py, 25104/Q1/main.c, Moodle folders ("Name_123_assignsubmission_file_/que1.py") and
per-student zips (25104.zip containing que1.*). Nothing here raises on bad names: a file that cannot be
placed is skipped with a warning, and every ambiguity (duplicates, conflicting names) is reported.
"""
import codecs
import io
import json
import os
import re
import zipfile
import zlib
from typing import Any, Dict, List, Optional, Set, Tuple

from app.config import settings

GENERIC_DIR_NAMES = {
    "src", "code", "codes", "submission", "submissions", "all", "batch", "class", "quiz", "lab", "labs",
    "master", "files", "file", "student", "students", "output", "input", "__macosx", "answers", "answer",
    "solutions", "solution", "upload", "uploads", "download", "downloads", "desktop", "documents", "final",
    "new folder", "untitled folder", "assignment", "assignments", "homework", "exam", "test", "main",
    "program", "programs", "data", "project", "archive", "extracted", "unzipped", "backup",
}
GENERIC_DIR_RE = re.compile(
    r"^(?:batch|quiz|lab|set|paper|assignment|assign|hw|homework|exam|test|task|day|week|section|group)\s*[-_]?\s*\d*$",
    re.IGNORECASE,
)
# Words that are never a student identifier when they appear where one is expected ("que1_solution.py").
NOISE_WORDS = {
    "solution", "solutions", "answer", "answers", "ans", "main", "code", "program", "prog", "submission",
    "final", "test", "output", "source", "src", "file", "copy", "new", "old", "untitled", "program1",
    "tempcoderunnerfile",
}
IGNORED_DIR_SEGMENTS = {
    "__macosx", "__pycache__", ".git", ".svn", ".hg", ".vscode", ".idea", "node_modules",
    ".ipynb_checkpoints", ".pytest_cache", ".mypy_cache", "venv", ".venv", "env",
}
IGNORED_BASENAMES = {"thumbs.db", "desktop.ini", "a.out", "__init__.py", "package-lock.json", "yarn.lock", "cargo.lock"}
IGNORED_EXTENSIONS = {
    ".out", ".exe", ".o", ".obj", ".dll", ".so", ".dylib", ".png", ".jpg", ".jpeg", ".pdf", ".ds_store", ".git",
    ".gif", ".bmp", ".class", ".pyc", ".pyo", ".pyd", ".a", ".lib", ".bin", ".wasm", ".jar", ".war", ".swp", ".swo",
}
OFFICE_OR_BINARY_DOCS = {".doc", ".docx", ".rtf", ".odt", ".xls", ".xlsx", ".ppt", ".pptx"}
UNSUPPORTED_ARCHIVES = (".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zipx")

EXT_TO_LANGUAGE: Dict[str, str] = {
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".c++": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".ino": "cpp",
    ".py": "python",
    ".pyw": "python",
    ".ipynb": "python",
    ".java": "java",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".rs": "rust",
    ".go": "go",
    ".r": "r",
    ".rmd": "r",
    ".sql": "sql",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".jl": "julia",
    ".m": "matlab",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".swift": "swift",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".scala": "scala",
    ".hs": "haskell",
    ".lua": "lua",
    ".pl": "perl",
    ".pm": "perl",
    ".dart": "dart",
    ".zig": "zig",
    ".pas": "pascal",
    ".f90": "fortran",
    ".f95": "fortran",
    ".for": "fortran",
    ".cob": "cobol",
    ".v": "verilog",
    ".sv": "verilog",
    ".vhd": "vhdl",
    ".vhdl": "vhdl",
    ".asm": "assembly",
    ".s": "assembly",
    ".clj": "clojure",
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
    ".ml": "ocaml",
    ".fs": "fsharp",
    ".sol": "solidity",
    ".tex": "latex",
    ".md": "markdown",
    ".markdown": "markdown",
    ".rst": "markdown",
    ".txt": "text",
    ".text": "text",
    ".ans": "text",
    ".pseudo": "text",
    ".csv": "text",
    ".tsv": "text",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".xml": "xml",
    ".html": "html",
    ".css": "css",
}

SUPPORTED_EXTENSIONS: Set[str] = set(EXT_TO_LANGUAGE.keys())
CODE_DOUBLE_EXT_RE = re.compile(
    r"\.(?:c|cpp|cc|cxx|h|hpp|py|java|js|ts|rs|go|r|sql|sh|jl|kt|swift|rb|php|cs|scala|hs|lua|pl)\.(?:txt|docx|doc|rtf)$",
    re.IGNORECASE,
)

Q_WORD = r"(?:questions?|ques|que|qn|qno|q|problems?|prob|programs?|prog|answers?|ans|solutions?|sol|task|exercise|ex|p)"
SEP = r"[\s_\-.]"
_Q_WORD_RE = re.compile(rf"^{Q_WORD}$", re.IGNORECASE)
_ROLL_FIRST = re.compile(rf"^(?P<roll>.+?){SEP}+{Q_WORD}{SEP}*(?P<n>\d{{1,2}})$", re.IGNORECASE)
_QUESTION_FIRST = re.compile(rf"^{Q_WORD}{SEP}*(?P<n>\d{{1,2}})(?:{SEP}+(?P<roll>.+))?$", re.IGNORECASE)
_NUMERIC_TAIL = re.compile(rf"^(?P<roll>[^\s_\-.]+){SEP}(?P<n>\d{{1,2}})$")
_BARE_NUMBER = re.compile(r"^(?P<n>\d{1,2})$")
_FALLBACK_Q = re.compile(rf"(?<![A-Za-z]){Q_WORD}{SEP}*(?P<n>\d{{1,2}})$", re.IGNORECASE)
# "26416_que1 (1)", "que1 - Copy", "que1_final", "que1-v2": download/editor decorations that are not part of the name.
_DECORATION = re.compile(
    rf"(?:{SEP}*\(\d+\)|{SEP}*\[\d+\]"
    rf"|{SEP}+(?:copy(?:{SEP}*\(?\d+\)?)?|final\d*|new|old|fixed|updated|submitted|correct(?:ed)?|done|edited|"
    rf"latest|backup|bak|resubmit(?:ted)?|attempt\d*|rev\d*|v(?:er(?:sion)?)?{SEP}?\d+))+$",
    re.IGNORECASE,
)
_COPY_PREFIX = re.compile(rf"^copy{SEP}+of{SEP}+", re.IGNORECASE)
# "RollNo_26416" -> "26416", "roll_no_26416" -> "26416"
_ROLL_PREFIX_RE = re.compile(r"^(?:roll(?:[_\-\s.]*(?:no|num|number))?|reg(?:[_\-\s.]*no)?|enrol\w*)[_\-\s.:]+(?=\d)", re.IGNORECASE)
# "student7_que_paper2" -> "student7"; "26416_quiz3" -> "26416"
ROLL_SUFFIX_RE = re.compile(r"[_\-](?:que|quiz|paper|set|batch|lab)[_\-\w]*$", re.IGNORECASE)
_MOODLE_RE = re.compile(r"_assignsubmission_\w*$", re.IGNORECASE)
_ROLL_OK = re.compile(r'^[^\x00-\x1f/\\:*?"<>|]{1,60}$')     # any script (combining marks are not \w); no path/control characters
_C_PREPROC_RE = re.compile(r"^#\s*(?:include|define|undef|ifdef|ifndef|if|elif|else|endif|pragma|error|line)\b", re.IGNORECASE)


class ZipIngestionError(Exception):
    pass


def detect_language(filename: str, code: str = "") -> str:
    """Infer canonical language identifier from filename extension, shebang, or content signatures."""
    ext = os.path.splitext(filename.lower())[1]
    lang = EXT_TO_LANGUAGE.get(ext, "")
    if lang and lang not in ("text", "markdown"):
        return lang

    first_line = (code.splitlines()[0] if code else "").strip()
    if first_line.startswith("#!"):
        low = first_line.lower()
        if "python" in low:
            return "python"
        if any(sh in low for sh in ("bash", "/sh", "zsh")):
            return "bash"
        if "node" in low:
            return "javascript"
        if "ruby" in low:
            return "ruby"
        if "perl" in low:
            return "perl"
        if "rscript" in low:
            return "r"

    sample = code[:2000]
    if re.search(r"^\s*#include\s*[<\"].*?[>\"]", sample, re.M):
        if re.search(r"\b(?:std::|cout|cin|vector<|iostream)\b", sample):
            return "cpp"
        return "c"
    if re.search(r"^\s*def\s+[A-Za-z_]\w*\s*\(.*\)\s*:", sample, re.M) or re.search(r"^\s*import\s+\w+", sample, re.M):
        return "python"
    if re.search(r"\bpublic\s+(?:static\s+)?(?:class|void\s+main)\b", sample):
        return "java"
    if re.search(r"\b(?:SELECT|INSERT\s+INTO|CREATE\s+TABLE|FROM|WHERE|GROUP\s+BY)\b", sample, re.I):
        return "sql"
    if re.search(r"\b(?:function\s+\w+\s*\(|const\s+\w+\s*=\s*\(|console\.log\s*\()", sample):
        return "javascript"
    if re.search(r"\bfn\s+main\s*\(\s*\)|println!\s*\(", sample):
        return "rust"
    if re.search(r"\bpackage\s+main\b|\bfunc\s+main\s*\(\s*\)", sample):
        return "go"
    return lang or "text"


def _strip_submission_ext(fname: str) -> str:
    base, ext = os.path.splitext(fname)
    if ext.lower() in SUPPORTED_EXTENSIONS:
        return base
    return fname


def _is_generic(name: str) -> bool:
    low = name.strip().lower()
    return low in GENERIC_DIR_NAMES or bool(GENERIC_DIR_RE.match(low)) or bool(_Q_WORD_RE.match(low))


def _strip_decorations(stem: str) -> Tuple[str, bool]:
    """(stem without copy/version decorations, whether anything was removed)."""
    original = stem
    stem = _COPY_PREFIX.sub("", stem.strip())
    stem = _DECORATION.sub("", stem).strip(" _-.")
    return (stem or original.strip()), stem != original.strip()


def _clean_roll(name: str) -> str:
    name = _MOODLE_RE.sub("", name.strip())
    name = _DECORATION.sub("", name)
    name = _ROLL_PREFIX_RE.sub("", name)
    cleaned = ROLL_SUFFIX_RE.sub("", name)
    cleaned = re.sub(r"\s+", " ", (cleaned or name)).strip(" _-.")
    return cleaned


def _plausible_roll(text: Optional[str]) -> bool:
    if not text:
        return False
    text = text.strip(" _-.")
    return bool(_ROLL_OK.match(text)) and len(text) >= 1 and text.lower() not in NOISE_WORDS and not _is_generic(text)


def _parse_stem(stem: str, allow_bare_number: bool) -> Tuple[Optional[str], Optional[str]]:
    """(roll, question) from a name without extension; either may be None."""
    m = _ROLL_FIRST.match(stem)
    if m and _plausible_roll(m.group("roll")):
        return _clean_roll(m.group("roll")), f"Q{int(m.group('n'))}"
    m = _QUESTION_FIRST.match(stem)
    if m:
        tail = (m.group("roll") or "").strip(" _-.")
        looks_like_id = bool(tail) and _plausible_roll(tail) and (any(c.isdigit() for c in tail) or (" " not in tail and len(tail) >= 3))
        return (_clean_roll(tail) if looks_like_id else None), f"Q{int(m.group('n'))}"
    m = _NUMERIC_TAIL.match(stem)
    if m:
        roll = m.group("roll")
        if _plausible_roll(roll):
            return _clean_roll(roll), f"Q{int(m.group('n'))}"
        return None, f"Q{int(m.group('n'))}"
    if allow_bare_number:
        m = _BARE_NUMBER.match(stem)
        if m:
            return None, f"Q{int(m.group('n'))}"
    m = _FALLBACK_Q.search(stem)
    if m:
        return None, f"Q{int(m.group('n'))}"
    return None, None


def _folder_identity(folder: str) -> Optional[str]:
    folder = _clean_roll(_strip_decorations(folder)[0])
    return folder if _plausible_roll(folder) else None


def parse_roll_and_question(path_hint: str, zip_context: str = "") -> Tuple[Optional[str], Optional[str]]:
    """Student identifier and question id (Q1..Qn) from a path inside an archive."""
    roll, q_id, _, _ = _parse_path(path_hint, zip_context)
    if q_id is None:
        return None, None
    return roll, q_id


def _parse_path(path_hint: str, zip_context: str = "") -> Tuple[Optional[str], Optional[str], bool, Optional[str]]:
    """-> (roll, question, name_was_decorated, source) where source is "file", "folder", "zip" or None."""
    parts = [p for p in path_hint.replace("\\", "/").strip("/").split("/") if p]
    if not parts:
        return None, None, False, None
    fname, dir_parts = parts[-1], parts[:-1]
    ext = os.path.splitext(fname.lower())[1]
    if ext not in SUPPORTED_EXTENSIONS:
        return None, None, False, None
    stem = _strip_submission_ext(fname)
    stem, decorated = _strip_decorations(stem)

    roll, q_id = _parse_stem(stem, allow_bare_number=True)
    if q_id and roll:
        return roll, q_id, decorated, "file"

    source = "file"
    if q_id is None:                                   # main.py inside 25104/Q1/, or 25104_que1/main.c
        for folder in reversed(dir_parts):
            f_roll, f_q = _parse_stem(_strip_decorations(folder)[0], allow_bare_number=False)
            if f_q:
                q_id, roll, source = f_q, f_roll, "folder"
                break
    if q_id is None:
        if _plausible_roll(stem) and any(c.isdigit() for c in stem):
            return _clean_roll(stem), None, decorated, "file"
        for folder in reversed(dir_parts):
            found = _folder_identity(folder)
            if found:
                return found, None, decorated, "folder"
        if zip_context:
            stem_ctx = os.path.splitext(os.path.basename(zip_context))[0]
            found = _folder_identity(stem_ctx)
            if found:
                return found, None, decorated, "zip"
        return None, None, decorated, None

    if roll:
        return roll, q_id, decorated, source

    for folder in reversed(dir_parts):                 # innermost folder first
        found = _folder_identity(folder)
        if found and _parse_stem(_strip_decorations(folder)[0], allow_bare_number=False)[1] is None:
            return found, q_id, decorated, "folder"
    if zip_context:
        stem_ctx = os.path.splitext(os.path.basename(zip_context))[0]
        found = _folder_identity(stem_ctx)
        if found:
            return found, q_id, decorated, "zip"
    return None, q_id, decorated, None



def _folder_roll(path_hint: str) -> Optional[str]:
    """Identity implied by the containing folders only (used to warn about name conflicts)."""
    parts = [p for p in path_hint.replace("\\", "/").strip("/").split("/") if p][:-1]
    for folder in reversed(parts):
        found = _folder_identity(folder)
        if found and _parse_stem(_strip_decorations(folder)[0], allow_bare_number=False)[1] is None:
            return found
    return None


# ---- identity written inside the file ("// Name: Alice  Roll No: 25104" or "# Roll No: 25104" or "-- Roll No: 25104")
_ID_LABEL = r"(?:roll|rollno|enrol\w*|registration|regd?|reg|admission|student|stu)(?![A-Za-z])"
_ROLL_LABELLED = re.compile(
    rf"(?<![A-Za-z]){_ID_LABEL}[\s._]*(?:no|number|num|id|#)?\.?\s*[:=\-\u2013\u2014#]?\s*(?P<v>[A-Za-z]{{0,6}}\d[\w\-/]{{2,20}})",
    re.IGNORECASE,
)
_KEYWORD = (r"(?:roll|rollno|reg|regd|registration|enrol\w*|id|no|number|class|section|sec|date|batch|lab|program|"
            r"question|problem|que|q|quiz|paper|set|time|email|e-mail|student|name|author)")
_NAME_WORD = rf"(?!{_KEYWORD}(?![A-Za-z]))[^\W\d_][\w.'\u2019\-]*"
_NAME_PHRASE = rf"{_NAME_WORD}(?:\s+{_NAME_WORD}){{0,3}}"
_NAME_LABELLED = re.compile(
    rf"(?:^|[,;|(]\s*)(?:full\s*name|student\s*name|student|name|author|submitted\s+by|written\s+by|created\s+by|made\s+by)"
    rf"\s*[:=\-\u2013\u2014]\s*(?P<v>{_NAME_PHRASE})",
    re.IGNORECASE,
)
_NAME_THEN_ROLL = re.compile(rf"^(?P<name>{_NAME_PHRASE})\s*[-\u2013\u2014,:|/]\s*(?P<roll>\d{{5,12}})$", re.IGNORECASE)
_ROLL_THEN_NAME = re.compile(rf"^(?P<roll>\d{{5,12}})\s*[-\u2013\u2014,:|/]?\s*(?P<name>{_NAME_PHRASE})$", re.IGNORECASE)
_BARE_ROLL = re.compile(r"^\d{5,12}$")
_PLACEHOLDER_NAMES = {
    "name", "your name", "student name", "full name", "xyz", "abc", "xxx", "n/a", "na", "none", "anonymous", "unknown",
    "john doe", "jane doe", "student", "your_name", "enter name", "firstname lastname", "first last", "test", "me",
}


def _header_comment_lines(text: str, limit: int = 40) -> List[str]:
    """Text of leading comments across languages (//, /* */, #, --, %, <!-- -->, Python docstrings)."""
    out: List[str] = []
    in_block = False
    block_end_token = "*/"
    for line in text.split("\n")[:limit]:
        s = line.strip()
        if in_block:
            end = s.find(block_end_token)
            body = s if end < 0 else s[:end]
            in_block = end < 0
        elif s.startswith("//") or s.startswith("--"):
            body = s[2:]
        elif s.startswith("#") and not s.startswith("#!") and not _C_PREPROC_RE.match(s):
            body = s[1:]
        elif s.startswith("%") and not s.startswith("%:"):
            body = s[1:]
        elif s.startswith("/*"):
            body = s[2:]
            end = body.find("*/")
            if end >= 0:
                body = body[:end]
            else:
                in_block = True
                block_end_token = "*/"
        elif s.startswith("<!--"):
            body = s[4:]
            end = body.find("-->")
            if end >= 0:
                body = body[:end]
            else:
                in_block = True
                block_end_token = "-->"
        elif s.startswith(('"""', "'''")):
            quote = s[:3]
            body = s[3:]
            end = body.find(quote)
            if end >= 0:
                body = body[:end]
            else:
                in_block = True
                block_end_token = quote
        else:
            continue
        body = body.strip("/*=-_#~<!>\"' \t")
        if body:
            out.append(body)
    return out


def _clean_name(value: str) -> Optional[str]:
    value = re.sub(r"\s+", " ", value).strip(" .,-_")
    low = value.lower()
    if (len(value) < 2 or low in _PLACEHOLDER_NAMES or any(w in low for w in ("your", "enter", "firstname", "lastname"))
            or not _plausible_roll(value)):
        return None
    return value


def _header_identity(text: str) -> Tuple[Optional[str], Optional[str]]:
    """(roll, name) a student wrote in the file's leading comments; either may be None."""
    lines = _header_comment_lines(text)
    roll = name = None
    for line in lines:
        if roll is None:
            m = _ROLL_LABELLED.search(line)
            if m:
                value = m.group("v").strip(".,;:-/")
                if 3 <= len(value) <= 24 and _plausible_roll(value):
                    roll = value
        if name is None:
            m = _NAME_LABELLED.search(line)
            if m:
                name = _clean_name(m.group("v"))
    if roll is None or name is None:
        for line in lines[:6]:                         # unlabelled "Alice Smith - 25104" / "25104 Alice" / "25104"
            for rx in (_NAME_THEN_ROLL, _ROLL_THEN_NAME):
                m = rx.match(line)
                if m:
                    roll = roll or m.group("roll")
                    name = name or _clean_name(m.group("name"))
            if roll is None and _BARE_ROLL.match(line):
                roll = line
    return roll, name


_HEADER_Q_RE = re.compile(
    rf"(?:^|[,;|\s(])(?:question|ques|que|qn|qno|q|problem|prob|task|exercise|ex)\s*[:=\-\u2013\u2014#.]*\s*(?P<n>\d{{1,2}})\b",
    re.IGNORECASE,
)


def _header_question(text: str) -> Optional[str]:
    """Detect if a student specified their question number in leading file comments (e.g. // Question 2)."""
    lines = _header_comment_lines(text, limit=30)
    for line in lines:
        m = _HEADER_Q_RE.search(line)
        if m:
            n = int(m.group("n"))
            if 1 <= n <= settings.max_questions_per_paper:
                return f"Q{n}"
    return None


def _decode_source(raw: bytes, ext: str = "") -> str:
    """Bytes -> text for UTF-8 (with/without BOM), UTF-16/32, Windows-1252/Latin-1, and .ipynb notebooks."""
    if raw.startswith(codecs.BOM_UTF8):
        text = raw[3:].decode("utf-8", errors="replace")
    elif raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        text = raw.decode("utf-32", errors="replace")
    elif raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        text = raw.decode("utf-16", errors="replace")
    elif len(raw) >= 4 and raw[:400].count(b"\x00") > len(raw[:400]) // 4:       # UTF-16 without a BOM
        text = raw.decode("utf-16-le" if raw[1:400:2].count(b"\x00") < raw[0:400:2].count(b"\x00") else "utf-16-be",
                          errors="replace")
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = raw.decode("cp1252")
            except UnicodeDecodeError:
                text = raw.decode("latin-1", errors="replace")
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")


    if ext == ".ipynb":
        try:
            nb = json.loads(text)
            cells = nb.get("cells", [])
            extracted_cells: List[str] = []
            for idx, cell in enumerate(cells, start=1):
                ctype = cell.get("cell_type", "")
                src = cell.get("source", [])
                cell_text = "".join(src) if isinstance(src, list) else str(src)
                if not cell_text.strip():
                    continue
                if ctype == "markdown":
                    commented = "\n".join(f"# {ln}" for ln in cell_text.splitlines())
                    extracted_cells.append(f"# --- Markdown Cell {idx} ---\n{commented}")
                elif ctype == "code":
                    extracted_cells.append(f"# --- Code Cell {idx} ---\n{cell_text}")
            if extracted_cells:
                return "\n\n".join(extracted_cells) + "\n"
        except (ValueError, TypeError, AttributeError):
            pass
    return text


def _read_member(zf: zipfile.ZipFile, member: zipfile.ZipInfo, limit: int) -> Optional[bytes]:
    """Read at most `limit` bytes of decompressed data; None if the member is larger (header lies included)."""
    with zf.open(member) as handle:
        data = handle.read(limit + 1)
    return None if len(data) > limit else data


def _is_ignored_path(path_hint: str) -> bool:
    parts = [p.lower() for p in path_hint.replace("\\", "/").split("/") if p]
    if any(p in IGNORED_DIR_SEGMENTS for p in parts[:-1]):
        return True
    base = parts[-1] if parts else ""
    if not base or base.startswith((".", "~")) or base in IGNORED_BASENAMES:
        return True
    stem = os.path.splitext(base)[0]
    if stem.startswith("tempcoderunnerfile"):
        return True
    return False


def extract_student_submissions(
    uploaded_files: List[Tuple[str, Any]],
    allowed_extensions: Optional[Set[str]] = None,
    single_question_fallback: Optional[str] = None,
) -> Dict[str, Any]:
    students: Dict[str, Dict[str, Dict[str, str]]] = {}
    warnings: List[str] = []
    ignored_binaries: List[str] = []
    ignored_other: Dict[str, int] = {}
    languages_detected: Dict[str, int] = {}
    canonical: Dict[str, str] = {}                     # casefolded roll -> first spelling seen ("S26416" == "s26416")
    ranks: Dict[Tuple[str, str], Tuple[int, int, Tuple[int, ...], int]] = {}
    records: List[Dict[str, Any]] = []
    counters = {"c_files": 0, "uncompressed": 0, "processed": 0, "unknown": 0, "order": 0}
    valid_exts = {e.lower() for e in allowed_extensions} if allowed_extensions else SUPPORTED_EXTENSIONS

    total_upload = sum(os.path.getsize(c) if isinstance(c, (str, bytes, os.PathLike)) and not isinstance(c, (bytes, bytearray)) else len(c) for _, c in uploaded_files)
    if total_upload > settings.max_upload_bytes:
        raise ZipIngestionError(
            f"Upload ({total_upload / 1048576:.1f} MB) exceeds the {settings.max_upload_bytes / 1048576:.0f} MB limit."
        )

    def safe_path(path: str) -> bool:
        return not (".." in path.replace("\\", "/").split("/") or path.startswith(("/", "\\")) or re.match(r"^[a-zA-Z]:", path))

    def note_other(ext: str, path_hint: str) -> None:
        lower = path_hint.lower()
        if CODE_DOUBLE_EXT_RE.search(lower):
            warnings.append(f"'{path_hint}' looks like a source file saved with an extra extension; skipped (remove the trailing {ext}).")
        elif ext in OFFICE_OR_BINARY_DOCS or ext in SUPPORTED_EXTENSIONS:
            ignored_other[ext] = ignored_other.get(ext, 0) + 1

    def register_submission_file(path_hint: str, zip_context: str, raw: bytes, mtime: Tuple[int, ...] = ()) -> None:
        path_hint = path_hint.replace("\\", "/")
        basename = os.path.basename(path_hint)
        lower_base = basename.lower()
        ext = os.path.splitext(lower_base)[1]
        if ext in IGNORED_EXTENSIONS or lower_base == "a.out":
            ignored_binaries.append(path_hint)
            return
        if _is_ignored_path(path_hint):
            return
        if CODE_DOUBLE_EXT_RE.search(lower_base):
            note_other(ext, path_hint)
            return
        if ext not in valid_exts:
            note_other(ext, path_hint)
            return

        text = _decode_source(raw, ext)
        if not text.strip():
            warnings.append(f"'{path_hint}' is empty.")

        roll, q_id, decorated, source = _parse_path(path_hint, zip_context)
        if not q_id:
            h_q = _header_question(text)
            if h_q:
                q_id = h_q
                source = source or "header"
            elif single_question_fallback and (
                (roll and (any(c.isdigit() for c in roll) or source in ("folder", "zip")))
                or ext not in (".txt", ".md", ".markdown", ".json", ".csv", ".yaml", ".yml", ".xml", ".html", ".css")
            ):
                q_id = single_question_fallback
                source = source or "fallback"

        if not q_id:
            if ext in (".txt", ".md", ".markdown", ".json", ".csv", ".yaml", ".yml", ".xml", ".html", ".css"):
                ignored_other[ext] = ignored_other.get(ext, 0) + 1
                return

            warnings.append(f"Unrecognised submission filename '{path_hint}' (expected something like que1{ext} or 25104_que1{ext}); skipped.")
            return

        if not 1 <= int(q_id[1:]) <= settings.max_questions_per_paper:
            warnings.append(f"'{path_hint}' refers to {q_id}, outside 1..{settings.max_questions_per_paper}; skipped.")
            return

        lang = detect_language(basename, text)
        h_roll, h_name = _header_identity(text)
        records.append({"path": path_hint, "basename": basename, "context": zip_context, "mtime": mtime, "text": text,
                        "lang": lang, "q": q_id, "roll": roll, "decorated": decorated, "source": source,
                        "h_roll": h_roll, "h_name": h_name})

    def resolve_identities() -> None:
        """Fill in `roll` for every record."""
        groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for rec in records:
            if rec["source"] in ("file", "folder"):
                h = rec["h_roll"]
                if h and rec["roll"] and h.casefold() != rec["roll"].casefold() and any(c.isdigit() for c in h) \
                        and any(c.isdigit() for c in rec["roll"]):
                    warnings.append(f"'{rec['path']}': the path says student '{rec['roll']}' but the file says roll '{h}'; "
                                    f"using '{rec['roll']}'.")
                continue
            groups.setdefault((rec["context"], os.path.dirname(rec["path"])), []).append(rec)
        for group in groups.values():
            rolls = {r["h_roll"].casefold(): r["h_roll"] for r in group if r["h_roll"]}
            names = {r["h_name"].casefold(): r["h_name"] for r in group if r["h_name"]}
            one_student = len({r["q"] for r in group}) == len(group)
            batch = len(rolls) > 1 or len(names) > 1          # several people share this zip/folder
            sole_roll = next(iter(rolls.values())) if len(rolls) == 1 and one_student else None
            sole_name = next(iter(names.values())) if len(names) == 1 and one_student else None
            for rec in group:
                ctx = rec["roll"]                              # identity taken from the zip name, if any
                if rec["h_roll"]:
                    if ctx and ctx.casefold() != rec["h_roll"].casefold() and any(c.isdigit() for c in ctx):
                        warnings.append(f"'{rec['path']}': the archive name says '{ctx}' but the file says roll "
                                        f"'{rec['h_roll']}'; using the roll written in the file.")
                    rec["roll"] = rec["h_roll"]
                elif sole_roll:
                    rec["roll"] = sole_roll
                elif ctx and not batch and any(c.isdigit() for c in ctx):
                    rec["roll"] = ctx
                elif rec["h_name"]:
                    rec["roll"] = rec["h_name"]
                elif sole_name:
                    rec["roll"] = sole_name
                elif ctx and not batch:
                    rec["roll"] = ctx
                else:
                    rec["roll"] = None

    def place_records() -> None:
        for rec in records:
            roll, q_id, path_hint, basename = rec["roll"], rec["q"], rec["path"], rec["basename"]
            if not roll:
                counters["unknown"] += 1
                roll = f"UNKNOWN_STUDENT_{counters['unknown']}"
                warnings.append(f"Could not identify the student for '{path_hint}'; assigned '{roll}'.")
            else:
                folder_roll = _folder_roll(path_hint)
                if (folder_roll and folder_roll.casefold() != roll.casefold() and "/" in path_hint
                        and any(c.isdigit() for c in roll) and any(c.isdigit() for c in folder_roll)):
                    warnings.append(f"'{path_hint}': the file name says student '{roll}' but its folder says '{folder_roll}'; using '{roll}'.")
                roll = canonical.setdefault(roll.casefold(), roll)

            text = rec["text"]
            lang = rec["lang"]
            counters["order"] += 1
            rank = (1 if text.strip() else 0, 0 if rec["decorated"] else 1, rec["mtime"], counters["order"])
            key = (roll, q_id)
            if key in ranks:
                prior = students[roll][q_id]["filename"]
                if rank > ranks[key]:
                    warnings.append(f"Duplicate file for student '{roll}', {q_id}: '{basename}' replaced earlier '{prior}'.")
                    old_lang = students[roll][q_id].get("language", "c")
                    languages_detected[old_lang] = max(0, languages_detected.get(old_lang, 1) - 1)
                else:
                    warnings.append(f"Duplicate file for student '{roll}', {q_id}: kept '{prior}', ignored '{basename}'.")
                    continue
            else:
                counters["c_files"] += 1
            ranks[key] = rank
            languages_detected[lang] = languages_detected.get(lang, 0) + 1
            students.setdefault(roll, {})[q_id] = {"filename": basename, "code": text, "language": lang}

    def account(size: int) -> None:
        counters["uncompressed"] += size
        if counters["uncompressed"] > settings.max_uncompressed_bytes:
            raise ZipIngestionError(
                f"Decompressed data exceeds {settings.max_uncompressed_bytes / 1048576:.0f} MB (possible zip bomb)."
            )

    def process_zip(zip_name: str, data: Any, context: str, depth: int) -> None:
        if depth > settings.max_nesting_depth:
            warnings.append(f"Skipped nested archive '{zip_name}' (deeper than {settings.max_nesting_depth} levels).")
            return
        try:
            if isinstance(data, (str, os.PathLike)):
                zf = zipfile.ZipFile(str(data))
            else:
                zf = zipfile.ZipFile(io.BytesIO(data))
        except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError):
            warnings.append(f"Corrupted ZIP skipped: {zip_name}")
            return
        with zf:
            for member in zf.infolist():
                if member.is_dir():
                    continue
                name = member.filename.replace("\\", "/")
                if not safe_path(name) or "__MACOSX" in name or os.path.basename(name).startswith("."):
                    continue
                counters["processed"] += 1
                if counters["processed"] > settings.max_file_count:
                    warnings.append(f"Stopped after {settings.max_file_count} archive entries (file-count limit).")
                    return
                lower = name.lower()
                is_zip = lower.endswith(".zip")
                if member.flag_bits & 0x1:
                    warnings.append(f"Skipped '{name}': the archive entry is password-protected.")
                    continue
                if member.compress_size > 0 and member.file_size / member.compress_size > 100:
                    warnings.append(f"Skipped '{name}': suspicious compression ratio.")
                    continue
                if lower.endswith(UNSUPPORTED_ARCHIVES):
                    warnings.append(f"Skipped '{name}': only .zip archives are supported (re-zip it).")
                    continue
                limit = settings.max_upload_bytes if is_zip else settings.max_single_file_bytes
                try:
                    payload = _read_member(zf, member, limit)
                except (RuntimeError, NotImplementedError, zlib.error, EOFError, OSError, zipfile.BadZipFile):
                    warnings.append(f"Skipped '{name}': damaged or unreadable archive entry.")
                    continue
                if payload is None:
                    warnings.append(f"Skipped '{name}': larger than the {limit // 1024} KB per-file limit.")
                    continue
                account(len(payload))
                if is_zip:
                    process_zip(name, payload, os.path.basename(name), depth + 1)
                else:
                    register_submission_file(name, context, payload, tuple(member.date_time))

    for fname, content in uploaded_files:
        norm = (fname or "").replace("\\", "/")
        base = os.path.basename(norm)
        if not base or not safe_path(norm):
            warnings.append(f"Rejected unsafe filename: {fname!r}")
            continue
        lower = base.lower()
        ext = os.path.splitext(lower)[1]
        is_path = isinstance(content, (str, os.PathLike))

        if lower.endswith(".zip"):
            process_zip(base, content, base, 1)
        elif ext in valid_exts and not CODE_DOUBLE_EXT_RE.search(lower):
            c_size = os.path.getsize(content) if is_path else len(content)
            if c_size > settings.max_single_file_bytes:
                warnings.append(f"Skipped '{base}': larger than the per-file limit.")
                continue
            if is_path:
                try:
                    with open(content, "rb") as f:
                        raw_data = f.read(settings.max_single_file_bytes + 1)
                except OSError:
                    warnings.append(f"Could not read upload file '{base}'.")
                    continue
            else:
                raw_data = content
            account(len(raw_data))
            register_submission_file(norm, "", raw_data)
        elif lower.endswith(UNSUPPORTED_ARCHIVES):
            warnings.append(f"Skipped '{base}': only .zip archives and supported source/text files are accepted (re-zip it).")
        else:
            note_other(ext, norm)


    resolve_identities()
    place_records()

    if ignored_other:
        listing = ", ".join(f"{ext} x{n}" for ext, n in sorted(ignored_other.items()))
        warnings.append(f"Ignored non-submission or auxiliary files ({listing}).")

    return {
        "students": students,
        "warnings": warnings,
        "ignored_binaries": ignored_binaries,
        "total_c_files": counters["c_files"],
        "total_files": counters["c_files"],
        "languages_detected": {k: v for k, v in sorted(languages_detected.items()) if v > 0},
        "student_count": len(students),
    }
