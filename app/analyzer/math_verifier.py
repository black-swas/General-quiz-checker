"""Symbolic Math & SQL Verification Engine for Polyglot Quiz Evaluator.

Provides 100% deterministic verification for mathematical derivations and SQL queries:
  1. Boxed answer & LaTeX expression extraction from OCR transcriptions.
  2. Symbolic equivalence checking via SymPy:
     - Handles radical normalization (e.g. 1/sqrt(2) == sqrt(2)/2)
     - Fraction equivalence (0.5 == 1/2)
     - Factored / expanded algebraic identities (e.g. (x+1)^2 == x^2 + 2x + 1)
     - Trigonometric identities (e.g. sin^2(x) + cos^2(x) == 1)
     - Floating point numerical tolerance matching
  3. In-memory SQLite table harness for deterministic SQL query verification.
"""

import math
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

try:
    import sympy
    from sympy.parsing.sympy_parser import parse_expr, standard_transformations, implicit_multiplication_application
    SYMPY_AVAILABLE = True
except ImportError:
    sympy = None
    SYMPY_AVAILABLE = False


_BOXED_RE = re.compile(r"\\boxed\s*\{([^{}]+(?:\s*\{[^{}]*\}\s*)*)\}", re.DOTALL)
_ANS_KEYWORD_RE = re.compile(r"(?:ans|answer|final\s*answer)\s*(?:=|:|\bis\b)\s*([^\n\r;]+)", re.IGNORECASE)
_EQUATION_RE = re.compile(r"(?:[a-zA-Z_]\w*\s*=\s*)([^\n\r;]+)")


def clean_latex_to_algebra(text: str) -> str:
    """Normalizes LaTeX notation into standard mathematical expression syntax."""
    s = text.strip()
    # Strip dollar signs and formatting
    s = s.replace("$", "").replace("\\,", " ").replace("\\;", " ").replace("\\!", "")
    s = s.replace("\\left", "").replace("\\right", "")
    s = s.replace("\\cdot", "*").replace("\\times", "*")
    s = s.replace("^", "**")

    # Strip \boxed{...} wrapper if present
    while "\\boxed" in s:
        pos = s.find("\\boxed")
        b_open = s.find("{", pos)
        if b_open == -1:
            break
        depth = 1
        cur = b_open + 1
        while cur < len(s) and depth > 0:
            if s[cur] == '{':
                depth += 1
            elif s[cur] == '}':
                depth -= 1
            cur += 1
        if depth == 0:
            inner = s[b_open + 1:cur - 1]
            s = s[:pos] + inner + s[cur:]
        else:
            break

    # Replace \frac{A}{B} with ((A)/(B)) using balanced braces
    while "\\frac" in s:
        pos = s.find("\\frac")
        b1 = s.find("{", pos)
        if b1 == -1:
            break
        depth = 1
        cur = b1 + 1
        while cur < len(s) and depth > 0:
            if s[cur] == '{':
                depth += 1
            elif s[cur] == '}':
                depth -= 1
            cur += 1
        num = s[b1 + 1:cur - 1]

        b2 = s.find("{", cur)
        if b2 == -1:
            break
        depth = 1
        cur2 = b2 + 1
        while cur2 < len(s) and depth > 0:
            if s[cur2] == '{':
                depth += 1
            elif s[cur2] == '}':
                depth -= 1
            cur2 += 1
        den = s[b2 + 1:cur2 - 1]
        s = s[:pos] + f"(({num})/({den}))" + s[cur2:]

    # Replace \sqrt{A} with sqrt(A) using balanced braces
    while "\\sqrt" in s:
        pos = s.find("\\sqrt")
        b = s.find("{", pos)
        if b == -1:
            break
        depth = 1
        cur = b + 1
        while cur < len(s) and depth > 0:
            if s[cur] == '{':
                depth += 1
            elif s[cur] == '}':
                depth -= 1
            cur += 1
        inner = s[b + 1:cur - 1]
        s = s[:pos] + f"sqrt({inner})" + s[cur:]

    # Common functions & constants
    s = re.sub(r"\\pi\b", "pi", s)
    s = re.sub(r"\\sin\b", "sin", s)
    s = re.sub(r"\\cos\b", "cos", s)
    s = re.sub(r"\\tan\b", "tan", s)
    s = re.sub(r"\\ln\b", "log", s)
    s = re.sub(r"\\log\b", "log", s)
    s = re.sub(r"\\exp\b", "exp", s)
    s = s.replace("{", "(").replace("}", ")")

    return s.strip()


