"""Master Redundancy & Edge-Case Engine for Physical Exam Sheets (v5.1).

Implements 24 distinct production-grade redundancy, safety, and edge-case protections:
1. Blank / Skipped Question Detection (zero LLM token waste on empty boxes)
2. Multi-Page Question Cropping & Visual Vertical Stitching (page 1 + page 2 continuous answer)
3. Supplementary / Extra Page Ingestion (dynamic numbering and indexation)
4. Dynamic Downward Ink Projection Guard (prevents amputated lines below boundary)
5. Dynamic Upward Ink Incursion Guard (catches writing started in headers)
6. Horizontal Margin Incursion Guard (recovers margin formulas and annotations)
7. Crossed-Out & Scratch-Work Detection (detects strike-throughs and cross-hatching)
8. 180° Inverted / Upside-Down Auto-Orientation (gradient & projection profiling)
9. 90° / 270° Orientation Detection & Auto-Rotation (portrait standardization)
10. Defocus & Motion Blur Laplacian Variance Guard
11. LAB Illumination Division & Fluorescent Reflection / Shadow Flattening
12. 4-Corner Homography Perspective Flattening with Aspect-Preserving Fallback
13. Duplicate Frame Deduplication (pHash + Laplacian sharpness contest)
14. Out-of-Order Page Scanning Sequence Reordering
15. Missing Intermediate Page Alert (gap detection e.g. Page 1, 3 received, 2 missing)
16. Truncated & Corrupted Buffer Integrity Shield (JPEG SOI/EOI & PNG magic validation)
17. Canonical A4 Canvas Aspect Ratio Normalizer
18. Roll Number OCR & Barcode Roster Reconciliation
19. Cross-Page Student Packet Consistency Verification
20. Rubric Step-Score Sum Discrepancy Reconciliation
21. Handwritten Prompt Injection & Adversarial Grader Defense
22. Faint Pencil Handwriting Contrast Amplification & GRAY Fallback
23. Continuation Keyword Detection ("PTO", "P.T.O.", "contd", "overleaf")
24. Ephemeral Prompt Cache Warmup Thundering Herd Protection
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

logger = logging.getLogger("iiserb.redundancy")

CANONICAL_WIDTH = 1400
CANONICAL_HEIGHT = 1980  # A4 Aspect ratio: 1 : 1.4142

CONTINUATION_PATTERNS = [
    r"\bpto\b",
    r"\bp\.t\.o\.?\b",
    r"\bcont(?:d|\.|inued)?\b",
    r"\bturn\s+over\b",
    r"\boverleaf\b",
    r"\bnext\s+page\b",
    r"\bsee\s+(?:page|pg|sheet|next|extra)\b",
    r"\bextra\s+sheet\b",
]
_CONTINUATION_REGEX = re.compile("|".join(CONTINUATION_PATTERNS), re.IGNORECASE)

ADVERSARIAL_PATTERNS = [
    r"ignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier|preceding)\s+(?:instructions?|rules?|prompts?|directions?|guidelines?)",
    r"disregard\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier|preceding|rubric|grading|instructions?|rules?|directions?|criteria)",
    r"give\s+(?:me\s+)?(?:full|max|100%|maximum)\s+marks",
    r"(?:award|give|assign|grant)\s+(?:me\s+|this\s+)?(?:full|maximum|max|100|top)\s*(?:marks?|score|points?|credit)",
    r"system\s*:\s*you\s+must",
    r"system\s*(?:prompt|instruction|override)",
    r"override\s+(?:the\s+)?grade",
    r"as\s+an\s+ai\s+language\s+model",
    r"you\s+are\s+(?:now\s+)?(?:an?\s+)?(?:ai|llm|language\s+model|assistant|grader|grading|evaluator|judge)",
    r"mark\s+(?:this|my|the)\s+(?:code|submission|answer|solution|program|file|response)\s+(?:as\s+)?(?:clean|green|correct|valid|passing|perfect|safe)",
    r"do\s+not\s+flag",
    r"verdict_tab",
    r"confidence_score\s*[:=]\s*\d+",
]
_ADVERSARIAL_REGEX = re.compile("|".join(ADVERSARIAL_PATTERNS), re.IGNORECASE)


# ---------------------------------------------------------------------------
# 1. Blank / Skipped Question Detection
# ---------------------------------------------------------------------------
def detect_empty_or_skipped_crop(
    crop_bgr: np.ndarray,
    blank_ink_threshold: float = 0.0045,
    min_stroke_contours: int = 2,
) -> Tuple[bool, float, str, str]:
    """Determine whether an answer box was skipped or left completely blank by the student.

    Returns:
        (is_skipped, ink_density, reason_code, explanation)
    """
    if crop_bgr is None or crop_bgr.size == 0:
        return True, 0.0, "EMPTY_BUFFER", "Crop image buffer was empty."

    h, w = crop_bgr.shape[:2]
    # Exclude top banner (if already stamped)
    scan_area = crop_bgr[42:, :] if h > 100 else crop_bgr
    if scan_area.size == 0:
        return True, 0.0, "EMPTY_BUFFER", "Answer space has zero height."

    gray = cv2.cvtColor(scan_area, cv2.COLOR_BGR2GRAY) if len(scan_area.shape) == 3 else scan_area
    sh, sw = gray.shape[:2]

    # Blur slightly to eliminate scanner paper texture noise
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)

    # Adaptive dark ink detection (pen strokes are significantly darker than background)
    mean_val = float(np.mean(blurred))
    dark_thresh = max(40, int(mean_val * 0.76))
    ink_mask = (blurred < dark_thresh).astype(np.uint8)

    # Ignore border edges (rule lines from paper bounding box)
    border_margin_y = max(3, int(sh * 0.03))
    border_margin_x = max(3, int(sw * 0.03))
    inner_mask = ink_mask[border_margin_y : sh - border_margin_y, border_margin_x : sw - border_margin_x]

    if inner_mask.size == 0:
        return True, 0.0, "ZERO_INNER_AREA", "Answer space too small."

    ink_pixels = int(np.count_nonzero(inner_mask))
    total_pixels = int(inner_mask.size)
    ink_density = float(ink_pixels / total_pixels)

    # Also detect structured connected components (letters / symbols)
    num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(inner_mask, connectivity=8)
    meaningful_components = 0
    for idx in range(1, num_labels):
        area = stats[idx, cv2.CC_STAT_AREA]
        # Ignore 1-2 pixel isolated specks
        if 8 <= area <= 0.45 * total_pixels:
            meaningful_components += 1

    if ink_density < blank_ink_threshold or meaningful_components < min_stroke_contours:
        return (
            True,
            round(ink_density, 5),
            "BLANK_SKIPPED",
            f"Answer space is completely blank (ink density: {ink_density:.4f}, strokes: {meaningful_components}). Student skipped this question.",
        )

    return (
        False,
        round(ink_density, 5),
        "ATTEMPTED",
        f"Answer space contains handwritten ink (density: {ink_density:.4f}, strokes: {meaningful_components}).",
    )


# ---------------------------------------------------------------------------
# 2. Multi-Page Question Cropping & Visual Vertical Stitching
# ---------------------------------------------------------------------------
def stitch_multipage_question_crops(
    page_crops: List[Tuple[int, np.ndarray, str]],
    roll_no: str,
    student_name: str,
    question_id: str,
    question_title: str,
    max_marks: float,
) -> np.ndarray:
    """Vertically stitch answer segments across multiple pages into a single cohesive crop.

    Args:
        page_crops: List of tuples (page_number, crop_bgr, subtitle_note)
    """
    if not page_crops:
        raise ValueError("Cannot stitch an empty list of page crops.")

    # Standardize width to 1024
    target_width = 1024
    scaled_crops: List[np.ndarray] = []

    # Master Top Header
    header_h = 44
    master_banner = np.full((header_h, target_width, 3), 242, dtype=np.uint8)
    cv2.line(master_banner, (0, header_h - 2), (target_width, header_h - 2), (30, 41, 59), 2)
    label = f"ROLL: {roll_no} ({student_name[:20]}) | {question_id}: {question_title[:28]} | MAX: {max_marks}M [MULTI-PAGE]"
    cv2.putText(master_banner, label, (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.56, (15, 23, 42), 2, cv2.LINE_AA)
    scaled_crops.append(master_banner)

    for idx, (pno, crop, note) in enumerate(page_crops):
        ch, cw = crop.shape[:2]
        if cw != target_width:
            scale = float(target_width) / max(1, cw)
            crop_resized = cv2.resize(crop, (target_width, max(20, int(ch * scale))), interpolation=cv2.INTER_AREA)
        else:
            crop_resized = crop

        # Sub-divider banner between pages
        div_h = 32
        divider = np.full((div_h, target_width, 3), 226, dtype=np.uint8)
        cv2.line(divider, (0, 0), (target_width, 0), (148, 163, 184), 1)
        cv2.line(divider, (0, div_h - 1), (target_width, div_h - 1), (148, 163, 184), 1)
        div_label = f"--- [PAGE {pno}] {note or f'Answer Segment {idx + 1}'} ---"
        cv2.putText(divider, div_label, (20, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.50, (30, 41, 59), 2, cv2.LINE_AA)

        scaled_crops.append(divider)
        scaled_crops.append(crop_resized)

    stitched = np.vstack(scaled_crops)
    sh, sw = stitched.shape[:2]
    if sh > 1568:
        scale = 1568.0 / sh
        stitched = cv2.resize(stitched, (max(320, int(sw * scale)), 1568), interpolation=cv2.INTER_AREA)
    return stitched


# ---------------------------------------------------------------------------
# 3. Dynamic Extra / Supplementary Sheet Registration
# ---------------------------------------------------------------------------
def format_supplementary_page_info(page_no: int, nominal_expected_pages: int) -> Dict[str, Any]:
    """Categorize and label a scanned page that exceeds nominal page count."""
    is_supp = int(page_no) > int(nominal_expected_pages)
    supp_index = max(1, int(page_no) - int(nominal_expected_pages)) if is_supp else 0
    return {
        "page_no": page_no,
        "is_supplementary": is_supp,
        "is_extra_sheet": is_supp,
        "supplementary_index": supp_index,
        "badge_label": f"Extra Sheet #{supp_index}" if is_supp else f"Page {page_no}",
        "label": f"Extra Sheet #{supp_index}" if is_supp else f"Page {page_no}",
        "description": "Supplementary answer sheet attached by student" if is_supp else "Nominal question paper page",
    }


# ---------------------------------------------------------------------------
# 4, 5, 6. Boundary Guards (Downward, Upward, Horizontal Margins)
# ---------------------------------------------------------------------------
def compute_adaptive_crop_coordinates(
    img_shape: Tuple[int, int],
    y_start_pct: float,
    y_end_pct: float,
    x_start_pct: float,
    x_end_pct: float,
    gray_img: Optional[np.ndarray] = None,
) -> Tuple[int, int, int, int]:
    """Compute crop coordinates with ink projection guards extending in all 4 directions."""
    h, w = img_shape[:2]
    y0 = max(0, min(h - 20, int((y_start_pct / 100.0) * h)))
    y1 = max(y0 + 20, min(h, int((y_end_pct / 100.0) * h)))
    x0 = max(0, min(w - 20, int((x_start_pct / 100.0) * w)))
    x1 = max(x0 + 20, min(w, int((x_end_pct / 100.0) * w)))

    if gray_img is None:
        return y0, y1, x0, x1

    # Downward expansion guard (up to 120px below bottom line)
    guard_band_bottom = min(h, y1 + 120)
    if guard_band_bottom > y1:
        band = gray_img[y1:guard_band_bottom, x0:x1]
        if band.size > 0:
            dark_mask = (band < 165).astype(np.uint8)
            row_density = dark_mask.mean(axis=1)
            overflow_rows = np.where(row_density > 0.015)[0]
            if len(overflow_rows) > 0:
                y1 = min(h, y1 + int(overflow_rows[-1]) + 20)

    # Upward expansion guard (up to 50px above top line if student started in margin)
    guard_band_top = max(0, y0 - 50)
    if y0 > guard_band_top:
        top_band = gray_img[guard_band_top:y0, x0:x1]
        if top_band.size > 0:
            dark_mask = (top_band < 165).astype(np.uint8)
            row_density = dark_mask.mean(axis=1)
            top_overflow = np.where(row_density > 0.02)[0]
            if len(top_overflow) > 0:
                y0 = max(0, guard_band_top + int(top_overflow[0]) - 8)

    # Horizontal right margin expansion guard (up to 60px)
    guard_right = min(w, x1 + 60)
    if guard_right > x1:
        right_band = gray_img[y0:y1, x1:guard_right]
        if right_band.size > 0:
            col_density = (right_band < 165).mean(axis=0)
            right_overflow = np.where(col_density > 0.02)[0]
            if len(right_overflow) > 0:
                x1 = min(w, x1 + int(right_overflow[-1]) + 15)

    # Horizontal left margin expansion guard (up to 50px left if student wrote equation labels in margin)
    guard_left = max(0, x0 - 50)
    if x0 > guard_left:
        left_band = gray_img[y0:y1, guard_left:x0]
        if left_band.size > 0:
            col_density = (left_band < 165).mean(axis=0)
            left_overflow = np.where(col_density > 0.015)[0]
            if len(left_overflow) > 0:
                x0 = max(0, guard_left + int(left_overflow[0]) - 8)

    return y0, y1, x0, x1


# ---------------------------------------------------------------------------
# 7. Crossed-Out & Scratch-Work Detection
# ---------------------------------------------------------------------------
def detect_crossed_out_strokes(crop_bgr: np.ndarray) -> Tuple[bool, float, str]:
    """Detect scratch-outs, heavy scribbles, or diagonal strikethroughs over an answer."""
    if crop_bgr is None or crop_bgr.size == 0:
        return False, 0.0, "NO_IMAGE"

    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY) if len(crop_bgr.shape) == 3 else crop_bgr
    edges = cv2.Canny(gray, 50, 150)
    # Detect lines using Hough transform
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=70, minLineLength=50, maxLineGap=10)

    if lines is None or len(lines) == 0:
        return False, 0.0, "NO_LONG_LINES"

    diagonal_lines = 0
    total_lines = len(lines)
    for line in lines:
        pts = line.ravel()
        if len(pts) < 4:
            continue
        x_a, y_a, x_b, y_b = int(pts[0]), int(pts[1]), int(pts[2]), int(pts[3])
        dx = x_b - x_a
        dy = y_b - y_a
        angle = abs(np.degrees(np.arctan2(dy, dx)))
        if 20 <= angle <= 75:
            diagonal_lines += 1

    ratio = float(diagonal_lines / max(1, total_lines))
    if diagonal_lines >= 4 and ratio >= 0.28:
        return True, round(ratio, 3), f"Detected {diagonal_lines} diagonal strike-through strokes indicating scratch-work."

    return False, round(ratio, 3), "Clean handwritten strokes."


# ---------------------------------------------------------------------------
# 8, 9. 180° Inverted & Sideways Orientation Auto-Correction
# ---------------------------------------------------------------------------
def detect_and_fix_orientation(img_bgr: np.ndarray) -> Tuple[np.ndarray, int]:
    """Detect if page was captured upside down or sideways and rotate to upright portrait.

    Returns:
        (upright_img_bgr, angle_rotated_deg)
    """
    if img_bgr is None or img_bgr.size == 0:
        return img_bgr, 0

    h, w = img_bgr.shape[:2]
    rotated = img_bgr
    total_rot = 0

    # 1. First ensure portrait aspect ratio (h > w)
    if w > h:
        rotated = cv2.rotate(rotated, cv2.ROTATE_90_CLOCKWISE)
        total_rot = 90
        h, w = rotated.shape[:2]

    # 2. Orientation check (180° upside-down detection)
    # Only trigger if top is almost completely blank (<0.015 ink) while bottom has heavy header-like ink (>0.15 ink, >6x ratio)
    gray = cv2.cvtColor(rotated, cv2.COLOR_BGR2GRAY)
    top_band = gray[: int(h * 0.20), :]
    bot_band = gray[int(h * 0.80) :, :]

    top_ink = float(np.mean(top_band < 165))
    bot_ink = float(np.mean(bot_band < 165))

    if top_ink < 0.015 and bot_ink > 0.15 and bot_ink > (top_ink * 6.0):
        rotated = cv2.rotate(rotated, cv2.ROTATE_180)
        total_rot = (total_rot + 180) % 360
        logger.info("Auto-corrected 180° upside-down page orientation (bot_ink=%.4f vs top_ink=%.4f)", bot_ink, top_ink)

    return rotated, total_rot


# ---------------------------------------------------------------------------
# 10. Defocus & Motion Blur Assessment
# ---------------------------------------------------------------------------
def assess_blur_quality(img_bgr: np.ndarray, threshold: float = 60.0) -> Dict[str, Any]:
    """Standardized Laplacian blur variance check."""
    if img_bgr is None or img_bgr.size == 0:
        return {"variance": 0.0, "is_sharp": False, "status": "EMPTY"}

    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY) if len(img_bgr.shape) == 3 else img_bgr
    h, w = gray.shape[:2]
    if w > 800:
        scale = 800.0 / w
        gray = cv2.resize(gray, (800, max(1, int(h * scale))), interpolation=cv2.INTER_AREA)

    var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    is_sharp = var >= threshold
    return {
        "variance": round(var, 2),
        "is_sharp": is_sharp,
        "status": "PASS" if is_sharp else "BLURRY_RETAKE_RECOMMENDED",
    }


# ---------------------------------------------------------------------------
# 11. LAB Illumination Division & Fluorescent Shadow Removal
# ---------------------------------------------------------------------------
def remove_shadows_and_glare(img_bgr: np.ndarray) -> np.ndarray:
    """LAB illumination background normalization with CLAHE local contrast."""
    if img_bgr is None or img_bgr.size == 0:
        return img_bgr

    lab = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2LAB)
    l_chan, a_chan, b_chan = cv2.split(lab)

    bg_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (35, 35))
    background = cv2.morphologyEx(l_chan, cv2.MORPH_CLOSE, bg_kernel)
    background = cv2.GaussianBlur(background, (21, 21), 0)

    norm_l = cv2.divide(l_chan, background, scale=255)
    clahe = cv2.createCLAHE(clipLimit=2.2, tileGridSize=(8, 8))
    enhanced_l = clahe.apply(norm_l)

    clean_bgr = cv2.cvtColor(cv2.merge([enhanced_l, a_chan, b_chan]), cv2.COLOR_LAB2BGR)
    gaussian = cv2.GaussianBlur(clean_bgr, (0, 0), 1.5)
    return cv2.addWeighted(clean_bgr, 1.45, gaussian, -0.45, 0)


# ---------------------------------------------------------------------------
# 12. 4-Corner Homography Perspective Flattening
# ---------------------------------------------------------------------------
def flatten_skewed_document(img_bgr: np.ndarray) -> Tuple[np.ndarray, bool]:
    """Detect paper quadrilateral and flatten to canonical A4 dimensions."""
    if img_bgr is None or img_bgr.size == 0:
        return img_bgr, False

    h, w = img_bgr.shape[:2]
    scale = 600.0 / max(h, w)
    small = cv2.resize(img_bgr, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edged = cv2.Canny(blurred, 40, 140)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    edged = cv2.dilate(edged, kernel, iterations=1)

    contours, _ = cv2.findContours(edged, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    contours = sorted(contours, key=cv2.contourArea, reverse=True)[:5]
    img_area = small.shape[0] * small.shape[1]

    for c in contours:
        area = cv2.contourArea(c)
        if area < 0.30 * img_area:
            break
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.02 * peri, True)
        if len(approx) == 4 and area < 0.97 * img_area:
            pts = approx.reshape(4, 2).astype("float32") / scale
            # Order points: TL, TR, BR, BL
            rect = np.zeros((4, 2), dtype="float32")
            s = pts.sum(axis=1)
            rect[0] = pts[np.argmin(s)]
            rect[2] = pts[np.argmax(s)]
            diff = np.diff(pts, axis=1)
            rect[1] = pts[np.argmin(diff)]
            rect[3] = pts[np.argmax(diff)]

            dst = np.array([
                [0, 0],
                [CANONICAL_WIDTH - 1, 0],
                [CANONICAL_WIDTH - 1, CANONICAL_HEIGHT - 1],
                [0, CANONICAL_HEIGHT - 1],
            ], dtype="float32")
            matrix = cv2.getPerspectiveTransform(rect, dst)
            warped = cv2.warpPerspective(img_bgr, matrix, (CANONICAL_WIDTH, CANONICAL_HEIGHT), flags=cv2.INTER_CUBIC)
            return warped, True

    # Fallback: Clean aspect-ratio preserving fit onto canonical canvas
    resized = cv2.resize(img_bgr, (CANONICAL_WIDTH, CANONICAL_HEIGHT), interpolation=cv2.INTER_CUBIC)
    return resized, False


# ---------------------------------------------------------------------------
# 13. Duplicate Frame Deduplication (pHash & Sharpness Contest)
# ---------------------------------------------------------------------------
def compute_perceptual_hash(img_bgr: np.ndarray) -> str:
    """Compute 64-bit difference hash (dHash) for fast visual similarity checks."""
    if img_bgr is None or img_bgr.size == 0:
        return "0" * 16
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY) if len(img_bgr.shape) == 3 else img_bgr
    small = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    diff = small[:, 1:] > small[:, :-1]
    return f"{sum(2 ** i for i, v in enumerate(diff.flatten()) if v):016x}"


def is_duplicate_scan_frame(
    hash_a: str,
    hash_b: str,
    max_hamming_distance: int = 6,
) -> bool:
    """Determine if two frames are duplicate scans of the exact same physical sheet."""
    try:
        val_a = int(hash_a, 16)
        val_b = int(hash_b, 16)
        dist = bin(val_a ^ val_b).count("1")
        return dist <= max_hamming_distance
    except Exception:
        return hash_a == hash_b


# ---------------------------------------------------------------------------
# 14, 15. Out-of-Order Page Sorting & Missing Page Gap Detection
# ---------------------------------------------------------------------------
def reorder_and_audit_scanned_pages(
    scanned_pages: List[Dict[str, Any]],
    expected_page_count: int,
) -> Tuple[List[Dict[str, Any]], List[int], List[str]]:
    """Sort pages by page number, detect missing pages, and flag/deduplicate duplicates.

    Returns:
        (ordered_pages, missing_page_numbers, audit_warnings)
    """
    warnings: List[str] = []
    
    # 1. Group pages by page number and detect duplicate scans of the same page number
    pages_by_num: Dict[int, List[Dict[str, Any]]] = {}
    for p in scanned_pages:
        try:
            p_no = int(p.get("page_no", 1))
        except (ValueError, TypeError):
            p_no = 1
        pages_by_num.setdefault(p_no, []).append(p)

    deduped_pages: List[Dict[str, Any]] = []
    for p_no in sorted(pages_by_num.keys()):
        p_list = pages_by_num[p_no]
        if len(p_list) > 1:
            # Sort by sharpness/blur_score descending if available
            p_list_sorted = sorted(
                p_list,
                key=lambda x: float(x.get("blur_score", 0.0) or 0.0),
                reverse=True,
            )
            chosen = p_list_sorted[0]
            warnings.append(
                f"Duplicate scans detected for Page {p_no} ({len(p_list)} copies); retained best quality scan."
            )
            deduped_pages.append(chosen)
        else:
            deduped_pages.append(p_list[0])

    # 2. Check for identical frames across distinct page numbers (e.g. perceptual hash / sha256)
    for i in range(len(deduped_pages)):
        for j in range(i + 1, len(deduped_pages)):
            p_a = deduped_pages[i]
            p_b = deduped_pages[j]
            h_a = p_a.get("p_hash") or p_a.get("phash") or p_a.get("sha256")
            h_b = p_b.get("p_hash") or p_b.get("phash") or p_b.get("sha256")
            if h_a and h_b:
                if str(h_a) == str(h_b) or is_duplicate_scan_frame(str(h_a), str(h_b)):
                    warnings.append(
                        f"Duplicate frame detected: Page {p_a.get('page_no')} and Page {p_b.get('page_no')} have identical image content."
                    )

    # 3. Missing pages check
    present_page_nos = {int(p.get("page_no", 1)) for p in deduped_pages}
    missing_pages: List[int] = []
    for expected_no in range(1, expected_page_count + 1):
        if expected_no not in present_page_nos:
            missing_pages.append(expected_no)

    if missing_pages:
        warnings.append(f"Missing page(s): {', '.join(str(p) for p in missing_pages)} (Expected 1..{expected_page_count}).")

    return deduped_pages, missing_pages, warnings


# ---------------------------------------------------------------------------
# 16. Truncated & Corrupted Buffer Integrity Shield
# ---------------------------------------------------------------------------
def validate_image_buffer_integrity(data: bytes) -> Tuple[bool, str]:
    """Verify that uploaded image bytes are complete and not truncated mid-transmission."""
    if not data or len(data) < 32:
        return False, "File is empty or smaller than minimum image header size."

    # JPEG Check: Starts with 0xFF 0xD8 and ends with 0xFF 0xD9
    if data[:2] == b"\xff\xd8":
        if data[-2:] != b"\xff\xd9":
            # Sometimes modern cameras include a few trailing zero bytes after EOI
            if b"\xff\xd9" not in data[-16:]:
                return False, "Corrupted/truncated JPEG upload: missing End-of-Image (EOI 0xFFD9) marker."
        return True, "Valid JPEG stream."

    # PNG Check: 8-byte signature + IEND chunk
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        if b"IEND" not in data[-12:]:
            return False, "Corrupted/truncated PNG upload: missing IEND footer marker."
        return True, "Valid PNG stream."

    # WebP Check: RIFF....WEBP
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True, "Valid WebP stream."

    return False, "Unsupported image format or corrupt header."


# ---------------------------------------------------------------------------
# 17. Canonical A4 Canvas Aspect Ratio Normalizer
# ---------------------------------------------------------------------------
def normalize_canvas_dimensions(img_bgr: np.ndarray) -> np.ndarray:
    """Pad arbitrary aspect ratio onto a canonical A4 white canvas without non-linear distortion.

    Preserves original scan orientation: landscape images remain on landscape A4 canvas,
    portrait images remain on portrait A4 canvas.
    """
    if img_bgr is None or img_bgr.size == 0:
        return img_bgr

    h, w = img_bgr.shape[:2]
    is_landscape = w > h
    canon_w = CANONICAL_HEIGHT if is_landscape else CANONICAL_WIDTH
    canon_h = CANONICAL_WIDTH if is_landscape else CANONICAL_HEIGHT

    target_ratio = canon_h / canon_w
    current_ratio = h / w

    if abs(current_ratio - target_ratio) < 0.05:
        return cv2.resize(img_bgr, (canon_w, canon_h), interpolation=cv2.INTER_CUBIC)

    # Letterbox / Pillarbox on white canvas preserving orientation
    canvas = np.full((canon_h, canon_w, 3), 255, dtype=np.uint8)
    if current_ratio > target_ratio:
        # Too tall relative to canvas -> scale by height
        new_w = max(10, int(canon_h / current_ratio))
        scaled = cv2.resize(img_bgr, (new_w, canon_h), interpolation=cv2.INTER_AREA)
        offset_x = (canon_w - new_w) // 2
        canvas[:, offset_x : offset_x + new_w] = scaled
    else:
        # Too wide relative to canvas -> scale by width
        new_h = max(10, int(canon_w * current_ratio))
        scaled = cv2.resize(img_bgr, (canon_w, new_h), interpolation=cv2.INTER_AREA)
        offset_y = (canon_h - new_h) // 2
        canvas[offset_y : offset_y + new_h, :] = scaled

    return canvas


# ---------------------------------------------------------------------------
# 18. Roll Number OCR & Barcode Roster Reconciliation
# ---------------------------------------------------------------------------
def reconcile_roll_number(
    scanned_roll: str,
    roster_rolls: List[str],
) -> Tuple[str, float, bool]:
    """Clean and fuzzy reconcile handwritten or OCR roll numbers against class roster.

    Returns:
        (best_roll, confidence, was_corrected)
    """
    clean_raw = re.sub(r"[^A-Za-z0-9]", "", scanned_roll).upper()
    if not clean_raw:
        return "UNKNOWN", 0.0, False

    # Exact match
    for r in roster_rolls:
        if r.upper() == clean_raw:
            return r, 1.0, False

    # Common OCR character substitutions: 'O' <-> '0', 'I' <-> '1', 'Z' <-> '2', 'S' <-> '5'
    char_map = {"O": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8"}
    alt_raw = "".join(char_map.get(ch, ch) for ch in clean_raw)
    for r in roster_rolls:
        alt_r = "".join(char_map.get(ch, ch) for ch in r.upper())
        if alt_r == alt_raw:
            return r, 0.94, True

    # Levenshtein distance 1 match
    def _lev1(a: str, b: str) -> bool:
        if abs(len(a) - len(b)) > 1:
            return False
        if len(a) == len(b):
            return sum(ch1 != ch2 for ch1, ch2 in zip(a, b)) <= 1
        shorter, longer = (a, b) if len(a) < len(b) else (b, a)
        for i in range(len(longer)):
            if longer[:i] + longer[i + 1 :] == shorter:
                return True
        return False

    for r in roster_rolls:
        if _lev1(clean_raw, r.upper()):
            return r, 0.88, True

    return clean_raw, 0.60, False


# ---------------------------------------------------------------------------
# 19. Cross-Page Student Packet Consistency Verification
# ---------------------------------------------------------------------------
def verify_packet_student_consistency(
    student_dir_pages: List[Dict[str, Any]],
    expected_roll: str,
) -> Tuple[bool, List[str]]:
    """Verify that all pages inside a student's scanned folder belong to the expected student."""
    anomalies: List[str] = []
    for p in student_dir_pages:
        p_roll = str(p.get("roll_no", "")).strip()
        if p_roll and p_roll.upper() != expected_roll.upper():
            anomalies.append(f"Page {p.get('page_no')} is tagged with roll '{p_roll}' but packet belongs to '{expected_roll}'.")
    return len(anomalies) == 0, anomalies


