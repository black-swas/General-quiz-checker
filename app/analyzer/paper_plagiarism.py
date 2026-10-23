"""Handwritten Exam Paper Plagiarism & Collusion Detection Engine.

Processes OCR transcriptions and step-evaluation signatures of physical paper exams:
  1. Runs tokenized pairwise shingle comparison (via plagiarism_engine.py) over normalized
     LaTeX math and handwritten text transcriptions.
  2. Detects Shared Wrong-Answer & Shared Intermediate-Error signatures:
     Flags student pairs who make the exact same rare arithmetic error, identical wrong constants,
     or identical intermediate missteps.
  3. Corroborates cross-question collusion across multi-question physical exams.
"""

import json
import re
from collections import Counter
from typing import Any, Dict, List, Set, Tuple

from app.analyzer.plagiarism_engine import normalize_c_tokens, run_question_plagiarism_matrix
from app.config import settings


_NUMBER_PATTERN = re.compile(r"\b\d+(?:\.\d+)?\b")


COMMON_EXAM_NUMBERS = {
    "0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10",
    "0.0", "1.0", "2.0", "3.0", "4.0", "5.0", "10.0", "100", "100.0", "0.5", "50", "50.0",
}


def extract_numeric_tokens(text: str) -> Set[str]:
    """Extracts non-trivial numbers from an OCR transcription."""
    nums = set(_NUMBER_PATTERN.findall(text))
    # Filter out trivial and ubiquitous small numbers
    return {n for n in nums if n not in COMMON_EXAM_NUMBERS and (len(n) > 1 or "." in n)}


