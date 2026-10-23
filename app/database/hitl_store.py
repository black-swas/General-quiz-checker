"""TA calibration memory & persistent paper-exam session store (SQLite WAL mode).

Supports:
1. Persistent Exam Sessions (`exam_sessions`) created by the TA (exam_name, date, total_marks, custom storage_dir, question specs, live LLM telemetry).
2. Student Roll No & Name tracking (`session_students`) for manual TA entry or CSV roster import across 400+ students.
3. Scanned Page & Chunked Sync state (`scanned_pages`, `page_upload_chunks`) with retake tracking and page deletion.
4. Question-level cropped image evaluations (`question_evaluations`) with AI confidence (0.0-1.0),
   auto-approval status, TA mark overrides, and comments for Excel export.
5. Legacy `hitl_feedback` and `evaluation_jobs` tables for full backward compatibility.
"""
import json
import logging
import re
import shutil
import sqlite3
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from app.analyzer.c_static_analyzer import PROMPT_INJECTION_PATTERNS
from app.analyzer.paper_redundancy_engine import (
    format_supplementary_page_info,
    reorder_and_audit_scanned_pages,
)
from app.config import settings
from app.schemas import HITLFeedbackRequest

logger = logging.getLogger("iiserb.hitl")

_LOCK = threading.Lock()
_READY: Dict[str, Path] = {}
_DB_INITIALIZED: Set[Path] = set()

SEED_EXAMPLES = [
    {
        "question_id": "Q1", "topic_tags": ["bitwise", "loops", "number-theory"],
        "problem_summary": "Count set bits of N",
        "code_snippet": "while (n) { n &= (n - 1); count++; }",
        "original_category": "GREEN", "override_category": "BLUE",
        "ta_explanation": "Kernighan's n &= (n-1) clears the lowest set bit: a creative O(set bits) approach worth a quick look.",
    },
    {
        "question_id": "Q3", "topic_tags": ["loops", "series", "math"],
        "problem_summary": "Sum of multiples below N",
        "code_snippet": "long long s3 = 3 * p3 * (p3 + 1) / 2;",
        "original_category": "GREEN", "override_category": "BLUE",
        "ta_explanation": "Closed-form arithmetic-series sums replace the loop: valid and creative.",
    },
    {
        "question_id": "Q2", "topic_tags": ["arrays", "pointers", "reversal"],
        "problem_summary": "Print an array reversed",
        "code_snippet": 'printf("%d%s", a[i], (i == n - 1) ? "\\n" : " ");',
        "original_category": "BLUE", "override_category": "GREEN",
        "ta_explanation": "Using ?: only to print separators is ordinary style, not a creative approach.",
    },
    {
        "question_id": "Q1", "topic_tags": ["recursion", "number-theory", "loops"],
        "problem_summary": "Greatest common divisor",
        "code_snippet": "return b ? gcd(b, a % b) : a;",
        "original_category": "BLUE", "override_category": "GREEN",
        "ta_explanation": "Recursive Euclid is the standard answer when the topic is recursion.",
    },
]


def ist_now() -> str:
    return datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d %H:%M:%S IST")


def parse_timestamp_to_epoch(ts_val: Any) -> float:
    """Safely parse epoch float, ISO format string, or IST string to unix epoch timestamp."""
    if isinstance(ts_val, (int, float)):
        return float(ts_val)
    if not ts_val or not isinstance(ts_val, str):
        return 0.0
    s = ts_val.strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        pass
    try:
        clean = s.replace(" IST", "")
        dt = datetime.strptime(clean, "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=timezone(timedelta(hours=5, minutes=30))).timestamp()
    except Exception:
        pass
    try:
        return float(s)
    except Exception:
        return 0.0


def sanitize_ta_text(text: str, max_len: int = 400) -> str:
    """Plain-text only: no control characters, no angle brackets, instruction-like phrases removed."""
    if not text:
        return ""
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text).replace("<", " ").replace(">", " ")
    for pattern in PROMPT_INJECTION_PATTERNS:
        text = re.sub(pattern, "[removed]", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()[:max_len]


def _writable(path: Path) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=5.0)
        conn.execute("CREATE TABLE IF NOT EXISTS _probe (id INTEGER)")
        conn.execute("DROP TABLE IF EXISTS _probe")
        conn.commit()
        conn.close()
        return True
    except (OSError, sqlite3.Error):
        return False


def _db_path() -> Path:
    configured = settings.sqlite_db_path
    if configured in _READY:
        return _READY[configured]
    target = Path(configured).resolve()
    if not _writable(target):
        raise PermissionError(
            f"Configured SQLite database path '{target}' is not writable. Refusing to silently redirect to a temporary database."
        )
    _READY[configured] = target
    return target


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-64000")  # 64MB RAM cache
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA mmap_size=268435456")  # 256MB memory map
    except sqlite3.Error:
        pass
    return conn


