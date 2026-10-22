"""Runtime configuration for the 100% Laptop-Hosted Paper Scanner & AI Grading Hub (v5.1)."""
import os
from dataclasses import dataclass
from pathlib import Path

import sys

_is_testing = "pytest" in sys.modules or any("pytest" in arg for arg in sys.argv)
if not _is_testing:
    try:
        from dotenv import load_dotenv
        # Load .env file automatically so API keys & config are available without manual export
        env_path = Path(__file__).resolve().parent.parent / ".env"
        if env_path.is_file():
            load_dotenv(dotenv_path=env_path)
        else:
            load_dotenv()
    except ImportError:
        pass


def _env_str(name: str, default: str = "") -> str:
    val = os.getenv(name, default).strip()
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        val = val[1:-1].strip()
    return val


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _is_render() -> bool:
    return os.getenv("RENDER", "").strip().lower() in ("true", "1") or bool(os.getenv("RENDER_SERVICE_ID"))


def _is_low_memory() -> bool:
    if os.getenv("LOW_MEMORY_MODE", "").strip().lower() in ("true", "1"):
        return True
    try:
        p2 = Path("/sys/fs/cgroup/memory.max")
        if p2.is_file():
            val = p2.read_text().strip()
            if val.isdigit() and int(val) <= 600 * 1024 * 1024:
                return True
        p1 = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
        if p1.is_file():
            val = p1.read_text().strip()
            if val.isdigit() and int(val) <= 600 * 1024 * 1024:
                return True
    except Exception:
        pass
    return False


_LOW_MEM = _is_low_memory()
_DEFAULT_PARALLELISM = 2 if _LOW_MEM else max(2, min(os.cpu_count() or 4, 8))
_DEFAULT_TEST_WORKERS = 2 if _LOW_MEM else 4
_DEFAULT_AS_BYTES = (256 * 1024 * 1024) if _LOW_MEM else (512 * 1024 * 1024)
_DEFAULT_LLM_CONCURRENCY = 4 if _LOW_MEM else 6
_DEFAULT_LLM_BATCH_SIZE = 5
_DEFAULT_MAX_JOBS = 20 if _LOW_MEM else 50
_DEFAULT_DB_PATH = "./data/hitl_calibration.db"
_DEFAULT_STORAGE_DIR = "./data/paper_scans"


