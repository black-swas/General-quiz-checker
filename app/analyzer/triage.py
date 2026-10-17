"""Deterministic verdicts for one submission across programming and general languages.

Precedence (first match wins):
  1. security violation (never executed)            5. forbidden / missing required construct
  2. text aimed at the automated grader             6. no sandbox tests ran   -> clone / static cheat / GRAY (unverified)
  3. empty / unattempted stub                       7. all tests pass         -> clone flag / BLUE / GREEN
  4. compile/syntax error, then timeout             8. some tests fail        -> hard-coded / spoofing / decoy / bug

Hard facts (compilation, test results, security, clones) are always evaluated before heuristics, and a
failing sandbox test can never be turned into a pass. When sandbox tests cannot run (e.g. languages without
a local compiler, function-only submissions, or open-ended questions), submissions without deterministic flags
enter GRAY (unverified) so the 4-way LLM evaluator can classify them into RED, GREEN, BLUE, or GRAY.
"""
import re
from typing import List, Optional

from app.analyzer.c_static_analyzer import PROMPT_INJECTION_PATTERNS
from app.schemas import (
    CloneMatch,
    GccTestResult,
    QuestionSpec,
    StaticAnalysisResult,
    SubmissionEvaluation,
)

FAILURE_MODE_LABELS = {
    "SEGFAULT": "Segmentation fault (invalid memory access)",
    "FLOATING_POINT_EXCEPTION": "Floating-point exception (division by zero)",
    "ABORTED": "Process aborted (assertion or memory error)",
    "RUNTIME_ERROR": "Non-zero runtime exit code",
    "OFF_BY_ONE": "Off-by-one boundary error (±1 from expected)",
    "INTEGER_OVERFLOW": "Likely 32-bit integer overflow on large input",
    "CASE_OR_FORMAT_MISMATCH": "Output formatting, prompt text, or letter-case mismatch",
}


def _first_failure(gcc: GccTestResult) -> str:
    for d in gcc.case_details:
        if not d.get("passed"):
            kind = "sample " if d.get("is_sample") else ""
            fmode = d.get("failure_mode", "")
            mode_note = f" [{FAILURE_MODE_LABELS[fmode]}]" if fmode in FAILURE_MODE_LABELS else ""
            return (f"First failing {kind}test (#{d.get('case_index')}): input {str(d.get('input', ''))[:40]!r}, "
                    f"expected {str(d.get('expected', ''))[:40]!r}, got {str(d.get('actual', ''))[:40]!r}.{mode_note}")
    return ""


def _constant_output(gcc: GccTestResult) -> bool:
    """Same non-empty output for every test although the expected outputs differ => the input is ignored.

    Requires non-empty output and zero runtime crashes so programs that forget printf() or segfault before
    printing are classified as BROKEN_LOGIC rather than falsely accused of HARDCODED_PRINTF.
    """
    details = gcc.case_details
    if len(details) < 2:
        return False
    if any(str(d.get("actual", "")).startswith("[") for d in details):   # timeout / truncation markers
        return False
    if any(d.get("returncode", 0) != 0 for d in details):
        return False
    actuals = {str(d.get("actual", "")).strip() for d in details}
    if "" in actuals:
        return False
    return len(actuals) == 1 and len({str(d.get("expected", "")).strip() for d in details}) > 1


def _clone_notes(clones: List[CloneMatch]) -> List[str]:
    notes: List[str] = []
    strong = [c for c in clones if c.strength == "STRONG"]
    weak_partners = {c.partner_roll_no for c in clones if c.strength == "WEAK"}
    for c in strong[:3]:
        extra = ", near-identical in several questions" if c.corroborated else ""
        notes.append(f"Strong structural match with {c.partner_roll_no} ({c.similarity_percent}%{extra})")
    if weak_partners:
        notes.append(
            f"Structurally similar to {len(weak_partners)} other submission(s), but this shape is common in the class "
            "(weak evidence, shown for context only)"
        )
    return notes


