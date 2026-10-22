"""FastAPI application: 100% Laptop-Hosted Paper Scanner Hub, Folder Manager, & Concurrent AI Grading Dashboard (v5.1)."""
import asyncio
import base64
import csv
import gc
import hashlib
import io
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import httpx
import numpy as np
from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.analyzer.annotated_pdf import generate_feedback_zip, generate_student_feedback_pdf
from app.analyzer.booklet_generator import generate_booklet_pdf
from app.analyzer.excel_exporter import build_session_excel_bytes
from app.analyzer.gcc_sandbox import gcc_available, sandbox_runtimes
from app.analyzer.image_processor import crop_multipage_question, crop_question_from_page, process_and_save_page
from app.analyzer.llm_evaluator import evaluate_session_question_crops, evaluate_session_whole_pages
from app.analyzer.paper_plagiarism import run_paper_exam_plagiarism
from app.analyzer.paper_redundancy_engine import (
    compute_perceptual_hash,
    format_supplementary_page_info,
    is_duplicate_scan_frame,
    reconcile_roll_number,
    reorder_and_audit_scanned_pages,
    validate_image_buffer_integrity,
    verify_packet_student_consistency,
)
from app.analyzer.pdf_ingestor import is_pdf_buffer, process_bulk_exam_files, render_pdf_to_images
from app.analyzer.pipeline import run_full_quiz_evaluation
from app.analyzer.zip_ingestor import SUPPORTED_EXTENSIONS, ZipIngestionError, extract_student_submissions
from app.config import settings
from app.database.hitl_store import (
    apply_ta_grade_override,
    bulk_approve_session_green,
    claim_next_student,
    create_exam_session,
    create_regrade_request,
    delete_exam_session,
    delete_scanned_page_record,
    delete_session_student,
    get_calibration_examples,
    get_exam_analytics,
    get_exam_session,
    get_feedback_stats,
    get_global_llm_usage,
    get_scanned_page_record,
    get_student_question_evaluations,
    init_hitl_db,
    list_exam_sessions,
    list_regrade_requests,
    record_global_llm_usage,
    record_student_presence,
    release_student_claim,
    reset_global_llm_usage,
    resolve_regrade_request,
    save_hitl_feedback,
    save_scanned_page_record,
    update_exam_session,
    update_session_progress,
    update_ta_in_persisted_jobs,
    upsert_session_student,
)
from app.jobs import JobStore
from app.schemas import (
    AddStudentRequest,
    BatchEvaluationResponse,
    BulkRosterRequest,
    ChunkUploadRequest,
    ClaimNextStudentRequest,
    CreateSessionRequest,
    EvaluateSessionRequest,
    FinalizeStudentRequest,
    FolderSelectRequest,
    HITLFeedbackRequest,
    HITLFeedbackResponse,
    JobCreated,
    JobStatus,
    PresenceRequest,
    QuizPaper,
    ReleaseClaimRequest,
    ResolveRegradeRequest,
    RuntimeSettingsUpdateRequest,
    StudentRegradeRequest,
    SwitchModelRequest,
    TAHeartbeatRequest,
    TAOverrideGradeRequest,
    UpdateSessionRequest,
    VLMConfigRequest,
)
from app.security import SlidingWindowRateLimiter, constant_time_equals, harden_process, scrub_secret_env

from logging.handlers import RotatingFileHandler

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
SAMPLES_DIR = APP_DIR.parent / "sample_quiz_data"
LOGS_DIR = APP_DIR.parent / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
_log_file = LOGS_DIR / "iiserb_hub.log"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
_root_logger = logging.getLogger()
if not any(isinstance(h, RotatingFileHandler) for h in _root_logger.handlers):
    try:
        _file_h = RotatingFileHandler(
            str(_log_file),
            maxBytes=10_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        _file_h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        _root_logger.addHandler(_file_h)
    except Exception:
        pass

logger = logging.getLogger("iiserb.api")
SAMPLE_NAME_SUFFIX = "_paper.json"
_NORM_QID_RE = re.compile(r"^(?:q|que|question)[_\-\s]*(\d{1,2})$", re.IGNORECASE)
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_\-.]")

jobs = JobStore(ttl_seconds=settings.job_ttl_seconds, max_jobs=settings.max_stored_jobs)
rate_limiter = SlidingWindowRateLimiter()
_job_slot = asyncio.Semaphore(settings.job_concurrency)
_background_tasks: Dict[str, asyncio.Task] = {}
# Per-session grading queues to decouple individual student finalization from session-wide locks
_session_grading_queues: Dict[str, asyncio.Queue] = {}
_session_worker_tasks: Dict[str, asyncio.Task] = {}


def _ensure_session_grading_worker(session_id: str) -> None:
    worker = _session_worker_tasks.get(session_id)
    if worker is None or worker.done():
        task = asyncio.create_task(_session_grading_worker(session_id))
        _session_worker_tasks[session_id] = task
        _background_tasks[f"worker:{session_id}"] = task
        task.add_done_callback(lambda _t, sid=session_id: (_background_tasks.pop(f"worker:{sid}", None), _session_worker_tasks.pop(sid, None)))


async def _session_grading_worker(session_id: str) -> None:
    q = _session_grading_queues.get(session_id)
    if not q:
        return
    while True:
        try:
            try:
                item = await asyncio.wait_for(q.get(), timeout=15.0)
            except (asyncio.TimeoutError, TimeoutError):
                break
            roll_no, qid, force_recrop = item
            try:
                await _run_session_async_grading(
                    session_id,
                    only_roll_no=roll_no,
                    only_question_id=qid,
                    force_recrop=force_recrop,
                )
            except Exception as e:
                logger.exception("Error grading student %s in session %s: %s", roll_no, session_id, e)
            finally:
                q.task_done()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.exception("Grading worker loop error for %s: %s", session_id, e)

# In-memory buffer for chunked 64 KB mobile uploads before final assembly on disk
_CHUNK_BUFFERS: Dict[str, Dict[int, bytes]] = {}
_CHUNK_BUFFERS_TIMESTAMPS: Dict[str, float] = {}
_CHUNK_LOCK = asyncio.Lock()

# Live activity log so the Laptop UI shows real-time mobile scan events
_RECENT_EVENTS: List[Dict[str, Any]] = []
_ACTIVE_TAS: Dict[str, Dict[str, Dict[str, Any]]] = {}


def record_active_ta(session_id: str, ta_name: str, station: Optional[str] = None) -> List[Dict[str, Any]]:
    if not session_id or not ta_name:
        return []
    clean_ta = ta_name.strip()
    if not clean_ta:
        return []
    now = time.time()
    s_tas = _ACTIVE_TAS.setdefault(session_id, {})
    s_tas[clean_ta] = {"last_seen": now, "station": station or "Station"}
    # Evict TAs not seen in last 10 minutes (600s)
    stale = [k for k, v in s_tas.items() if now - v["last_seen"] > 600]
    for k in stale:
        s_tas.pop(k, None)
    return [{"ta_name": k, "station": v.get("station", "Station"), "last_seen": round(v["last_seen"], 1)} for k, v in s_tas.items()]


def get_active_tas(session_id: str) -> List[Dict[str, Any]]:
    now = time.time()
    s_tas = _ACTIVE_TAS.get(session_id, {})
    return [{"ta_name": k, "station": v.get("station", "Station"), "last_seen": round(v["last_seen"], 1)} for k, v in s_tas.items() if now - v.get("last_seen", 0) <= 600]


def _push_event(kind: str, message: str, session_id: str = "", roll_no: str = "", page_no: int = 0, ta_name: str = "") -> None:
    _RECENT_EVENTS.insert(0, {
        "ts": round(time.time(), 2),
        "kind": kind,
        "message": message,
        "session_id": session_id,
        "roll_no": roll_no,
        "page_no": page_no,
        "ta_name": ta_name,
    })
    if len(_RECENT_EVENTS) > 60:
        del _RECENT_EVENTS[60:]


def detect_local_wifi_ips() -> List[str]:
    """Automatically detect the laptop's local college Wi-Fi / Hotspot IPv4 addresses."""
    ips: List[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("10.255.255.255", 1))
        primary_ip = s.getsockname()[0]
        s.close()
        if primary_ip and not primary_ip.startswith("127."):
            ips.append(primary_ip)
    except Exception:
        pass
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips or ["127.0.0.1"]


def _storage_root(custom_dir: Optional[str] = None) -> Path:
    target = Path(custom_dir or settings.paper_storage_dir).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    return target


def _session_storage_root(session: Dict[str, Any]) -> Path:
    s_dir = session.get("storage_dir") or settings.paper_storage_dir
    return _storage_root(s_dir)


def _safe_segment(val: str) -> str:
    cleaned = _SAFE_ID_RE.sub("_", val.strip())
    return cleaned[:80] or "unknown"


def _disk_stats(path: Path) -> Dict[str, Any]:
    try:
        usage = shutil.disk_usage(str(path))
        return {
            "total_gb": round(usage.total / (1024 ** 3), 2),
            "used_gb": round(usage.used / (1024 ** 3), 2),
            "free_gb": round(usage.free / (1024 ** 3), 2),
        }
    except Exception:
        return {"total_gb": 0.0, "used_gb": 0.0, "free_gb": 0.0}


def generate_qr_svg(text: str, box_size: int = 6, border: int = 2) -> str:
    """Generate a crisp, dependency-free SVG QR code using OpenCV's built-in QRCodeEncoder."""
    encoder = cv2.QRCodeEncoder_create()
    qr_mat = encoder.encode(text)
    h, w = qr_mat.shape[:2]
    total_w = (w + 2 * border) * box_size
    total_h = (h + 2 * border) * box_size
    rects: List[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total_w} {total_h}" width="{total_w}" height="{total_h}" shape-rendering="crispEdges">',
        f'<rect width="{total_w}" height="{total_h}" fill="#ffffff"/>',
    ]
    for r in range(h):
        for c in range(w):
            if qr_mat[r, c] == 0:
                x = (c + border) * box_size
                y = (r + border) * box_size
                rects.append(f'<rect x="{x}" y="{y}" width="{box_size}" height="{box_size}" fill="#0f172a"/>')
    rects.append("</svg>")
    return "".join(rects)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    hardened = harden_process()
    scrub_secret_env()
    init_hitl_db()
    _storage_root()
    runtimes = sandbox_runtimes()
    wifi_ips = detect_local_wifi_ips()
    logger.info(
        "Started Laptop Hub v%s | wifi_ips=%s | storage=%s | llm=%s (%s) | concurrency=%s | batch=%s",
        settings.app_version, wifi_ips, _storage_root(), settings.llm_available, settings.primary_model,
        settings.llm_concurrency, settings.llm_vision_batch_size,
    )
    yield


app = FastAPI(
    title="IISER Bhopal Local Laptop Paper Scanner & AI Grading Hub (v5.1)",
    version=settings.app_version,
    description="100% Laptop-Hosted Mobile Paper Scanner (400+ students x 10 pages), Folder Manager, OpenCV Flattening, and Concurrent Prompt-Cached Claude Haiku 5.5 Grading.",
    lifespan=lifespan,
)

if settings.allowed_origins:
    origins = ["*"] if settings.allowed_origins.strip() == "*" else [o.strip() for o in settings.allowed_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.middleware("http")
async def guard_and_harden(request: Request, call_next):
    if request.method in ("POST", "PUT"):
        length = request.headers.get("content-length", "")
        if length.isdigit() and int(length) > settings.max_upload_bytes + 4 * 1024 * 1024:
            return JSONResponse({"detail": "Upload too large."}, status_code=413)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob: mediastream:; worker-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
    )
    return response


# --------------------------------------------------------------------------- helpers
def require_auth(request: Request, x_api_key: Optional[str] = Header(default=None)) -> None:
    if not settings.auth_required:
        return
    if not settings.api_auth_token:
        raise HTTPException(503, "Authentication is required but API_AUTH_TOKEN is not configured on the server.")
    bearer = request.headers.get("Authorization", "")
    supplied = ""
    if isinstance(x_api_key, str) and x_api_key:
        supplied = x_api_key
    elif bearer.lower().startswith("bearer "):
        supplied = bearer[7:].strip()
    if not supplied:
        supplied = request.query_params.get("token") or request.cookies.get("auth_token") or request.cookies.get("access_token") or ""
    if not supplied or not constant_time_equals(supplied, settings.api_auth_token):
        raise HTTPException(401, "Missing or invalid access token.", headers={"WWW-Authenticate": "Bearer"})


def require_loopback_client(request: Request) -> None:
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(403, "Folder management operations are restricted to localhost.")


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _enforce_rate_limit(request: Request, bucket: str, limit: int, window_seconds: int) -> None:
    allowed, retry_after = rate_limiter.check(f"{bucket}:{_client_key(request)}", limit, window_seconds)
    if not allowed:
        raise HTTPException(429, f"Too many requests. Try again in {retry_after}s.", headers={"Retry-After": str(retry_after)})


