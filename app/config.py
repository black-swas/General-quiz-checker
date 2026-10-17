"""Runtime configuration. Everything comes from environment variables (see .env.example)."""
import os
from dataclasses import dataclass


def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


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


from pathlib import Path


def _is_render() -> bool:
    return os.getenv("RENDER", "").strip().lower() in ("true", "1") or bool(os.getenv("RENDER_SERVICE_ID"))


def _is_low_memory() -> bool:
    if os.getenv("LOW_MEMORY_MODE", "").strip().lower() in ("true", "1"):
        return True
    if _is_render() and os.getenv("RENDER_INSTANCE_TYPE", "free").lower() in ("free", ""):
        return True
    # Check Linux cgroup memory limit if available (Render Free has 512 MB ceiling)
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
_DEFAULT_LLM_CONCURRENCY = 3 if _LOW_MEM else 6
_DEFAULT_LLM_BATCH_SIZE = 6 if _LOW_MEM else 8
_DEFAULT_MAX_JOBS = 4 if _LOW_MEM else 10
_DEFAULT_DB_PATH = "/tmp/hitl_calibration.db" if (os.name != "nt" and _is_render()) else "./data/hitl_calibration.db"


@dataclass
class Settings:
    app_version: str = "4.1.0"
    is_render: bool = _is_render()
    is_low_memory: bool = _LOW_MEM

    # ---- polyglot sandbox (C, C++, Python, Bash, Node/JS when available)
    enable_gcc_sandbox: bool = _env_bool("ENABLE_GCC_SANDBOX", True)
    test_timeout_seconds: float = _env_float("GCC_TIMEOUT_SECONDS", 3.0)       # wall clock per test case
    compile_timeout_seconds: float = _env_float("COMPILE_TIMEOUT_SECONDS", 10.0)
    rlimit_cpu_seconds: int = _env_int("RLIMIT_CPU_SECONDS", 5)
    rlimit_as_bytes: int = _env_int("RLIMIT_AS_BYTES", _DEFAULT_AS_BYTES)     # 256MB on low-memory/Render Free, 512MB otherwise
    rlimit_fsize_bytes: int = _env_int("RLIMIT_FSIZE_BYTES", 0)
    rlimit_nproc: int = _env_int("RLIMIT_NPROC", 16)
    max_output_bytes: int = _env_int("MAX_OUTPUT_BYTES", 65536)
    sandbox_parallelism: int = _env_int("SANDBOX_PARALLELISM", _DEFAULT_PARALLELISM)
    sandbox_test_workers: int = _env_int("SANDBOX_TEST_WORKERS", _DEFAULT_TEST_WORKERS)       # test cases run at once per submission
    float_rel_tol: float = _env_float("FLOAT_REL_TOL", 1e-6)
    float_abs_tol: float = _env_float("FLOAT_ABS_TOL", 1e-6)
    enable_function_driver_synthesis: bool = _env_bool("ENABLE_FUNCTION_DRIVER_SYNTHESIS", True)

    # ---- LLM (primary 4-way evaluator for general/unverified tests; hard integrity flags remain locked)
    anthropic_api_key: str = _env_str("ANTHROPIC_API_KEY")
    primary_model: str = _env_str("ANTHROPIC_MODEL", "claude-haiku-5-5")
    fallback_model: str = _env_str("FALLBACK_MODEL", "claude-haiku-4-5-20251001")
    llm_enabled: bool = _env_bool("LLM_ENABLED", True)
    llm_full_triage: bool = _env_bool("LLM_FULL_TRIAGE", True)
    llm_batch_size: int = _env_int("LLM_BATCH_SIZE", _DEFAULT_LLM_BATCH_SIZE)
    llm_concurrency: int = _env_int("LLM_CONCURRENCY", _DEFAULT_LLM_CONCURRENCY)
    llm_timeout_seconds: float = _env_float("LLM_TIMEOUT_SECONDS", 60.0)
    llm_max_retries: int = _env_int("LLM_MAX_RETRIES", 2)

    # ---- plagiarism & stylometry
    clone_threshold_percent: float = _env_float("CLONE_THRESHOLD_PERCENT", 88.0)
    clone_near_exact_percent: float = _env_float("CLONE_NEAR_EXACT_PERCENT", 97.0)
    clone_min_tokens: int = _env_int("CLONE_MIN_TOKENS", 25)
    clone_exclusive_min_percent: float = _env_float("CLONE_EXCLUSIVE_MIN_PERCENT", 20.0)
    collusion_min_questions: int = _env_int("COLLUSION_MIN_QUESTIONS", 2)

    # ---- upload limits (sized for full college batches)
    max_upload_bytes: int = _env_int("MAX_UPLOAD_BYTES", 50 * 1024 * 1024)
    max_uncompressed_bytes: int = _env_int("MAX_UNCOMPRESSED_BYTES", 100 * 1024 * 1024)
    max_single_file_bytes: int = _env_int("MAX_SINGLE_FILE_BYTES", 2 * 1024 * 1024)
    max_file_count: int = _env_int("MAX_FILE_COUNT", 3000)
    max_nesting_depth: int = _env_int("MAX_NESTING_DEPTH", 4)
    max_students_per_session: int = _env_int("MAX_STUDENTS_PER_SESSION", 250)
    max_questions_per_paper: int = _env_int("MAX_QUESTIONS_PER_PAPER", 20)

    # ---- access control
    api_auth_token: str = _env_str("API_AUTH_TOKEN")
    require_auth: bool = _env_bool("REQUIRE_AUTH", False)
    allowed_origins: str = _env_str("ALLOWED_ORIGINS", "")
    rate_limit_jobs_per_window: int = _env_int("RATE_LIMIT_JOBS_PER_WINDOW", 10)
    rate_limit_window_seconds: int = _env_int("RATE_LIMIT_WINDOW_SECONDS", 600)
    rate_limit_feedback_per_minute: int = _env_int("RATE_LIMIT_FEEDBACK_PER_MIN", 120)

    # ---- storage / jobs
    sqlite_db_path: str = _env_str("SQLITE_DB_PATH", _DEFAULT_DB_PATH)
    job_ttl_seconds: int = _env_int("JOB_TTL_SECONDS", 14400)
    max_stored_jobs: int = _env_int("MAX_STORED_JOBS", _DEFAULT_MAX_JOBS)

    @property
    def auth_required(self) -> bool:
        return self.require_auth or bool(self.api_auth_token)

    @property
    def llm_available(self) -> bool:
        return self.llm_enabled and bool(self.anthropic_api_key)


settings = Settings()