def _grep_lines(raw_code: str, pattern: str, limit: int = 3) -> List[int]:
    out: List[int] = []
    for idx, line in enumerate(raw_code.splitlines(), start=1):
        if re.search(pattern, line):
            out.append(idx)
            if len(out) >= limit:
                break
    return out


def _approach(static: StaticAnalysisResult, raw_code: str):
    """(tag, flagged lines) for the BLUE tab."""
    if static.has_bitwise:
        return "Bitwise technique", _grep_lines(raw_code, r"<<|>>|\^|~|(?<![&])&(?![&])\s*[\w(]|\|=|&=")
    if static.has_recursion and not static.has_loops:
        return "Recursive formulation", []
    if static.has_pointers:
        return "Pointer arithmetic", _grep_lines(raw_code, r"(?:^|[=(,;{}!&|<>+\-*/%?:\[]|\breturn\b)\s*\*+\s*[A-Za-z_(]")
    if any("generator" in n.lower() for n in static.uncommon_construct_notes):
        return "Generator / lazy stream", _grep_lines(raw_code, r"\byield\b")
    if any("memoization" in n.lower() for n in static.uncommon_construct_notes):
        return "Memoized DP", _grep_lines(raw_code, r"@.*cache")
    if any("window function" in n.lower() for n in static.uncommon_construct_notes):
        return "SQL window function", _grep_lines(raw_code, r"\bOVER\s*\(")
    if static.has_recursion:
        return "Recursive formulation", []
    if not static.has_loops:
        return "Closed-form solution", []
    return "Uncommon construct", _grep_lines(raw_code, r"\bgoto\b")