@dataclass
class Settings:
    app_version: str = "5.1.0"
    is_render: bool = _is_render()
    is_low_memory: bool = _LOW_MEM

    # ---- polyglot sandbox (C, C++, Python, Bash, Node/JS when available)
    enable_gcc_sandbox: bool = _env_bool("ENABLE_GCC_SANDBOX", True)
    test_timeout_seconds: float = _env_float("GCC_TIMEOUT_SECONDS", 3.0)
    compile_timeout_seconds: float = _env_float("COMPILE_TIMEOUT_SECONDS", 10.0)
    rlimit_cpu_seconds: int = _env_int("RLIMIT_CPU_SECONDS", 5)
    rlimit_as_bytes: int = _env_int("RLIMIT_AS_BYTES", _DEFAULT_AS_BYTES)
    rlimit_fsize_bytes: int = _env_int("RLIMIT_FSIZE_BYTES", 0)
    rlimit_nproc: int = _env_int("RLIMIT_NPROC", 16)
    max_output_bytes: int = _env_int("MAX_OUTPUT_BYTES", 65536)
    sandbox_parallelism: int = _env_int("SANDBOX_PARALLELISM", _DEFAULT_PARALLELISM)
    sandbox_test_workers: int = _env_int("SANDBOX_TEST_WORKERS", _DEFAULT_TEST_WORKERS)
    float_rel_tol: float = _env_float("FLOAT_REL_TOL", 1e-6)
    float_abs_tol: float = _env_float("FLOAT_ABS_TOL", 1e-6)
    enable_function_driver_synthesis: bool = _env_bool("ENABLE_FUNCTION_DRIVER_SYNTHESIS", True)

    # ---- LLM (Primary 4-way evaluator using Claude Haiku 5.5 + Ephemeral Prompt Caching)
    anthropic_api_key: str = _env_str("ANTHROPIC_API_KEY")
    primary_model: str = _env_str("ANTHROPIC_MODEL", "claude-haiku-5-5")
    fallback_model: str = _env_str("FALLBACK_MODEL", "claude-haiku-5-5")
    llm_enabled: bool = _env_bool("LLM_ENABLED", True)
    llm_full_triage: bool = _env_bool("LLM_FULL_TRIAGE", True)
    llm_batch_size: int = _env_int("LLM_BATCH_SIZE", _DEFAULT_LLM_BATCH_SIZE)
    llm_vision_batch_size: int = _env_int("LLM_VISION_BATCH_SIZE", 2)
    llm_concurrency: int = _env_int("LLM_CONCURRENCY", _DEFAULT_LLM_CONCURRENCY)
    llm_timeout_seconds: float = _env_float("LLM_TIMEOUT_SECONDS", 75.0)
    llm_max_retries: int = _env_int("LLM_MAX_RETRIES", 2)
    auto_approve_confidence_threshold: float = _env_float("AUTO_APPROVE_CONFIDENCE", 0.85)
    auto_approve_legibility_threshold: float = _env_float("AUTO_APPROVE_LEGIBILITY", 0.75)

    # ---- Plagiarism (Active for Polyglot Code Evaluator; never executed for Paper Exam Scans)
    enable_plagiarism: bool = _env_bool("ENABLE_PLAGIARISM", True)
    clone_threshold_percent: float = _env_float("CLONE_THRESHOLD_PERCENT", 88.0)
    clone_near_exact_percent: float = _env_float("CLONE_NEAR_EXACT_PERCENT", 97.0)
    clone_min_tokens: int = _env_int("CLONE_MIN_TOKENS", 25)
    clone_exclusive_min_percent: float = _env_float("CLONE_EXCLUSIVE_MIN_PERCENT", 20.0)
    collusion_min_questions: int = _env_int("COLLUSION_MIN_QUESTIONS", 2)

    # ---- Paper Redundancies & Edge Case Engine
    paper_blank_ink_threshold: float = _env_float("PAPER_BLANK_INK_THRESHOLD", 0.0045)
    paper_auto_rotate: bool = _env_bool("PAPER_AUTO_ROTATE", True)
    paper_blur_threshold: float = _env_float("PAPER_BLUR_THRESHOLD", 60.0)
    enable_blank_page_detector: bool = _env_bool("ENABLE_BLANK_PAGE_DETECTOR", False)

    # ---- Upload & Paper Session limits (sized for 400+ students x 25 pages)
    max_upload_bytes: int = _env_int("MAX_UPLOAD_BYTES", 200 * 1024 * 1024)
    max_uncompressed_bytes: int = _env_int("MAX_UNCOMPRESSED_BYTES", 400 * 1024 * 1024)
    max_single_file_bytes: int = _env_int("MAX_SINGLE_FILE_BYTES", 20 * 1024 * 1024)
    max_file_count: int = _env_int("MAX_FILE_COUNT", 6000)
    max_nesting_depth: int = _env_int("MAX_NESTING_DEPTH", 4)
    max_students_per_session: int = _env_int("MAX_STUDENTS_PER_SESSION", 600)
    max_pages_per_student: int = _env_int("MAX_PAGES_PER_STUDENT", 25)
    max_questions_per_paper: int = _env_int("MAX_QUESTIONS_PER_PAPER", 50)
    chunk_size_bytes: int = _env_int("CHUNK_SIZE_BYTES", 65536)

    # ---- Access control
    api_auth_token: str = _env_str("API_AUTH_TOKEN")
    require_auth: bool = _env_bool("REQUIRE_AUTH", False)
    allowed_origins: str = _env_str("ALLOWED_ORIGINS", "*")
    rate_limit_jobs_per_window: int = _env_int("RATE_LIMIT_JOBS_PER_WINDOW", 30)
    rate_limit_window_seconds: int = _env_int("RATE_LIMIT_WINDOW_SECONDS", 600)
    rate_limit_feedback_per_minute: int = _env_int("RATE_LIMIT_FEEDBACK_PER_MIN", 300)

    # ---- Storage / Local Laptop Hub paths
    sqlite_db_path: str = _env_str("SQLITE_DB_PATH", _DEFAULT_DB_PATH)
    paper_storage_dir: str = _env_str("PAPER_STORAGE_DIR", _DEFAULT_STORAGE_DIR)
    hub_port: int = _env_int("HUB_PORT", 8000)
    job_ttl_seconds: int = _env_int("JOB_TTL_SECONDS", 86400)
    max_stored_jobs: int = _env_int("MAX_STORED_JOBS", _DEFAULT_MAX_JOBS)
    job_concurrency: int = _env_int("JOB_CONCURRENCY", 2 if _LOW_MEM else 4)
    early_abort_on_timeout: bool = _env_bool("EARLY_ABORT_ON_TIMEOUT", False)
    enable_timeout_rerun: bool = _env_bool("ENABLE_TIMEOUT_RERUN", True)

    # ---- Local Offline VLM (Ollama / Qwen2.5-VL / Local OpenAI-compatible)
    vlm_provider: str = _env_str("VLM_PROVIDER", "anthropic")  # "anthropic", "ollama", "openai", "local"
    local_vlm_url: str = _env_str("LOCAL_VLM_URL", "http://localhost:11434/v1")
    local_vlm_model: str = _env_str("LOCAL_VLM_MODEL", "qwen2.5-vl:7b")
    local_vlm_api_key: str = _env_str("LOCAL_VLM_API_KEY", "ollama")

    # ---- Paper Plagiarism & Symbolic Verification
    enable_paper_plagiarism: bool = _env_bool("ENABLE_PAPER_PLAGIARISM", False)
    enable_symbolic_math_verification: bool = _env_bool("ENABLE_SYMBOLIC_MATH_VERIFICATION", True)

    @property
    def auth_required(self) -> bool:
        return self.require_auth or bool(self.api_auth_token)

    @property
    def llm_available(self) -> bool:
        if not self.llm_enabled:
            return False
        if self.vlm_provider in ("ollama", "openai", "local"):
            return bool(self.local_vlm_url)
        return bool(self.anthropic_api_key)


settings = Settings()