def extract_candidate_answers(text: str) -> List[str]:
    """Extracts candidate answer expressions from an OCR transcription or solution string."""
    if not text:
        return []

    candidates: List[str] = []

    # 1. LaTeX \boxed{...} matches with balanced brace handling
    tag = r"\boxed{"
    idx = 0
    while True:
        pos = text.find(tag, idx)
        if pos == -1:
            break
        start = pos + len(tag)
        depth = 1
        cur = start
        while cur < len(text) and depth > 0:
            if text[cur] == '{':
                depth += 1
            elif text[cur] == '}':
                depth -= 1
            cur += 1
        if depth == 0:
            content = text[start:cur - 1].strip()
            if content:
                candidates.append(content)
            idx = cur
        else:
            break

    # 2. Answer keyword matches ("Answer: 42", "Ans = 1/2")
    for m in _ANS_KEYWORD_RE.finditer(text):
        content = m.group(1).strip()
        if content:
            candidates.append(content)

    # 3. Last line with an assignment or equation
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    for line in reversed(lines):
        eq_m = _EQUATION_RE.search(line)
        if eq_m:
            candidates.append(eq_m.group(1).strip())
            break
        # Or last non-empty line
        if len(candidates) == 0 and len(line) < 120 and any(ch in line for ch in "+-*/=^\\0123456789"):
            candidates.append(line)
            break

    # If still none, add the raw text if short
    if not candidates and len(text.strip()) < 120:
        candidates.append(text.strip())

    return candidates


def parse_symbolic_expression(expr_str: str) -> Optional[Any]:
    """Parses a cleaned math string into a SymPy expression object."""
    if not SYMPY_AVAILABLE:
        return None

    cleaned = clean_latex_to_algebra(expr_str)
    if not cleaned:
        return None

    # Try SymPy parsing with implicit multiplication
    try:
        transformations = standard_transformations + (implicit_multiplication_application,)
        return parse_expr(cleaned, transformations=transformations)
    except Exception:
        pass

    # Fallback to direct sympify
    try:
        return sympy.sympify(cleaned)
    except Exception:
        return None


