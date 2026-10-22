"""Pydantic models shared by the API, the analysers, the mobile paper scanner, and the laptop hub UI.

Includes a compatibility shim so the models work seamlessly on both Pydantic v1 (1.10+) and Pydantic v2 (2.6+).
"""
from typing import Any, Dict, List, Literal, Optional

import pydantic
from pydantic import BaseModel as _PydanticBaseModel, Field

_IS_PYDANTIC_V2 = int(pydantic.__version__.split(".")[0]) >= 2


def _str_pattern_field(*args: Any, pattern: str, **kwargs: Any) -> Any:
    if _IS_PYDANTIC_V2:
        return Field(*args, pattern=pattern, **kwargs)
    return Field(*args, regex=pattern, **kwargs)


class BaseModel(_PydanticBaseModel):
    """Unified BaseModel supporting `.model_dump()` and `.model_copy()` across Pydantic v1 and v2."""

    if not _IS_PYDANTIC_V2:
        def model_dump(self, **kwargs: Any) -> Dict[str, Any]:
            return self.dict(**kwargs)

        def model_copy(self, **kwargs: Any) -> Any:
            return self.copy(**kwargs)


TabCategory = Literal["GREEN", "BLUE", "RED", "GRAY"]

CheatType = Literal[
    "NONE",
    "HARDCODED_PRINTF",
    "SAMPLE_INPUT_SPOOFING",
    "DEAD_CODE_PADDING",
    "PLAGIARISM_CLONE",
    "PROMPT_INJECTION",
    "SECURITY_VIOLATION",
    "RULE_VIOLATION",
    "COMPILE_ERROR",
    "TIMEOUT",
    "BROKEN_LOGIC",
    "UNSUBMITTED_OR_STUB",
    "ILLEGIBLE_HANDWRITING",
    "PARTIAL_OR_INCOMPLETE",
]


# --------------------------------------------------------------------------- question paper & coordinate boxes
class TestCase(BaseModel):
    input_data: str = Field(default="", max_length=65536, description="Standard input or prompt case passed to the submission")
    expected_output: str = Field(..., max_length=65536, description="Expected standard output or reference answer")
    is_sample: bool = Field(default=False, description="True if this case was visible in the question paper")


class QuestionBoxSpec(BaseModel):
    """Normalized coordinate box on a designated exam page for coordinate-based visual splitting."""
    page_number: int = Field(default=1, ge=1, le=25, description="1-indexed page number where the answer space appears")
    y_start_pct: float = Field(default=0.0, ge=0.0, le=100.0, description="Top vertical percentage (0 to 100) of the designated answer box")
    y_end_pct: float = Field(default=100.0, ge=0.0, le=100.0, description="Bottom vertical percentage (0 to 100) of the designated answer box")
    x_start_pct: float = Field(default=0.0, ge=0.0, le=100.0, description="Left horizontal percentage (0 to 100)")
    x_end_pct: float = Field(default=100.0, ge=0.0, le=100.0, description="Right horizontal percentage (0 to 100)")
    title: Optional[str] = Field(default=None, max_length=120, description="Optional subtitle e.g. 'Page 1 Part A'")


class QuestionSpec(BaseModel):
    question_id: str = _str_pattern_field(..., pattern=r"^Q\d{1,2}$", description="Q1, Q2, ...")
    title: str = Field(..., min_length=1, max_length=200)
    problem_statement: str = Field(default="", max_length=20000)
    max_marks: float = Field(default=5.0, ge=0.0, le=500.0, description="Maximum marks allocated to this question")
    page_number: int = Field(default=1, ge=1, le=25, description="Primary page number where the student writes the answer")
    y_start_pct: float = Field(default=0.0, ge=0.0, le=100.0, description="Top coordinate percentage of the designated answer box")
    y_end_pct: float = Field(default=100.0, ge=0.0, le=100.0, description="Bottom coordinate percentage of the designated answer box")
    x_start_pct: float = Field(default=0.0, ge=0.0, le=100.0)
    x_end_pct: float = Field(default=100.0, ge=0.0, le=100.0)
    # Redundancy support for multi-page answers and extra sheets
    boxes: List[QuestionBoxSpec] = Field(default_factory=list, description="Coordinate boxes if answer spans across pages or sections")
    spans_pages: List[int] = Field(default_factory=list, description="Pages spanned by this question e.g. [1, 2]")
    continuation_pages: List[int] = Field(default_factory=list, description="Extra or supplementary pages attached to this question")
    language: str = Field(default="auto", max_length=40, description="Target language: auto, c, cpp, python, java, javascript, sql, r, bash, text, handwritten, etc.")
    execution_mode: Literal["auto", "sandbox", "llm_only", "static_only"] = Field(
        default="auto",
        description="Verification mode: 'auto' uses sandbox when runtime + test_cases exist, else static + LLM",
    )
    grading_rubric: str = Field(default="", max_length=10000, description="Explicit grading criteria or step-by-step rubric for LLM grading")
    reference_solution: str = Field(default="", max_length=20000, description="Optional reference solution or key points")
    topic_tags: List[str] = Field(default_factory=list)
    expected_complexity: str = Field(default="O(N)", max_length=80)
    forbidden_constructs: List[str] = Field(default_factory=list)
    required_constructs: List[str] = Field(default_factory=list)
    match_mode: Literal["exact", "sorted_tokens", "unordered_set", "regex"] = Field(
        default="exact",
        description="Output comparison strategy",
    )
    test_cases: List[TestCase] = Field(default_factory=list)


