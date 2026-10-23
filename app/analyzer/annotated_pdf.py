"""Automated Annotated Student Return PDF Generator.

Produces comprehensive, student-facing feedback PDFs stamping each student's
flattened exam page scans with:
  1. Grade summary cover scorecard (Total marks, percentage, question breakdown table).
  2. Question crop overlays with colored status badges (GREEN / BLUE / RED / GRAY).
  3. Step-by-step rubric score breakdown and TA feedback comments.
  4. ZIP archive batch generation for mass student distribution.
"""

import io
import logging
import os
import zipfile
from typing import Any, Dict, List, Optional
from PIL import Image

logger = logging.getLogger(__name__)

try:
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False


def generate_student_feedback_pdf(
    session: Dict[str, Any],
    student: Dict[str, Any],
) -> bytes:
    """Generates an annotated PDF feedback packet for an individual student."""
    buffer = io.BytesIO()

    if not REPORTLAB_AVAILABLE:
        raise RuntimeError("reportlab is required for feedback PDF generation. Please install reportlab.")

    c = canvas.Canvas(buffer, pagesize=A4)
    page_width, page_height = A4  # 595.27 x 841.89 points

    course_code = session.get("course_code", "COURSE")
    exam_name = session.get("exam_name", "Exam")
    exam_date = session.get("exam_date", "")
    total_possible = float(student.get("total_max_marks", session.get("total_marks", 100.0)) or 100.0)
    total_awarded = float(student.get("total_awarded_marks", 0.0) or 0.0)
    percentage = float(student.get("percentage", 0.0) or 0.0)
    roll_no = student.get("roll_no", "UNKNOWN")
    student_name = student.get("student_name", "")
    questions = session.get("questions", [])
    evals = student.get("evaluations", {})
    pages = student.get("pages", [])

    # ==================== PAGE 1: OFFICIAL SCORECARD ====================
    margin = 15 * mm

    # Top banner
    c.setFillColor(HexColor("#0f172a"))
    c.rect(margin, page_height - margin - 22 * mm, page_width - 2 * margin, 22 * mm, fill=1, stroke=0)

    c.setFont("Helvetica-Bold", 12)
    c.setFillColor(HexColor("#f8fafc"))
    c.drawString(margin + 5 * mm, page_height - margin - 8 * mm, "INDIAN INSTITUTE OF SCIENCE EDUCATION AND RESEARCH BHOPAL")
    c.setFont("Helvetica", 9)
    c.setFillColor(HexColor("#94a3b8"))
    c.drawString(margin + 5 * mm, page_height - margin - 14 * mm, "Department of Data Science & Engineering  |  Evaluated Exam Return Packet")

    # Embed IISER Bhopal Logo on the right of banner
    logo_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "iiserb_logo.png")
    if os.path.exists(logo_path):
        try:
            c.drawImage(logo_path, page_width - margin - 20 * mm, page_height - margin - 20 * mm, width=18 * mm, height=18 * mm, preserveAspectRatio=True, mask='auto')
        except Exception:
            pass

    # Student & Exam Info Box
    info_top = page_height - margin - 26 * mm
    c.setStrokeColor(HexColor("#cbd5e1"))
    c.setFillColor(HexColor("#f8fafc"))
    c.rect(margin, info_top - 32 * mm, page_width - 2 * margin, 32 * mm, fill=1, stroke=1)

    c.setFont("Helvetica-Bold", 10)
    c.setFillColor(HexColor("#1e293b"))
    c.drawString(margin + 6 * mm, info_top - 8 * mm, f"Roll Number:  {roll_no}")
    if student_name:
        c.drawString(margin + 70 * mm, info_top - 8 * mm, f"Student Name:  {student_name}")

    c.setFont("Helvetica", 9)
    c.setFillColor(HexColor("#475569"))
    c.drawString(margin + 6 * mm, info_top - 16 * mm, f"Course Code:   {course_code}")
    c.drawString(margin + 70 * mm, info_top - 16 * mm, f"Examination:   {exam_name} ({exam_date})")

    # Score Pill
    c.setFont("Helvetica-Bold", 14)
    c.setFillColor(HexColor("#0284c7") if percentage >= 50 else HexColor("#e11d48"))
    score_str = f"Final Score:  {total_awarded} / {total_possible}  ({percentage:.1f}%)"
    c.drawString(margin + 6 * mm, info_top - 26 * mm, score_str)

    # Questions Breakdown Table
    table_top = info_top - 38 * mm
    c.setFont("Helvetica-Bold", 10)
    c.setFillColor(HexColor("#0f172a"))
    c.drawString(margin, table_top, "QUESTION-BY-QUESTION EVALUATION BREAKDOWN")

    y_pos = table_top - 6 * mm
    # Table header
    c.setFillColor(HexColor("#e2e8f0"))
    c.rect(margin, y_pos - 6 * mm, page_width - 2 * margin, 6 * mm, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 8)
    c.setFillColor(HexColor("#334155"))
    c.drawString(margin + 3 * mm, y_pos - 4.5 * mm, "QID")
    c.drawString(margin + 16 * mm, y_pos - 4.5 * mm, "Description")
    c.drawString(margin + 80 * mm, y_pos - 4.5 * mm, "Max")
    c.drawString(margin + 94 * mm, y_pos - 4.5 * mm, "Awarded")
    c.drawString(margin + 114 * mm, y_pos - 4.5 * mm, "Status")
    c.drawString(margin + 138 * mm, y_pos - 4.5 * mm, "TA / Grader Notes")

    y_pos -= 6 * mm

    for q in questions:
        row_h = 7 * mm
        if y_pos - row_h < margin + 18 * mm:
            c.setFont("Helvetica-Oblique", 7)
            c.setFillColor(HexColor("#64748b"))
            c.drawString(margin, margin + 8 * mm, f"Continued on next page... (Roll No: {roll_no})")
            c.showPage()

            # Continuation table header
            y_pos = page_height - margin - 10 * mm
            c.setFont("Helvetica-Bold", 9)
            c.setFillColor(HexColor("#0f172a"))
            c.drawString(margin, y_pos, f"QUESTION BREAKDOWN (CONTINUED) — {roll_no}")
            y_pos -= 6 * mm
            c.setFillColor(HexColor("#e2e8f0"))
            c.rect(margin, y_pos - 6 * mm, page_width - 2 * margin, 6 * mm, fill=1, stroke=0)
            c.setFont("Helvetica-Bold", 8)
            c.setFillColor(HexColor("#334155"))
            c.drawString(margin + 3 * mm, y_pos - 4.5 * mm, "QID")
            c.drawString(margin + 16 * mm, y_pos - 4.5 * mm, "Description")
            c.drawString(margin + 80 * mm, y_pos - 4.5 * mm, "Max")
            c.drawString(margin + 94 * mm, y_pos - 4.5 * mm, "Awarded")
            c.drawString(margin + 114 * mm, y_pos - 4.5 * mm, "Status")
            c.drawString(margin + 138 * mm, y_pos - 4.5 * mm, "TA / Grader Notes")
            y_pos -= 6 * mm

        qid = q.get("question_id", "Q")
        max_m = float(q.get("max_marks", 5.0))
        ev = evals.get(qid) or evals.get(str(qid).upper()) or evals.get(str(qid).lower()) or {}
        awarded = float(ev.get("final_marks", ev.get("ai_awarded_marks", 0.0)) or 0.0)
        status = ev.get("ta_status", "PENDING")
        tab = ev.get("verdict_tab", "GRAY")
        comment = ev.get("ta_comment") or ev.get("ai_comment") or "-"
        if len(comment) > 42:
            comment = comment[:39] + "..."

        c.setStrokeColor(HexColor("#e2e8f0"))
        c.line(margin, y_pos - row_h, page_width - margin, y_pos - row_h)

        c.setFont("Helvetica-Bold", 8)
        c.setFillColor(HexColor("#0f172a"))
        c.drawString(margin + 3 * mm, y_pos - 5 * mm, str(qid))

        c.setFont("Helvetica", 7.5)
        c.setFillColor(HexColor("#475569"))
        desc = str(q.get("problem_statement", ""))[:32]
        c.drawString(margin + 16 * mm, y_pos - 5 * mm, desc)
        c.drawString(margin + 80 * mm, y_pos - 5 * mm, f"{max_m:.1f}")

        c.setFont("Helvetica-Bold", 8)
        if tab == "GREEN":
            c.setFillColor(HexColor("#16a34a"))
        elif tab == "RED":
            c.setFillColor(HexColor("#dc2626"))
        elif tab == "BLUE":
            c.setFillColor(HexColor("#2563eb"))
        else:
            c.setFillColor(HexColor("#64748b"))
        c.drawString(margin + 94 * mm, y_pos - 5 * mm, f"{awarded:.1f}")

        c.setFont("Helvetica", 7.5)
        c.drawString(margin + 114 * mm, y_pos - 5 * mm, status)
        c.setFillColor(HexColor("#334155"))
        c.drawString(margin + 138 * mm, y_pos - 5 * mm, comment)

        y_pos -= row_h

    # Instructions at bottom of Page 1
    c.setFont("Helvetica-Bold", 8)
    c.setFillColor(HexColor("#0284c7"))
    c.drawString(margin, margin + 14 * mm, "REGULATION & REGRADE NOTICE:")
    c.setFont("Helvetica", 7.5)
    c.setFillColor(HexColor("#64748b"))
    c.drawString(margin, margin + 10 * mm, "If you identify a discrepancy in step scoring, submit an online regrade appeal within 48 hours.")
    c.drawString(margin, margin + 6 * mm, "Stamped scanned pages follow with highlighted rubric bounding boxes and grader deductions.")

    c.showPage()

    # ==================== SUBSEQUENT PAGES: ANNOTATED SCANS ====================
    # Sort pages by page_no
    sorted_pages = sorted(pages, key=lambda p: int(p.get("page_no", 1) or 1))

    for p in sorted_pages:
        p_no = int(p.get("page_no", 1) or 1)
        raw_path = p.get("processed_path") or p.get("raw_path")

        # Page Header
        c.setFont("Helvetica-Bold", 8)
        c.setFillColor(HexColor("#64748b"))
        c.drawString(margin, page_height - 10 * mm, f"Roll: {roll_no}  |  {course_code} {exam_name}  |  Page {p_no}")

        img_drawn = False
        img_x = margin
        img_y = margin
        img_w = page_width - 2 * margin
        img_h = page_height - 2 * margin - 8 * mm

        if raw_path and os.path.exists(raw_path):
            try:
                pil_img = Image.open(raw_path)
                c.drawInlineImage(pil_img, img_x, img_y, width=img_w, height=img_h)
                img_drawn = True
            except Exception:
                img_drawn = False

        if not img_drawn:
            c.setStrokeColor(HexColor("#cbd5e1"))
            c.rect(img_x, img_y, img_w, img_h, stroke=1, fill=0)
            c.drawString(img_x + 20 * mm, img_y + img_h / 2.0, f"[Scanned Image for Page {p_no}]")

        # Find questions on this page
        page_qs = [q for q in questions if int(q.get("page_number", 1) or 1) == p_no]

        for q in page_qs:
            qid = q.get("question_id", "Q")
            ev = evals.get(qid) or evals.get(str(qid).upper()) or evals.get(str(qid).lower()) or {}
            x0_pct = float(q.get("x_start_pct", 0.0)) / 100.0
            x1_pct = float(q.get("x_end_pct", 100.0)) / 100.0
            y0_pct = float(q.get("y_start_pct", 10.0)) / 100.0
            y1_pct = float(q.get("y_end_pct", 90.0)) / 100.0
            max_m = float(q.get("max_marks", 5.0))
            awarded = float(ev.get("final_marks", ev.get("ai_awarded_marks", 0.0)) or 0.0)
            tab = ev.get("verdict_tab", "GRAY")
            ta_comment = ev.get("ta_comment") or ev.get("ai_comment") or ""

            # Coordinates in PDF points (ReportLab origin at bottom-left)
            # y0_pct is top of question, y1_pct is bottom of question
            box_top = (img_y + img_h) - (y0_pct * img_h)
            box_bottom = (img_y + img_h) - (y1_pct * img_h)
            actual_h = max(15 * mm, box_top - box_bottom)
            actual_bottom = box_top - actual_h
            box_x = img_x + (x0_pct * img_w) + 2 * mm
            box_w = max(40 * mm, ((x1_pct - x0_pct) * img_w) - 4 * mm)

            # Set border color based on verdict tab
            if tab == "GREEN":
                border_color = HexColor("#16a34a")
                badge_bg = HexColor("#dcfce7")
            elif tab == "RED":
                border_color = HexColor("#dc2626")
                badge_bg = HexColor("#fee2e2")
            elif tab == "BLUE":
                border_color = HexColor("#2563eb")
                badge_bg = HexColor("#dbeafe")
            else:
                border_color = HexColor("#64748b")
                badge_bg = HexColor("#f1f5f9")

            # Draw question box border
            c.setStrokeColor(border_color)
            c.setLineWidth(1.8)
            c.rect(box_x, actual_bottom, box_w, actual_h, stroke=1, fill=0)

            # Draw Score Badge in upper-right corner of box
            badge_w = min(48 * mm, box_w - 6 * mm)
            badge_h = 6 * mm
            badge_x = box_x + box_w - badge_w - 2 * mm
            badge_y = box_top - badge_h - 2 * mm
            c.setFillColor(badge_bg)
            c.rect(badge_x, badge_y, badge_w, badge_h, stroke=0, fill=1)

            c.setFont("Helvetica-Bold", 8)
            c.setFillColor(border_color)
            c.drawString(badge_x + 2 * mm, badge_y + 1.8 * mm, f"{qid}: {awarded:.1f} / {max_m:.1f} Marks")

            # Draw TA feedback callout note if comment exists
            if ta_comment:
                avail_w = max(20 * mm, badge_x - box_x - 6 * mm)
                note_w = min(avail_w, 90 * mm)
                note_h = 6 * mm
                note_x = box_x + 3 * mm
                note_y = box_top - note_h - 2 * mm
                c.setFillColor(HexColor("#ffffff"))
                c.setStrokeColor(border_color)
                c.setLineWidth(0.6)
                c.rect(note_x, note_y, note_w, note_h, stroke=1, fill=1)

                c.setFont("Helvetica-Oblique", 7)
                c.setFillColor(HexColor("#1e293b"))
                trunc_note = ta_comment[:55] + "..." if len(ta_comment) > 55 else ta_comment
                c.drawString(note_x + 2 * mm, note_y + 1.8 * mm, f"Note: {trunc_note}")

        c.showPage()

    c.save()
    return buffer.getvalue()


def generate_feedback_zip(
    session: Dict[str, Any],
) -> bytes:
    """Generates a ZIP archive containing annotated feedback PDFs for all students in the session."""
    zip_buffer = io.BytesIO()
    students = session.get("students", [])
    errors: List[str] = []

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for st in students:
            roll = st.get("roll_no", "unknown")
            try:
                pdf_bytes = generate_student_feedback_pdf(session, st)
                filename = f"{roll}_Feedback.pdf"
                zf.writestr(filename, pdf_bytes)
            except Exception as e:
                err_msg = f"Failed to generate feedback PDF for student {roll}: {str(e)}"
                logger.error(err_msg)
                errors.append(err_msg)
                zf.writestr(f"{roll}_ERROR.txt", err_msg)

        if errors:
            zf.writestr("_EXPORT_ERRORS.txt", "\n".join(errors))

    return zip_buffer.getvalue()
