"""
OCR helpers using pytesseract.

- read_labels(img, regions)   → text near each detected member
- parse_scale_from_titleblock(img) → returns scale tuple (numerator, denom)
                                     e.g. "1:100" → (1, 100). None if not found.
"""
import os
import re
import sys
from typing import List, Dict, Tuple, Optional

import cv2
import numpy as np


def _resolve_tesseract_cmd() -> Optional[str]:
    """
    Locate bundled tesseract.exe.
    Search order:
      1. STRUCTIQ_TESSERACT env var
      2. <exe_dir>/tesseract/tesseract.exe (PyInstaller bundle)
      3. <exe_dir>/_internal/tesseract/tesseract.exe
      4. Fallback to system PATH
    """
    env = os.environ.get("STRUCTIQ_TESSERACT")
    if env and os.path.isfile(env):
        return env
    base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
        else os.path.dirname(os.path.abspath(__file__))
    for cand in [
        os.path.join(base, "tesseract", "tesseract.exe"),
        os.path.join(base, "_internal", "tesseract", "tesseract.exe"),
        os.path.join(os.path.dirname(base), "tesseract", "tesseract.exe"),
    ]:
        if os.path.isfile(cand):
            return cand
    return None  # pytesseract will use PATH


try:
    import pytesseract
    _cmd = _resolve_tesseract_cmd()
    if _cmd:
        pytesseract.pytesseract.tesseract_cmd = _cmd
    _HAS_TESS = True
except ImportError:
    _HAS_TESS = False


def _preprocess(img: np.ndarray) -> np.ndarray:
    """Convert to grayscale + threshold to maximise OCR accuracy."""
    if len(img.shape) == 3:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    else:
        gray = img
    # Adaptive threshold handles uneven PDF brightness
    th = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                cv2.THRESH_BINARY, 21, 10)
    return th


def ocr_region(img: np.ndarray, x: int, y: int, w: int, h: int,
               psm: int = 7) -> str:
    """OCR a single rectangular region. psm=7 = single text line."""
    if not _HAS_TESS:
        return ""
    H, W = img.shape[:2]
    x0 = max(0, x); y0 = max(0, y)
    x1 = min(W, x + w); y1 = min(H, y + h)
    if x1 <= x0 or y1 <= y0:
        return ""
    crop = img[y0:y1, x0:x1]
    pre = _preprocess(crop)
    try:
        txt = pytesseract.image_to_string(
            pre,
            config=f"--psm {psm} -c tessedit_char_whitelist="
                   "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-x:/. "
        )
        return txt.strip()
    except Exception:
        return ""


def read_labels(img: np.ndarray, members: Dict[str, list],
                search_radius: int = 60) -> Dict[str, list]:
    """
    For each detected member, OCR a small region around it to find the label
    (e.g. "C1", "B-300x500", "SW1"). Adds "label" field to each member.
    """
    if not _HAS_TESS:
        for grp in members.values():
            if isinstance(grp, list):
                for m in grp:
                    m["label"] = ""
        return members

    # Columns: search square around centroid
    for col in members.get("columns", []):
        cx, cy = int(col["cx"]), int(col["cy"])
        col["label"] = ocr_region(img, cx - search_radius, cy - search_radius,
                                   search_radius * 2, search_radius * 2, psm=7)

    # Beams: midpoint search
    for bm in members.get("beams", []):
        mx = (bm["x1"] + bm["x2"]) // 2
        my = (bm["y1"] + bm["y2"]) // 2
        bm["label"] = ocr_region(img, mx - search_radius, my - search_radius // 2,
                                  search_radius * 2, search_radius, psm=7)

    # Slabs/walls: centroid of vertices
    for key in ("slabs", "walls"):
        for poly in members.get(key, []):
            verts = np.array(poly["vertices"])
            cx, cy = int(verts[:, 0].mean()), int(verts[:, 1].mean())
            poly["label"] = ocr_region(img,
                                        cx - search_radius, cy - search_radius // 2,
                                        search_radius * 2, search_radius, psm=7)

    return members


_SCALE_PATTERNS = [
    re.compile(r"\b(?:SCALE|Scale|scale)\s*[:=]?\s*1\s*[:/]\s*(\d{1,4})\b"),
    re.compile(r"\b1\s*[:/]\s*(\d{2,4})\b"),
]


def parse_scale_from_titleblock(img: np.ndarray) -> Optional[Tuple[int, int]]:
    """
    OCR bottom-right quadrant (typical title-block location) for "1:NN" scale.
    Returns (1, denom) or None.
    """
    if not _HAS_TESS:
        return None
    H, W = img.shape[:2]
    # Bottom-right quarter
    crop = img[int(H * 0.55):H, int(W * 0.55):W]
    pre = _preprocess(crop)
    try:
        txt = pytesseract.image_to_string(pre, config="--psm 6")
    except Exception:
        return None
    for pat in _SCALE_PATTERNS:
        m = pat.search(txt)
        if m:
            denom = int(m.group(1))
            if 5 <= denom <= 5000:
                return (1, denom)
    return None