class QuizPaper(BaseModel):
    course_code: str = Field(default="DSE-101", max_length=40)
    quiz_title: str = Field(default="Programming & General Quiz", max_length=200)
    exam_date: str = Field(default="", max_length=40)
    total_marks: float = Field(default=0.0, ge=0.0, le=5000.0)
    expected_pages: int = Field(default=4, ge=1, le=25)
    default_language: str = Field(default="auto", max_length=40)
    questions: List[QuestionSpec]


# --------------------------------------------------------------------------- paper exam session & folder schemas
class CreateSessionRequest(BaseModel):
    exam_name: str = Field(..., min_length=1, max_length=200)
    course_code: str = Field(default="DSE-101", max_length=40)
    exam_date: str = Field(..., min_length=1, max_length=40)
    total_marks: float = Field(default=0.0, ge=0.0, le=5000.0, description="Total exam marks. If 0, auto-calculated from sum of question weights.")
    expected_pages: int = Field(default=1, ge=1, le=25)
    paper_type: str = Field(default="COMBINED_QUESTION_ANSWER", description="Type of exam paper: 'COMBINED_QUESTION_ANSWER' or 'SEPARATE_QUESTION_BOOKLET'")
    question_paper_image_path: Optional[str] = Field(default="", max_length=500, description="Optional path to scanned question paper image")
    storage_dir: Optional[str] = Field(default=None, max_length=500, description="Custom folder path on the laptop for storing this session's photos")
    questions: List[QuestionSpec] = Field(default_factory=list)


class UpdateSessionRequest(BaseModel):
    exam_name: Optional[str] = Field(default=None, max_length=200)
    course_code: Optional[str] = Field(default=None, max_length=40)
    exam_date: Optional[str] = Field(default=None, max_length=40)
    total_marks: Optional[float] = Field(default=None, gt=0.0, le=5000.0)
    expected_pages: Optional[int] = Field(default=None, ge=1, le=25)
    paper_type: Optional[str] = Field(default=None, description="'COMBINED_QUESTION_ANSWER' or 'SEPARATE_QUESTION_BOOKLET'")
    question_paper_image_path: Optional[str] = Field(default=None, max_length=500)
    storage_dir: Optional[str] = Field(default=None, max_length=500)
    questions: Optional[List[QuestionSpec]] = None


class FolderSelectRequest(BaseModel):
    path: str = Field(..., min_length=1, max_length=500, description="Selected or newly created folder path on the laptop")
    create_if_missing: bool = Field(default=True)
    session_id: Optional[str] = Field(default=None, max_length=80)


class RuntimeSettingsUpdateRequest(BaseModel):
    anthropic_api_key: Optional[str] = Field(default=None, max_length=300)
    primary_model: Optional[str] = Field(default=None, max_length=100)
    llm_concurrency: Optional[int] = Field(default=None, ge=1, le=20)
    llm_vision_batch_size: Optional[int] = Field(default=None, ge=1, le=15)
    auto_approve_confidence_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    paper_storage_dir: Optional[str] = Field(default=None, max_length=500)
    vlm_provider: Optional[str] = Field(default=None, max_length=40)
    local_vlm_url: Optional[str] = Field(default=None, max_length=300)
    local_vlm_model: Optional[str] = Field(default=None, max_length=100)


class AddStudentRequest(BaseModel):
    roll_no: str = Field(..., min_length=1, max_length=80)
    student_name: str = Field(default="", max_length=160)


class BulkRosterRequest(BaseModel):
    students: List[AddStudentRequest] = Field(default_factory=list)


class ChunkUploadRequest(BaseModel):
    roll_no: str = Field(..., min_length=1, max_length=80)
    student_name: str = Field(default="", max_length=160)
    page_no: int = Field(..., ge=1, le=25)
    upload_id: str = Field(..., min_length=1, max_length=120)
    chunk_index: int = Field(..., ge=0, le=2000)
    total_chunks: int = Field(..., ge=1, le=2000)
    chunk_base64: str = Field(..., min_length=1)
    is_retake: bool = Field(default=False)
    sha256_full: Optional[str] = Field(default=None, max_length=128)
    ta_name: Optional[str] = Field(default=None, max_length=120)