def deterministic_triage(
    roll_no: str,
    question: QuestionSpec,
    filename: str,
    raw_code: str,
    static_res: StaticAnalysisResult,
    gcc_res: Optional[GccTestResult],
    clone_matches: List[CloneMatch],
    normalized_preview: str = "",
) -> SubmissionEvaluation:
    max_sim = max((c.similarity_percent for c in clone_matches), default=0.0)
    notes = _clone_notes(clone_matches)
    if static_res.trojan_or_homoglyph_warnings:
        notes.extend(static_res.trojan_or_homoglyph_warnings)
    if static_res.complexity_warning:
        notes.append(static_res.complexity_warning)
    strong = [c for c in clone_matches if c.strength == "STRONG"]
    top = max(strong, key=lambda c: c.similarity_percent) if strong else None

    def make(**fields) -> SubmissionEvaluation:
        base = dict(
            roll_no=roll_no,
            question_id=question.question_id,
            filename=filename,
            language=static_res.language,
            raw_code=raw_code,
            normalized_tokens_preview=normalized_preview,
            estimated_complexity=static_res.estimated_complexity,
            integrity_notes=list(notes),
            static_analysis=static_res,
            gcc_result=gcc_res,
            clone_matches=clone_matches,
            max_clone_similarity=max_sim,
        )
        base.update(fields)
        return SubmissionEvaluation(**base)

    tests = f"{gcc_res.passed_count}/{gcc_res.total_count}" if gcc_res and gcc_res.total_count else ""

    # 1 ------------------------------------------------------------------ security (never executed)
    if static_res.security_violations:
        first = static_res.security_violations[0]
        return make(
            tab_category="RED", verdict_badge="Security Policy Violation", confidence_score=100,
            cheat_type="SECURITY_VIOLATION",
            logic_summary="The file attempts shell, network, file, environment or preprocessor operations that are not "
                          "permitted in this evaluator. It was not compiled or executed.",
            instructor_callout=first, flagged_lines=[1], line_annotations={"1": first},
        )

    # 2 ------------------------------------------------------------------ text addressed to the grader
    if static_res.prompt_injection_detected:
        flagged, annotations = [], {}
        for idx, line in enumerate(raw_code.splitlines(), start=1):
            if any(re.search(p, line, re.IGNORECASE) for p in PROMPT_INJECTION_PATTERNS):
                flagged.append(idx)
                annotations[str(idx)] = "Text addressed to an automated grader"
        snippet = static_res.prompt_injection_snippets[0] if static_res.prompt_injection_snippets else ""
        return make(
            tab_category="RED", verdict_badge="Instructions Aimed at the Grader", confidence_score=99,
            cheat_type="PROMPT_INJECTION",
            logic_summary="Comments or strings try to instruct an automated grader (for example to mark the work as correct). "
                          + (f"Test result for the code itself: {tests}." if tests else ""),
            instructor_callout=f"Found: '{snippet[:60]}'. Review before crediting this submission.",
            flagged_lines=flagged or [1], line_annotations=annotations or {"1": "Text addressed to an automated grader"},
        )

    # 3 ------------------------------------------------------------------ stub
    active = static_res.scanf_count > 0 or static_res.printf_count > 0 or static_res.has_loops or static_res.has_recursion
    if static_res.is_empty_or_stub and not active:
        return make(
            tab_category="RED", verdict_badge="Empty / Unattempted", confidence_score=99,
            cheat_type="UNSUBMITTED_OR_STUB",
            logic_summary="The file is empty or only contains a skeleton stub.",
            instructor_callout="No implementation submitted for this question.",
            flagged_lines=[1], line_annotations={"1": "Empty or skeleton file"},
        )

    # 4 ------------------------------------------------------------------ compile/syntax error / timeout
    if gcc_res and not gcc_res.compiled:
        first_error = (gcc_res.compiler_errors.splitlines() or ["Compilation failed."])[0]
        m = re.search(r"(?:submission\.\w+|line\s+)[:\s]*(\d+)", gcc_res.compiler_errors, re.I)
        line = int(m.group(1)) if m else 1
        return make(
            tab_category="RED", verdict_badge="Compilation Error", confidence_score=99, cheat_type="COMPILE_ERROR",
            logic_summary=f"The program does not compile with {gcc_res.runtime_used or 'gcc'}, so no test could run.",
            instructor_callout=f"Compiler error near line {line}.",
            flagged_lines=[line], line_annotations={str(line): f"{gcc_res.runtime_used}: {first_error[:110]}"},
        )
    if (gcc_res is None or gcc_res.total_count == 0) and static_res.syntax_errors:
        first_err = static_res.syntax_errors[0]
        m = re.search(r"line\s+(\d+)", first_err, re.I)
        line = int(m.group(1)) if m else 1
        return make(
            tab_category="RED", verdict_badge="Syntax Error", confidence_score=98, cheat_type="COMPILE_ERROR",
            logic_summary=f"Static syntax check failed: {first_err}",
            instructor_callout=first_err[:120],
            flagged_lines=[line], line_annotations={str(line): first_err[:110]},
        )
    if gcc_res and gcc_res.compiled and gcc_res.timed_out:
        loop_lines = _grep_lines(raw_code, r"\b(?:while|for|loop)\b", 1) or [1]
        return make(
            tab_category="RED", verdict_badge="Timeout / Infinite Loop", confidence_score=97, cheat_type="TIMEOUT",
            logic_summary=f"The program compiles but did not finish within the time limit on at least one test ({tests} passed).",
            instructor_callout="Likely a non-terminating loop. " + _first_failure(gcc_res),
            flagged_lines=loop_lines, line_annotations={str(loop_lines[0]): "Possible non-terminating loop"},
        )

    # 5 ------------------------------------------------------------------ rule from the paper
    if static_res.forbidden_hits:
        hits = ", ".join(static_res.forbidden_hits)
        lines = _grep_lines(raw_code, "|".join(re.escape(h) for h in static_res.forbidden_hits)) or [1]
        return make(
            tab_category="RED", verdict_badge="Forbidden Construct Used", confidence_score=96, cheat_type="RULE_VIOLATION",
            logic_summary=f"The question forbids: {hits}. " + (f"Tests passed: {tests}." if tests else ""),
            instructor_callout=f"Uses forbidden construct(s): {hits}.",
            flagged_lines=lines, line_annotations={str(n): "Forbidden by the question" for n in lines},
        )
    if static_res.missing_required_hits:
        missing = ", ".join(static_res.missing_required_hits)
        return make(
            tab_category="RED", verdict_badge="Missing Required Construct", confidence_score=94, cheat_type="RULE_VIOLATION",
            logic_summary=f"The question requires using: {missing}. " + (f"Tests passed: {tests}." if tests else ""),
            instructor_callout=f"Missing required construct(s): {missing}.",
            flagged_lines=[1], line_annotations={"1": f"Missing required construct: {missing}"},
        )

    # 6 ------------------------------------------------------------------ nothing to verify against in sandbox
    if gcc_res is None or gcc_res.total_count == 0:
        if top:
            return make(
                tab_category="RED", verdict_badge=f"Strong Clone Match ({top.similarity_percent}% with {top.partner_roll_no})",
                confidence_score=min(99, max(85, round(top.similarity_percent))), cheat_type="PLAGIARISM_CLONE",
                logic_summary="Correctness could not be tested in sandbox, but the structure is distinctively close to another submission.",
                instructor_callout=f"Compare with {top.partner_roll_no} before deciding.",
                flagged_lines=top.matched_lines_self[:12] or [1],
            )
        if question.test_cases and gcc_res is not None and "No local" in (gcc_res.skipped_reason or ""):
            if static_res.dead_loop_suspected:
                lines = sorted(set(static_res.dead_loop_lines + static_res.constant_printf_lines))
                return make(
                    tab_category="RED", verdict_badge="Decoy Code + Constant Output", confidence_score=92,
                    cheat_type="DEAD_CODE_PADDING",
                    logic_summary="Static analysis detected a loop whose result never reaches the constant output.",
                    instructor_callout="Loop variables never influence the printed answer.",
                    flagged_lines=lines or [1],
                )
            if static_res.sample_spoof_lines:
                return make(
                    tab_category="RED", verdict_badge="Sample-Input Special-casing", confidence_score=92,
                    cheat_type="SAMPLE_INPUT_SPOOFING",
                    logic_summary="Branches directly on visible sample inputs and prints hardcoded outputs.",
                    instructor_callout=f"Sample-input equality branches at lines {static_res.sample_spoof_lines}.",
                    flagged_lines=static_res.sample_spoof_lines,
                )
            if static_res.constant_printf_lines and not static_res.has_loops and not static_res.has_recursion and static_res.scanf_count == 0:
                return make(
                    tab_category="RED", verdict_badge="Hard-coded Output", confidence_score=92,
                    cheat_type="HARDCODED_PRINTF",
                    logic_summary="Prints a constant literal without reading input or computing a result.",
                    instructor_callout=f"Unconditional constant output at lines {static_res.constant_printf_lines}.",
                    flagged_lines=static_res.constant_printf_lines,
                )
        reason = "the GCC sandbox is disabled or unavailable" if gcc_res is None else (gcc_res.skipped_reason or "no tests ran")
        return make(
            tab_category="GRAY", verdict_badge="Unverified (No Tests)", confidence_score=50,
            logic_summary=f"Correctness was not verified: {reason}. No verdict is given.",
            instructor_callout="Add test cases to the question paper or enable AI review to verify.",
        )

    all_passed = gcc_res.passed_count == gcc_res.total_count

    # 7 ------------------------------------------------------------------ everything passes
    if all_passed:
        if top:
            lines = top.matched_lines_self[:12] or [1]
            return make(
                tab_category="RED", verdict_badge=f"Strong Clone Match ({top.similarity_percent}% with {top.partner_roll_no})",
                confidence_score=min(99, max(85, round(top.similarity_percent))), cheat_type="PLAGIARISM_CLONE",
                logic_summary=f"All {tests} tests pass, but the structure is distinctively close to {top.partner_roll_no} "
                              "(identifiers renamed, same statements in the same order).",
                instructor_callout=f"Compare with {top.partner_roll_no}. Strong match, not proof: confirm in the Integrity view.",
                flagged_lines=lines,
                line_annotations={str(n): f"Matches {top.partner_roll_no}" for n in lines},
            )
        if static_res.uncommon_construct_notes:
            tag, lines = _approach(static_res, raw_code)
            note = static_res.uncommon_construct_notes[0]
            return make(
                tab_category="BLUE", verdict_badge=tag, confidence_score=95, uncommon_approach_tag=tag,
                logic_summary=f"Passes all {tests} tests using a non-standard approach: {note}.",
                instructor_callout=f"Quick check: {note}.",
                flagged_lines=lines, line_annotations={str(n): note for n in lines},
            )
        confidence = 98 - (4 if gcc_res.compiler_warnings else 0)
        loops = ", ".join(static_res.loop_types) or "control"
        return make(
            tab_category="GREEN", verdict_badge="Clean Standard Logic", confidence_score=max(75, confidence),
            logic_summary=f"Passes all {tests} tests with standard {loops} flow (est. {static_res.estimated_complexity}).",
            instructor_callout="Auto-cleared: tests pass and no integrity indicators.",
        )

    # 8 ------------------------------------------------------------------ some tests fail
    failure = _first_failure(gcc_res)
    ratio = gcc_res.passed_count / gcc_res.total_count
    constant_lines = static_res.constant_printf_lines or _grep_lines(raw_code, r"\b(?:printf|puts|print|println|cout)\b")

    if _constant_output(gcc_res) and static_res.dead_loop_suspected:
        lines = sorted(set(static_res.dead_loop_lines + constant_lines))
        return make(
            tab_category="RED", verdict_badge="Decoy Code + Constant Output", confidence_score=95, cheat_type="DEAD_CODE_PADDING",
            logic_summary="The program prints the same output whatever the input, and its loop(s) never influence that output.",
            instructor_callout=f"{tests} tests pass. " + failure,
            flagged_lines=lines,
            line_annotations={**{str(n): "Loop result is never used in the output" for n in static_res.dead_loop_lines},
                              **{str(n): "Constant output" for n in constant_lines}},
        )
    if _constant_output(gcc_res):
        return make(
            tab_category="RED", verdict_badge="Hard-coded Output", confidence_score=95, cheat_type="HARDCODED_PRINTF",
            logic_summary="The program prints the same output for every input although the expected answers differ.",
            instructor_callout=f"{tests} tests pass. " + failure,
            flagged_lines=constant_lines, line_annotations={str(n): "Output does not depend on the input" for n in constant_lines},
        )
    if static_res.sample_spoof_lines and gcc_res.sample_passed > 0:
        lines = static_res.sample_spoof_lines
        return make(
            tab_category="RED", verdict_badge="Sample-Input Special-casing", confidence_score=95,
            cheat_type="SAMPLE_INPUT_SPOOFING",
            logic_summary="Branches on the visible sample inputs and prints their answers; it does not solve the general case.",
            instructor_callout=f"{tests} tests pass (samples only). " + failure,
            flagged_lines=lines, line_annotations={str(n): "Branch on a sample input" for n in lines},
        )
    if static_res.dead_loop_suspected:
        lines = sorted(set(static_res.dead_loop_lines + constant_lines))
        return make(
            tab_category="RED", verdict_badge="Decoy Code + Constant Output", confidence_score=92, cheat_type="DEAD_CODE_PADDING",
            logic_summary="A loop whose result is never printed, followed by literal output; tests fail.",
            instructor_callout=f"{tests} tests pass. " + failure,
            flagged_lines=lines, line_annotations={str(n): "Does not influence the output" for n in lines},
        )

    return make(
        tab_category="RED", verdict_badge=f"Fails Tests ({tests})",
        confidence_score=min(96, max(80, round(80 + 16 * (1 - ratio)))), cheat_type="BROKEN_LOGIC",
        logic_summary=f"Compiles and runs, but {gcc_res.total_count - gcc_res.passed_count} of {gcc_res.total_count} tests fail.",
        instructor_callout=failure or "Wrong output on some tests.",
    )