def run_paper_exam_plagiarism(
    session_id: str,
    evaluations: List[Dict[str, Any]],
    threshold_percent: float = 85.0,
) -> Dict[str, Any]:
    """Analyzes all question evaluations for a paper session to detect text/formula plagiarism

    and shared wrong-answer collusion.
    """
    # Group evaluations by question_id: qid -> roll -> eval
    evals_by_q: Dict[str, Dict[str, Dict[str, Any]]] = {}
    students: Set[str] = set()

    for ev in evaluations:
        roll = ev.get("roll_no")
        qid = ev.get("question_id")
        if not roll or not qid:
            continue
        students.add(roll)
        evals_by_q.setdefault(qid, {})[roll] = ev

    question_clones: Dict[str, List[Dict[str, Any]]] = {}
    pair_collusion_scores: Dict[Tuple[str, str], List[str]] = {}
    shared_error_signatures: List[Dict[str, Any]] = []

    for qid, st_map in evals_by_q.items():
        # Build submissions dictionary for plagiarism_engine
        submissions: Dict[str, str] = {}
        for roll, ev in st_map.items():
            transcription = ev.get("ocr_transcription") or ""
            if len(transcription.strip()) >= 20:
                submissions[roll] = transcription

        if len(submissions) >= 2:
            try:
                # Treat math/derivation transcriptions as latex/prose
                clone_map, _ = run_question_plagiarism_matrix(
                    qid,
                    submissions,
                    threshold_percent=threshold_percent,
                    languages_by_roll={r: "latex" for r in submissions},
                )
                q_matches: List[Dict[str, Any]] = []
                seen_pairs: Set[Tuple[str, str]] = set()

                for roll, matches in clone_map.items():
                    for m in matches:
                        p_roll = getattr(m, "partner_roll_no", getattr(m, "partner_roll", ""))
                        if not p_roll:
                            continue
                        pair = tuple(sorted([roll, p_roll]))
                        if pair not in seen_pairs:
                            seen_pairs.add(pair)
                            match_dict = {
                                "roll_a": pair[0],
                                "roll_b": pair[1],
                                "similarity_percent": m.similarity_percent,
                                "strength": m.strength,
                                "exclusive_percent": m.exclusive_percent,
                            }
                            q_matches.append(match_dict)

                            q_label = qid if qid.startswith("Q") else f"Q{qid}"
                            reason = f"{q_label}: {m.similarity_percent:.1f}% LaTeX match ({m.strength})"
                            pair_collusion_scores.setdefault(pair, []).append(reason)

                question_clones[qid] = q_matches
            except Exception as exc:
                import logging
                logging.getLogger("iiserb.paper_plagiarism").warning("Failed question plagiarism for %s: %s", qid, exc)
                question_clones[qid] = []

        # ---- Shared Wrong-Answer & Intermediate-Error Detection ----
        # Identify students who received < 80% marks on this question
        wrong_students: Dict[str, Dict[str, Any]] = {}
        for roll, ev in st_map.items():
            awarded = float(ev.get("ta_final_marks") if ev.get("ta_final_marks") is not None else ev.get("ai_awarded_marks", 0.0) or 0.0)
            max_m = float(ev.get("max_marks", 5.0) or 5.0)
            if max_m > 0 and (awarded / max_m) < 0.85:
                wrong_students[roll] = ev

        # Extract unusual numbers and error patterns in wrong answers
        num_frequencies: Counter = Counter()
        student_nums: Dict[str, Set[str]] = {}
        for roll, ev in wrong_students.items():
            nums = extract_numeric_tokens(ev.get("ocr_transcription") or "")
            student_nums[roll] = nums
            num_frequencies.update(nums)

        # Rare numbers appearing in exactly 2 or 3 wrong submissions (strong marker of copied arithmetic)
        rare_shared_nums = {num for num, count in num_frequencies.items() if 2 <= count <= 3}

        wrong_rolls = sorted(wrong_students.keys())
        q_label = qid if qid.startswith("Q") else f"Q{qid}"
        for i in range(len(wrong_rolls)):
            for j in range(i + 1, len(wrong_rolls)):
                r1, r2 = wrong_rolls[i], wrong_rolls[j]
                shared_rare = student_nums[r1].intersection(student_nums[r2]).intersection(rare_shared_nums)
                if shared_rare:
                    ev1, ev2 = wrong_students[r1], wrong_students[r2]
                    # Also check if they had similar anomaly flags or negative TA comments
                    sig = {
                        "question_id": qid,
                        "roll_a": r1,
                        "roll_b": r2,
                        "shared_rare_constants": sorted(list(shared_rare)),
                        "score_a": ev1.get("ai_awarded_marks", 0.0),
                        "score_b": ev2.get("ai_awarded_marks", 0.0),
                        "reason": f"Shared rare incorrect constant(s) {sorted(list(shared_rare))} on {q_label}",
                    }
                    shared_error_signatures.append(sig)
                    pair = (r1, r2)
                    pair_collusion_scores.setdefault(pair, []).append(
                        f"{q_label}: Shared rare incorrect constant(s) {sorted(list(shared_rare))}"
                    )

    # Compile Final Collusion Pairs
    collusion_pairs: List[Dict[str, Any]] = []
    for (r1, r2), reasons in pair_collusion_scores.items():
        q_count = len({r.split(":")[0] for r in reasons})
        confidence = min(0.99, 0.65 + (0.12 * q_count) + (0.05 * len(reasons)))
        verdict = "CONFIRMED_COLLUSION" if q_count >= 2 or confidence >= 0.90 else "SUSPECTED_SIMILARITY"
        collusion_pairs.append({
            "roll_a": r1,
            "roll_b": r2,
            "questions_involved_count": q_count,
            "evidence_count": len(reasons),
            "confidence": round(confidence, 2),
            "verdict": verdict,
            "reasons": reasons,
        })

    collusion_pairs.sort(key=lambda x: (x["questions_involved_count"], x["confidence"]), reverse=True)

    return {
        "session_id": session_id,
        "total_students": len(students),
        "questions_analyzed": sorted(list(evals_by_q.keys())),
        "collusion_pairs": collusion_pairs,
        "question_clones": question_clones,
        "shared_error_signatures": shared_error_signatures,
    }