def init_hitl_db() -> None:
    db_p = _db_path()
    if db_p in _DB_INITIALIZED:
        return
    with _LOCK:
        if db_p in _DB_INITIALIZED:
            return
        conn = _connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hitl_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    course_code TEXT NOT NULL,
                    question_id TEXT NOT NULL,
                    topic_tags TEXT NOT NULL,
                    problem_summary TEXT NOT NULL,
                    roll_no TEXT NOT NULL,
                    code_snippet TEXT NOT NULL,
                    original_category TEXT NOT NULL,
                    original_cheat_type TEXT NOT NULL,
                    ta_decision TEXT NOT NULL,
                    override_category TEXT NOT NULL,
                    ta_explanation TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS evaluation_jobs (
                    job_id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    quiz_title TEXT NOT NULL,
                    course_code TEXT NOT NULL,
                    student_count INTEGER NOT NULL,
                    total_files INTEGER NOT NULL,
                    summary_json TEXT NOT NULL,
                    result_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS exam_sessions (
                    session_id TEXT PRIMARY KEY,
                    course_code TEXT NOT NULL,
                    exam_name TEXT NOT NULL,
                    exam_date TEXT NOT NULL,
                    total_marks REAL NOT NULL,
                    expected_pages INTEGER NOT NULL DEFAULT 4,
                    storage_dir TEXT NOT NULL DEFAULT '',
                    questions_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    grading_progress REAL NOT NULL DEFAULT 0.0,
                    grading_stage TEXT NOT NULL DEFAULT 'Ready',
                    llm_telemetry_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            # Safely migrate existing exam_sessions table if columns were added in v5.1
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(exam_sessions)").fetchall()}
            if "storage_dir" not in cols:
                conn.execute("ALTER TABLE exam_sessions ADD COLUMN storage_dir TEXT NOT NULL DEFAULT ''")
            if "llm_telemetry_json" not in cols:
                conn.execute("ALTER TABLE exam_sessions ADD COLUMN llm_telemetry_json TEXT NOT NULL DEFAULT '{}'")
            if "paper_type" not in cols:
                conn.execute("ALTER TABLE exam_sessions ADD COLUMN paper_type TEXT NOT NULL DEFAULT 'COMBINED_QUESTION_ANSWER'")
            if "question_paper_image_path" not in cols:
                conn.execute("ALTER TABLE exam_sessions ADD COLUMN question_paper_image_path TEXT NOT NULL DEFAULT ''")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_students (
                    session_id TEXT NOT NULL,
                    roll_no TEXT NOT NULL,
                    student_name TEXT NOT NULL DEFAULT '',
                    pages_uploaded INTEGER NOT NULL DEFAULT 0,
                    finalized INTEGER NOT NULL DEFAULT 0,
                    grading_status TEXT NOT NULL DEFAULT 'PENDING',
                    claimed_by TEXT NOT NULL DEFAULT '',
                    claimed_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (session_id, roll_no)
                )
                """
            )
            # Safe migration for session_students
            st_cols = {r["name"] for r in conn.execute("PRAGMA table_info(session_students)").fetchall()}
            if "claimed_by" not in st_cols:
                conn.execute("ALTER TABLE session_students ADD COLUMN claimed_by TEXT NOT NULL DEFAULT ''")
            if "claimed_at" not in st_cols:
                conn.execute("ALTER TABLE session_students ADD COLUMN claimed_at TEXT NOT NULL DEFAULT ''")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scanned_pages (
                    page_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    roll_no TEXT NOT NULL,
                    page_no INTEGER NOT NULL,
                    raw_path TEXT NOT NULL,
                    processed_path TEXT NOT NULL DEFAULT '',
                    blur_score REAL NOT NULL DEFAULT 0.0,
                    is_sharp INTEGER NOT NULL DEFAULT 1,
                    retake_count INTEGER NOT NULL DEFAULT 0,
                    file_size_bytes INTEGER NOT NULL DEFAULT 0,
                    sha256 TEXT NOT NULL DEFAULT '',
                    uploaded_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS page_upload_chunks (
                    upload_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    roll_no TEXT NOT NULL,
                    page_no INTEGER NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    total_chunks INTEGER NOT NULL,
                    chunk_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (upload_id, chunk_index)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS question_evaluations (
                    eval_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    roll_no TEXT NOT NULL,
                    student_name TEXT NOT NULL DEFAULT '',
                    question_id TEXT NOT NULL,
                    crop_path TEXT NOT NULL DEFAULT '',
                    ocr_transcription TEXT NOT NULL DEFAULT '',
                    legibility_score REAL NOT NULL DEFAULT 1.0,
                    ai_awarded_marks REAL NOT NULL DEFAULT 0.0,
                    max_marks REAL NOT NULL DEFAULT 5.0,
                    step_scores_json TEXT NOT NULL DEFAULT '[]',
                    confidence REAL NOT NULL DEFAULT 0.0,
                    verdict_tab TEXT NOT NULL DEFAULT 'GRAY',
                    anomaly_flag TEXT NOT NULL DEFAULT 'NONE',
                    ai_comment TEXT NOT NULL DEFAULT '',
                    auto_approved INTEGER NOT NULL DEFAULT 0,
                    ta_status TEXT NOT NULL DEFAULT 'PENDING',
                    ta_final_marks REAL,
                    ta_comment TEXT NOT NULL DEFAULT '',
                    question_text TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS regrade_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    roll_no TEXT NOT NULL,
                    question_id TEXT NOT NULL,
                    student_reason TEXT NOT NULL,
                    claimed_marks REAL DEFAULT 0.0,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    ta_response TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS global_llm_usage (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    model TEXT NOT NULL DEFAULT 'claude-haiku-5-5',
                    calls INTEGER NOT NULL DEFAULT 0,
                    ok_calls INTEGER NOT NULL DEFAULT 0,
                    failed_calls INTEGER NOT NULL DEFAULT 0,
                    submissions_reviewed INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_elapsed_seconds REAL NOT NULL DEFAULT 0.0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO global_llm_usage (id, model, calls, ok_calls, failed_calls, submissions_reviewed, input_tokens, cache_creation_input_tokens, cache_read_input_tokens, output_tokens, total_elapsed_seconds, updated_at)
                VALUES (1, 'claude-haiku-5-5', 0, 0, 0, 0, 0, 0, 0, 0, 0.0, ?)
                """,
                (datetime.now(timezone.utc).isoformat(),),
            )

            # Secondary composite indexes for high-throughput queries
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pages_session_roll ON scanned_pages(session_id, roll_no)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_evals_session_roll ON question_evaluations(session_id, roll_no)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_session_students_sid ON session_students(session_id, roll_no)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_session_students_claim ON session_students(session_id, claimed_by, finalized)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_evals_lookup ON question_evaluations(session_id, roll_no, question_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_evals_verdict ON question_evaluations(session_id, verdict_tab, auto_approved)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_hitl_feedback_decision ON hitl_feedback(ta_decision, id DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_regrade_session ON regrade_requests(session_id, roll_no)")

            # Safe migration for question_text in question_evaluations
            try:
                cols = [r[1] for r in conn.execute("PRAGMA table_info(question_evaluations)").fetchall()]
                if "question_text" not in cols:
                    conn.execute("ALTER TABLE question_evaluations ADD COLUMN question_text TEXT NOT NULL DEFAULT ''")
            except Exception:
                pass

            if conn.execute("SELECT COUNT(*) FROM hitl_feedback").fetchone()[0] == 0:
                now = datetime.now(timezone.utc).isoformat()
                for ex in SEED_EXAMPLES:
                    conn.execute(
                        "INSERT INTO hitl_feedback (created_at, course_code, question_id, topic_tags, problem_summary, roll_no,"
                        " code_snippet, original_category, original_cheat_type, ta_decision, override_category, ta_explanation)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (now, "DSE-101", ex["question_id"], json.dumps(ex["topic_tags"]), ex["problem_summary"], "seed",
                         ex["code_snippet"], ex["original_category"], "NONE", "DISAGREE", ex["override_category"], ex["ta_explanation"]),
                    )
            conn.commit()
            _DB_INITIALIZED.add(db_p)
        finally:
            conn.close()


# --------------------------------------------------------------------------- Paper Exam Session CRUD
def create_exam_session(
    course_code: str,
    exam_name: str,
    exam_date: str,
    total_marks: float,
    expected_pages: int = 1,
    storage_dir: Optional[str] = None,
    questions: Optional[List[Dict[str, Any]]] = None,
    paper_type: str = "COMBINED_QUESTION_ANSWER",
    question_paper_image_path: str = "",
) -> Dict[str, Any]:
    init_hitl_db()
    session_id = "SES-" + uuid.uuid4().hex[:8].upper()
    now = ist_now()
    resolved_dir = str(Path(storage_dir or settings.paper_storage_dir).resolve())
    try:
        Path(resolved_dir).mkdir(parents=True, exist_ok=True)
    except OSError:
        resolved_dir = str(Path(settings.paper_storage_dir).resolve())

    if questions is None:
        per_q = round(total_marks / 4.0, 2)
        questions = [
            {
                "question_id": f"Q{i+1}",
                "title": f"Question {i+1}",
                "problem_statement": f"Solve problem for Question {i+1} with complete derivations and steps.",
                "max_marks": per_q if i < 3 else round(total_marks - 3 * per_q, 2),
                "page_number": (i // 2) + 1,
                "y_start_pct": 0.0 if (i % 2 == 0) else 50.0,
                "y_end_pct": 50.0 if (i % 2 == 0) else 100.0,
                "x_start_pct": 0.0,
                "x_end_pct": 100.0,
                "language": "auto",
                "execution_mode": "llm_only",
                "grading_rubric": f"Step 1 [2.0 Marks]: Formulation and initial setup.\nStep 2 [2.0 Marks]: Core derivation or algorithm execution.\nStep 3 [1.0 Mark]: Final solution with boundary checking and correct units.",
                "reference_solution": f"Reference solution for Question {i+1}: apply standard principles, derive intermediate expressions, and evaluate final result.",
            }
            for i in range(4)
        ]
    with _LOCK:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO exam_sessions
                (session_id, course_code, exam_name, exam_date, total_marks, expected_pages, storage_dir, questions_json, status, llm_telemetry_json, paper_type, question_paper_image_path, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (session_id, course_code, exam_name, exam_date, float(total_marks), int(expected_pages),
                 resolved_dir, json.dumps(questions), "ACTIVE", "{}", paper_type, question_paper_image_path, now, now),
            )
            conn.commit()
        finally:
            conn.close()
    return get_exam_session(session_id) or {}


def update_exam_session(session_id: str, **updates: Any) -> Optional[Dict[str, Any]]:
    init_hitl_db()
    if not updates:
        return get_exam_session(session_id)

    now = ist_now()
    column_map = {
        "course_code": lambda v: str(v),
        "exam_name": lambda v: str(v),
        "exam_date": lambda v: str(v),
        "total_marks": lambda v: float(v),
        "expected_pages": lambda v: int(v),
        "storage_dir": lambda v: str(v),
        "questions": lambda v: ("questions_json", json.dumps(v)),
        "paper_type": lambda v: str(v),
        "question_paper_image_path": lambda v: str(v),
        "status": lambda v: str(v),
        "grading_progress": lambda v: float(v),
        "grading_stage": lambda v: str(v),
        "llm_telemetry": lambda v: ("llm_telemetry_json", json.dumps(v)),
    }

    with _LOCK:
        conn = _connect()
        try:
            row = conn.execute("SELECT session_id FROM exam_sessions WHERE session_id=?", (session_id,)).fetchone()
            if not row:
                return None

            set_clauses: List[str] = []
            params: List[Any] = []

            for k, val in updates.items():
                if val is None and k not in ("question_paper_image_path",):
                    continue
                if k in column_map:
                    mapped = column_map[k](val)
                    if isinstance(mapped, tuple):
                        col_name, db_val = mapped
                    else:
                        col_name, db_val = k, mapped
                    set_clauses.append(f"{col_name}=?")
                    params.append(db_val)
                elif k in ("questions_json", "llm_telemetry_json"):
                    set_clauses.append(f"{k}=?")
                    params.append(json.dumps(val) if not isinstance(val, str) else val)

            if not set_clauses:
                return get_exam_session(session_id)

            set_clauses.append("updated_at=?")
            params.append(now)
            params.append(session_id)

            sql = f"UPDATE exam_sessions SET {', '.join(set_clauses)} WHERE session_id=?"
            conn.execute(sql, tuple(params))
            conn.commit()
        finally:
            conn.close()

    return get_exam_session(session_id)


def delete_exam_session(session_id: str) -> bool:
    """Permanently delete an exam session, including student records, pages, evaluations, and disk files."""
    init_hitl_db()
    clean_sid = session_id.strip()
    existing = get_exam_session(clean_sid)
    if not existing:
        return False
    with _LOCK:
        conn = _connect()
        try:
            conn.execute("DELETE FROM question_evaluations WHERE session_id=?", (clean_sid,))
            conn.execute("DELETE FROM scanned_pages WHERE session_id=?", (clean_sid,))
            conn.execute("DELETE FROM session_students WHERE session_id=?", (clean_sid,))
            conn.execute("DELETE FROM regrade_requests WHERE session_id=?", (clean_sid,))
            conn.execute("DELETE FROM page_upload_chunks WHERE session_id=?", (clean_sid,))
            cur = conn.execute("DELETE FROM exam_sessions WHERE session_id=?", (clean_sid,))
            conn.commit()
            deleted = cur.rowcount > 0
        finally:
            conn.close()
    try:
        storage_dir = Path(existing.get("storage_dir") or settings.paper_storage_dir)
        safe_sid = "".join(c for c in clean_sid if c.isalnum() or c in ("-", "_")).strip()
        session_folder = storage_dir / safe_sid
        if session_folder.is_dir():
            shutil.rmtree(session_folder, ignore_errors=True)
    except Exception as exc:
        logger.warning("Could not delete session folder for %s: %s", clean_sid, exc)
    return deleted


def update_session_progress(
    session_id: str,
    status: str,
    grading_progress: float,
    grading_stage: str,
    llm_telemetry: Dict[str, Any],
) -> None:
    """Lightweight single-statement progress update with zero SELECT joins."""
    init_hitl_db()
    now = ist_now()
    with _LOCK:
        conn = _connect()
        try:
            conn.execute(
                """
                UPDATE exam_sessions
                SET status=?, grading_progress=?, grading_stage=?, llm_telemetry_json=?, updated_at=?
                WHERE session_id=?
                """,
                (status, float(grading_progress), grading_stage, json.dumps(llm_telemetry), now, session_id),
            )
            conn.commit()
        finally:
            conn.close()


def list_exam_sessions(limit: int = 50) -> List[Dict[str, Any]]:
    init_hitl_db()
    with _LOCK:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM exam_sessions ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
            if not rows:
                return []
            sids = [r["session_id"] for r in rows]
            placeholders = ",".join("?" for _ in sids)
            st_counts = {r[0]: r[1] for r in conn.execute(
                f"SELECT session_id, COUNT(*) FROM session_students WHERE session_id IN ({placeholders}) GROUP BY session_id", sids
            ).fetchall()}
            pg_counts = {r[0]: r[1] for r in conn.execute(
                f"SELECT session_id, COUNT(*) FROM scanned_pages WHERE session_id IN ({placeholders}) GROUP BY session_id", sids
            ).fetchall()}
            ev_rows = conn.execute(
                f"SELECT session_id, verdict_tab, COUNT(*) as c FROM question_evaluations WHERE session_id IN ({placeholders}) GROUP BY session_id, verdict_tab", sids
            ).fetchall()
            tab_counts_by_sid: Dict[str, Dict[str, int]] = {}
            for er in ev_rows:
                tab_counts_by_sid.setdefault(er["session_id"], {"GREEN": 0, "BLUE": 0, "RED": 0, "GRAY": 0})[er["verdict_tab"]] = er["c"]

            out = []
            for r in rows:
                sid = r["session_id"]
                st_count = st_counts.get(sid, 0)
                pg_count = pg_counts.get(sid, 0)
                tab_counts = tab_counts_by_sid.get(sid, {"GREEN": 0, "BLUE": 0, "RED": 0, "GRAY": 0})
                try:
                    qs = json.loads(r["questions_json"] or "[]")
                except ValueError:
                    qs = []
                try:
                    telem = json.loads(r["llm_telemetry_json"] or "{}")
                except ValueError:
                    telem = {}
                out.append({
                    "session_id": sid,
                    "course_code": r["course_code"],
                    "exam_name": r["exam_name"],
                    "exam_date": r["exam_date"],
                    "total_marks": r["total_marks"],
                    "expected_pages": r["expected_pages"],
                    "storage_dir": r["storage_dir"] or str(Path(settings.paper_storage_dir).resolve()),
                    "paper_type": r["paper_type"] if "paper_type" in r.keys() else "COMBINED_QUESTION_ANSWER",
                    "question_paper_image_path": r["question_paper_image_path"] if "question_paper_image_path" in r.keys() else "",
                    "questions_count": len(qs),
                    "questions": qs,
                    "status": r["status"],
                    "grading_progress": r["grading_progress"],
                    "grading_stage": r["grading_stage"],
                    "llm_telemetry": telem,
                    "student_count": st_count,
                    "pages_scanned": pg_count,
                    "summary_counts": tab_counts,
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                })
            return out
        finally:
            conn.close()


def get_exam_session(session_id: str) -> Optional[Dict[str, Any]]:
    init_hitl_db()
    with _LOCK:
        conn = _connect()
        try:
            r = conn.execute("SELECT * FROM exam_sessions WHERE session_id=?", (session_id,)).fetchone()
            if not r:
                return None
            students = [dict(s) for s in conn.execute(
                "SELECT * FROM session_students WHERE session_id=? ORDER BY roll_no ASC", (session_id,)
            ).fetchall()]
            pages = [dict(p) for p in conn.execute(
                "SELECT page_id, session_id, roll_no, page_no, raw_path, processed_path, blur_score, is_sharp, retake_count, file_size_bytes, sha256, uploaded_at "
                "FROM scanned_pages WHERE session_id=? ORDER BY roll_no ASC, page_no ASC",
                (session_id,),
            ).fetchall()]
            evals = [dict(e) for e in conn.execute(
                "SELECT * FROM question_evaluations WHERE session_id=? ORDER BY roll_no ASC, question_id ASC",
                (session_id,),
            ).fetchall()]
        finally:
            conn.close()

    try:
        questions = json.loads(r["questions_json"] or "[]")
    except ValueError:
        questions = []
    try:
        llm_telemetry = json.loads(r["llm_telemetry_json"] or "{}")
    except ValueError:
        llm_telemetry = {}

    pages_by_student: Dict[str, List[Dict[str, Any]]] = {}
    for p in pages:
        p["image_url"] = f"/api/sessions/{session_id}/students/{p['roll_no']}/pages/{p['page_no']}/image"
        p["supplementary"] = format_supplementary_page_info(p["page_no"], int(r["expected_pages"] or 4))
        pages_by_student.setdefault(p["roll_no"], []).append(p)
        clean_p_roll = str(p["roll_no"]).strip().casefold()
        if clean_p_roll != p["roll_no"]:
            pages_by_student.setdefault(clean_p_roll, []).append(p)

    evals_by_student: Dict[str, Dict[str, Any]] = {}
    summary_counts = {"GREEN": 0, "BLUE": 0, "RED": 0, "GRAY": 0, "AUTO_APPROVED": 0, "TA_REVIEWED": 0}
    for e in evals:
        try:
            e["step_scores"] = json.loads(e.get("step_scores_json") or "[]")
        except ValueError:
            e["step_scores"] = []
        e["crop_url"] = f"/api/sessions/{session_id}/students/{e['roll_no']}/questions/{e['question_id']}/crop"
        e["final_marks"] = e["ta_final_marks"] if e["ta_final_marks"] is not None else e["ai_awarded_marks"]
        e["question_number"] = e.get("question_id", "")
        e["question"] = e.get("question_text", "")
        e["answer"] = e.get("ocr_transcription", "")
        e["grade"] = e.get("final_marks", 0.0)
        e["explanation"] = e.get("ta_comment") or e.get("ai_comment", "")
        tab = e.get("verdict_tab", "GRAY")
        if tab in summary_counts:
            summary_counts[tab] += 1
        if e.get("auto_approved"):
            summary_counts["AUTO_APPROVED"] += 1
        if e.get("ta_status") in ("TA_APPROVED", "TA_OVERRIDDEN", "OVERRIDDEN"):
            summary_counts["TA_REVIEWED"] += 1
        evals_by_student.setdefault(e["roll_no"], {})[e["question_id"]] = e
        clean_e_roll = str(e["roll_no"]).strip().casefold()
        if clean_e_roll != e["roll_no"]:
            evals_by_student.setdefault(clean_e_roll, {})[e["question_id"]] = e

    # Autonomously merge any questions discovered dynamically by AI into questions list
    known_q_ids = {str(q.get("question_id", "")).upper() for q in questions if isinstance(q, dict)}
    for e in evals:
        qid = str(e.get("question_id", "")).strip()
        if qid and qid.upper() not in known_q_ids:
            known_q_ids.add(qid.upper())
            questions.append({
                "question_id": qid,
                "question_number": qid,
                "title": f"Question {qid}",
                "max_marks": float(e.get("max_marks", 5.0) or 5.0),
                "problem_statement": e.get("question_text", ""),
                "grading_rubric": "",
                "reference_solution": "",
            })

    # Ensure any student present in scanned_pages or evals is included in student list
    known_student_rolls = {str(s.get("roll_no", "")).strip().casefold() for s in students}
    for p in pages:
        p_roll = str(p.get("roll_no", "")).strip()
        if p_roll and p_roll.casefold() not in known_student_rolls:
            known_student_rolls.add(p_roll.casefold())
            students.append({
                "session_id": session_id,
                "roll_no": p_roll,
                "student_name": "",
                "pages_uploaded": 0,
                "finalized": 0,
                "grading_status": "PENDING",
                "claimed_by": "",
                "claimed_at": "",
                "created_at": p.get("uploaded_at", ist_now()),
                "updated_at": p.get("uploaded_at", ist_now()),
            })

    for s in students:
        roll = s["roll_no"]
        raw_st_pages = pages_by_student.get(roll) or pages_by_student.get(roll.strip().casefold()) or []
        ordered_pages, missing_pages, audit_warnings = reorder_and_audit_scanned_pages(
            raw_st_pages, int(r["expected_pages"] or 4)
        )
        s["pages"] = ordered_pages
        s["missing_pages"] = missing_pages
        s["audit_warnings"] = audit_warnings
        s["pages_uploaded"] = len(ordered_pages)
        st_evals = evals_by_student.get(roll) or evals_by_student.get(roll.strip().casefold()) or {}
        s["evaluations"] = st_evals
        s["total_awarded_marks"] = round(sum(float(ev.get("final_marks", 0.0) or 0.0) for ev in st_evals.values()), 2)
        has_stored_marks = ("total_marks" in r.keys() and r["total_marks"] is not None and float(r["total_marks"]) > 0)
        total_possible = float(r["total_marks"]) if has_stored_marks else sum(float(q.get("max_marks", 5.0)) for q in questions)
        s["total_max_marks"] = round(total_possible, 2)
        s["percentage"] = round((s["total_awarded_marks"] / total_possible * 100), 1) if total_possible > 0 else 0.0
        s["has_red_flag"] = any(ev.get("verdict_tab") == "RED" and ev.get("ta_status") == "PENDING" for ev in st_evals.values())
        s["has_gray_flag"] = any(ev.get("verdict_tab") == "GRAY" and ev.get("ta_status") == "PENDING" for ev in st_evals.values())
        if not st_evals:
            s["overall_tab"] = "GRAY"
        elif s["has_red_flag"]:
            s["overall_tab"] = "RED"
        elif s["has_gray_flag"]:
            s["overall_tab"] = "GRAY"
        elif any(ev.get("verdict_tab") == "BLUE" for ev in st_evals.values()):
            s["overall_tab"] = "BLUE"
        else:
            s["overall_tab"] = "GREEN"

    return {
        "session_id": r["session_id"],
        "course_code": r["course_code"],
        "exam_name": r["exam_name"],
        "exam_date": r["exam_date"],
        "total_marks": r["total_marks"],
        "expected_pages": r["expected_pages"],
        "storage_dir": r["storage_dir"] or str(Path(settings.paper_storage_dir).resolve()),
        "paper_type": r["paper_type"] if "paper_type" in r.keys() else "COMBINED_QUESTION_ANSWER",
        "question_paper_image_path": r["question_paper_image_path"] if "question_paper_image_path" in r.keys() else "",
        "questions": questions,
        "status": r["status"],
        "grading_progress": r["grading_progress"],
        "grading_stage": r["grading_stage"],
        "llm_telemetry": llm_telemetry,
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        "student_count": len(students),
        "pages_scanned": len(pages),
        "summary_counts": summary_counts,
        "students": students,
    }


def upsert_session_student(session_id: str, roll_no: str, student_name: str = "", finalized: Optional[bool] = None, grading_status: Optional[str] = None) -> Dict[str, Any]:
    init_hitl_db()
    clean_roll = roll_no.strip()
    clean_name = sanitize_ta_text(student_name, 160)
    now = ist_now()
    with _LOCK:
        conn = _connect()
        try:
            existing = conn.execute(
                "SELECT * FROM session_students WHERE session_id=? AND (roll_no=? OR roll_no=? COLLATE NOCASE)",
                (session_id, clean_roll, clean_roll),
            ).fetchone()
            if existing:
                matched_roll = existing["roll_no"]
                new_name = clean_name if clean_name else existing["student_name"]
                new_fin = int(finalized) if finalized is not None else existing["finalized"]
                new_stat = grading_status if grading_status is not None else existing["grading_status"]
                conn.execute(
                    "UPDATE session_students SET student_name=?, finalized=?, grading_status=?, updated_at=? WHERE session_id=? AND roll_no=?",
                    (new_name, new_fin, new_stat, now, session_id, matched_roll),
                )
            else:
                conn.execute(
                    "INSERT INTO session_students (session_id, roll_no, student_name, pages_uploaded, finalized, grading_status, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (session_id, clean_roll, clean_name, 0, int(bool(finalized)), grading_status or "PENDING", now, now),
                )
            conn.execute("UPDATE exam_sessions SET updated_at=? WHERE session_id=?", (now, session_id))
            conn.commit()
            row = conn.execute("SELECT * FROM session_students WHERE session_id=? AND (roll_no=? OR roll_no=? COLLATE NOCASE)", (session_id, clean_roll, clean_roll)).fetchone()
            return dict(row) if row else {}
        finally:
            conn.close()


def delete_session_student(session_id: str, roll_no: str) -> bool:
    init_hitl_db()
    clean_roll = roll_no.strip()
    now = ist_now()
    with _LOCK:
        conn = _connect()
        try:
            conn.execute("DELETE FROM question_evaluations WHERE session_id=? AND roll_no=? COLLATE NOCASE", (session_id, clean_roll))
            conn.execute("DELETE FROM scanned_pages WHERE session_id=? AND roll_no=? COLLATE NOCASE", (session_id, clean_roll))
            conn.execute("DELETE FROM page_upload_chunks WHERE session_id=? AND roll_no=? COLLATE NOCASE", (session_id, clean_roll))
            conn.execute("DELETE FROM regrade_requests WHERE session_id=? AND roll_no=? COLLATE NOCASE", (session_id, clean_roll))
            cur = conn.execute("DELETE FROM session_students WHERE session_id=? AND roll_no=? COLLATE NOCASE", (session_id, clean_roll))
            conn.execute("UPDATE exam_sessions SET updated_at=? WHERE session_id=?", (now, session_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def delete_scanned_page_record(session_id: str, roll_no: str, page_no: int) -> bool:
    init_hitl_db()
    clean_roll = roll_no.strip()
    page_id = f"{session_id}_{clean_roll}_p{int(page_no)}"
    now = ist_now()
    with _LOCK:
        conn = _connect()
        try:
            cur = conn.execute("DELETE FROM scanned_pages WHERE page_id=?", (page_id,))
            if cur.rowcount == 0:
                cur = conn.execute(
                    "DELETE FROM scanned_pages WHERE session_id=? AND roll_no=? COLLATE NOCASE AND page_no=?",
                    (session_id, clean_roll, int(page_no)),
                )
            pg_count = conn.execute(
                "SELECT COUNT(*) FROM scanned_pages WHERE session_id=? AND roll_no=? COLLATE NOCASE", (session_id, clean_roll)
            ).fetchone()[0]
            conn.execute(
                "UPDATE session_students SET pages_uploaded=?, updated_at=? WHERE session_id=? AND (roll_no=? OR roll_no=? COLLATE NOCASE)",
                (pg_count, now, session_id, clean_roll, clean_roll),
            )
            conn.execute("UPDATE exam_sessions SET updated_at=? WHERE session_id=?", (now, session_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def save_scanned_page_record(
    session_id: str,
    roll_no: str,
    page_no: int,
    raw_path: str,
    processed_path: str,
    blur_score: float,
    is_sharp: bool,
    file_size_bytes: int,
    sha256: str,
    is_retake: bool = False,
) -> Dict[str, Any]:
    init_hitl_db()
    clean_roll = roll_no.strip()
    page_id = f"{session_id}_{clean_roll}_p{int(page_no)}"
    now = ist_now()
    with _LOCK:
        conn = _connect()
        try:
            existing = conn.execute("SELECT retake_count FROM scanned_pages WHERE page_id=?", (page_id,)).fetchone()
            retake_count = (existing["retake_count"] + 1) if (existing and is_retake) else (existing["retake_count"] if existing else 0)
            conn.execute(
                """
                INSERT OR REPLACE INTO scanned_pages
                (page_id, session_id, roll_no, page_no, raw_path, processed_path, blur_score, is_sharp, retake_count, file_size_bytes, sha256, uploaded_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (page_id, session_id, clean_roll, int(page_no), raw_path, processed_path,
                 round(float(blur_score), 2), int(bool(is_sharp)), retake_count, int(file_size_bytes), sha256, now),
            )
            pg_count = conn.execute(
                "SELECT COUNT(*) FROM scanned_pages WHERE session_id=? AND roll_no=? COLLATE NOCASE", (session_id, clean_roll)
            ).fetchone()[0]
            up_cur = conn.execute(
                "UPDATE session_students SET pages_uploaded=?, updated_at=? WHERE session_id=? AND (roll_no=? OR roll_no=? COLLATE NOCASE)",
                (pg_count, now, session_id, clean_roll, clean_roll),
            )
            if up_cur.rowcount == 0:
                conn.execute(
                    "INSERT OR IGNORE INTO session_students (session_id, roll_no, student_name, pages_uploaded, finalized, grading_status, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (session_id, clean_roll, "", pg_count, 0, "PENDING", now, now),
                )
            conn.execute("UPDATE exam_sessions SET updated_at=? WHERE session_id=?", (now, session_id))
            conn.commit()
            row = conn.execute("SELECT * FROM scanned_pages WHERE page_id=?", (page_id,)).fetchone()
            return dict(row) if row else {}
        finally:
            conn.close()


def get_scanned_page_record(session_id: str, roll_no: str, page_no: int) -> Optional[Dict[str, Any]]:
    init_hitl_db()
    page_id = f"{session_id}_{roll_no.strip()}_p{int(page_no)}"
    with _LOCK:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM scanned_pages WHERE page_id=?", (page_id,)).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT * FROM scanned_pages WHERE session_id=? AND roll_no=? COLLATE NOCASE AND page_no=?",
                    (session_id, roll_no.strip(), int(page_no)),
                ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def save_question_evaluation(
    session_id: str,
    roll_no: str,
    student_name: str,
    question_id: str,
    crop_path: str,
    ocr_transcription: str,
    legibility_score: float,
    ai_awarded_marks: float,
    max_marks: float,
    step_scores: List[Dict[str, Any]],
    confidence: float,
    verdict_tab: str,
    anomaly_flag: str,
    ai_comment: str,
    auto_approved: bool,
    question_text: str = "",
) -> Dict[str, Any]:
    init_hitl_db()
    clean_roll = roll_no.strip()
    eval_id = f"{session_id}_{clean_roll}_{question_id}"
    now = ist_now()
    ta_status = "AUTO_APPROVED" if auto_approved else "PENDING"
    ta_final_marks = float(ai_awarded_marks) if auto_approved else None
    with _LOCK:
        conn = _connect()
        try:
            existing = conn.execute(
                "SELECT eval_id, roll_no, ta_status, ta_final_marks, ta_comment, verdict_tab FROM question_evaluations WHERE session_id=? AND (roll_no=? OR roll_no=? COLLATE NOCASE) AND (question_id=? OR question_id=? COLLATE NOCASE)",
                (session_id, clean_roll, clean_roll, question_id, question_id)
            ).fetchone()
            if existing:
                eval_id = existing["eval_id"]
                clean_roll = existing["roll_no"]
                if existing["ta_status"] in ("TA_APPROVED", "TA_OVERRIDDEN", "OVERRIDDEN"):
                    ta_status = "TA_OVERRIDDEN" if existing["ta_status"] == "OVERRIDDEN" else existing["ta_status"]
                    ta_final_marks = existing["ta_final_marks"]
                    ta_comment = existing["ta_comment"]
                    if existing["verdict_tab"]:
                        verdict_tab = existing["verdict_tab"]
                else:
                    ta_comment = ""
            else:
                ta_comment = ""

            conn.execute(
                """
                INSERT OR REPLACE INTO question_evaluations
                (eval_id, session_id, roll_no, student_name, question_id, crop_path, ocr_transcription,
                 legibility_score, ai_awarded_marks, max_marks, step_scores_json, confidence,
                 verdict_tab, anomaly_flag, ai_comment, auto_approved, ta_status, ta_final_marks, ta_comment, question_text, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    eval_id, session_id, clean_roll, student_name, question_id, crop_path, ocr_transcription,
                    round(float(legibility_score), 3), round(float(ai_awarded_marks), 2), round(float(max_marks), 2),
                    json.dumps(step_scores), round(float(confidence), 3), verdict_tab, anomaly_flag,
                    ai_comment, int(bool(auto_approved)), ta_status, ta_final_marks, ta_comment, question_text, now,
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM question_evaluations WHERE eval_id=?", (eval_id,)).fetchone()
            return dict(row) if row else {}
        finally:
            conn.close()


def apply_ta_grade_override(
    session_id: str,
    roll_no: str,
    question_id: str,
    ta_status: str,
    awarded_marks: float,
    verdict_tab: str,
    ta_comment: str,
) -> Optional[Dict[str, Any]]:
    init_hitl_db()
    eval_id = f"{session_id}_{roll_no.strip()}_{question_id}"
    now = ist_now()
    clean_comment = sanitize_ta_text(ta_comment, 600)
    with _LOCK:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM question_evaluations WHERE eval_id=?", (eval_id,)).fetchone()
            if not row:
                row = conn.execute(
                    """
                    SELECT * FROM question_evaluations
                    WHERE session_id=? AND roll_no=? COLLATE NOCASE AND (question_id=? OR question_id=? COLLATE NOCASE)
                    LIMIT 1
                    """,
                    (session_id, roll_no.strip(), question_id, question_id),
                ).fetchone()
                if row:
                    eval_id = row["eval_id"]
            if not row:
                st_row = conn.execute(
                    "SELECT student_name FROM session_students WHERE session_id=? AND (roll_no=? OR roll_no=? COLLATE NOCASE)",
                    (session_id, roll_no.strip(), roll_no.strip()),
                ).fetchone()
                student_name = st_row["student_name"] if st_row else ""
                max_m = 5.0
                sess_row = conn.execute("SELECT questions_json FROM exam_sessions WHERE session_id=?", (session_id,)).fetchone()
                if sess_row and sess_row["questions_json"]:
                    try:
                        q_specs = json.loads(sess_row["questions_json"])
                        for q in q_specs:
                            if str(q.get("question_id", "")).strip().upper() == str(question_id).strip().upper():
                                max_m = float(q.get("max_marks", 5.0))
                                break
                    except Exception:
                        pass
                clamped_marks = round(max(0.0, min(max_m, float(awarded_marks))), 2)
                conn.execute(
                    """
                    INSERT INTO question_evaluations
                    (eval_id, session_id, roll_no, student_name, question_id, crop_path, ocr_transcription,
                     legibility_score, ai_awarded_marks, max_marks, step_scores_json, confidence,
                     verdict_tab, anomaly_flag, ai_comment, auto_approved, ta_status, ta_final_marks, ta_comment, question_text, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        eval_id, session_id, roll_no.strip(), student_name, question_id, "", "",
                        1.0, 0.0, max_m, "[]", 1.0,
                        verdict_tab, "NONE", "", 0, ta_status, clamped_marks, clean_comment, "", now,
                    ),
                )
                conn.commit()
                created = conn.execute("SELECT * FROM question_evaluations WHERE eval_id=?", (eval_id,)).fetchone()
                return dict(created) if created else None
            max_m = float(row["max_marks"] or 5.0)
            clamped_marks = round(max(0.0, min(max_m, float(awarded_marks))), 2)
            effective_status = ta_status
            conn.execute(
                """
                UPDATE question_evaluations
                SET ta_status=?, ta_final_marks=?, verdict_tab=?, ta_comment=?, updated_at=?
                WHERE eval_id=?
                """,
                (effective_status, clamped_marks, verdict_tab, clean_comment, now, eval_id),
            )
            conn.commit()
            updated = conn.execute("SELECT * FROM question_evaluations WHERE eval_id=?", (eval_id,)).fetchone()
            return dict(updated) if updated else None
        finally:
            conn.close()


def bulk_approve_session_green(session_id: str) -> int:
    init_hitl_db()
    now = ist_now()
    with _LOCK:
        conn = _connect()
        try:
            cur = conn.execute(
                """
                UPDATE question_evaluations
                SET ta_status='TA_APPROVED', ta_final_marks=ai_awarded_marks, updated_at=?
                WHERE session_id=? AND verdict_tab = 'GREEN' AND ta_status IN ('PENDING', 'AUTO_APPROVED')
                """,
                (now, session_id),
            )
            conn.commit()
            return int(cur.rowcount)
        finally:
            conn.close()


def get_student_question_evaluations(session_id: str, roll_no: str) -> List[Dict[str, Any]]:
    init_hitl_db()
    clean_roll = roll_no.strip()
    with _LOCK:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM question_evaluations WHERE session_id=? AND (roll_no=? OR roll_no=? COLLATE NOCASE)",
                (session_id, clean_roll, clean_roll),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


# --------------------------------------------------------------------------- Legacy HITL & Jobs Store
def save_hitl_feedback(req: HITLFeedbackRequest) -> int:
    init_hitl_db()
    row: Tuple[Any, ...] = (
        datetime.now(timezone.utc).isoformat(),
        req.course_code,
        req.question_id,
        json.dumps([t[:40] for t in req.topic_tags[:10]]),
        sanitize_ta_text(req.problem_summary, 300),
        req.roll_no[:80],
        sanitize_ta_text(req.raw_code, 800),
        req.original_category,
        req.original_cheat_type[:60],
        req.ta_decision,
        req.override_category,
        sanitize_ta_text(req.ta_explanation, 400),
    )
    with _LOCK:
        conn = _connect()
        try:
            cur = conn.execute(
                "INSERT INTO hitl_feedback (created_at, course_code, question_id, topic_tags, problem_summary, roll_no,"
                " code_snippet, original_category, original_cheat_type, ta_decision, override_category, ta_explanation)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()


def get_calibration_examples(topic_tags: Optional[List[str]] = None, limit: int = 5) -> List[Dict[str, Any]]:
    init_hitl_db()
    with _LOCK:
        conn = _connect()
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM hitl_feedback WHERE ta_decision = 'DISAGREE' ORDER BY id DESC LIMIT 200").fetchall()]
        finally:
            conn.close()
    if not topic_tags:
        return rows[:limit]
    wanted = {t.lower() for t in topic_tags}
    scored = []
    for r in rows:
        try:
            have = {t.lower() for t in json.loads(r.get("topic_tags") or "[]")}
        except (ValueError, TypeError):
            have = set()
        scored.append((len(wanted & have), r["id"], r))
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    return [s[2] for s in scored[:limit]]


def get_feedback_stats() -> Dict[str, int]:
    init_hitl_db()
    with _LOCK:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT ta_decision, COUNT(*) AS n FROM hitl_feedback WHERE roll_no != 'seed' GROUP BY ta_decision").fetchall()
        finally:
            conn.close()
    counts = {r["ta_decision"]: r["n"] for r in rows}
    agree, disagree = counts.get("AGREE", 0), counts.get("DISAGREE", 0)
    return {"agree": agree, "disagree": disagree, "total": agree + disagree}


def save_persisted_job(job_id: str, created_at: float, result: Dict[str, Any]) -> None:
    init_hitl_db()
    try:
        payload = json.dumps(result)
        summary = json.dumps(result.get("summary_counts", {}))
    except (TypeError, ValueError):
        return
    with _LOCK:
        conn = _connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO evaluation_jobs "
                "(job_id, created_at, quiz_title, course_code, student_count, total_files, summary_json, result_json) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    created_at,
                    str(result.get("quiz_title", "Quiz"))[:200],
                    str(result.get("course_code", "DSE-101"))[:40],
                    int(result.get("student_count", 0)),
                    int(result.get("total_files", result.get("total_c_files", 0))),
                    summary,
                    payload,
                ),
            )
            keep = max(3, settings.max_stored_jobs)
            conn.execute(
                "DELETE FROM evaluation_jobs WHERE job_id NOT IN "
                "(SELECT job_id FROM evaluation_jobs ORDER BY created_at DESC LIMIT ?)",
                (keep,),
            )
            conn.commit()
        except sqlite3.Error as exc:
            logger.warning("Could not persist job %s to SQLite: %s", job_id, exc)
        finally:
            conn.close()


def load_persisted_job(job_id: str) -> Optional[Tuple[float, Dict[str, Any]]]:
    init_hitl_db()
    with _LOCK:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT created_at, result_json FROM evaluation_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        except sqlite3.Error:
            return None
        finally:
            conn.close()
    if not row:
        return None
    try:
        return float(row["created_at"]), json.loads(row["result_json"])
    except (ValueError, TypeError):
        return None


def list_persisted_jobs(limit: int = 8) -> List[Dict[str, Any]]:
    init_hitl_db()
    with _LOCK:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT job_id, created_at, quiz_title, course_code, student_count, total_files, summary_json "
                "FROM evaluation_jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()
    out: List[Dict[str, Any]] = []
    for r in rows:
        try:
            summary = json.loads(r["summary_json"] or "{}")
        except ValueError:
            summary = {}
        out.append({
            "job_id": r["job_id"],
            "status": "done",
            "created_at": r["created_at"],
            "quiz_title": r["quiz_title"],
            "course_code": r["course_code"],
            "student_count": r["student_count"],
            "total_files": r["total_files"],
            "summary_counts": summary,
        })
    return out


def update_ta_in_persisted_jobs(req: HITLFeedbackRequest) -> None:
    init_hitl_db()
    with _LOCK:
        conn = _connect()
        try:
            rows = conn.execute("SELECT job_id, result_json FROM evaluation_jobs ORDER BY created_at DESC LIMIT 5").fetchall()
            for r in rows:
                try:
                    data = json.loads(r["result_json"])
                except ValueError:
                    continue
                if data.get("course_code") != req.course_code:
                    continue
                q_evals = (data.get("evaluations_by_question") or {}).get(req.question_id) or []
                changed = False
                for ev in q_evals:
                    if ev.get("roll_no") == req.roll_no:
                        ev["ta_feedback"] = req.ta_decision
                        ev["ta_override_category"] = req.override_category
                        ev["ta_note"] = sanitize_ta_text(req.ta_explanation, 400)
                        changed = True
                if changed:
                    conn.execute(
                        "UPDATE evaluation_jobs SET result_json = ? WHERE job_id = ?",
                        (json.dumps(data), r["job_id"]),
                    )
            conn.commit()
        except sqlite3.Error:
            pass
        finally:
            conn.close()


# --------------------------------------------------------------------------- Multi-TA Room & Claims
def claim_next_student(
    session_id: str,
    ta_id: str,
    filter_tab: Optional[str] = None,
    lease_seconds: int = 300,
) -> Optional[Dict[str, Any]]:
    """Atomically claims the next unreviewed student for a TA.
    
    If filter_tab is provided ('RED', 'GRAY', 'BLUE', 'GREEN'), selects a student
    having pending questions in that tab. Otherwise prioritizes RED, then GRAY.
    Prevents two TAs from colliding on the same student.
    """
    init_hitl_db()
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    expire_threshold = (now_dt.timestamp() - lease_seconds)

    with _LOCK:
        conn = _connect()
        try:
            # Find eligible students
            query = """
                SELECT DISTINCT s.roll_no, s.student_name, s.claimed_by, s.claimed_at,
                                e.verdict_tab, e.ta_status
                FROM session_students s
                JOIN question_evaluations e ON s.session_id = e.session_id AND (s.roll_no = e.roll_no OR s.roll_no = e.roll_no COLLATE NOCASE)
                WHERE s.session_id = ?
                  AND e.ta_status = 'PENDING'
            """
            params: List[Any] = [session_id]

            if filter_tab:
                query += " AND e.verdict_tab = ?"
                params.append(filter_tab.upper())

            rows = conn.execute(query, params).fetchall()
            if not rows:
                return None

            # Sort by priority: RED first, then GRAY, then others
            tab_priority = {"RED": 0, "GRAY": 1, "BLUE": 2, "GREEN": 3}
            eligible = []
            for r in rows:
                # Check if currently claimed and unexpired
                c_by = r["claimed_by"] or ""
                c_at = r["claimed_at"] or ""
                is_claimed_active = False
                if c_by and c_by != ta_id and c_at:
                    c_ts = parse_timestamp_to_epoch(c_at)
                    if c_ts > expire_threshold:
                        is_claimed_active = True

                if not is_claimed_active:
                    eligible.append(r)

            if not eligible:
                return None

            eligible.sort(key=lambda x: (tab_priority.get(x["verdict_tab"], 9), x["roll_no"]))
            target = eligible[0]
            target_roll = target["roll_no"]

            # Claim the student
            conn.execute(
                "UPDATE session_students SET claimed_by = ?, claimed_at = ? WHERE session_id = ? AND (roll_no = ? OR roll_no = ? COLLATE NOCASE)",
                (ta_id, now_iso, session_id, target_roll, target_roll),
            )
            conn.commit()

            return {
                "roll_no": target_roll,
                "student_name": target["student_name"],
                "claimed_by": ta_id,
                "claimed_at": now_iso,
            }
        finally:
            conn.close()


def release_student_claim(session_id: str, roll_no: str, ta_id: str) -> bool:
    """Releases a student claim held by ta_id (or unconditionally if ta_id == 'force')."""
    init_hitl_db()
    clean_roll = roll_no.strip()
    with _LOCK:
        conn = _connect()
        try:
            if ta_id == "force":
                cur = conn.execute(
                    "UPDATE session_students SET claimed_by = '', claimed_at = '' WHERE session_id = ? AND (roll_no = ? OR roll_no = ? COLLATE NOCASE)",
                    (session_id, clean_roll, clean_roll),
                )
            else:
                cur = conn.execute(
                    "UPDATE session_students SET claimed_by = '', claimed_at = '' WHERE session_id = ? AND (roll_no = ? OR roll_no = ? COLLATE NOCASE) AND claimed_by = ?",
                    (session_id, clean_roll, clean_roll, ta_id),
                )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def record_student_presence(session_id: str, roll_no: str, ta_id: str) -> bool:
    """Heartbeat extending the TA's lease on a student."""
    init_hitl_db()
    clean_roll = roll_no.strip()
    now_iso = datetime.now(timezone.utc).isoformat()
    with _LOCK:
        conn = _connect()
        try:
            cur = conn.execute(
                """
                UPDATE session_students
                SET claimed_by = ?, claimed_at = ?
                WHERE session_id = ?
                  AND (roll_no = ? OR roll_no = ? COLLATE NOCASE)
                  AND (claimed_by = ? OR claimed_by = '' OR claimed_by IS NULL)
                """,
                (ta_id, now_iso, session_id, clean_roll, clean_roll, ta_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


# --------------------------------------------------------------------------- Exam Analytics & Metrics
def get_exam_analytics(session_id: str) -> Dict[str, Any]:
    """Computes comprehensive rubric statistics, item difficulty, and discrimination indices."""
    session = get_exam_session(session_id)
    if not session:
        return {}

    students = session.get("students", [])
    questions = session.get("questions", [])

    total_scores: List[float] = [float(s.get("total_awarded_marks", 0.0) or 0.0) for s in students]
    n_students = len(total_scores)

    if n_students == 0:
        return {
            "session_id": session_id,
            "student_count": 0,
            "mean_score": 0.0,
            "median_score": 0.0,
            "std_dev": 0.0,
            "min_score": 0.0,
            "max_score": 0.0,
            "questions_metrics": [],
        }

    total_scores_sorted = sorted(total_scores)
    mean_val = round(sum(total_scores) / n_students, 2)
    if n_students % 2 == 1:
        median_val = round(total_scores_sorted[n_students // 2], 2)
    else:
        mid = n_students // 2
        median_val = round((total_scores_sorted[mid - 1] + total_scores_sorted[mid]) / 2.0, 2)
    variance = sum((x - mean_val) ** 2 for x in total_scores) / n_students
    std_dev_val = round(variance ** 0.5, 2)
    min_val = round(total_scores_sorted[0], 2)
    max_val = round(total_scores_sorted[-1], 2)

    # Top 27% and Bottom 27% for Discrimination Index
    split_count = max(1, int(round(n_students * 0.27)))
    top_students = set(s["roll_no"] for s in sorted(students, key=lambda s: float(s.get("total_awarded_marks", 0.0) or 0.0), reverse=True)[:split_count])
    bottom_students = set(s["roll_no"] for s in sorted(students, key=lambda s: float(s.get("total_awarded_marks", 0.0) or 0.0))[:split_count])

    # Per-question analytics
    q_metrics: List[Dict[str, Any]] = []
    for q in questions:
        qid = q.get("question_id")
        max_m = float(q.get("max_marks", 5.0) or 5.0)

        q_scores: List[float] = []
        top_scores: List[float] = []
        bottom_scores: List[float] = []

        for s in students:
            evals_dict = s.get("evaluations", {})
            ev = evals_dict.get(qid) or evals_dict.get(str(qid).upper()) or evals_dict.get(str(qid).lower()) or {}
            score = float(ev.get("final_marks", ev.get("ai_awarded_marks", 0.0)) or 0.0)
            q_scores.append(score)
            if s["roll_no"] in top_students:
                top_scores.append(score)
            if s["roll_no"] in bottom_students:
                bottom_scores.append(score)

        if q_scores:
            q_mean = round(sum(q_scores) / len(q_scores), 2)
            # Item difficulty index P = mean / max_marks (higher means easier)
            difficulty_p = round(q_mean / max_m, 2) if max_m > 0 else 0.0
            diff_label = "EASY" if difficulty_p >= 0.75 else ("DIFFICULT" if difficulty_p < 0.40 else "MODERATE")

            # Discrimination index D = (mean_top - mean_bottom) / max_marks
            top_mean = sum(top_scores) / len(top_scores) if top_scores else q_mean
            bottom_mean = sum(bottom_scores) / len(bottom_scores) if bottom_scores else q_mean
            discrim_d = round((top_mean - bottom_mean) / max_m, 2) if max_m > 0 else 0.0
        else:
            q_mean, difficulty_p, diff_label, discrim_d = 0.0, 0.0, "UNKNOWN", 0.0

        q_metrics.append({
            "question_id": qid,
            "max_marks": max_m,
            "mean_score": q_mean,
            "difficulty_index_p": difficulty_p,
            "difficulty_rating": diff_label,
            "discrimination_index_d": discrim_d,
            "scored_count": len(q_scores),
        })

    return {
        "session_id": session_id,
        "student_count": n_students,
        "mean_score": mean_val,
        "median_score": median_val,
        "std_dev": std_dev_val,
        "min_score": min_val,
        "max_score": max_val,
        "questions_metrics": q_metrics,
    }


# --------------------------------------------------------------------------- Regrade Requests CRUD
def create_regrade_request(
    session_id: str,
    roll_no: str,
    question_id: str,
    student_reason: str,
    claimed_marks: float = 0.0,
) -> int:
    """Submits a student regrade appeal."""
    init_hitl_db()
    now_iso = datetime.now(timezone.utc).isoformat()
    with _LOCK:
        conn = _connect()
        try:
            cur = conn.execute(
                """
                INSERT INTO regrade_requests (session_id, roll_no, question_id, student_reason, claimed_marks, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'PENDING', ?)
                """,
                (session_id, roll_no, question_id, student_reason.strip(), float(claimed_marks), now_iso),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def list_regrade_requests(
    session_id: str,
    status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Lists regrade requests for a session."""
    init_hitl_db()
    with _LOCK:
        conn = _connect()
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM regrade_requests WHERE session_id = ? AND status = ? ORDER BY id DESC",
                    (session_id, status.upper()),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM regrade_requests WHERE session_id = ? ORDER BY id DESC",
                    (session_id,),
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def resolve_regrade_request(
    request_id: int,
    status: str,
    ta_response: str = "",
    granted_marks: Optional[float] = None,
    session_id: Optional[str] = None,
) -> bool:
    """Resolves a student regrade request with ACCEPTED or REJECTED status, and updates question evaluations if accepted."""
    init_hitl_db()
    now_iso = datetime.now(timezone.utc).isoformat()
    status_upper = status.upper().strip()
    with _LOCK:
        conn = _connect()
        try:
            req_row = conn.execute("SELECT * FROM regrade_requests WHERE id = ?", (request_id,)).fetchone()
            if not req_row:
                return False
            if session_id and req_row["session_id"] != session_id:
                return False
            cur = conn.execute(
                """
                UPDATE regrade_requests
                SET status = ?, ta_response = ?, resolved_at = ?
                WHERE id = ?
                """,
                (status_upper, ta_response.strip(), now_iso, request_id),
            )
            conn.commit()
            updated = cur.rowcount > 0
        finally:
            conn.close()

    if updated and status_upper == "ACCEPTED" and req_row:
        final_marks = float(granted_marks) if granted_marks is not None else float(req_row["claimed_marks"] or 0.0)
        apply_ta_grade_override(
            session_id=req_row["session_id"],
            roll_no=req_row["roll_no"],
            question_id=req_row["question_id"],
            ta_status="OVERRIDDEN",
            awarded_marks=final_marks,
            verdict_tab="GREEN",
            ta_comment=f"Regrade Appeal Accepted: {ta_response.strip() or 'Marks updated per student petition'}",
        )
    return updated


# --------------------------------------------------------------------------- Persistent Global Haiku 5.5 LLM Telemetry
def record_global_llm_usage(
    model: str = "claude-haiku-5-5",
    calls: int = 0,
    ok_calls: int = 0,
    failed_calls: int = 0,
    submissions_reviewed: int = 0,
    input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    output_tokens: int = 0,
    elapsed_seconds: float = 0.0,
) -> Dict[str, Any]:
    """Atomically increments cumulative persistent LLM token counters and call health metrics."""
    init_hitl_db()
    now = ist_now()
    with _LOCK:
        conn = _connect()
        try:
            conn.execute(
                """
                UPDATE global_llm_usage
                SET model = ?,
                    calls = calls + ?,
                    ok_calls = ok_calls + ?,
                    failed_calls = failed_calls + ?,
                    submissions_reviewed = submissions_reviewed + ?,
                    input_tokens = input_tokens + ?,
                    cache_creation_input_tokens = cache_creation_input_tokens + ?,
                    cache_read_input_tokens = cache_read_input_tokens + ?,
                    output_tokens = output_tokens + ?,
                    total_elapsed_seconds = total_elapsed_seconds + ?,
                    updated_at = ?
                WHERE id = 1
                """,
                (
                    model,
                    int(calls),
                    int(ok_calls),
                    int(failed_calls),
                    int(submissions_reviewed),
                    int(input_tokens),
                    int(cache_creation_input_tokens),
                    int(cache_read_input_tokens),
                    int(output_tokens),
                    float(elapsed_seconds),
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    return get_global_llm_usage()


def get_global_llm_usage() -> Dict[str, Any]:
    """Retrieves cumulative persistent LLM usage statistics."""
    init_hitl_db()
    with _LOCK:
        conn = _connect()
        try:
            r = conn.execute("SELECT * FROM global_llm_usage WHERE id = 1").fetchone()
            if not r:
                return {
                    "model": settings.primary_model,
                    "calls": 0, "ok_calls": 0, "failed_calls": 0,
                    "submissions_reviewed": 0, "input_tokens": 0,
                    "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                    "output_tokens": 0, "total_elapsed_seconds": 0.0,
                    "updated_at": ist_now(),
                }
            return dict(r)
        finally:
            conn.close()


def reset_global_llm_usage() -> Dict[str, Any]:
    """Resets global LLM token and call counters back to zero."""
    init_hitl_db()
    now = ist_now()
    with _LOCK:
        conn = _connect()
        try:
            conn.execute(
                """
                UPDATE global_llm_usage
                SET calls = 0, ok_calls = 0, failed_calls = 0,
                    submissions_reviewed = 0, input_tokens = 0,
                    cache_creation_input_tokens = 0, cache_read_input_tokens = 0,
                    output_tokens = 0, total_elapsed_seconds = 0.0,
                    updated_at = ?
                WHERE id = 1
                """,
                (now,),
            )
            conn.commit()
        finally:
            conn.close()
    return get_global_llm_usage()