def parse_question_paper(raw: str) -> QuizPaper:
    """Accepts {"questions": [...]} or a bare list. Normalises 'q1'/'que_1' -> 'Q1'."""
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"The question paper is not valid JSON: {exc}")
    if isinstance(data, list):
        data = {"questions": data}
    if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
        raise ValueError('Expected an object with a "questions" list.')
    for q in data["questions"]:
        if isinstance(q, dict) and isinstance(q.get("question_id"), str):
            m = _NORM_QID_RE.match(q["question_id"].strip())
            if m and 1 <= int(m.group(1)) <= 99:
                q["question_id"] = f"Q{int(m.group(1))}"
    paper = QuizPaper(**data)
    if not paper.questions:
        raise ValueError("The question paper has no questions.")
    if len(paper.questions) > settings.max_questions_per_paper:
        raise ValueError(f"The paper has {len(paper.questions)} questions; the limit is {settings.max_questions_per_paper}.")
    ids = [q.question_id for q in paper.questions]
    if len(set(ids)) != len(ids):
        raise ValueError("Question ids must be unique.")
    return paper


# --------------------------------------------------------------------------- Pages, QR Code, & Laptop Hub Info
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/scan", include_in_schema=False)
def mobile_scanner_page() -> FileResponse:
    scanner_html = STATIC_DIR / "scanner.html"
    if scanner_html.is_file():
        return FileResponse(scanner_html, headers={"Cache-Control": "no-cache"})
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/qr.svg", include_in_schema=False)
def api_qr_svg(request: Request, url: Optional[str] = Query(default=None), session_id: Optional[str] = Query(default=None)) -> Response:
    """Render an instant SVG QR code pointing to the laptop's local Wi-Fi URL so TA phones can scan & open immediately."""
    ips = detect_local_wifi_ips()
    port = request.url.port or settings.hub_port
    scheme = request.url.scheme or "http"
    target_url = url or f"{scheme}://{ips[0]}:{port}/scan"
    if session_id and "session=" not in target_url:
        sep = "&" if "?" in target_url else "?"
        target_url = f"{target_url}{sep}session={session_id}"
    svg_str = generate_qr_svg(target_url, box_size=6, border=2)
    return Response(content=svg_str, media_type="image/svg+xml", headers={"Cache-Control": "no-cache"})


@app.get("/api/health")
def health() -> Dict[str, Any]:
    ips = detect_local_wifi_ips()
    return {
        "status": "ok",
        "version": settings.app_version,
        "gcc": gcc_available(),
        "runtimes": sandbox_runtimes(),
        "low_memory": settings.is_low_memory,
        "render": settings.is_render,
        "local_ips": ips,
        "hub_url": f"http://{ips[0]}:{settings.hub_port}/scan",
    }


@app.get("/api/hub-info")
def hub_info(request: Request, _auth: Any = Depends(require_auth)) -> Dict[str, Any]:
    """Return local Wi-Fi IPs, active storage folder, disk free space, LLM settings, and recent scan events."""
    ips = detect_local_wifi_ips()
    port = request.url.port or settings.hub_port
    scheme = request.url.scheme or "http"
    primary_url = f"{scheme}://{ips[0]}:{port}/scan"
    laptop_url = f"{scheme}://{ips[0]}:{port}/scan?view=laptop"
    root = _storage_root()
    return {
        "version": settings.app_version,
        "status": "ok",
        "laptop_name": socket.gethostname(),
        "local_ips": ips,
        "ip": ips[0] if ips else "127.0.0.1",
        "all_ips": ips,
        "port": port,
        "scheme": scheme,
        "scanner_url": primary_url,
        "laptop_url": laptop_url,
        "laptop_dashboard_url": laptop_url,
        "all_scanner_urls": [f"{scheme}://{ip}:{port}/scan" for ip in ips],
        "storage_dir": str(root),
        "paper_storage_dir": str(root),
        "disk": _disk_stats(root),
        "anthropic_configured": bool(settings.anthropic_api_key),
        "primary_model": settings.primary_model,
        "llm_model": settings.primary_model,
        "vlm_provider": settings.vlm_provider,
        "local_vlm_url": settings.local_vlm_url,
        "local_vlm_model": settings.local_vlm_model,
        "llm_available": settings.llm_available,
        "llm_concurrency": settings.llm_concurrency,
        "llm_vision_batch_size": settings.llm_vision_batch_size,
        "auto_approve_threshold": settings.auto_approve_confidence_threshold,
        "auto_approve_confidence_threshold": settings.auto_approve_confidence_threshold,
        "max_students": settings.max_students_per_session,
        "max_pages_per_student": settings.max_pages_per_student,
        "active_sessions_count": len(list_exam_sessions(limit=500)),
        "recent_events": [{"ts": e["ts"], "kind": e["kind"], "message": e["message"]} for e in _RECENT_EVENTS[:20]],
    }


@app.post("/api/runtime-settings")
def api_update_runtime_settings(req: RuntimeSettingsUpdateRequest, _auth: Any = Depends(require_auth)) -> Dict[str, Any]:
    """Update runtime LLM settings (API key, model, concurrency, batch size, storage folder) on the laptop without restarting."""
    if req.anthropic_api_key is not None and req.anthropic_api_key.strip():
        k = req.anthropic_api_key.strip()
        settings.anthropic_api_key = k
        settings.llm_enabled = True
        os.environ["ANTHROPIC_API_KEY"] = k
        try:
            env_file = Path(__file__).resolve().parent.parent / ".env"
            if env_file.is_file():
                content = env_file.read_text(encoding="utf-8")
                if "ANTHROPIC_API_KEY=" in content:
                    lines = content.splitlines()
                    new_lines = []
                    for line in lines:
                        if line.strip().startswith("ANTHROPIC_API_KEY="):
                            new_lines.append(f'ANTHROPIC_API_KEY="{k}"')
                        else:
                            new_lines.append(line)
                    env_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
                else:
                    env_file.write_text(content.rstrip() + f'\nANTHROPIC_API_KEY="{k}"\n', encoding="utf-8")
        except Exception as exc:
            logger.warning("Could not persist ANTHROPIC_API_KEY to .env: %s", exc)

    if req.primary_model is not None and req.primary_model.strip():
        settings.primary_model = req.primary_model.strip()
        lm = req.primary_model.strip().lower()
        if "qwen" in lm or "ollama" in lm or "local" in lm:
            settings.vlm_provider = "ollama"
            settings.local_vlm_model = req.primary_model.strip()
        elif "gpt" in lm:
            settings.vlm_provider = "openai"
        elif "claude" in lm or "haiku" in lm or "sonnet" in lm or "opus" in lm:
            settings.vlm_provider = "anthropic"
    if req.vlm_provider is not None and req.vlm_provider.strip():
        settings.vlm_provider = req.vlm_provider.strip().lower()
    if req.local_vlm_url is not None and req.local_vlm_url.strip():
        settings.local_vlm_url = req.local_vlm_url.strip()
    if req.local_vlm_model is not None and req.local_vlm_model.strip():
        settings.local_vlm_model = req.local_vlm_model.strip()
    if req.llm_concurrency is not None:
        settings.llm_concurrency = max(1, min(20, int(req.llm_concurrency)))
    if req.llm_vision_batch_size is not None:
        settings.llm_vision_batch_size = max(1, min(15, int(req.llm_vision_batch_size)))
    if req.auto_approve_confidence_threshold is not None:
        settings.auto_approve_confidence_threshold = round(max(0.0, min(1.0, float(req.auto_approve_confidence_threshold))), 2)
    if req.paper_storage_dir is not None and req.paper_storage_dir.strip():
        target = _storage_root(req.paper_storage_dir.strip())
        settings.paper_storage_dir = str(target)

    return {
        "status": "updated",
        "llm_available": settings.llm_available,
        "llm_model": settings.primary_model,
        "vlm_provider": settings.vlm_provider,
        "local_vlm_url": settings.local_vlm_url,
        "local_vlm_model": settings.local_vlm_model,
        "llm_concurrency": settings.llm_concurrency,
        "llm_vision_batch_size": settings.llm_vision_batch_size,
        "auto_approve_threshold": settings.auto_approve_confidence_threshold,
        "storage_dir": str(_storage_root()),
    }


@app.post("/api/llm/test")
async def api_test_llm_connection(
    req: Optional[Dict[str, Any]] = Body(default=None),
    _auth: Any = Depends(require_auth),
) -> Dict[str, Any]:
    """Test LLM credentials and availability by making a small handshake or checking key validity."""
    api_key = ((req.get("anthropic_api_key") if req else None) or "").strip() or settings.anthropic_api_key
    model = ((req.get("model") or req.get("primary_model") if req else None) or "").strip() or settings.primary_model or "claude-haiku-5-5"

    if not api_key:
        return {
            "status": "error",
            "message": "No Anthropic API key configured. Enter your API key above or in .env.",
            "llm_available": False,
        }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            headers = {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            payload = {
                "model": model,
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "Ping"}],
            }
            resp = await client.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload)
            if resp.status_code == 200:
                settings.anthropic_api_key = api_key
                settings.primary_model = model
                settings.llm_enabled = True
                return {
                    "status": "ok",
                    "message": f"Successfully connected to Anthropic API ({model})",
                    "llm_available": True,
                    "model": model,
                }
            elif resp.status_code == 429:
                settings.anthropic_api_key = api_key
                settings.primary_model = model
                settings.llm_enabled = True
                return {
                    "status": "ok",
                    "message": f"API key is valid & authenticated ({model}), but currently rate-limited (HTTP 429). Ready for use.",
                    "llm_available": True,
                    "model": model,
                }
            else:
                err_data = resp.json().get("error", {}) if resp.headers.get("content-type", "").startswith("application/json") else {}
                err_msg = err_data.get("message") or f"Anthropic returned HTTP {resp.status_code}"
                return {
                    "status": "error",
                    "message": f"Anthropic API test failed: {err_msg}",
                    "llm_available": False,
                    "http_status": resp.status_code,
                }
    except Exception as exc:
        return {
            "status": "error",
            "message": f"Network / connection error: {exc}",
            "llm_available": False,
        }


@app.get("/api/config")
def public_config() -> Dict[str, Any]:
    runtimes = sandbox_runtimes()
    ips = detect_local_wifi_ips()
    return {
        "version": settings.app_version,
        "is_render": settings.is_render,
        "is_low_memory": settings.is_low_memory,
        "auth_required": settings.auth_required,
        "sandbox": settings.enable_gcc_sandbox and any(runtimes.values()),
        "runtimes": runtimes,
        "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
        "llm": {
            "available": settings.llm_available,
            "model": settings.primary_model,
            "full_triage": settings.llm_full_triage,
            "concurrency": settings.llm_concurrency,
            "vision_batch_size": settings.llm_vision_batch_size,
            "auto_approve_confidence": settings.auto_approve_confidence_threshold,
        },
        "plagiarism_enabled": settings.enable_plagiarism,
        "clone_threshold_percent": settings.clone_threshold_percent,
        "local_ips": ips,
        "scanner_url": f"http://{ips[0]}:{settings.hub_port}/scan",
        "storage_dir": str(_storage_root()),
        "limits": {
            "max_upload_mb": settings.max_upload_bytes // (1024 * 1024),
            "max_students": settings.max_students_per_session,
            "max_pages_per_student": settings.max_pages_per_student,
            "max_questions": settings.max_questions_per_paper,
            "test_timeout_seconds": settings.test_timeout_seconds,
            "chunk_size_bytes": settings.chunk_size_bytes,
        },
    }