class FinalizeStudentRequest(BaseModel):
    roll_no: str = Field(..., min_length=1, max_length=80)
    student_name: str = Field(default="", max_length=160)
    trigger_grading: bool = Field(default=False)
    ta_name: Optional[str] = Field(default=None, max_length=120)


class EvaluateSessionRequest(BaseModel):
    only_roll_no: Optional[str] = Field(default=None, max_length=80)
    only_question_id: Optional[str] = Field(default=None, max_length=20)
    force_regrade: bool = Field(default=False)


class TAOverrideGradeRequest(BaseModel):
    roll_no: str = Field(..., min_length=1, max_length=80)
    question_id: str = _str_pattern_field(..., pattern=r"^Q\d{1,2}$")
    ta_status: Literal["TA_APPROVED", "TA_OVERRIDDEN", "FLAGGED_RETAKE"] = Field(default="TA_APPROVED")
    awarded_marks: float = Field(..., ge=0.0, le=500.0)
    verdict_tab: TabCategory = Field(default="GREEN")
    ta_comment: str = Field(default="", max_length=600)
    ta_id: Optional[str] = Field(default=None, max_length=120)


class TAHeartbeatRequest(BaseModel):
    ta_name: str = Field(..., min_length=1, max_length=120)
    station: Optional[str] = Field(default=None, max_length=120)


class ClaimNextStudentRequest(BaseModel):
    ta_id: str = Field(..., min_length=1, max_length=80)
    filter_tab: Optional[str] = Field(default=None, max_length=20)


class ReleaseClaimRequest(BaseModel):
    roll_no: str = Field(..., min_length=1, max_length=80)
    ta_id: str = Field(..., min_length=1, max_length=80)


class PresenceRequest(BaseModel):
    ta_id: str = Field(..., min_length=1, max_length=80)


class StudentRegradeRequest(BaseModel):
    question_id: str = Field(..., min_length=1, max_length=20)
    student_reason: str = Field(..., min_length=1, max_length=1000)
    claimed_marks: float = Field(default=0.0, ge=0.0, le=500.0)


class ResolveRegradeRequest(BaseModel):
    status: Literal["ACCEPTED", "REJECTED"] = Field(...)
    ta_response: str = Field(default="", max_length=1000)
    granted_marks: Optional[float] = Field(default=None, ge=0.0, le=500.0)


class VLMConfigRequest(BaseModel):
    vlm_provider: str = Field(default="anthropic", max_length=40)
    local_vlm_url: Optional[str] = Field(default=None, max_length=300)
    local_vlm_model: Optional[str] = Field(default=None, max_length=100)
    local_vlm_api_key: Optional[str] = Field(default=None, max_length=200)


class SwitchModelRequest(BaseModel):
    model: str = Field(..., min_length=1, max_length=100)
    vlm_provider: Optional[str] = Field(default=None, max_length=40)



# --------------------------------------------------------------------------- analysis results
class StaticAnalysisResult(BaseModel):
    language: str = "c"
    line_count: int
    has_loops: bool
    loop_types: List[str]
    has_recursion: bool
    has_pointers: bool
    has_bitwise: bool
    has_ternary_or_goto: bool
    printf_count: int
    scanf_count: int
    constant_printf_lines: List[int]
    sample_spoof_lines: List[int]
    dead_loop_suspected: bool
    dead_loop_lines: List[int]
    prompt_injection_detected: bool
    prompt_injection_snippets: List[str]
    security_violations: List[str] = Field(default_factory=list)
    forbidden_hits: List[str] = Field(default_factory=list)
    missing_required_hits: List[str] = Field(default_factory=list)
    syntax_errors: List[str] = Field(default_factory=list)
    trojan_or_homoglyph_warnings: List[str] = Field(default_factory=list)
    estimated_complexity: str = "O(1)"
    complexity_warning: Optional[str] = None
    include_notes: List[str] = Field(default_factory=list)
    is_empty_or_stub: bool
    is_function_only: bool = False
    uncommon_construct_notes: List[str]
    static_suspicion_reasons: List[str]


class GccTestResult(BaseModel):
    language: str = "c"
    runtime_used: str = "gcc"
    compiled: bool
    compiler_warnings: str = ""
    compiler_errors: str = ""
    passed_count: int = 0
    total_count: int = 0
    sample_passed: int = 0
    sample_total: int = 0
    hidden_passed: int = 0
    hidden_total: int = 0
    timed_out: bool = False
    runtime_error: bool = False
    failure_modes: List[str] = Field(default_factory=list)
    skipped_reason: str = ""
    case_details: List[Dict[str, Any]] = Field(default_factory=list)