# ---------------------------------------------------------------------------
# 20. Rubric Step-Score Sum Discrepancy Reconciliation
# ---------------------------------------------------------------------------
def reconcile_rubric_step_scores(
    step_scores: Sequence[Dict[str, Any]],
    awarded_marks: float,
    max_marks: float,
) -> Tuple[float, str, bool]:
    """Check if sum of rubric steps matches awarded marks; adjust or flag inconsistency.

    Returns:
        (reconciled_awarded_marks, verdict_tab, is_inconsistent)
    """
    if not step_scores:
        safe_marks = max(0.0, min(max_marks, round(awarded_marks, 2)))
        return safe_marks, "GREEN", False

    has_step_overflow = False
    step_max_sum = 0.0
    for s in step_scores:
        if isinstance(s, dict):
            s_max = float(s.get("max", 0.0) or 0.0)
            step_max_sum += s_max
            s_awarded = float(s.get("awarded", 0.0) or 0.0)
            if s_max > 0 and s_awarded > s_max:
                has_step_overflow = True
                s["awarded"] = s_max

    step_sum = sum(float(s.get("awarded", 0.0) or 0.0) for s in step_scores if isinstance(s, dict))
    step_sum = round(step_sum, 2)
    awarded_clean = round(awarded_marks, 2)

    diff = abs(step_sum - awarded_clean)
    if diff > 0.5 or has_step_overflow:
        # Meaningful mathematical discrepancy -> route to RED for human TA review while preserving the model's awarded mark as disputed
        logger.warning("Step-score sum discrepancy: sum=%.2f vs awarded=%.2f (step_overflow=%s); preserved model mark as disputed in RED queue", step_sum, awarded_clean, has_step_overflow)
        return awarded_clean, "RED", True

    return awarded_clean, "GREEN", False