@app.get("/api/polyglot/benchmark-results")
def api_polyglot_benchmark_results() -> Dict[str, Any]:
    """Returns the latest HumanEval-X Polyglot benchmark report and iteration history."""
    from pathlib import Path
    runs_dir = Path(__file__).resolve().parent.parent / "data" / "benchmark_runs"
    history: List[Dict[str, Any]] = []
    latest_run: Optional[Dict[str, Any]] = None

    if runs_dir.is_dir():
        for run_file in sorted(runs_dir.glob("run_iter_*.json")):
            try:
                with open(run_file, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                    summary = {
                        "iteration": data.get("iteration", 0),
                        "timestamp": data.get("timestamp", ""),
                        "num_samples_per_lang": data.get("num_samples_per_lang", 0),
                        "total_tasks": data.get("total_tasks", 0),
                        "total_passed": data.get("total_passed", 0),
                        "overall_accuracy_pct": data.get("overall_accuracy_pct", 0.0),
                        "total_wall_clock_sec": data.get("total_wall_clock_sec", 0.0),
                        "per_language_stats": data.get("per_language_stats", {}),
                        "filename": run_file.name,
                    }
                    history.append(summary)
                    latest_run = data
            except Exception:
                pass

    return {
        "status": "success",
        "runtimes": sandbox_runtimes(),
        "latest": latest_run,
        "history": sorted(history, key=lambda x: (x["iteration"], x["total_tasks"])),
        "available_datasets": ["python", "cpp", "js", "go", "java"],
    }


# --------------------------------------------------------------------------- Interactive Laptop Folder Picker APIs
@app.get("/api/folders/browse")
def api_browse_folders(
    path: Optional[str] = Query(default=None),
    _auth: Any = Depends(require_auth),
    _loopback: None = Depends(require_loopback_client),
) -> Dict[str, Any]:
    """Browse directories on the laptop so the user can choose or create any destination folder for scanned papers."""
    raw_target = path.strip() if path and path.strip() else settings.paper_storage_dir
    try:
        current = Path(raw_target).expanduser().resolve()
        if not current.exists():
            current = Path.cwd().resolve()
        if current.is_file():
            current = current.parent
    except Exception:
        current = Path.cwd().resolve()

    subdirs: List[Dict[str, str]] = []
    try:
        for entry in sorted(current.iterdir(), key=lambda x: x.name.lower()):
            if entry.is_dir() and not entry.name.startswith("."):
                subdirs.append({"name": entry.name, "path": str(entry)})
    except PermissionError:
        pass

    home = Path.home().resolve()
    presets = [
        {"label": "Default Scans Folder", "path": str((Path.cwd() / "data" / "paper_scans").resolve())},
        {"label": "User Home", "path": str(home)},
        {"label": "Desktop", "path": str(home / "Desktop")},
        {"label": "Documents", "path": str(home / "Documents")},
        {"label": "Downloads", "path": str(home / "Downloads")},
    ]
    return {
        "current_path": str(current),
        "parent_path": str(current.parent) if current.parent != current else str(current),
        "subdirectories": subdirs[:200],
        "presets": presets,
        "disk": _disk_stats(current),
    }


@app.post("/api/folders/select")
def api_select_folder(
    req: FolderSelectRequest,
    _auth: Any = Depends(require_auth),
    _loopback: None = Depends(require_loopback_client),
) -> Dict[str, Any]:
    """Set or create the active storage folder on the laptop (globally and optionally for a specific session)."""
    try:
        target = Path(req.path).expanduser().resolve()
        if req.create_if_missing:
            target.mkdir(parents=True, exist_ok=True)
        if not target.is_dir():
            raise HTTPException(400, f"Directory does not exist: {target}")
        probe = target / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, f"Cannot write to folder '{req.path}': {exc}")

    settings.paper_storage_dir = str(target)
    if req.session_id:
        update_exam_session(req.session_id, storage_dir=str(target))
    _push_event("folder", f"Storage folder set to {target}", session_id=req.session_id or "")
    return {
        "status": "ok",
        "storage_dir": str(target),
        "disk": _disk_stats(target),
    }


@app.post("/api/folders/open-os")
def api_open_folder_in_os(
    path: Optional[str] = Body(default=None, embed=True),
    _auth: Any = Depends(require_auth),
    _loopback: None = Depends(require_loopback_client),
) -> Dict[str, Any]:
    """Open the storage folder in the laptop's native OS File Explorer / Finder / Nautilus when clicked on the laptop."""
    target = _storage_root(path)
    try:
        if sys.platform.startswith("darwin"):
            subprocess.Popen(["open", str(target)])
        elif os.name == "nt":
            os.startfile(str(target))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"status": "opened", "path": str(target)}
    except Exception as exc:
        return {"status": "headless", "path": str(target), "note": f"Path ready at {target} ({exc})"}


# --------------------------------------------------------------------------- Paper Exam Session & Mobile Scanner APIs
@app.get("/api/sessions")
def api_list_sessions(_auth: Any = Depends(require_auth)) -> Dict[str, Any]:
    return {"sessions": list_exam_sessions(limit=50)}


@app.post("/api/sessions", status_code=201)
def api_create_session(req: CreateSessionRequest, _auth: Any = Depends(require_auth)) -> Dict[str, Any]:
    q_list = [q.model_dump() for q in req.questions] if req.questions else None
    computed_marks = req.total_marks
    if (computed_marks is None or computed_marks <= 0.0) and req.questions:
        computed_marks = sum(float(q.max_marks) for q in req.questions)
    if computed_marks is None or computed_marks <= 0.0:
        computed_marks = 20.0
    session = create_exam_session(
        exam_name=req.exam_name,
        course_code=req.course_code,
        exam_date=req.exam_date,
        total_marks=computed_marks,
        expected_pages=req.expected_pages,
        storage_dir=req.storage_dir,
        questions=q_list,
        paper_type=req.paper_type,
        question_paper_image_path=req.question_paper_image_path,
    )
    _push_event("session", f"Created session {session['session_id']} ({session['exam_name']})", session_id=session["session_id"])
    return session