class CloneMatch(BaseModel):
    partner_roll_no: str
    question_id: str
    similarity_percent: float
    jaccard_percent: float
    sequence_percent: float
    exclusive_percent: float = 0.0
    cluster_size: int = 2
    near_cluster_size: Optional[int] = None
    strength: Literal["STRONG", "WEAK"] = "WEAK"
    corroborated: bool = False
    matched_lines_self: List[int] = Field(default_factory=list)
    matched_lines_partner: List[int] = Field(default_factory=list)


class LLMRedReviewRecommendation(BaseModel):
    status: Literal["NONE", "RED_REVIEW"] = "NONE"
    reason: Optional[str] = None
    confidence: Optional[int] = Field(default=None, ge=0, le=100)
    evidence_lines: List[int] = Field(default_factory=list)


class SubmissionEvaluation(BaseModel):
    roll_no: str
    student_name: str = ""
    question_id: str
    filename: str
    language: str = "c"
    raw_code: str
    normalized_tokens_preview: str = ""
    tab_category: TabCategory
    verdict_badge: str
    confidence_score: int = Field(ge=0, le=100)
    confidence_float: float = Field(default=0.95, ge=0.0, le=1.0)
    awarded_marks: float = 0.0
    max_marks: float = 5.0
    auto_approved: bool = False
    cheat_type: CheatType = "NONE"
    uncommon_approach_tag: Optional[str] = None
    estimated_complexity: str = "O(1)"
    logic_summary: str
    instructor_callout: str
    flagged_lines: List[int] = Field(default_factory=list)
    line_annotations: Dict[str, str] = Field(default_factory=dict)
    integrity_notes: List[str] = Field(default_factory=list)
    static_analysis: StaticAnalysisResult
    gcc_result: Optional[GccTestResult] = None
    clone_matches: List[CloneMatch] = Field(default_factory=list)
    max_clone_similarity: float = 0.0
    evaluation_source: Literal["DETERMINISTIC", "DETERMINISTIC+LLM", "LLM_PRIMARY"] = "DETERMINISTIC"
    llm_summary: Optional[str] = None
    llm_callout: Optional[str] = None
    llm_model: Optional[str] = None
    llm_red_review: LLMRedReviewRecommendation = Field(default_factory=LLMRedReviewRecommendation)
    ta_feedback: Optional[Literal["AGREE", "DISAGREE"]] = None
    ta_override_category: Optional[Literal["GREEN", "BLUE", "RED", "GRAY"]] = None
    ta_note: Optional[str] = None
    crop_image_url: Optional[str] = None


class CollusionPair(BaseModel):
    roll_a: str
    roll_b: str
    shared_questions: List[str]
    average_similarity: float
    max_similarity: float


# --------------------------------------------------------------------------- TA feedback (calibration memory)
class HITLFeedbackRequest(BaseModel):
    course_code: str = Field(default="DSE-101", max_length=40)
    question_id: str = _str_pattern_field(..., pattern=r"^Q\d{1,2}$")
    topic_tags: List[str] = Field(default_factory=list)
    problem_summary: str = Field(..., max_length=400)
    roll_no: str = Field(..., max_length=80)
    raw_code: str = Field(..., max_length=20000)
    original_category: Literal["GREEN", "BLUE", "RED", "GRAY"]
    original_cheat_type: str = Field(default="NONE", max_length=60)
    ta_decision: Literal["AGREE", "DISAGREE"]
    override_category: Literal["GREEN", "BLUE", "RED", "GRAY"]
    ta_explanation: str = Field(default="", max_length=600)


class HITLFeedbackResponse(BaseModel):
    status: str
    record_id: int
    message: str


# --------------------------------------------------------------------------- batch response / jobs
class BatchEvaluationResponse(BaseModel):
    course_code: str
    quiz_title: str
    evaluated_at: str = ""
    elapsed_seconds: float = 0.0
    timings: Dict[str, float] = Field(default_factory=dict)
    student_count: int
    total_c_files: int
    total_files: int = 0
    languages_detected: Dict[str, int] = Field(default_factory=dict)
    question_analytics: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    summary_counts: Dict[str, int]
    questions: List[QuestionSpec]
    evaluations_by_question: Dict[str, List[SubmissionEvaluation]]
    student_matrix: Dict[str, Dict[str, Dict[str, Any]]]
    collusion_pairs: List[CollusionPair] = Field(default_factory=list)
    stylometry_warnings: List[str] = Field(default_factory=list)
    ingestion_warnings: List[str] = Field(default_factory=list)
    ignored_binaries: List[str] = Field(default_factory=list)
    engine: Dict[str, Any] = Field(default_factory=dict)


class JobCreated(BaseModel):
    job_id: str
    status: str


class JobStatus(BaseModel):
    job_id: str
    status: Literal["queued", "running", "done", "error", "cancelled"]
    stage: str = ""
    progress: float = 0.0
    message: str = ""
    error: Optional[str] = None