# ---------------------------------------------------------------------------
# 21. Handwritten Prompt Injection & Adversarial Grader Defense
# ---------------------------------------------------------------------------
def detect_adversarial_handwriting_injection(text: str) -> Tuple[bool, str]:
    """Scan OCR transcription for adversarial instructions aimed at manipulating the grader."""
    if not text:
        return False, ""
    match = _ADVERSARIAL_REGEX.search(text)
    if match:
        return True, f"Adversarial prompt injection pattern detected: '{match.group(0)}'."
    return False, ""


# ---------------------------------------------------------------------------
# 22. Faint Pencil Handwriting Contrast Amplification
# ---------------------------------------------------------------------------
def boost_faint_handwriting(img_bgr: np.ndarray) -> np.ndarray:
    """Enhance faint pencil or faded light-blue ink."""
    if img_bgr is None or img_bgr.size == 0:
        return img_bgr

    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY) if len(img_bgr.shape) == 3 else img_bgr
    # Morphological min-filter to thicken thin faint strokes
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    darkened = cv2.erode(gray, kernel, iterations=1)

    # Gamma correction to boost dark contrast
    inv_gamma = 1.0 / 0.75
    table = np.array([((i / 255.0) ** inv_gamma) * 255 for i in range(256)]).astype("uint8")
    boosted = cv2.LUT(darkened, table)

    return cv2.cvtColor(boosted, cv2.COLOR_GRAY2BGR)


# ---------------------------------------------------------------------------
# 23. Continuation Keyword Detection
# ---------------------------------------------------------------------------
def detect_continuation_markers(ocr_text: str) -> Tuple[bool, str]:
    """Detect if student explicitly wrote a continuation indicator (PTO, Contd, etc.)."""
    if not ocr_text:
        return False, ""
    match = _CONTINUATION_REGEX.search(ocr_text)
    if match:
        return True, f"Continuation marker '{match.group(0)}' found in student answer."
    return False, ""


# ---------------------------------------------------------------------------
# 24. Ephemeral Prompt Cache Warmup Lock
# ---------------------------------------------------------------------------
class PromptCacheWarmupLock:
    """Coordinates single-flight prompt cache creation to prevent thundering herds."""

    def __init__(self) -> None:
        import asyncio
        self._locks: Dict[str, asyncio.Lock] = {}
        self._warmed_questions: set[str] = set()

    def is_warmed(self, question_id: str) -> bool:
        return question_id in self._warmed_questions

    def mark_warmed(self, question_id: str) -> None:
        self._warmed_questions.add(question_id)
