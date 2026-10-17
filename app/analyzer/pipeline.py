"""Evaluate one quiz: ingested submissions + question paper -> full polyglot report."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple


from app.analyzer.c_static_analyzer import analyze_c_submission
from app.analyzer.driver_synthesizer import synthesize_driver
from app.analyzer.gcc_sandbox import gcc_available, run_gcc_verification, runtime_available_for, sandbox_runtimes
from app.analyzer.llm_evaluator import annotate_question, new_llm_report
from app.analyzer.plagiarism_engine import (
    apply_collusion_corroboration,
    detect_cross_question_collusion,
    detect_stylometric_anomalies,
    run_question_plagiarism_matrix,
)
from app.analyzer.triage import deterministic_triage
from app.config import settings
from app.schemas import CloneMatch, GccTestResult, QuizPaper, SubmissionEvaluation

logger = logging.getLogger("iiserb.pipeline")

ProgressFn = Callable[[str, float, str], None]


def ist_timestamp() -> str:
    return datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d %H:%M:%S IST")


def _matrix_cell(ev: SubmissionEvaluation) -> Dict[str, Any]:
    g = ev.gcc_result
    return {
        "submitted": True,
        "language": ev.language,
        "tab_category": ev.tab_category,
        "verdict_badge": ev.verdict_badge,
        "cheat_type": ev.cheat_type,
        "confidence_score": ev.confidence_score,
        "max_clone_similarity": ev.max_clone_similarity,
        "passed_tests": f"{g.passed_count}/{g.total_count}" if g and g.total_count else "-",
        "passed": g.passed_count if g else 0,
        "total": g.total_count if g else 0,
    }


async def run_full_quiz_evaluation(
    quiz_paper: QuizPaper,
    extracted: Dict[str, Any],
    progress: Optional[ProgressFn] = None,
) -> Dict[str, Any]:
    started = time.time()
    notify: ProgressFn = progress or (lambda stage, fraction, message: None)
    students: Dict[str, Dict[str, Dict[str, str]]] = extracted["students"]
    rolls = sorted(students)
    warnings: List[str] = list(extracted["warnings"])
    questions = quiz_paper.questions
    n_q = max(1, len(questions))

    unknown_questions = sorted({qid for files in students.values() for qid in files} - {q.question_id for q in questions})
    if unknown_questions:
        warnings.append(f"Files for {', '.join(unknown_questions)} were ignored: the question paper does not define them.")

    runtimes = sandbox_runtimes()
    sandbox_on = settings.enable_gcc_sandbox and any(runtimes.values())
    if not sandbox_on:
        warnings.append("The execution sandbox is disabled or unavailable, so no tests were run in the sandbox.")

    # ---- Overlapped Pass 1 & Pass 2 (S1)
    # Plagiarism matrix calculation, cross-question collusion, and stylometric consistency
    # run in parallel background tasks/threads while static analysis and sandbox verification execute immediately.
    plag_start = time.perf_counter()
    plagiarism_seconds = 0.0

    async def run_one_question_plagiarism(qi: int, q):
        notify("Comparing submissions", 0.02 + 0.08 * (qi + 0.5) / n_q, f"{q.question_id}: pairwise structural comparison")
        submissions = {r: students[r][q.question_id]["code"] for r in rolls if q.question_id in students[r]}
        langs_by_roll = {r: students[r][q.question_id].get("language", "c") for r in rolls if q.question_id in students[r]}
        clone_map, preview_map = await asyncio.to_thread(
            run_question_plagiarism_matrix, q.question_id, submissions, None, langs_by_roll
        )
        return q.question_id, clone_map, preview_map

    async def compute_plagiarism_and_stylometry():
        nonlocal plagiarism_seconds
        clones_by_q: Dict[str, Dict[str, List[CloneMatch]]] = {}
        previews_by_q: Dict[str, Dict[str, str]] = {}
        for qid, clone_map, preview_map in await asyncio.gather(*(run_one_question_plagiarism(qi, q) for qi, q in enumerate(questions))):
            clones_by_q[qid], previews_by_q[qid] = clone_map, preview_map
        corroborated = await asyncio.to_thread(apply_collusion_corroboration, clones_by_q, len(rolls))
        collusion = await asyncio.to_thread(detect_cross_question_collusion, clones_by_q)
        style_notes, style_warns = await asyncio.to_thread(detect_stylometric_anomalies, students)
        plagiarism_seconds = round(time.perf_counter() - plag_start, 2)
        return clones_by_q, previews_by_q, corroborated, collusion, style_notes, style_warns

    plag_task = asyncio.create_task(compute_plagiarism_and_stylometry())

    # ---- static analysis + polyglot sandbox + triage + 4-way LLM (pipelined)
    llm_report = new_llm_report()
    semaphore = asyncio.Semaphore(max(1, settings.sandbox_parallelism))
    evaluations: Dict[str, List[SubmissionEvaluation]] = {}
    languages_counter: Counter = Counter()

    total_submissions = sum(len([r for r in rolls if q.question_id in students[r]]) for q in questions)
    finished_submissions = 0
    sandbox_start = time.perf_counter()
    llm_seconds = 0.0

    async def evaluate_one(roll: str, meta: Dict[str, str], q) -> Tuple[str, Dict[str, str], Any, Optional[GccTestResult]]:
        nonlocal finished_submissions
        code = meta["code"]
        file_lang = meta.get("language", "auto")
        effective_lang = q.language if (q.language and q.language != "auto") else file_lang
        static = await asyncio.to_thread(analyze_c_submission, code, q, effective_lang)
        gcc = None
        exec_mode = getattr(q, "execution_mode", "auto")

        runnable_code = code
        was_synthesized = False
        if static.is_function_only and settings.enable_function_driver_synthesis and q.test_cases:
            synth_code, was_synth = synthesize_driver(code, static.language)
            if was_synth:
                runnable_code = synth_code
                was_synthesized = True

        if static.is_function_only and not was_synthesized and exec_mode != "sandbox":
            # LeetCode-style function submission without main() or stdin/stdout driver:
            # avoid false linker/empty-output failure and let the 4-way LLM evaluator grade it.
            gcc = GccTestResult(
                language=static.language,
                runtime_used="none",
                compiled=True,
                total_count=0,
                skipped_reason="Function-only submission (no main/stdin driver); evaluated by AI review",
            )
        else:
            can_run_sandbox = (
                sandbox_on
                and exec_mode in ("auto", "sandbox")
                and not static.is_empty_or_stub
                and not static.security_violations
                and runtime_available_for(static.language)
            )
            if can_run_sandbox:
                match_mode = getattr(q, "match_mode", "exact")
                async with semaphore:
                    gcc = await asyncio.to_thread(
                        run_gcc_verification, runnable_code, q.test_cases, None, static.language, match_mode
                    )
                if was_synthesized and gcc and not gcc.compiled:
                    # If auto-synthesized driver signature did not match, fall back cleanly to unverified AI review
                    gcc = GccTestResult(
                        language=static.language,
                        runtime_used="none",
                        compiled=True,
                        total_count=0,
                        skipped_reason="Function-only submission (driver signature could not be auto-synthesized); evaluated by AI review",
                    )
        finished_submissions += 1
        notify(
            "Running analysis & tests",
            0.10 + 0.75 * finished_submissions / max(1, total_submissions),
            f"{q.question_id}: evaluated {finished_submissions}/{total_submissions} total submissions",
        )
        return roll, meta, static, gcc

    async def evaluate_question(q) -> Tuple[str, List[SubmissionEvaluation]]:
        nonlocal llm_seconds
        todo = [(r, students[r][q.question_id]) for r in rolls if q.question_id in students[r]]
        # 1. Run static analysis & sandbox verification concurrently
        preliminary = await asyncio.gather(*(evaluate_one(r, m, q) for r, m in todo))

        # 2. Wait for plagiarism & stylometry pass to complete before triage
        clones_by_q, previews_by_q, _corroborated, _collusion, style_notes_by_sub, _style_warns = await plag_task

        # 3. Deterministic triage
        results: List[SubmissionEvaluation] = []
        for roll, meta, static, gcc in preliminary:
            ev = deterministic_triage(
                roll, q, meta["filename"], meta["code"], static, gcc,
                clones_by_q[q.question_id].get(roll, []), previews_by_q[q.question_id].get(roll, ""),
            )
            style_note = style_notes_by_sub.get((roll, q.question_id))
            if style_note:
                ev.integrity_notes.append(style_note)
            results.append(ev)
            languages_counter[ev.language] += 1

        results.sort(key=lambda e: e.roll_no)

        # 4. LLM review
        if settings.llm_available:
            notify("Reviewing with AI evaluator", 0.88, f"{q.question_id}: reviewing {len(results)} submissions")
            t_llm = time.perf_counter()
            await annotate_question(q, results, llm_report)
            llm_seconds += time.perf_counter() - t_llm
        return q.question_id, results

    for qid, q_results in await asyncio.gather(*(evaluate_question(q) for q in questions)):
        evaluations[qid] = q_results

    # Retrieve final cross-question collusion and stylometry warnings from plagiarism task
    _, _, corroborated_pairs, collusion_pairs, _, stylometry_warnings = await plag_task
    sandbox_seconds = round(time.perf_counter() - sandbox_start, 2)

    # ---- assemble matrix & per-question analytics
    matrix: Dict[str, Dict[str, Dict[str, Any]]] = {r: {} for r in rolls}
    counts = {"GREEN": 0, "BLUE": 0, "RED": 0, "GRAY": 0, "NOT_SUBMITTED": 0}
    question_analytics: Dict[str, Dict[str, Any]] = {}

    for q in questions:
        q_evs = evaluations[q.question_id]
        q_tabs = Counter(e.tab_category for e in q_evs)
        q_langs = Counter(e.language for e in q_evs)
        q_complexities = Counter(e.estimated_complexity for e in q_evs)
        for ev in q_evs:
            matrix[ev.roll_no][q.question_id] = _matrix_cell(ev)
            counts[ev.tab_category] += 1
        for r in rolls:
            if q.question_id not in students[r]:
                counts["NOT_SUBMITTED"] += 1
                warnings.append(f"Student '{r}' did not submit {q.question_id}.")
                matrix[r][q.question_id] = {
                    "submitted": False, "language": "-", "tab_category": "GRAY", "verdict_badge": "Not submitted",
                    "cheat_type": "NONE", "confidence_score": 100, "max_clone_similarity": 0.0,
                    "passed_tests": "-", "passed": 0, "total": 0,
                }
        question_analytics[q.question_id] = {
            "submitted": len(q_evs),
            "tabs": dict(q_tabs),
            "languages": dict(q_langs),
            "complexities": dict(q_complexities),
        }
    counts["COLLUSION_PAIRS"] = len(collusion_pairs)

    report = {k: v for k, v in llm_report.items() if k != "fatal"}
    notify("Done", 1.0, "Evaluation complete")
    return {
        "course_code": quiz_paper.course_code,
        "quiz_title": quiz_paper.quiz_title,
        "evaluated_at": ist_timestamp(),
        "elapsed_seconds": round(time.time() - started, 1),
        "timings": {
            "plagiarism_seconds": plagiarism_seconds,
            "sandbox_and_static_seconds": sandbox_seconds,
            "llm_seconds": round(llm_seconds, 2),
            "total_seconds": round(time.time() - started, 2),
        },
        "student_count": len(rolls),
        "total_c_files": extracted["total_c_files"],
        "total_files": extracted.get("total_files", extracted["total_c_files"]),
        "languages_detected": dict(languages_counter),
        "question_analytics": question_analytics,
        "summary_counts": counts,
        "questions": [q.model_dump() for q in questions],
        "evaluations_by_question": {qid: [e.model_dump() for e in evs] for qid, evs in evaluations.items()},
        "student_matrix": matrix,
        "collusion_pairs": [c.model_dump() for c in collusion_pairs],
        "stylometry_warnings": stylometry_warnings,
        "ingestion_warnings": warnings,
        "ignored_binaries": extracted["ignored_binaries"],
        "engine": {
            "version": settings.app_version,
            "sandbox": sandbox_on,
            "runtimes": runtimes,
            "llm": report,
            "clone_threshold_percent": settings.clone_threshold_percent,
            "corroborated_pairs": corroborated_pairs,
        },
    }
