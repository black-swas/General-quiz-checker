"""TA calibration memory & persistent evaluation session store (SQLite).

TA corrections are shown to the LLM as few-shot examples when it judges submissions.
Completed evaluation reports are also persisted in SQLite so browser refreshes, multiple TAs,
or container restarts never lose an evaluated session.
"""
import json
import logging
import re
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.analyzer.c_static_analyzer import PROMPT_INJECTION_PATTERNS
from app.config import settings
from app.schemas import HITLFeedbackRequest

logger = logging.getLogger("iiserb.hitl")

_LOCK = threading.Lock()
_READY: Dict[str, Path] = {}      # configured path -> resolved, initialised path

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
        fallback = Path(tempfile.gettempdir()) / "iiserb_hitl_calibration.db"
        logger.warning("SQLite path %s is not writable; using %s (data will not persist across restarts)", target, fallback)
        target = fallback
    _READY[configured] = target
    return target


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_hitl_db() -> None:
    with _LOCK:
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
        finally:
            conn.close()


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
    """Most relevant TA corrections (DISAGREE rows), preferring matching topic tags, then recency."""
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
    """TA agreement with automatic verdicts (seed rows excluded): a live accuracy signal."""
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


# --------------------------------------------------------------------------- persistent job storage
def save_persisted_job(job_id: str, created_at: float, result: Dict[str, Any]) -> None:
    """Persist a completed evaluation report in SQLite and prune beyond max_stored_jobs."""
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
            # Keep only the latest max_stored_jobs records in SQLite
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
    """Load (created_at, result_dict) from SQLite if present."""
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
    """List metadata of recent persisted evaluation jobs."""
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
    """Propagate a TA decision into any persisted job containing that student and question."""
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