def verify_symbolic_equivalence(
    student_text: str,
    reference_text: str,
    rel_tol: float = 1e-4,
    abs_tol: float = 1e-6,
) -> Dict[str, Any]:
    """Tests if a student's answer is symbolically or numerically equivalent to the reference.
    
    Returns:
      {
        "verified": bool,
        "match_type": "EXACT_SYMBOLIC" | "TRIG_SIMPLIFIED" | "NUMERICAL_EQUIVALENT" | "EXACT_STRING" | "NONE",
        "student_candidate": str,
        "reference_candidate": str,
        "confidence": float
      }
    """
    st_candidates = extract_candidate_answers(student_text)
    ref_candidates = extract_candidate_answers(reference_text) or [reference_text.strip()]

    # Check trivial string match first
    for sc in st_candidates:
        cleaned_sc = clean_latex_to_algebra(sc).replace(" ", "")
        if not cleaned_sc or not any(ch.isalnum() for ch in cleaned_sc):
            continue
        for rc in ref_candidates:
            cleaned_rc = clean_latex_to_algebra(rc).replace(" ", "")
            if cleaned_sc == cleaned_rc:
                return {
                    "verified": True,
                    "match_type": "EXACT_STRING",
                    "student_candidate": sc,
                    "reference_candidate": rc,
                    "confidence": 1.0,
                }

    if not SYMPY_AVAILABLE:
        return {"verified": False, "match_type": "NONE", "confidence": 0.0}

    # Test SymPy algebraic equivalence across all candidate pairs
    for sc in st_candidates:
        s_expr = parse_symbolic_expression(sc)
        if s_expr is None:
            continue

        for rc in ref_candidates:
            r_expr = parse_symbolic_expression(rc)
            if r_expr is None:
                continue

            try:
                # 1. Algebraic simplification: simplify(s - r) == 0
                diff = sympy.simplify(s_expr - r_expr)
                if diff == 0:
                    return {
                        "verified": True,
                        "match_type": "EXACT_SYMBOLIC",
                        "student_candidate": sc,
                        "reference_candidate": rc,
                        "confidence": 1.0,
                    }

                # 2. Trigonometric simplification: trigsimp(diff) == 0
                if hasattr(sympy, "trigsimp") and sympy.trigsimp(diff) == 0:
                    return {
                        "verified": True,
                        "match_type": "TRIG_SIMPLIFIED",
                        "student_candidate": sc,
                        "reference_candidate": rc,
                        "confidence": 1.0,
                    }

                # 3. Numerical float evaluation (only if expressions contain no free symbols)
                if not s_expr.free_symbols and not r_expr.free_symbols:
                    s_val = complex(s_expr.evalf())
                    r_val = complex(r_expr.evalf())
                    if abs(s_val.imag) < 1e-9 and abs(r_val.imag) < 1e-9:
                        diff_val = abs(s_val.real - r_val.real)
                        target = abs(r_val.real)
                        if diff_val <= max(abs_tol, rel_tol * target):
                            return {
                                "verified": True,
                                "match_type": "NUMERICAL_EQUIVALENT",
                                "student_candidate": sc,
                                "reference_candidate": rc,
                                "confidence": 0.98,
                            }
            except Exception:
                continue

    return {
        "verified": False,
        "match_type": "NONE",
        "student_candidate": st_candidates[0] if st_candidates else "",
        "reference_candidate": ref_candidates[0] if ref_candidates else "",
        "confidence": 0.0,
    }


def _sql_read_only_authorizer(action: int, arg1: Any, arg2: Any, dbname: Any, source: Any) -> int:
    # Only permit SELECT, READ, and FUNCTION calls; block ATTACH, DETACH, PRAGMA, DDL, write
    allowed_actions = {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
    }
    return sqlite3.SQLITE_OK if action in allowed_actions else sqlite3.SQLITE_DENY


def run_sql_in_memory_sandbox(
    schema_sql: str,
    student_query: str,
    reference_query: str,
    match_mode: str = "exact",
) -> Dict[str, Any]:
    """Executes a student's SQL query and reference query against an in-memory SQLite database.
    
    Ensures safe, deterministic verification of SQL submissions with strict query-only authorization.
    """
    for forbidden in ("ATTACH", "DETACH", "PRAGMA", "LOAD_EXTENSION", "VACUUM"):
        if re.search(rf"\b{forbidden}\b", student_query, re.I):
            return {
                "passed": False,
                "error": f"Forbidden SQL operation: {forbidden} is not permitted in student queries",
                "expected_rows": [],
                "actual_rows": [],
            }

    conn = sqlite3.connect(":memory:")
    try:
        # Run schema setup
        conn.executescript(schema_sql)

        # Run reference query
        ref_cursor = conn.execute(reference_query)
        ref_rows = ref_cursor.fetchall()
        col_names = [d[0] for d in ref_cursor.description] if ref_cursor.description else []

        # Run student query under strict read-only authorizer
        conn.set_authorizer(_sql_read_only_authorizer)
        try:
            st_cursor = conn.execute(student_query)
            st_rows = st_cursor.fetchall()
        except sqlite3.Error as e:
            return {
                "passed": False,
                "error": f"SQL Execution Error: {str(e)}",
                "expected_rows": ref_rows,
                "actual_rows": [],
            }
        finally:
            conn.set_authorizer(None)

        if match_mode == "unordered_set":
            passed = set(map(tuple, st_rows)) == set(map(tuple, ref_rows))
        else:
            passed = (st_rows == ref_rows)

        return {
            "passed": passed,
            "error": None,
            "columns": col_names,
            "expected_rows": ref_rows,
            "actual_rows": st_rows,
        }
    finally:
        conn.close()