@app.get("/api/sessions/{session_id}")
def api_get_session(session_id: str, _auth: Any = Depends(require_auth)) -> Dict[str, Any]:
    session = get_exam_session(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found.")
    session["active_tas"] = get_active_tas(session_id)
    return session


@app.post("/api/sessions/{session_id}/ta-heartbeat")
def api_ta_heartbeat(
    session_id: str,
    req: TAHeartbeatRequest,
    _auth: Any = Depends(require_auth),
) -> Dict[str, Any]:
    """Register or refresh TA presence for concurrent multi-TA work."""
    session = get_exam_session(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found.")
    active_tas = record_active_ta(session_id, req.ta_name, req.station)
    return {"status": "ok", "session_id": session_id, "active_tas": active_tas}


@app.put("/api/sessions/{session_id}")
def api_update_session(session_id: str, req: UpdateSessionRequest, _auth: Any = Depends(require_auth)) -> Dict[str, Any]:
    updates = req.model_dump(exclude_none=True)
    if "questions" in updates and req.questions is not None:
        updates["questions"] = [q.model_dump() for q in req.questions]
        if req.total_marks is None or req.total_marks <= 0.0:
            updates["total_marks"] = sum(float(q.max_marks) for q in req.questions)
    updated = update_exam_session(session_id, **updates)
    if not updated:
        raise HTTPException(404, f"Session '{session_id}' not found.")
    return updated


@app.delete("/api/sessions/{session_id}")
def api_delete_session(session_id: str, _auth: Any = Depends(require_auth)) -> Dict[str, Any]:
    """Delete an exam session and purge all its associated records and scans."""
    session = get_exam_session(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found.")
    deleted = delete_exam_session(session_id)
    _push_event("session", f"Deleted session {session_id}", session_id=session_id)
    return {"status": "deleted" if deleted else "not_found", "session_id": session_id}


@app.post("/api/sessions/{session_id}/question-paper-image")
async def api_upload_question_paper_image(
    session_id: str,
    file: UploadFile = File(...),
    _auth: Any = Depends(require_auth),
) -> Dict[str, Any]:
    """Upload a scanned or photographed Question Paper image (PNG, JPG, JPEG) to be referenced during AI grading."""
    session = get_exam_session(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found.")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Uploaded question paper image file is empty.")
    if len(data) > settings.max_single_file_bytes:
        raise HTTPException(413, "Image exceeds maximum single-page file size limit.")

    is_valid, reason = validate_image_buffer_integrity(data)
    if not is_valid:
        raise HTTPException(400, f"Corrupted or truncated image upload: {reason}")

    safe_sid = _safe_segment(session_id)
    session_dir = _session_storage_root(session) / safe_sid
    session_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename or "qp.jpg").suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
    qp_img_path = session_dir / f"question_paper{ext}"
    qp_img_path.write_bytes(data)

    update_exam_session(session_id, question_paper_image_path=str(qp_img_path))
    _push_event("session", f"Uploaded Question Paper image for session {session_id}", session_id=session_id)
    return {
        "status": "ok",
        "session_id": session_id,
        "question_paper_image_path": str(qp_img_path),
        "url": f"/api/sessions/{session_id}/question-paper-image",
    }


@app.get("/api/sessions/{session_id}/question-paper-image")
def api_get_question_paper_image(session_id: str, _auth: Any = Depends(require_auth)) -> FileResponse:
    session = get_exam_session(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found.")
    img_path = session.get("question_paper_image_path")
    if not img_path:
        safe_sid = _safe_segment(session_id)
        candidate = _session_storage_root(session) / safe_sid / "question_paper.jpg"
        if candidate.is_file():
            img_path = str(candidate)
    if not img_path or not Path(img_path).is_file():
        raise HTTPException(404, "No Question Paper image uploaded for this session.")
    p = Path(img_path)
    media = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
    return FileResponse(p, media_type=media, headers={"Cache-Control": "no-cache"})


@app.post("/api/sessions/{session_id}/students")
def api_add_student(session_id: str, req: AddStudentRequest, _auth: Any = Depends(require_auth)) -> Dict[str, Any]:
    session = get_exam_session(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found.")
    clean_roll = req.roll_no.strip()
    roster_rolls = [s["roll_no"] for s in (session.get("students") or [])]
    if roster_rolls:
        reconciled, conf, matched = reconcile_roll_number(clean_roll, roster_rolls)
        if matched and conf >= 0.85:
            clean_roll = reconciled
    student = upsert_session_student(session_id, clean_roll, req.student_name)
    return {"status": "ok", "student": student}


@app.post("/api/sessions/{session_id}/roster-csv")
async def api_upload_roster_csv(
    session_id: str,
    file: UploadFile = File(...),
    _auth: Any = Depends(require_auth),
) -> Dict[str, Any]:
    """Import a student roster CSV (`roll_no, student_name`) so TAs have instant autocomplete on mobile and laptop."""
    session = get_exam_session(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found.")
    raw = (await file.read()).decode("utf-8", errors="replace")
    reader = csv.reader(io.StringIO(raw))
    added = 0
    for row in reader:
        if not row:
            continue
        first = row[0].strip()
        if not first or first.lower() in ("roll", "roll_no", "roll no", "roll number", "id", "student_id"):
            continue
        second = row[1].strip() if len(row) > 1 else ""
        upsert_session_student(session_id, first, second)
        added += 1
    _push_event("roster", f"Imported {added} students into roster", session_id=session_id)
    return {"status": "ok", "imported_count": added, "session": get_exam_session(session_id)}


@app.delete("/api/sessions/{session_id}/students/{roll_no}")
def api_delete_student(session_id: str, roll_no: str, _auth: Any = Depends(require_auth)) -> Dict[str, Any]:
    session = get_exam_session(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found.")
    deleted = delete_session_student(session_id, roll_no)
    safe_sid = _safe_segment(session_id)
    safe_roll = _safe_segment(roll_no)
    student_dir = _session_storage_root(session) / safe_sid / safe_roll
    if student_dir.is_dir():
        shutil.rmtree(student_dir, ignore_errors=True)
    return {"status": "deleted" if deleted else "not_found", "roll_no": roll_no}


@app.delete("/api/sessions/{session_id}/students/{roll_no}/pages/{page_no}")
def api_delete_student_page(session_id: str, roll_no: str, page_no: int, _auth: Any = Depends(require_auth)) -> Dict[str, Any]:
    rec = get_scanned_page_record(session_id, roll_no, page_no)
    if rec:
        for k in ("raw_path", "processed_path"):
            p = Path(rec.get(k) or "")
            if p.is_file():
                try:
                    p.unlink()
                except OSError:
                    pass
        proc_p = Path(rec.get("processed_path") or "")
        if proc_p.parent.is_dir():
            for sibling in (proc_p.with_name(f"page_{page_no}_top.jpg"), proc_p.with_name(f"page_{page_no}_bottom.jpg")):
                if sibling.is_file():
                    try:
                        sibling.unlink()
                    except OSError:
                        pass
    deleted = delete_scanned_page_record(session_id, roll_no, page_no)
    return {"status": "deleted" if deleted else "not_found", "roll_no": roll_no, "page_no": page_no}


async def _ingest_complete_page_bytes(
    session_id: str,
    roll_no: str,
    student_name: str,
    page_no: int,
    raw_bytes: bytes,
    is_retake: bool = False,
    ta_name: str = "",
) -> Dict[str, Any]:
    """Save raw uploaded image bytes to the chosen laptop folder, flatten & sharpen with OpenCV, and crop questions."""
    session = get_exam_session(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found.")
    if page_no < 1 or page_no > settings.max_pages_per_student:
        raise HTTPException(400, f"Page number must be between 1 and {settings.max_pages_per_student}.")

    if ta_name:
        record_active_ta(session_id, ta_name)

    is_valid, reason = validate_image_buffer_integrity(raw_bytes)
    if not is_valid:
        raise HTTPException(400, f"Corrupted or truncated image upload: {reason}")

    clean_roll = roll_no.strip()
    roster_rolls = [s["roll_no"] for s in (session.get("students") or [])]
    if roster_rolls:
        reconciled, conf, matched = reconcile_roll_number(clean_roll, roster_rolls)
        if matched and conf >= 0.85:
            clean_roll = reconciled
    roll_no = clean_roll

    upsert_session_student(session_id, roll_no, student_name)
    safe_sid = _safe_segment(session_id)
    safe_roll = _safe_segment(roll_no)
    student_dir = _session_storage_root(session) / safe_sid / safe_roll
    raw_dir = student_dir / "raw"
    proc_dir = student_dir / "processed"
    crops_dir = student_dir / "crops"
    raw_dir.mkdir(parents=True, exist_ok=True)
    proc_dir.mkdir(parents=True, exist_ok=True)
    crops_dir.mkdir(parents=True, exist_ok=True)

    raw_path = raw_dir / f"page_{page_no}.jpg"
    proc_path = proc_dir / f"page_{page_no}_flattened.jpg"

    tmp_raw_path = raw_dir / f"page_{page_no}.tmp.{uuid.uuid4().hex}.jpg"
    tmp_raw_path.write_bytes(raw_bytes)
    os.replace(tmp_raw_path, raw_path)
    sha256_hex = hashlib.sha256(raw_bytes).hexdigest()

    try:
        cv_info = await asyncio.to_thread(process_and_save_page, raw_path, proc_path)
    except Exception as exc:
        logger.error("Failed to process scanned page for %s p%s: %s", roll_no, page_no, exc, exc_info=True)
        raise HTTPException(400, f"Image processing failed: {exc}")

    if is_retake:
        existing_rec = get_scanned_page_record(session_id, roll_no, page_no)
        if existing_rec and existing_rec.get("processed_path"):
            old_p = Path(existing_rec["processed_path"])
            if old_p.is_file() and existing_rec.get("blur_score", 0.0) > cv_info["blur_score"]:
                old_img = cv2.imread(str(old_p))
                curr_img = cv2.imread(str(proc_path))
                if old_img is not None and curr_img is not None:
                    h_old = compute_perceptual_hash(old_img)
                    h_curr = compute_perceptual_hash(curr_img)
                    if is_duplicate_scan_frame(h_old, h_curr, max_hamming_distance=5):
                        logger.info("Retake has lower sharpness than existing identical frame; keeping existing")
                        return existing_rec

    questions_on_page = [q for q in (session.get("questions") or []) if int(q.get("page_number", 1)) == int(page_no)]
    cropped_qids: List[str] = []
    for q in questions_on_page:
        qid = str(q.get("question_id", "Q1"))
        crop_path = crops_dir / f"{qid}.jpg"
        try:
            await asyncio.to_thread(crop_question_from_page, proc_path, crop_path, roll_no, student_name, q)
            cropped_qids.append(qid)
        except Exception as exc:
            logger.warning("Could not crop %s for %s: %s", qid, roll_no, exc)

    record = save_scanned_page_record(
        session_id=session_id,
        roll_no=roll_no,
        page_no=page_no,
        raw_path=str(raw_path),
        processed_path=str(proc_path),
        blur_score=cv_info["blur_score"],
        is_sharp=cv_info["is_sharp"],
        file_size_bytes=len(raw_bytes),
        sha256=sha256_hex,
        is_retake=is_retake,
    )
    exp_pages = int(session.get("expected_pages") or 1)
    supp_info = format_supplementary_page_info(page_no, exp_pages)
    record["is_extra_sheet"] = supp_info["is_extra_sheet"]
    record["page_label"] = supp_info["label"]
    record["was_warped"] = cv_info["was_warped"]
    record["cropped_questions"] = cropped_qids
    record["image_url"] = f"/api/sessions/{session_id}/students/{roll_no}/pages/{page_no}/image"
    ta_tag = f"TA {ta_name}: " if ta_name else ""
    _push_event(
        "scan",
        f"{ta_tag}{'Retook' if is_retake else 'Scanned'} Roll {roll_no} · Page {page_no} (blur={cv_info['blur_score']}, crops={','.join(cropped_qids) or 'none'})",
        session_id=session_id,
        roll_no=roll_no,
        page_no=page_no,
        ta_name=ta_name,
    )
    return record


@app.post("/api/sessions/{session_id}/upload-page")
async def api_upload_page_direct(
    session_id: str,
    roll_no: str = Form(...),
    student_name: str = Form(default=""),
    page_no: int = Form(...),
    is_retake: bool = Form(default=False),
    ta_name: str = Form(default=""),
    file: UploadFile = File(...),
    _auth: Any = Depends(require_auth),
) -> Dict[str, Any]:
    """Direct single-page multipart photo or PDF upload (with automatic OpenCV flattening & sharpening)."""
    data = await file.read()
    if not data:
        raise HTTPException(400, "Uploaded file is empty.")
    if len(data) > settings.max_single_file_bytes:
        raise HTTPException(413, "File exceeds maximum single-page file size limit.")

    # Automatically render requested page from PDF if a PDF file was uploaded
    if is_pdf_buffer(data) or (file.filename and file.filename.lower().endswith(".pdf")):
        try:
            rendered = render_pdf_to_images(data, scale=1.0)
            target_idx = max(0, min(page_no - 1, len(rendered) - 1))
            data = rendered[target_idx][1]
        except Exception as exc:
            raise HTTPException(400, f"Failed to render uploaded PDF page: {exc}")

    record = await _ingest_complete_page_bytes(session_id, roll_no, student_name, page_no, data, is_retake=is_retake, ta_name=ta_name)
    return {"status": "saved", "page": record}


@app.post("/api/sessions/{session_id}/upload-bulk")
async def api_upload_bulk(
    session_id: str,
    files: List[UploadFile] = File(...),
    roll_no: Optional[str] = Form(default=None),
    student_name: Optional[str] = Form(default=""),
    ta_name: str = Form(default=""),
    _auth: Any = Depends(require_auth),
) -> Dict[str, Any]:
    """Bulk upload endpoint accepting multiple student PDFs, multi-page PDFs, images, or ZIP archives."""
    session = get_exam_session(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found.")

    spooled: List[Tuple[str, bytes]] = []
    for f in files:
        content = await f.read()
        if content:
            spooled.append((f.filename or "file", content))

    if not spooled:
        raise HTTPException(400, "No valid files received.")

    extracted_students = await asyncio.to_thread(process_bulk_exam_files, spooled, roll_no)

    saved_records = []
    for s_roll, page_tuples in extracted_students.items():
        s_name = student_name if (roll_no and s_roll == roll_no) else ""
        upsert_session_student(session_id, s_roll, s_name)
        for pno, jpg_bytes, orig_src in page_tuples:
            rec = await _ingest_complete_page_bytes(
                session_id=session_id,
                roll_no=s_roll,
                student_name=s_name,
                page_no=pno,
                raw_bytes=jpg_bytes,
                is_retake=False,
                ta_name=ta_name or "Bulk Upload",
            )
            saved_records.append({
                "roll_no": s_roll,
                "page_no": pno,
                "blur_score": rec.get("blur_score", 0.0),
                "is_sharp": rec.get("is_sharp", True),
                "source": orig_src,
            })

    _push_event(
        "bulk_upload",
        f"Bulk ingested {len(saved_records)} pages across {len(extracted_students)} students from {len(files)} files",
        session_id=session_id,
        ta_name=ta_name,
    )
    return {
        "status": "ok",
        "session_id": session_id,
        "total_files": len(files),
        "total_students": len(extracted_students),
        "total_pages": len(saved_records),
        "students": list(extracted_students.keys()),
        "records": saved_records,
    }


@app.post("/api/sessions/{session_id}/upload-chunk")
async def api_upload_page_chunk(
    session_id: str,
    req: ChunkUploadRequest,
    _auth: Any = Depends(require_auth),
) -> Dict[str, Any]:
    """Failure-resistant 64 KB chunked background sync endpoint for offline-first mobile scanning."""
    try:
        chunk_bytes = base64.b64decode(req.chunk_base64)
    except Exception:
        raise HTTPException(400, "Invalid base64 chunk payload.")

    key = f"{session_id}:{req.upload_id}"
    assembled: Optional[bytes] = None
    now = time.monotonic()
    async with _CHUNK_LOCK:
        # TTL eviction: prune chunk buffers older than 15 minutes (900s)
        stale_keys = [k for k, ts in _CHUNK_BUFFERS_TIMESTAMPS.items() if now - ts > 900]
        for sk in stale_keys:
            _CHUNK_BUFFERS.pop(sk, None)
            _CHUNK_BUFFERS_TIMESTAMPS.pop(sk, None)

        buf = _CHUNK_BUFFERS.setdefault(key, {})
        _CHUNK_BUFFERS_TIMESTAMPS[key] = now
        buf[req.chunk_index] = chunk_bytes
        received_count = len(buf)
        if received_count == req.total_chunks:
            assembled = b"".join(buf[i] for i in range(req.total_chunks) if i in buf)
            _CHUNK_BUFFERS.pop(key, None)
            _CHUNK_BUFFERS_TIMESTAMPS.pop(key, None)

    if assembled is None:
        return {
            "status": "chunk_received",
            "upload_id": req.upload_id,
            "chunk_index": req.chunk_index,
            "received_chunks": received_count,
            "total_chunks": req.total_chunks,
        }

    if req.sha256_full:
        actual_sha = hashlib.sha256(assembled).hexdigest()
        if actual_sha.lower() != req.sha256_full.lower():
            raise HTTPException(400, "SHA-256 mismatch on reassembled page chunks; please retry upload.")

    record = await _ingest_complete_page_bytes(
        session_id=session_id,
        roll_no=req.roll_no,
        student_name=req.student_name,
        page_no=req.page_no,
        raw_bytes=assembled,
        is_retake=req.is_retake,
        ta_name=req.ta_name or "",
    )
    return {
        "status": "page_complete",
        "upload_id": req.upload_id,
        "page": record,
    }


@app.get("/api/sessions/{session_id}/students/{roll_no}/pages/{page_no}/image")
def api_get_page_image(
    session_id: str,
    roll_no: str,
    page_no: int,
    raw: bool = False,
    half: Optional[str] = None,
    _auth: Any = Depends(require_auth),
) -> FileResponse:
    rec = get_scanned_page_record(session_id, roll_no, page_no)
    if not rec:
        raise HTTPException(404, "Scanned page not found.")
    proc_p = Path(rec.get("processed_path") or "")
    if half and proc_p.is_file():
        h_name = f"page_{page_no}_top.jpg" if half.lower() in ("top", "1", "half_1") else f"page_{page_no}_bottom.jpg"
        h_path = proc_p.with_name(h_name)
        if h_path.is_file():
            return FileResponse(h_path, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})
    target = Path(rec["raw_path"] if raw or not rec.get("processed_path") else rec["processed_path"])
    if not target.is_file():
        target = Path(rec["raw_path"])
    if not target.is_file():
        raise HTTPException(404, "Page image file missing on disk.")
    return FileResponse(target, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})


@app.post("/api/sessions/{session_id}/students/{roll_no}/pages/{page_no}/rotate")
async def api_rotate_student_page(
    session_id: str,
    roll_no: str,
    page_no: int,
    angle: int = 90,
    _auth: Any = Depends(require_auth),
) -> Dict[str, Any]:
    """Rotate an existing scanned student page by 90/180/270 degrees and regenerate processed halves and crops."""
    session = get_exam_session(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found.")

    rec = get_scanned_page_record(session_id, roll_no, page_no)
    if not rec:
        raise HTTPException(404, f"Page {page_no} for student '{roll_no}' not found.")

    raw_path = Path(rec.get("raw_path") or "")
    proc_path = Path(rec.get("processed_path") or "")
    if not raw_path.is_file():
        raise HTTPException(404, "Raw page image file missing on disk.")

    img = cv2.imread(str(raw_path))
    if img is None:
        raise HTTPException(400, "Could not decode page image for rotation.")

    rot_flag = cv2.ROTATE_90_CLOCKWISE
    if angle == 180:
        rot_flag = cv2.ROTATE_180
    elif angle in (270, -90):
        rot_flag = cv2.ROTATE_90_COUNTERCLOCKWISE

    rotated = cv2.rotate(img, rot_flag)
    cv2.imwrite(str(raw_path), rotated, [cv2.IMWRITE_JPEG_QUALITY, 92])

    cv_info = await asyncio.to_thread(process_and_save_page, raw_path, proc_path)

    actual_roll = rec.get("roll_no") or roll_no
    safe_sid = _safe_segment(session_id)
    safe_roll = _safe_segment(actual_roll)
    crops_dir = _session_storage_root(session) / safe_sid / safe_roll / "crops"
    questions_on_page = [q for q in (session.get("questions") or []) if int(q.get("page_number", 1)) == int(page_no)]
    if not questions_on_page and int(page_no) == 1:
        crop_path = crops_dir / "Q1.jpg"
        try:
            top_half = proc_path.parent / "page_1_top.jpg"
            if top_half.is_file():
                shutil.copy2(top_half, crop_path)
            elif proc_path.is_file():
                shutil.copy2(proc_path, crop_path)
        except Exception:
            pass
    for q in questions_on_page:
        qid = str(q.get("question_id", "Q1"))
        crop_path = crops_dir / f"{qid}.jpg"
        try:
            await asyncio.to_thread(crop_question_from_page, proc_path, crop_path, roll_no, rec.get("student_name", ""), q)
        except Exception:
            pass

    updated_rec = save_scanned_page_record(
        session_id=session_id,
        roll_no=roll_no,
        page_no=page_no,
        raw_path=str(raw_path),
        processed_path=str(proc_path),
        blur_score=cv_info["blur_score"],
        is_sharp=cv_info["is_sharp"],
        file_size_bytes=raw_path.stat().st_size,
        sha256=hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        is_retake=rec.get("is_retake", False),
    )
    _push_event(
        "rotate",
        f"Rotated Roll {roll_no} · Page {page_no} by {angle}°",
        session_id=session_id,
        roll_no=roll_no,
        page_no=page_no,
    )
    return {"status": "ok", "message": f"Rotated page {page_no} by {angle}°", "page": updated_rec}


@app.get("/api/sessions/{session_id}/students/{roll_no}/questions/{question_id}/crop")
def api_get_question_crop_image(
    session_id: str,
    roll_no: str,
    question_id: str,
    _auth: Any = Depends(require_auth),
) -> FileResponse:
    session = get_exam_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found.")
    safe_sid = _safe_segment(session_id)
    safe_roll = _safe_segment(roll_no)
    crops_dir = _session_storage_root(session) / safe_sid / safe_roll / "crops"
    crop_path = crops_dir / f"{_safe_segment(question_id)}.jpg"
    if not crop_path.is_file():
        # Check case-insensitive question id match in crops directory
        if crops_dir.is_dir():
            target_q = question_id.strip().casefold()
            for cf in crops_dir.glob("*.jpg"):
                if cf.stem.casefold() == target_q:
                    crop_path = cf
                    break
    if not crop_path.is_file():
        # Fall back to processed full page 1 or raw page 1 for single-page exams
        p1 = _session_storage_root(session) / safe_sid / safe_roll / "processed" / "page_1_flattened.jpg"
        if p1.is_file():
            return FileResponse(p1, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})
        p1_raw = _session_storage_root(session) / safe_sid / safe_roll / "raw" / "page_1.jpg"
        if p1_raw.is_file():
            return FileResponse(p1_raw, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})
        raise HTTPException(404, "Cropped question image not found.")
    return FileResponse(crop_path, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})


async def _run_session_async_grading(
    session_id: str,
    only_roll_no: Optional[str] = None,
    only_question_id: Optional[str] = None,
    force_recrop: bool = False,
) -> None:
    """Background task: ensure all question crops exist and batch-evaluate them concurrently with Prompt Caching."""
    started_ts = time.perf_counter()
    try:
        session = get_exam_session(session_id)
        if not session:
            return
        questions = session.get("questions") or []
        if only_question_id:
            questions = [q for q in questions if str(q.get("question_id")) == only_question_id]
        if not questions:
            questions = [{
                "question_id": "Q1",
                "title": "Complete Exam",
                "max_marks": 20.0,
                "page_number": 1,
                "problem_statement": "Evaluate the student solution on the answer sheets.",
                "grading_rubric": "Step 1: Conceptual accuracy and problem setup\nStep 2: Method and derivations\nStep 3: Final result and units",
            }]
        students = session.get("students") or []
        if only_roll_no:
            students = [s for s in students if s["roll_no"] == only_roll_no]

        if not students:
            update_exam_session(session_id, status="ACTIVE", grading_progress=1.0, grading_stage="No student pages to grade yet")
            return

        telemetry: Dict[str, Any] = {
            "model": settings.primary_model,
            "concurrency": settings.llm_concurrency,
            "batch_size": settings.llm_vision_batch_size,
            "calls": 0,
            "ok_calls": 0,
            "submissions_reviewed": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "elapsed_seconds": 0.0,
        }
        update_exam_session(
            session_id,
            status="GRADING",
            grading_progress=0.04,
            grading_stage="Cropping & sharpening designated question spaces…",
            llm_telemetry=telemetry,
        )
        safe_sid = _safe_segment(session_id)
        s_root = _session_storage_root(session)

        # Whole-exam evaluation with Question Paper JSON & 2N photos for N pages per student
        has_custom_bounding_boxes = any(q.get("boxes") and len(q.get("boxes")) > 1 for q in questions)
        use_whole_page_eval = not has_custom_bounding_boxes

        if use_whole_page_eval:
            target_questions = [q for q in questions if str(q.get("question_id")) == str(only_question_id)] if only_question_id else questions
            update_exam_session(
                session_id,
                status="GRADING",
                grading_progress=0.08,
                grading_stage="Preparing student submission images (2N views for N sheets) & caching Question Paper JSON…",
                llm_telemetry=telemetry,
            )

            # Ensure every question has a valid crop image ready on disk for UI inspection
            for s in students:
                roll = s["roll_no"]
                safe_roll = _safe_segment(roll)
                student_dir = s_root / safe_sid / safe_roll
                proc_page = student_dir / "processed" / "page_1_top.jpg"
                if not proc_page.is_file():
                    proc_page = student_dir / "processed" / "page_1_flattened.jpg"
                if not proc_page.is_file():
                    proc_page = student_dir / "raw" / "page_1.jpg"
                crops_dir = student_dir / "crops"
                crops_dir.mkdir(parents=True, exist_ok=True)
                for q in target_questions:
                    qid = str(q["question_id"])
                    pno = int(q.get("page_number", 1) or 1)
                    q_page = student_dir / "processed" / f"page_{pno}_top.jpg"
                    if not q_page.is_file():
                        q_page = student_dir / "processed" / f"page_{pno}_flattened.jpg"
                    if not q_page.is_file():
                        q_page = student_dir / "raw" / f"page_{pno}.jpg"
                    if not q_page.is_file():
                        q_page = proc_page
                    crop_path = crops_dir / f"{qid}.jpg"
                    if (force_recrop or not crop_path.is_file()) and q_page and q_page.is_file():
                        try:
                            shutil.copy2(q_page, crop_path)
                        except Exception:
                            pass

            total_items = max(1, len(students) * len(target_questions))
            completed_items = 0

            def _on_whole_page_item_done(n: int, q_rep: Dict[str, Any]) -> None:
                nonlocal completed_items
                completed_items += n
                frac = min(0.98, round(0.10 + 0.88 * (completed_items / total_items), 3))
                telemetry["elapsed_seconds"] = round(time.perf_counter() - started_ts, 2)
                update_session_progress(
                    session_id,
                    status="GRADING",
                    grading_progress=frac,
                    grading_stage=f"Concurrent multimodal AI grading ({completed_items}/{total_items} questions · Cache Read: {telemetry['cache_read_input_tokens'] + q_rep.get('cache_read_input_tokens', 0)} tok)",
                    llm_telemetry=telemetry,
                )

            wp_report = await evaluate_session_whole_pages(
                session_id=session_id,
                course_code=session["course_code"],
                exam_name=session["exam_name"],
                questions=target_questions,
                students=students,
                storage_root=s_root,
                progress_cb=_on_whole_page_item_done,
                paper_type=session.get("paper_type", "COMBINED_QUESTION_ANSWER"),
                question_paper_image_path=session.get("question_paper_image_path"),
            )
            for k in ("calls", "ok_calls", "submissions_reviewed", "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
                telemetry[k] = telemetry.get(k, 0) + int(wp_report.get(k, 0) or 0)
        else:
            # Multi-page slice and crop evaluation
            crops_by_q: Dict[str, List[Dict[str, Any]]] = {str(q["question_id"]): [] for q in questions}
            crop_jobs: List[Tuple[Any, Tuple[Any, ...]]] = []

            for s in students:
                roll = s["roll_no"]
                sname = s.get("student_name", "")
                safe_roll = _safe_segment(roll)
                student_dir = s_root / safe_sid / safe_roll
                proc_dir = student_dir / "processed"
                crops_dir = student_dir / "crops"
                crops_dir.mkdir(parents=True, exist_ok=True)

                for q in questions:
                    qid = str(q["question_id"])
                    crop_path = crops_dir / f"{qid}.jpg"
                    boxes = q.get("boxes") or []
                    spanned = q.get("spans_pages") or []
                    cont_pages = q.get("continuation_pages") or []

                    if force_recrop or not crop_path.is_file():
                        if boxes and len(boxes) > 1:
                            multi_slices = []
                            for b in boxes:
                                bpno = int(b.get("page_number") or b.get("page_no") or 1)
                                bp_file = proc_dir / f"page_{bpno}_flattened.jpg"
                                if not bp_file.is_file():
                                    raw_cand = raw_dir / f"page_{bpno}.jpg"
                                    if raw_cand.is_file():
                                        bp_file = raw_cand
                                if bp_file.is_file():
                                    multi_slices.append((bpno, bp_file, b))
                            if multi_slices:
                                crop_jobs.append((crop_multipage_question, (multi_slices, crop_path, roll, sname, q)))
                        elif (spanned and len(spanned) > 1) or cont_pages:
                            all_pnos = sorted(list(set([int(q.get("page_number", 1))] + [int(p) for p in spanned] + [int(p) for p in cont_pages])))
                            multi_slices = []
                            for idx, bpno in enumerate(all_pnos):
                                bp_file = proc_dir / f"page_{bpno}_flattened.jpg"
                                if not bp_file.is_file():
                                    raw_cand = raw_dir / f"page_{bpno}.jpg"
                                    if raw_cand.is_file():
                                        bp_file = raw_cand
                                if bp_file.is_file():
                                    box_spec = {
                                        "y_start_pct": float(q.get("y_start_pct", 0.0)) if idx == 0 else 0.0,
                                        "y_end_pct": 100.0 if idx < len(all_pnos) - 1 else float(q.get("y_end_pct", 100.0)),
                                        "x_start_pct": float(q.get("x_start_pct", 0.0)),
                                        "x_end_pct": float(q.get("x_end_pct", 100.0)),
                                        "title": f"Page {bpno} Continuation" if idx > 0 else f"Page {bpno} Primary",
                                    }
                                    multi_slices.append((bpno, bp_file, box_spec))
                            if multi_slices:
                                crop_jobs.append((crop_multipage_question, (multi_slices, crop_path, roll, sname, q)))
                            else:
                                crop_jobs.append((crop_multipage_question, ([], crop_path, roll, sname, q)))
                        else:
                            pno = int(q.get("page_number", 1))
                            proc_page = proc_dir / f"page_{pno}_flattened.jpg"
                            if not proc_page.is_file():
                                raw_cand = raw_dir / f"page_{pno}.jpg"
                                if raw_cand.is_file():
                                    proc_page = raw_cand
                            if proc_page.is_file():
                                crop_jobs.append((crop_question_from_page, (proc_page, crop_path, roll, sname, q)))
                            else:
                                crop_jobs.append((crop_multipage_question, ([], crop_path, roll, sname, q)))

            # Execute all required slicing concurrently across CPU worker threads
            if crop_jobs:
                crop_sem = asyncio.Semaphore(min(12, max(4, (os.cpu_count() or 4) * 2)))

                async def _exec_crop(fn: Any, a: Tuple[Any, ...]) -> None:
                    async with crop_sem:
                        try:
                            await asyncio.to_thread(fn, *a)
                        except Exception as exc:
                            logger.warning("Parallel crop failed: %s", exc)

                await asyncio.gather(*(_exec_crop(fn, a) for fn, a in crop_jobs))

            # Assemble valid student crops per question
            for s in students:
                roll = s["roll_no"]
                sname = s.get("student_name", "")
                safe_roll = _safe_segment(roll)
                crops_dir = s_root / safe_sid / safe_roll / "crops"
                for q in questions:
                    qid = str(q["question_id"])
                    crop_path = crops_dir / f"{qid}.jpg"
                    if crop_path.is_file():
                        crops_by_q[qid].append({
                            "roll_no": roll,
                            "student_name": sname,
                            "crop_path": str(crop_path),
                        })

            total_items = max(1, sum(len(v) for v in crops_by_q.values()))
            completed_items = 0
            shared_sem = asyncio.Semaphore(max(1, settings.llm_concurrency))

            async def _grade_single_question(q_spec: Dict[str, Any]) -> Dict[str, Any]:
                qid = str(q_spec["question_id"])
                q_crops = crops_by_q.get(qid) or []
                if not q_crops:
                    return {
                        "calls": 0, "ok_calls": 0, "submissions_reviewed": 0,
                        "input_tokens": 0, "output_tokens": 0,
                        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                    }

                def _on_item_done(n: int, q_rep: Dict[str, Any]) -> None:
                    nonlocal completed_items
                    completed_items += n
                    frac = min(0.98, round(0.10 + 0.88 * (completed_items / total_items), 3))
                    telemetry["elapsed_seconds"] = round(time.perf_counter() - started_ts, 2)
                    update_session_progress(
                        session_id,
                        status="GRADING",
                        grading_progress=frac,
                        grading_stage=f"Concurrent {settings.primary_model} grading ({completed_items}/{total_items} crops · Cache Read: {telemetry['cache_read_input_tokens'] + q_rep.get('cache_read_input_tokens', 0)} tok)",
                        llm_telemetry=telemetry,
                    )

                return await evaluate_session_question_crops(
                    session_id=session_id,
                    course_code=session["course_code"],
                    exam_name=session["exam_name"],
                    question=q_spec,
                    student_crops=q_crops,
                    progress_cb=_on_item_done,
                    semaphore=shared_sem,
                    paper_type=session.get("paper_type", "COMBINED_QUESTION_ANSWER"),
                )

            # Fire all question evaluations concurrently through the shared concurrency pool
            q_reports = await asyncio.gather(*(_grade_single_question(q) for q in questions))
            for q_report in q_reports:
                for k in ("calls", "ok_calls", "submissions_reviewed", "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
                    telemetry[k] = telemetry.get(k, 0) + int(q_report.get(k, 0) or 0)

        fresh_session = get_exam_session(session_id) or session
        all_session_questions = fresh_session.get("questions") or []
        for s in students:
            s_roll = s["roll_no"]
            evs = get_student_question_evaluations(session_id, s_roll)
            total_expected_q = len(all_session_questions)
            if total_expected_q > 0 and len(evs) >= total_expected_q:
                status_to_set = "GRADED"
            elif len(evs) > 0:
                status_to_set = "PARTIALLY_GRADED"
            else:
                status_to_set = "GRADING_FAILED"
            upsert_session_student(session_id, s_roll, s.get("student_name", ""), finalized=True, grading_status=status_to_set)

        telemetry["elapsed_seconds"] = round(time.perf_counter() - started_ts, 2)
        try:
            record_global_llm_usage(
                model=telemetry.get("model") or settings.primary_model,
                calls=int(telemetry.get("calls", 0)),
                ok_calls=int(telemetry.get("ok_calls", 0)),
                failed_calls=max(0, int(telemetry.get("calls", 0)) - int(telemetry.get("ok_calls", 0))),
                submissions_reviewed=int(telemetry.get("submissions_reviewed", 0)),
                input_tokens=int(telemetry.get("input_tokens", 0)),
                cache_creation_input_tokens=int(telemetry.get("cache_creation_input_tokens", 0)),
                cache_read_input_tokens=int(telemetry.get("cache_read_input_tokens", 0)),
                output_tokens=int(telemetry.get("output_tokens", 0)),
                elapsed_seconds=float(telemetry.get("elapsed_seconds", 0.0)),
            )
        except Exception as _t_err:
            logger.warning("Could not persist global LLM usage: %s", _t_err)

        if only_roll_no:
            all_st = fresh_session.get("students") or []
            total_q = len(all_session_questions)
            graded_count = sum(
                1 for st_item in all_st
                if len(get_student_question_evaluations(session_id, st_item["roll_no"])) >= total_q
            ) if total_q > 0 else len(all_st)
            total_st = max(1, len(all_st))
            frac = round(graded_count / total_st, 2)
            all_done = (graded_count >= len(all_st)) and len(all_st) > 0
            update_exam_session(
                session_id,
                status="REVIEW_READY" if all_done else "ACTIVE",
                grading_progress=1.0 if all_done else frac,
                grading_stage=f"Student {only_roll_no} graded ({graded_count}/{total_st} students complete)",
                llm_telemetry=telemetry,
            )
        else:
            update_exam_session(
                session_id,
                status="REVIEW_READY",
                grading_progress=1.0,
                grading_stage=f"Evaluation complete in {telemetry['elapsed_seconds']}s ({telemetry['submissions_reviewed']} evaluations recorded)",
                llm_telemetry=telemetry,
            )
        _push_event(
            "grading",
            f"Completed concurrent {settings.primary_model} grading for {session_id} in {telemetry['elapsed_seconds']}s",
            session_id=session_id,
        )
    except Exception:
        logger.exception("Async session grading failed for %s", session_id)
        for s in students:
            try:
                s_roll = s["roll_no"]
                evs = get_student_question_evaluations(session_id, s_roll)
                total_expected_q = len(session.get("questions") or [])
                if total_expected_q > 0 and len(evs) >= total_expected_q:
                    status_to_set = "GRADED"
                elif len(evs) > 0:
                    status_to_set = "PARTIALLY_GRADED"
                else:
                    status_to_set = "GRADING_FAILED"
                upsert_session_student(session_id, s_roll, s.get("student_name", ""), finalized=True, grading_status=status_to_set)
            except Exception:
                pass
        update_exam_session(session_id, status="ERROR", grading_stage="Grading encountered an unexpected error")


@app.post("/api/sessions/{session_id}/finalize-student")
async def api_finalize_student(
    session_id: str,
    req: FinalizeStudentRequest,
    _auth: Any = Depends(require_auth),
) -> Dict[str, Any]:
    """Mark a student's paper upload as finalized and optionally trigger background AI grading."""
    session = get_exam_session(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found.")
    st = upsert_session_student(session_id, req.roll_no, req.student_name, finalized=True)

    if req.ta_name:
        record_active_ta(session_id, req.ta_name)

    # Audit student's scanned pages & verify packet consistency
    st_pages = [p for p in (session.get("scanned_pages") or []) if str(p.get("roll_no", "")).strip().casefold() == req.roll_no.strip().casefold()]
    expected_page_count = int(session.get("expected_pages") or len(st_pages) or 1)
    _, missing_pages, audit_warnings = reorder_and_audit_scanned_pages(st_pages, expected_page_count)
    is_consistent, consistency_warnings = verify_packet_student_consistency(st_pages, req.roll_no)
    audit_warnings.extend(consistency_warnings)

    ta_tag = f"TA {req.ta_name}: " if req.ta_name else ""
    _push_event("finalize", f"{ta_tag}Finalized student {req.roll_no} ({req.student_name})", session_id=session_id, roll_no=req.roll_no, ta_name=req.ta_name or "")
    if req.trigger_grading:
        if session_id not in _session_grading_queues:
            _session_grading_queues[session_id] = asyncio.Queue()
        _session_grading_queues[session_id].put_nowait((req.roll_no, None, False))
        _ensure_session_grading_worker(session_id)
    return {"status": "finalized", "student": st, "missing_pages": missing_pages, "audit_warnings": audit_warnings}


@app.post("/api/sessions/{session_id}/evaluate")
async def api_trigger_session_evaluation(
    session_id: str,
    req: Optional[EvaluateSessionRequest] = Body(default=None),
    _auth: Any = Depends(require_auth),
) -> Dict[str, Any]:
    """Trigger concurrent Claude batch evaluation with Prompt Caching across all uploaded answer books."""
    session = get_exam_session(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found.")

    only_roll = req.only_roll_no if req else None
    only_qid = req.only_question_id if req else None
    force_recrop = bool(req.force_regrade) if req else False

    if only_roll:
        if session_id not in _session_grading_queues:
            _session_grading_queues[session_id] = asyncio.Queue()
        _session_grading_queues[session_id].put_nowait((only_roll, only_qid, force_recrop))
        _ensure_session_grading_worker(session_id)
        return {
            "status": "started",
            "session_id": session_id,
            "roll_no": only_roll,
            "model": settings.primary_model,
        }

    full_task_key = f"full_session:{session_id}"
    if full_task_key in _background_tasks and not _background_tasks[full_task_key].done():
        return {
            "status": "already_grading",
            "session_id": session_id,
            "message": "AI grading is already active for this session.",
        }

    update_exam_session(session_id, status="GRADING", grading_progress=0.02, grading_stage="Warming up Prompt Cache & starting concurrent evaluation…")
    task = asyncio.create_task(_run_session_async_grading(session_id, only_roll_no=None, only_question_id=only_qid, force_recrop=force_recrop))
    _background_tasks[full_task_key] = task
    _background_tasks[session_id] = task
    task.add_done_callback(lambda _t, k=full_task_key, sid=session_id: (_background_tasks.pop(k, None), _background_tasks.pop(sid, None)))
    return {
        "status": "started",
        "session_id": session_id,
        "model": settings.primary_model,
        "concurrency": settings.llm_concurrency,
        "vision_batch_size": settings.llm_vision_batch_size,
    }


@app.post("/api/sessions/{session_id}/override")
def api_override_question_grade(
    session_id: str,
    req: TAOverrideGradeRequest,
    _auth: Any = Depends(require_auth),
) -> Dict[str, Any]:
    """Allow TA to approve or override the marks, queue category, and comment for a student's question."""
    if req.ta_id:
        record_active_ta(session_id, req.ta_id)
    updated = apply_ta_grade_override(
        session_id=session_id,
        roll_no=req.roll_no,
        question_id=req.question_id,
        ta_status=req.ta_status,
        awarded_marks=req.awarded_marks,
        verdict_tab=req.verdict_tab,
        ta_comment=req.ta_comment,
    )
    if not updated:
        raise HTTPException(404, "Question evaluation record not found.")
    return {"status": "updated", "evaluation": updated}


@app.post("/api/sessions/{session_id}/approve-all-green")
def api_approve_all_green(session_id: str, _auth: Any = Depends(require_auth)) -> Dict[str, Any]:
    """One-click bulk approval for all high-confidence GREEN/BLUE evaluations in the session."""
    count = bulk_approve_session_green(session_id)
    _push_event("approve", f"Bulk-approved {count} clean GREEN/BLUE question evaluations", session_id=session_id)
    return {"status": "ok", "approved_count": count}


@app.get("/api/sessions/{session_id}/export.xlsx")
def api_export_session_excel(session_id: str, _auth: Any = Depends(require_auth)) -> Response:
    """Export the session database as a clean, multi-sheet Excel (.xlsx) workbook with student details, marks, and comments."""
    session = get_exam_session(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found.")
    xlsx_bytes = build_session_excel_bytes(session)
    safe_name = re.sub(r"[^a-zA-Z0-9_\-]+", "_", f"{session['course_code']}_{session['exam_name']}_{session_id}").strip("_")
    # Production archival: save local copy in session exports folder on laptop
    try:
        exp_dir = _session_storage_root(session) / "exports"
        exp_dir.mkdir(parents=True, exist_ok=True)
        (exp_dir / f"{safe_name}_gradebook.xlsx").write_bytes(xlsx_bytes)
        logger.info("Archived gradebook copy to %s", exp_dir / f"{safe_name}_gradebook.xlsx")
    except Exception as exc:
        logger.warning("Could not write archive copy of Excel export: %s", exc)
    headers = {
        "Content-Disposition": f'attachment; filename="{safe_name}_gradebook.xlsx"',
        "Cache-Control": "no-cache",
    }
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )


# --------------------------------------------------------------------------- Strategic Feature Additions: V6.0 Roadmap
@app.get("/api/sessions/{session_id}/template.pdf")
def api_download_booklet_template(
    session_id: str,
    roll_no: Optional[str] = Query(default=None),
    _auth: Any = Depends(require_auth),
) -> Response:
    """Generate a print-ready A4 exam booklet PDF with 4 corner ArUco markers, header QR, and question boxes."""
    session = get_exam_session(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found.")
    pdf_bytes = generate_booklet_pdf(
        session_id=session_id,
        course_code=session.get("course_code", "COURSE"),
        exam_name=session.get("exam_name", "Exam"),
        exam_date=session.get("exam_date", ""),
        total_marks=float(session.get("total_marks", 100.0) or 100.0),
        expected_pages=int(session.get("expected_pages", 4) or 4),
        questions=session.get("questions", []),
        roll_no=roll_no,
    )
    safe_name = re.sub(r"[^a-zA-Z0-9_\-]+", "_", f"{session['course_code']}_{session['exam_name']}_Booklet").strip("_")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.pdf"', "Cache-Control": "no-cache"},
    )


@app.post("/api/booklet/preview.pdf")
def api_preview_booklet_pdf(req: CreateSessionRequest, _auth: Any = Depends(require_auth)) -> Response:
    """Generate print-ready A4 booklet PDF preview from current form values without requiring session creation first."""
    q_list = [q.model_dump() for q in req.questions] if req.questions else []
    pdf_bytes = generate_booklet_pdf(
        session_id="PREVIEW",
        course_code=req.course_code or "DSE-201",
        exam_name=req.exam_name or "Exam",
        exam_date=req.exam_date or "",
        total_marks=float(req.total_marks or 20.0),
        expected_pages=int(req.expected_pages or 1),
        questions=q_list,
        roll_no=None,
    )
    safe_name = re.sub(r"[^a-zA-Z0-9_\-]+", "_", f"{req.course_code or 'DSE'}_{req.exam_name or 'Exam'}_Booklet").strip("_")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.pdf"', "Cache-Control": "no-cache"},
    )


@app.get("/api/sessions/{session_id}/plagiarism")
def api_paper_session_plagiarism(
    session_id: str,
    threshold: float = Query(default=85.0),
    _auth: Any = Depends(require_auth),
) -> Dict[str, Any]:
    """Detect text/LaTeX plagiarism and shared wrong-answer collusion across student paper submissions (disabled by default)."""
    if not settings.enable_paper_plagiarism:
        return {
            "status": "disabled",
            "message": "Handwritten paper exam plagiarism forensics is disabled.",
            "collusion_pairs": [],
            "shared_error_signatures": [],
            "high_risk_pairs": 0,
        }
    session = get_exam_session(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found.")
    evals = []
    for s in session.get("students", []):
        for ev in (s.get("evaluations") or {}).values():
            evals.append(ev)
    return run_paper_exam_plagiarism(session_id, evals, threshold_percent=threshold)


@app.post("/api/sessions/{session_id}/claim-next")
def api_claim_next_student(
    session_id: str,
    req: ClaimNextStudentRequest,
    _auth: Any = Depends(require_auth),
) -> Dict[str, Any]:
    """Atomically claim the next unreviewed student for a TA to avoid multi-TA review collisions."""
    claimed = claim_next_student(session_id, req.ta_id, filter_tab=req.filter_tab)
    if not claimed:
        return {"claimed": False, "student": None, "message": "No unreviewed students available to claim."}
    return {"claimed": True, "student": claimed}


@app.post("/api/sessions/{session_id}/release-claim")
def api_release_claim(
    session_id: str,
    req: ReleaseClaimRequest,
    _auth: Any = Depends(require_auth),
) -> Dict[str, Any]:
    """Release a student claim."""
    ok = release_student_claim(session_id, req.roll_no, req.ta_id)
    return {"released": ok}


@app.post("/api/sessions/{session_id}/students/{roll_no}/presence")
def api_student_presence(
    session_id: str,
    roll_no: str,
    req: PresenceRequest,
    _auth: Any = Depends(require_auth),
) -> Dict[str, Any]:
    """Heartbeat to refresh the lease on a student currently being reviewed by a TA."""
    ok = record_student_presence(session_id, roll_no, req.ta_id)
    return {"status": "ok" if ok else "not_claimed"}


@app.get("/api/sessions/{session_id}/analytics")
def api_session_analytics(
    session_id: str,
    _auth: Any = Depends(require_auth),
) -> Dict[str, Any]:
    """Compute exam statistics, item difficulty index P, and discrimination index D."""
    return get_exam_analytics(session_id)


@app.get("/api/sessions/{session_id}/events")
async def api_session_events(session_id: str, request: Request) -> StreamingResponse:
    """Server-Sent Events (SSE) stream for live grading progress and review queue updates."""
    async def event_generator():
        last_prog = None
        last_stage = None
        for _ in range(300):
            if await request.is_disconnected():
                break
            sess = get_exam_session(session_id)
            if sess:
                prog = sess.get("grading_progress", 0.0)
                stage = sess.get("grading_stage", "")
                if prog != last_prog or stage != last_stage:
                    last_prog = prog
                    last_stage = stage
                    event_data = {
                        "type": "progress",
                        "session_id": session_id,
                        "progress": prog,
                        "stage": stage,
                        "status": sess.get("status"),
                        "summary_counts": sess.get("summary_counts"),
                    }
                    yield f"data: {json.dumps(event_data)}\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/sessions/{session_id}/students/{roll_no}/feedback.pdf")
def api_student_feedback_pdf(
    session_id: str,
    roll_no: str,
    _auth: Any = Depends(require_auth),
) -> Response:
    """Generate stamped annotated feedback PDF for a single student."""
    session = get_exam_session(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found.")
    st = next((s for s in session.get("students", []) if str(s.get("roll_no", "")).strip().casefold() == roll_no.strip().casefold()), None)
    if not st:
        raise HTTPException(404, f"Student '{roll_no}' not found in session.")
    pdf_bytes = generate_student_feedback_pdf(session, st)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{roll_no}_Feedback.pdf"', "Cache-Control": "no-cache"},
    )


@app.get("/api/sessions/{session_id}/export-feedback-zip")
def api_export_feedback_zip(
    session_id: str,
    _auth: Any = Depends(require_auth),
) -> Response:
    """Export a ZIP archive containing annotated feedback PDFs for all students in the session."""
    session = get_exam_session(session_id)
    if not session:
        raise HTTPException(404, f"Session '{session_id}' not found.")
    zip_bytes = generate_feedback_zip(session)
    safe_name = re.sub(r"[^a-zA-Z0-9_\-]+", "_", f"{session['course_code']}_{session['exam_name']}_All_Feedback").strip("_")
    # Production archival: save local copy in session exports folder on laptop
    try:
        exp_dir = _session_storage_root(session) / "exports"
        exp_dir.mkdir(parents=True, exist_ok=True)
        (exp_dir / f"{safe_name}.zip").write_bytes(zip_bytes)
        logger.info("Archived feedback zip copy to %s", exp_dir / f"{safe_name}.zip")
    except Exception as exc:
        logger.warning("Could not write archive copy of feedback zip: %s", exc)
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.zip"', "Cache-Control": "no-cache"},
    )


@app.post("/api/sessions/{session_id}/students/{roll_no}/regrade-request")
def api_submit_regrade(
    session_id: str,
    roll_no: str,
    req: StudentRegradeRequest,
    _auth: Any = Depends(require_auth),
) -> Dict[str, Any]:
    """Submit a student regrade appeal."""
    req_id = create_regrade_request(
        session_id=session_id,
        roll_no=roll_no,
        question_id=req.question_id,
        student_reason=req.student_reason,
        claimed_marks=req.claimed_marks,
    )
    return {"status": "submitted", "request_id": req_id}


@app.get("/api/sessions/{session_id}/regrade-requests")
def api_get_regrades(
    session_id: str,
    status: Optional[str] = Query(default=None),
    _auth: Any = Depends(require_auth),
) -> List[Dict[str, Any]]:
    """List regrade requests for a session."""
    return list_regrade_requests(session_id, status=status)


@app.post("/api/sessions/{session_id}/regrade-requests/{request_id}/resolve")
def api_resolve_regrade(
    session_id: str,
    request_id: int,
    req: ResolveRegradeRequest,
    _auth: Any = Depends(require_auth),
) -> Dict[str, Any]:
    """Resolve a student regrade appeal and auto-override marks if accepted."""
    ok = resolve_regrade_request(request_id, status=req.status, ta_response=req.ta_response, granted_marks=req.granted_marks)
    if not ok:
        raise HTTPException(404, "Regrade request not found.")
    _push_event("regrade", f"Resolved regrade request #{request_id} as {req.status}", session_id=session_id)
    return {"status": "resolved", "request_id": request_id, "resolution": req.status}


def calculate_haiku_pricing(tokens_dict: Dict[str, Any], inr_rate: float = 86.50) -> Dict[str, Any]:
    inp = int(tokens_dict.get("input_tokens", 0) or 0)
    c_write = int(tokens_dict.get("cache_creation_input_tokens", 0) or 0)
    c_read = int(tokens_dict.get("cache_read_input_tokens", 0) or 0)
    out = int(tokens_dict.get("output_tokens", 0) or 0)

    cost_input = (inp * 0.80) / 1_000_000.0
    cost_c_write = (c_write * 1.00) / 1_000_000.0
    cost_c_read = (c_read * 0.08) / 1_000_000.0
    cost_output = (out * 4.00) / 1_000_000.0

    actual_usd = cost_input + cost_c_write + cost_c_read + cost_output
    gross_usd = ((inp + c_write + c_read) * 0.80 + out * 4.00) / 1_000_000.0
    saved_usd = max(0.0, gross_usd - actual_usd)
    saved_pct = round((saved_usd / gross_usd * 100.0), 1) if gross_usd > 0 else 0.0

    return {
        "rates": {
            "regular_input_m": 0.80,
            "cache_write_m": 1.00,
            "cache_read_m": 0.08,
            "output_m": 4.00,
            "usd_to_inr": inr_rate,
        },
        "breakdown_usd": {
            "regular_input": round(cost_input, 6),
            "cache_write": round(cost_c_write, 6),
            "cache_read": round(cost_c_read, 6),
            "output": round(cost_output, 6),
        },
        "actual_cost_usd": round(actual_usd, 5),
        "total_cost_usd": round(actual_usd, 5),
        "actual_cost_inr": round(actual_usd * inr_rate, 2),
        "total_cost_inr": round(actual_usd * inr_rate, 2),
        "gross_without_cache_usd": round(gross_usd, 5),
        "cost_without_caching_usd": round(gross_usd, 5),
        "gross_without_cache_inr": round(gross_usd * inr_rate, 2),
        "savings_usd": round(saved_usd, 5),
        "net_savings_usd": round(saved_usd, 5),
        "savings_inr": round(saved_usd * inr_rate, 2),
        "net_savings_inr": round(saved_usd * inr_rate, 2),
        "savings_percent": saved_pct,
        "savings_pct": saved_pct,
        "total_tokens": inp + c_write + c_read + out,
    }


def _enrich_llm_telemetry(t: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not t:
        return None
    enriched = dict(t)
    calls = int(t.get("calls", 0))
    c_read = int(t.get("cache_read_input_tokens", 0))
    inp = int(t.get("input_tokens", 0))
    c_write = int(t.get("cache_creation_input_tokens", 0))
    out = int(t.get("output_tokens", 0))
    total_in = inp + c_write + c_read
    hit_rate = round((c_read / total_in * 100.0), 1) if total_in > 0 else 0.0
    errs = int(t.get("failed_calls", 0))
    ok_calls = int(t.get("ok_calls", max(0, calls - errs)))
    success_rate = round((ok_calls / calls * 100.0), 1) if calls > 0 else 100.0
    elapsed = float(t.get("total_elapsed_seconds", 0.0))
    avg_lat = int(round((elapsed / calls * 1000.0))) if calls > 0 else 0
    enriched.update({
        "cache_hit_rate_pct": hit_rate,
        "total_cache_read_tokens": c_read,
        "total_cache_write_tokens": c_write,
        "total_input_tokens": inp,
        "total_output_tokens": out,
        "total_calls": calls,
        "total_errors": errs,
        "success_rate_pct": success_rate,
        "avg_latency_ms": avg_lat,
    })
    return enriched


@app.get("/api/haiku-usage")
def api_get_haiku_usage(
    session_id: Optional[str] = Query(default=None),
    _auth: Any = Depends(require_auth),
) -> Dict[str, Any]:
    """Retrieve comprehensive token telemetry, prompt caching savings, and cost accounting for Claude Haiku 5.5."""
    global_telem = get_global_llm_usage()
    global_cost = calculate_haiku_pricing(global_telem)

    session_telem: Optional[Dict[str, Any]] = None
    session_cost: Optional[Dict[str, Any]] = None
    if session_id:
        sess = get_exam_session(session_id)
        if sess and sess.get("llm_telemetry"):
            session_telem = sess["llm_telemetry"]
            session_cost = calculate_haiku_pricing(session_telem)

    sessions = list_exam_sessions(limit=50)
    sessions_summary = []
    for s in sessions:
        telem = s.get("llm_telemetry") or {}
        sc = calculate_haiku_pricing(telem)
        sessions_summary.append({
            "session_id": s["session_id"],
            "exam_name": s["exam_name"],
            "course_code": s["course_code"],
            "paper_type": s.get("paper_type", "COMBINED_QUESTION_ANSWER"),
            "status": s.get("status"),
            "student_count": s.get("student_count", 0),
            "telemetry": telem,
            "cost": sc,
        })

    enriched_global_telem = _enrich_llm_telemetry(global_telem)
    enriched_session_telem = _enrich_llm_telemetry(session_telem)

    return {
        "status": "ok",
        "primary_model": settings.primary_model,
        "active_model": settings.primary_model,
        "is_haiku": "haiku" in settings.primary_model.lower(),
        "llm_available": settings.llm_available,
        "vlm_provider": settings.vlm_provider,
        "local_vlm_url": settings.local_vlm_url,
        "local_vlm_model": settings.local_vlm_model,
        "concurrency": settings.llm_concurrency,
        "batch_size": settings.llm_vision_batch_size,
        "global_telemetry": enriched_global_telem,
        "global_cost": global_cost,
        "global_pricing": global_cost,
        "active_session_telemetry": enriched_session_telem,
        "active_session_cost": session_cost,
        "active_session_pricing": session_cost,
        "sessions_summary": sessions_summary,
    }


@app.post("/api/haiku-usage/reset")
def api_reset_haiku_usage(_auth: Any = Depends(require_auth)) -> Dict[str, Any]:
    """Reset global LLM token and API counters."""
    reset = reset_global_llm_usage()
    return {"status": "reset", "global_telemetry": _enrich_llm_telemetry(reset)}


@app.post("/api/haiku-usage/model")
def api_switch_haiku_model(req: SwitchModelRequest, _auth: Any = Depends(require_auth)) -> Dict[str, Any]:
    """Switch active primary LLM model."""
    clean_m = req.model.strip()
    settings.primary_model = clean_m
    if req.vlm_provider and req.vlm_provider.strip():
        settings.vlm_provider = req.vlm_provider.strip().lower()
    else:
        lm = clean_m.lower()
        if "qwen" in lm or "ollama" in lm or "local" in lm:
            settings.vlm_provider = "ollama"
            settings.local_vlm_model = clean_m
        elif "gpt" in lm:
            settings.vlm_provider = "openai"
        elif "claude" in lm or "haiku" in lm or "sonnet" in lm or "opus" in lm:
            settings.vlm_provider = "anthropic"

    return {
        "status": "updated",
        "primary_model": settings.primary_model,
        "vlm_provider": settings.vlm_provider,
        "available": settings.llm_available,
    }


@app.get("/api/vlm-status")
def api_vlm_status() -> Dict[str, Any]:
    """Get active VLM provider and model status."""
    return {
        "provider": settings.vlm_provider,
        "available": settings.llm_available,
        "primary_model": settings.primary_model,
        "fallback_model": settings.fallback_model,
        "local_vlm_url": settings.local_vlm_url,
        "local_vlm_model": settings.local_vlm_model,
    }


@app.post("/api/vlm-config")
def api_vlm_config(
    req: VLMConfigRequest,
    _auth: Any = Depends(require_auth),
) -> Dict[str, Any]:
    """Configure VLM provider (Anthropic Cloud or Local Ollama/Qwen2.5-VL)."""
    settings.vlm_provider = req.vlm_provider.lower().strip()
    if req.local_vlm_url:
        settings.local_vlm_url = req.local_vlm_url.strip()
    if req.local_vlm_model:
        settings.local_vlm_model = req.local_vlm_model.strip()
    if req.local_vlm_api_key:
        settings.local_vlm_api_key = req.local_vlm_api_key.strip()
    return {
        "status": "updated",
        "provider": settings.vlm_provider,
        "available": settings.llm_available,
        "local_vlm_url": settings.local_vlm_url,
        "local_vlm_model": settings.local_vlm_model,
    }


# --------------------------------------------------------------------------- Legacy Digital Code/Text Endpoints
async def _spool_uploads(archives: List[UploadFile]) -> List[Tuple[str, Path]]:
    total = 0
    spooled: List[Tuple[str, Path]] = []
    for archive in archives:
        fname = archive.filename or "upload.zip"
        ext = os.path.splitext(fname)[1] or ".tmp"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
        try:
            while True:
                chunk = await archive.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > settings.max_upload_bytes:
                    tmp.close()
                    try:
                        os.unlink(tmp.name)
                    except OSError:
                        pass
                    for _, p in spooled:
                        try:
                            os.unlink(p)
                        except OSError:
                            pass
                    raise HTTPException(413, f"Upload exceeds the {settings.max_upload_bytes // (1024 * 1024)} MB limit.")
                tmp.write(chunk)
            tmp.close()
            spooled.append((fname, Path(tmp.name)))
        except Exception:
            tmp.close()
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            for _, p in spooled:
                try:
                    os.unlink(p)
                except OSError:
                    pass
            raise
    return spooled


async def _prepare(question_paper_json: str, archives: List[UploadFile]) -> Tuple[QuizPaper, Dict[str, Any]]:
    try:
        paper = parse_question_paper(question_paper_json)
    except ValueError as exc:
        raise HTTPException(400, f"Invalid question paper: {exc}")

    single_fallback = paper.questions[0].question_id if len(paper.questions) == 1 else None
    spooled = await _spool_uploads(archives)
    try:
        extracted = await asyncio.to_thread(
            extract_student_submissions,
            spooled,
            single_question_fallback=single_fallback,
        )
    except ZipIngestionError as exc:
        raise HTTPException(400, str(exc))
    finally:
        for _, p in spooled:
            try:
                os.unlink(p)
            except OSError:
                pass
        gc.collect()

    if extracted["total_c_files"] == 0:
        detail = "No submission files could be matched to students and questions."
        if extracted["warnings"]:
            detail += " " + " ".join(extracted["warnings"][:3])
        raise HTTPException(400, detail)
    if len(extracted["students"]) > settings.max_students_per_session:
        raise HTTPException(400, f"{len(extracted['students'])} students found; the limit is {settings.max_students_per_session}.")
    return paper, extracted


async def _run_job(job_id: str, paper: QuizPaper, extracted: Dict[str, Any]) -> None:
    try:
        async with _job_slot:
            current = jobs.get(job_id)
            if current and current.status == "cancelled":
                return
            jobs.update(job_id, status="running", stage="Starting", progress=0.01, message="Starting evaluation")

            def on_progress(stage: str, fraction: float, message: str) -> None:
                jobs.update(job_id, stage=stage, progress=round(min(max(fraction, 0.0), 1.0), 3), message=message)

            result = await run_full_quiz_evaluation(paper, extracted, progress=on_progress)
            gc.collect()
            jobs.update(job_id, status="done", stage="Done", progress=1.0, message="Evaluation complete", result=result)
    except asyncio.CancelledError:
        jobs.update(job_id, status="cancelled", stage="Cancelled", error="Evaluation was cancelled by the user.")
    except Exception:
        logger.exception("Evaluation job %s failed", job_id)
        jobs.update(job_id, status="error", error="The evaluation failed unexpectedly. See the server log for details.")


def _sample_files() -> List[Path]:
    return sorted(SAMPLES_DIR.glob(f"*{SAMPLE_NAME_SUFFIX}")) if SAMPLES_DIR.is_dir() else []


@app.get("/api/samples")
def list_samples() -> List[Dict[str, Any]]:
    out = []
    for path in _sample_files():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            out.append({
                "name": path.name,
                "title": data.get("quiz_title", path.stem),
                "questions": len(data.get("questions", [])),
                "default_language": data.get("default_language", "auto"),
            })
        except (OSError, ValueError):
            continue
    return out


@app.get("/api/samples/{name}")
def get_sample(name: str) -> JSONResponse:
    for path in _sample_files():
        if path.name == name:
            return JSONResponse(json.loads(path.read_text(encoding="utf-8")))
    raise HTTPException(404, "Unknown sample paper.")


@app.get("/api/jobs")
def list_recent_jobs(_auth: Any = Depends(require_auth)) -> Dict[str, Any]:
    return {"jobs": jobs.list_recent(limit=8)}


@app.post("/api/jobs", response_model=JobCreated, status_code=202)
async def create_job(
    request: Request,
    question_paper_json: str = Form(..., description="Question paper as JSON"),
    archives: List[UploadFile] = File(..., description="Student ZIPs, a master ZIP, or source/text files"),
    _auth: Any = Depends(require_auth),
) -> JobCreated:
    _enforce_rate_limit(request, "jobs", settings.rate_limit_jobs_per_window, settings.rate_limit_window_seconds)
    if jobs.active_count() >= 10:
        raise HTTPException(503, "The server is busy with other evaluations. Try again in a few minutes.")
    paper, extracted = await _prepare(question_paper_json, archives)
    job = jobs.create(owner=_client_key(request))
    task = asyncio.create_task(_run_job(job.id, paper, extracted))
    _background_tasks[job.id] = task
    task.add_done_callback(lambda _t, jid=job.id: _background_tasks.pop(jid, None))
    return JobCreated(job_id=job.id, status=job.status)


@app.get("/api/jobs/{job_id}", response_model=JobStatus)
def job_status(job_id: str, _auth: Any = Depends(require_auth)) -> JobStatus:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown or expired job.")
    return JobStatus(job_id=job.id, status=job.status, stage=job.stage, progress=job.progress, message=job.message, error=job.error)


@app.post("/api/jobs/{job_id}/cancel", response_model=JobStatus)
def cancel_job(job_id: str, _auth: Any = Depends(require_auth)) -> JobStatus:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown or expired job.")
    task = _background_tasks.get(job_id)
    if task and not task.done():
        task.cancel()
    if job.status in ("queued", "running"):
        jobs.update(job_id, status="cancelled", stage="Cancelled", error="Evaluation was cancelled by the user.")
    updated = jobs.get(job_id) or job
    return JobStatus(
        job_id=updated.id, status=updated.status, stage=updated.stage,
        progress=updated.progress, message=updated.message, error=updated.error,
    )


@app.get("/api/jobs/{job_id}/result")
def job_result(job_id: str, _auth: Any = Depends(require_auth)) -> JSONResponse:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown or expired job.")
    if job.status != "done" or job.result is None:
        raise HTTPException(409, f"Job is {job.status}.")
    return JSONResponse(job.result)


@app.post("/api/evaluate-batch", response_model=BatchEvaluationResponse)
async def evaluate_batch(
    request: Request,
    question_paper_json: str = Form(...),
    archives: List[UploadFile] = File(...),
    _auth: Any = Depends(require_auth),
) -> BatchEvaluationResponse:
    _enforce_rate_limit(request, "jobs", settings.rate_limit_jobs_per_window, settings.rate_limit_window_seconds)
    paper, extracted = await _prepare(question_paper_json, archives)
    async with _job_slot:
        result = await run_full_quiz_evaluation(paper, extracted)
    return BatchEvaluationResponse(**result)


@app.get("/api/hitl-examples")
def hitl_examples(_auth: Any = Depends(require_auth)) -> Dict[str, Any]:
    return {"examples": get_calibration_examples(limit=25), "stats": get_feedback_stats()}


@app.post("/api/hitl-feedback", response_model=HITLFeedbackResponse)
def hitl_feedback(request: Request, req: HITLFeedbackRequest, _auth: Any = Depends(require_auth)) -> HITLFeedbackResponse:
    _enforce_rate_limit(request, "feedback", settings.rate_limit_feedback_per_minute, 60)
    record_id = save_hitl_feedback(req)
    jobs.apply_ta_feedback(req)
    update_ta_in_persisted_jobs(req)
    return HITLFeedbackResponse(
        status="recorded",
        record_id=record_id,
        message=f"Saved TA decision for {req.roll_no} / {req.question_id}.",
    )


@app.get("/api/polyglot/benchmark-results")
def api_polyglot_benchmark_results(_auth: Any = Depends(require_auth)) -> Dict[str, Any]:
    """Returns the latest Polyglot HumanEval-X benchmark results across 5 programming languages."""
    bench_dir = Path(__file__).resolve().parent.parent / "data" / "benchmark_runs"
    runs: List[Dict[str, Any]] = []
    latest: Optional[Dict[str, Any]] = None
    if bench_dir.is_dir():
        def _iter_key(p: Path) -> int:
            m = re.search(r"run_iter_(\d+)_", p.name)
            return int(m.group(1)) if m else 0
        run_files = sorted(bench_dir.glob("run_iter_*.json"), key=_iter_key)
        for rf in run_files:
            try:
                with open(rf, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    summary = {
                        "iteration": data.get("iteration"),
                        "timestamp": data.get("timestamp"),
                        "num_samples_per_lang": data.get("num_samples_per_lang"),
                        "languages": data.get("languages", []),
                        "total_tasks": data.get("total_tasks", 0),
                        "total_passed": data.get("total_passed", 0),
                        "overall_accuracy_pct": data.get("overall_accuracy_pct", 0.0),
                        "total_wall_clock_sec": data.get("total_wall_clock_sec", 0.0),
                        "per_language_stats": data.get("per_language_stats", {}),
                    }
                    runs.append(summary)
            except Exception:
                pass
        if run_files:
            try:
                with open(run_files[-1], "r", encoding="utf-8") as f:
                    latest = json.load(f)
            except Exception:
                pass

    from app.analyzer.gcc_sandbox import sandbox_runtimes
    runtimes = sandbox_runtimes()
    return {
        "status": "ok",
        "runtimes": runtimes,
        "latest": latest,
        "history": runs,
    }


@app.get("/api/handwritten/benchmark-results")
def api_handwritten_benchmark_results(_auth: Any = Depends(require_auth)) -> Dict[str, Any]:
    """Returns the latest real handwritten examination paper benchmarks across students."""
    bench_dir = Path(__file__).resolve().parent.parent / "data" / "handwritten_benchmarks"
    runs: List[Dict[str, Any]] = []
    latest: Optional[Dict[str, Any]] = None
    if bench_dir.is_dir():
        def _iter_key(p: Path) -> int:
            m = re.search(r"run_iter_(\d+)_", p.name)
            return int(m.group(1)) if m else 0
        run_files = sorted(bench_dir.glob("run_iter_*.json"), key=_iter_key)
        for rf in run_files:
            try:
                with open(rf, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    summary = {
                        "iteration": data.get("iteration"),
                        "timestamp": data.get("timestamp"),
                        "student_count": data.get("student_count", 0),
                        "total_questions": data.get("total_questions", 0),
                        "transcription_accuracy_pct": data.get("transcription_accuracy_pct", 0.0),
                        "grading_accuracy_pct": data.get("grading_accuracy_pct", 0.0),
                        "total_wall_clock_sec": data.get("total_wall_clock_sec", 0.0),
                        "avg_sec_per_student": data.get("avg_sec_per_student", 0.0),
                        "model": data.get("model", settings.primary_model),
                    }
                    runs.append(summary)
            except Exception:
                pass
        if run_files:
            try:
                with open(run_files[-1], "r", encoding="utf-8") as f:
                    latest = json.load(f)
            except Exception:
                pass

    return {
        "status": "ok",
        "model": settings.primary_model,
        "latest": latest,
        "history": runs,
    }


@app.get("/scanner", response_class=FileResponse)
def serve_scanner_app() -> FileResponse:
    """Serve the 100% laptop-hosted exam scanner & AI grading hub interface."""
    return FileResponse(STATIC_DIR / "scanner.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
