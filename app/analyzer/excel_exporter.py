"""Clean Multi-Sheet Excel (.xlsx) Gradebook Exporter using openpyxl.

Exports a professional, formula-Safe Excel workbook containing:
- Sheet 1 (`Student Marks Summary`): Roll No, Student Name, per-question marks (`Q1..Qk`), Total Marks,
  Max Marks, Percentage, Review Status (`Auto-Approved` / `TA Approved` / `Needs Review`), and Consolidated Comment.
- Sheet 2 (`Detailed Question Feedback`): Roll No, Student Name, Question ID, Awarded Marks, Max Marks,
  Confidence (0.0-1.0), Legibility, Verdict Tab (`GREEN`/`BLUE`/`RED`/`GRAY`), OCR Transcription, and TA/AI Comment.
"""
from __future__ import annotations

import io
import re
from typing import Any, Dict, List

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter


def _safe_excel_cell(val: Any) -> Any:
    """Prevent Excel formula injection (`=`, `+`, `-`, `@`) on untrusted student/OCR strings."""
    if isinstance(val, (int, float)):
        return val
    s = str(val or "").strip()
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


def build_session_excel_bytes(session_data: Dict[str, Any]) -> bytes:
    """Generate a clean .xlsx workbook from a paper-exam session dictionary."""
    wb = openpyxl.Workbook()
    ws_summary = wb.active
    ws_summary.title = "Student Marks Summary"
    ws_detail = wb.create_sheet(title="Detailed Question Breakdown")

    # Styling definitions
    header_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    meta_fill = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid")
    meta_font = Font(name="Calibri", size=11, bold=True, color="0F172A")
    body_font = Font(name="Calibri", size=11, color="0F172A")
    bold_font = Font(name="Calibri", size=11, bold=True, color="0F172A")

    green_fill = PatternFill(start_color="DCFCE7", end_color="DCFCE7", fill_type="solid")
    red_fill = PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid")
    blue_fill = PatternFill(start_color="DBEAFE", end_color="DBEAFE", fill_type="solid")
    gray_fill = PatternFill(start_color="F3F4F6", end_color="F3F4F6", fill_type="solid")

    thin_side = Side(border_style="thin", color="CBD5E1")
    cell_border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)

    questions: List[Dict[str, Any]] = session_data.get("questions") or []
    q_ids = [str(q.get("question_id", f"Q{i+1}")) for i, q in enumerate(questions)]
    total_max = float(session_data.get("total_marks") or sum(float(q.get("max_marks", 5.0)) for q in questions) or 100.0)

    # ---- Headers on Sheet 1 ----
    summary_headers = [
        "Roll No",
        "Student Name",
        "Pages Scanned",
        *[f"{qid} ({q.get('max_marks', 5)}M)" for qid, q in zip(q_ids, questions)],
        "Total Marks",
        "Max Marks",
        "Percentage (%)",
        "Triage Status",
        "Review State",
        "Small Comment / Feedback",
    ]

    # ---- Banner Row on Sheet 1 ----
    title_str = (
        f"{session_data.get('course_code', 'COURSE')} — {session_data.get('exam_name', 'Exam')} "
        f"| Date: {session_data.get('exam_date', '-')} | Session ID: {session_data.get('session_id', '-')} "
        f"| Total Marks: {total_max}"
    )
    ws_summary.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(summary_headers))
    top_cell = ws_summary.cell(row=1, column=1, value=title_str)
    top_cell.font = meta_font
    top_cell.fill = meta_fill
    top_cell.alignment = Alignment(horizontal="left", vertical="center")
    ws_summary.row_dimensions[1].height = 24

    for col_idx, h_text in enumerate(summary_headers, start=1):
        c = ws_summary.cell(row=3, column=col_idx, value=h_text)
        c.fill = header_fill
        c.font = header_font
        c.alignment = Alignment(horizontal="center" if col_idx > 2 else "left", vertical="center", wrap_text=True)
        c.border = cell_border
    ws_summary.row_dimensions[3].height = 26

    # ---- Rows on Sheet 1 & Sheet 2 ----
    detail_headers = [
        "Roll No",
        "Student Name",
        "Question ID",
        "Question Title",
        "Awarded Marks",
        "Max Marks",
        "Confidence (0-1)",
        "Legibility (0-1)",
        "Queue Tab",
        "Approval Status",
        "Comment / Feedback",
        "Handwritten OCR Transcription",
    ]
    for col_idx, h_text in enumerate(detail_headers, start=1):
        c = ws_detail.cell(row=1, column=col_idx, value=h_text)
        c.fill = header_fill
        c.font = header_font
        c.alignment = Alignment(horizontal="center" if 5 <= col_idx <= 10 else "left", vertical="center")
        c.border = cell_border
    ws_detail.row_dimensions[1].height = 24

    students = session_data.get("students") or []
    detail_row_idx = 2

    for s_idx, student in enumerate(students, start=4):
        roll_no = str(student.get("roll_no", ""))
        student_name = str(student.get("student_name", ""))
        pages_cnt = int(student.get("pages_uploaded", 0))
        evals: Dict[str, Dict[str, Any]] = student.get("evaluations") or {}

        q_marks_list: List[float] = []
        comments_list: List[str] = []
        overall_tab = str(student.get("overall_tab", "GRAY"))
        all_approved = True

        for q in questions:
            qid = str(q.get("question_id", "Q1"))
            q_max = float(q.get("max_marks", 5.0))
            ev = evals.get(qid) or evals.get(qid.upper()) or evals.get(qid.lower())
            if ev:
                final_m = float(ev.get("final_marks", ev.get("ai_awarded_marks", 0.0)) or 0.0)
                q_marks_list.append(round(final_m, 2))
                note = str(ev.get("ta_comment") or ev.get("ai_comment") or "").strip()
                if note:
                    comments_list.append(f"{qid}: {note}")
                ta_stat = str(ev.get("ta_status", "PENDING"))
                if ta_stat == "PENDING":
                    all_approved = False

                # Populate Sheet 2 row
                d_vals = [
                    _safe_excel_cell(roll_no),
                    _safe_excel_cell(student_name),
                    qid,
                    _safe_excel_cell(q.get("title", "")),
                    round(final_m, 2),
                    q_max,
                    round(float(ev.get("confidence", 0.0) or 0.0), 2),
                    round(float(ev.get("legibility_score", 1.0) or 1.0), 2),
                    str(ev.get("verdict_tab", "GRAY")),
                    ta_stat,
                    _safe_excel_cell(note),
                    _safe_excel_cell(ev.get("ocr_transcription", "")),
                ]
                for c_i, v in enumerate(d_vals, start=1):
                    dc = ws_detail.cell(row=detail_row_idx, column=c_i, value=v)
                    dc.font = body_font
                    dc.border = cell_border
                    if c_i == 9:
                        dc.fill = green_fill if v == "GREEN" else (red_fill if v == "RED" else (blue_fill if v == "BLUE" else gray_fill))
                detail_row_idx += 1
            else:
                q_marks_list.append(0.0)
                all_approved = False

        total_awarded = round(sum(q_marks_list), 2)
        pct = round((100.0 * total_awarded) / max(1.0, total_max), 2)
        review_state = "Approved" if (evals and all_approved) else ("Needs TA Review" if overall_tab == "RED" else "Pending")
        combined_comment = " | ".join(comments_list) if comments_list else " Clean evaluation."

        row_values = [
            _safe_excel_cell(roll_no),
            _safe_excel_cell(student_name),
            pages_cnt,
            *q_marks_list,
            total_awarded,
            total_max,
            pct,
            overall_tab,
            review_state,
            _safe_excel_cell(combined_comment),
        ]
        for c_i, val in enumerate(row_values, start=1):
            cell = ws_summary.cell(row=s_idx, column=c_i, value=val)
            cell.font = bold_font if c_i in (1, len(row_values) - 5) else body_font
            cell.border = cell_border
            if isinstance(val, (int, float)) and c_i >= 3:
                cell.alignment = Alignment(horizontal="right", vertical="center")
            else:
                cell.alignment = Alignment(horizontal="left", vertical="center")
            if c_i == len(row_values) - 2:
                cell.fill = green_fill if val == "GREEN" else (red_fill if val == "RED" else (blue_fill if val == "BLUE" else gray_fill))
                cell.alignment = Alignment(horizontal="center", vertical="center")

    # Auto-fit column widths across both sheets
    for ws in (ws_summary, ws_detail):
        ws.views.sheetView[0].showGridLines = True
        for col in ws.columns:
            col_letter = get_column_letter(col[0].column)
            max_len = max((len(str(c.value or "")) for c in col if c.row > 1), default=12)
            ws.column_dimensions[col_letter].width = max(12, min(52, max_len + 4))

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
