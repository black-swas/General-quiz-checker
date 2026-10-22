"""PDF and Bulk Examination Paper Ingestion Engine.

Supports:
1. High-resolution, dependency-free rendering of PDF pages into JPEG/PNG using pypdfium2.
2. Bulk upload of student exam PDFs (e.g. Student_1.pdf, Student_2.pdf, ..., or roll_no.pdf).
3. ZIP ingestion of exams containing nested PDFs and/or images.
4. Auto-detection of student roll numbers and page ordering from filenames.
"""
from __future__ import annotations

import io
import logging
import os
import re
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
import pypdfium2 as pdfium
from PIL import Image

logger = logging.getLogger("iiserb.pdf_ingestor")

# Common patterns for student IDs in filenames
STUDENT_ID_PATTERNS = [
    re.compile(r"^(?:student|std|cand)[_\-\s]*(\d+)", re.IGNORECASE),
    re.compile(r"^(\d{6,12})", re.IGNORECASE),                         # Institutional roll numbers like 1101901107
    re.compile(r"^([a-zA-Z]{1,4}\d{4,10})", re.IGNORECASE),           # E.g. CS25104, B21001
    re.compile(r"^([a-zA-Z0-9_\-]+)", re.IGNORECASE),
]

PAGE_NUM_PATTERNS = [
    re.compile(r"[_\-\s]p(?:age)?[_\-\s]*(\d+)", re.IGNORECASE),
    re.compile(r"[_\-\s](\d+)$", re.IGNORECASE),
]


def extract_student_id_from_filename(filename: str) -> str:
    """Extract a clean, consistent student roll_no / ID from a filename."""
    stem = Path(filename).stem.strip()
    clean_stem = re.sub(r"[_\-\s]p(?:age)?[_\-\s]*\d+$", "", stem, flags=re.IGNORECASE)
    for pattern in STUDENT_ID_PATTERNS:
        m = pattern.search(clean_stem)
        if m:
            val = m.group(1).strip()
            if clean_stem.lower().startswith("student_"):
                return f"Student_{val}"
            return val
    return clean_stem or "Unknown_Student"


def extract_page_no_from_filename(filename: str, default: int = 1) -> int:
    """Extract page number from filename like 'Student_1_page_2.jpg' or '25104_p2.png'."""
    stem = Path(filename).stem.strip()
    for pattern in PAGE_NUM_PATTERNS:
        m = pattern.search(stem)
        if m:
            try:
                pno = int(m.group(1))
                if 1 <= pno <= 100:
                    return pno
            except ValueError:
                pass
    return default


def render_pdf_to_images(
    pdf_source: bytes | str | Path,
    scale: float = 1.0,
    jpeg_quality: int = 88,
    max_pages: int = 50,
) -> List[Tuple[int, bytes, Tuple[int, int]]]:
    """Render pages of a PDF document into high-quality JPEG byte buffers.

    Returns:
        List of (page_no, jpeg_bytes, (width, height)) where page_no is 1-indexed.
    """
    results: List[Tuple[int, bytes, Tuple[int, int]]] = []
    try:
        doc = pdfium.PdfDocument(pdf_source)
        total_pages = min(len(doc), max_pages)

        for i in range(total_pages):
            page_no = i + 1
            page = doc[i]
            bmp = page.render(scale=scale)
            pil_image = bmp.to_pil()
            w, h = pil_image.size

            buf = io.BytesIO()
            pil_image.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
            jpeg_bytes = buf.getvalue()
            results.append((page_no, jpeg_bytes, (w, h)))

    except Exception as exc:
        logger.error("Failed to render PDF: %s", exc, exc_info=True)
        raise ValueError(f"Could not render PDF document: {exc}") from exc

    return results


def is_pdf_buffer(data: bytes) -> bool:
    """Check if byte buffer has PDF magic header (%PDF-)."""
    return data.startswith(b"%PDF-") or (len(data) > 10 and b"%PDF-" in data[:1024])


def is_image_buffer(data: bytes) -> bool:
    """Check if byte buffer is a valid JPEG, PNG, or WEBP image."""
    if len(data) < 8:
        return False
    # JPEG magic: \xff\xd8\xff
    if data.startswith(b"\xff\xd8\xff"):
        return True
    # PNG magic: \x89PNG\r\n\x1a\n
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    # WEBP magic: RIFF....WEBP
    if data.startswith(b"RIFF") and b"WEBP" in data[8:16]:
        return True
    return False


def process_bulk_exam_files(
    files: List[Tuple[str, bytes]],
    target_roll_no: Optional[str] = None,
) -> Dict[str, List[Tuple[int, bytes, str]]]:
    """Process a heterogeneous collection of uploaded files (PDFs, ZIPs, images).

    Args:
        files: List of (filename, file_bytes).
        target_roll_no: Optional roll_no to assign all pages to if this is a single-student upload.

    Returns:
        Dict mapping roll_no -> List of (page_no, jpeg_bytes, original_filename)
    """
    students: Dict[str, List[Tuple[int, bytes, str]]] = {}

    for orig_name, raw_data in files:
        if not raw_data:
            continue

        name_lower = orig_name.lower()

        # Case 1: ZIP Archive
        if name_lower.endswith(".zip") or zipfile.is_zipfile(io.BytesIO(raw_data)):
            try:
                with zipfile.ZipFile(io.BytesIO(raw_data)) as zf:
                    for zinfo in zf.infolist():
                        if zinfo.is_dir():
                            continue
                        inner_name = zinfo.filename
                        if any(seg.startswith("__") or seg.startswith(".") for seg in Path(inner_name).parts):
                            continue
                        inner_bytes = zf.read(zinfo)
                        sub_results = process_bulk_exam_files([(inner_name, inner_bytes)])
                        for s_id, s_pages in sub_results.items():
                            students.setdefault(s_id, []).extend(s_pages)
            except Exception as exc:
                logger.warning("Could not unpack ZIP file %s: %s", orig_name, exc)
            continue

        # Case 2: PDF File
        if name_lower.endswith(".pdf") or is_pdf_buffer(raw_data):
            try:
                rendered_pages = render_pdf_to_images(raw_data, scale=1.0)
                assigned_roll = target_roll_no or extract_student_id_from_filename(orig_name)
                for pno, jpg_bytes, _ in rendered_pages:
                    students.setdefault(assigned_roll, []).append((pno, jpg_bytes, f"{orig_name}#page_{pno}"))
                logger.info("Rendered %d pages for student %s from %s", len(rendered_pages), assigned_roll, orig_name)
            except Exception as exc:
                logger.error("Failed to render PDF %s: %s", orig_name, exc)
            continue

        # Case 3: Single Image File (PNG, JPG, JPEG, WEBP)
        if is_image_buffer(raw_data) or name_lower.endswith((".jpg", ".jpeg", ".png", ".webp")):
            assigned_roll = target_roll_no or extract_student_id_from_filename(orig_name)
            pno = extract_page_no_from_filename(orig_name, default=1)
            # Ensure JPEG format
            if name_lower.endswith((".png", ".webp")):
                try:
                    img = Image.open(io.BytesIO(raw_data)).convert("RGB")
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=88)
                    raw_data = buf.getvalue()
                except Exception:
                    pass
            students.setdefault(assigned_roll, []).append((pno, raw_data, orig_name))

    # Sort each student's pages sequentially by page_no
    for s_id in students:
        students[s_id].sort(key=lambda x: x[0])

    return students
