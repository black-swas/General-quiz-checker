"""In-memory + SQLite-backed job registry for long-running evaluations (polled by the UI)."""
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.database.hitl_store import list_persisted_jobs, load_persisted_job, save_persisted_job
from app.schemas import HITLFeedbackRequest


@dataclass
class Job:
    id: str
    owner: str
    created_at: float = field(default_factory=time.time)
    status: str = "queued"          # queued | running | done | error | cancelled
    stage: str = "Queued"
    progress: float = 0.0
    message: str = ""
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None


class JobStore:
    def __init__(self, ttl_seconds: int = 14400, max_jobs: int = 10) -> None:
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds
        self._max = max(1, max_jobs)

    def _prune_locked(self) -> None:
        now = time.time()
        for jid in [j.id for j in self._jobs.values() if now - j.created_at > self._ttl and j.status in ("done", "error", "cancelled")]:
            self._jobs.pop(jid, None)
        # keep memory bounded: drop the oldest finished jobs first
        finished = sorted((j for j in self._jobs.values() if j.status in ("done", "error", "cancelled")), key=lambda j: j.created_at)
        while len(self._jobs) > self._max and finished:
            self._jobs.pop(finished.pop(0).id, None)

    def create(self, owner: str) -> Job:
        with self._lock:
            self._prune_locked()
            job = Job(id=uuid.uuid4().hex, owner=owner)
            self._jobs[job.id] = job
            return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                return job
        # Fallback to SQLite if the job completed earlier and was evicted or server restarted
        persisted = load_persisted_job(job_id)
        if persisted is not None:
            created_at, result = persisted
            recovered = Job(
                id=job_id,
                owner="persisted",
                created_at=created_at,
                status="done",
                stage="Done",
                progress=1.0,
                message="Evaluation complete",
                result=result,
            )
            with self._lock:
                self._jobs[job_id] = recovered
            return recovered
        return None

    def update(self, job_id: str, **fields: Any) -> None:
        to_persist: Optional[Tuple[float, Dict[str, Any]]] = None
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            for key, value in fields.items():
                setattr(job, key, value)
            if job.status == "done" and isinstance(job.result, dict):
                to_persist = (job.created_at, job.result)
        if to_persist is not None:
            save_persisted_job(job_id, to_persist[0], to_persist[1])

    def apply_ta_feedback(self, req: HITLFeedbackRequest) -> None:
        with self._lock:
            for job in self._jobs.values():
                if not isinstance(job.result, dict):
                    continue
                if job.result.get("course_code") != req.course_code:
                    continue
                q_evals = (job.result.get("evaluations_by_question") or {}).get(req.question_id) or []
                for ev in q_evals:
                    if str(ev.get("roll_no", "")).strip().casefold() == str(req.roll_no).strip().casefold():
                        ev["ta_feedback"] = req.ta_decision
                        ev["ta_override_category"] = req.override_category
                        ev["ta_note"] = req.ta_explanation

    def list_recent(self, limit: int = 8) -> List[Dict[str, Any]]:
        with self._lock:
            active = [
                {
                    "job_id": j.id,
                    "status": j.status,
                    "stage": j.stage,
                    "progress": j.progress,
                    "created_at": j.created_at,
                    "quiz_title": (j.result or {}).get("quiz_title", "Running evaluation…"),
                    "course_code": (j.result or {}).get("course_code", ""),
                    "student_count": (j.result or {}).get("student_count", 0),
                    "total_files": (j.result or {}).get("total_files", 0),
                    "summary_counts": (j.result or {}).get("summary_counts", {}),
                }
                for j in sorted(self._jobs.values(), key=lambda x: x.created_at, reverse=True)
                if j.status in ("queued", "running")
            ]
        persisted = list_persisted_jobs(limit=limit)
        seen = {a["job_id"] for a in active}
        combined = active + [p for p in persisted if p["job_id"] not in seen]
        return combined[:limit]

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if j.status in ("queued", "running"))
