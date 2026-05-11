"""
Auto-detect ETABS grid system from architectural drawing.

Approach:
  1. Mask near-black pixels (grid lines).
  2. HoughLinesP → long straight lines.
  3. Cluster by orientation: vertical → X-grids, horizontal → Y-grids.
  4. Snap to unique X / Y coordinates (collapsing duplicates within 10 px).
  5. OCR small region near each line's end-bubble for grid label (A, B, 1, 2).
"""
from typing import List, Dict, Tuple, Optional

import cv2
import numpy as np

from .ocr import ocr_region, _HAS_TESS


def _mask_black_lines(img_bgr: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    # Threshold: keep dark strokes only
    _, th = cv2.threshold(gray, 80, 255, cv2.THRESH_BINARY_INV)
    return th


def _cluster_coords(coords: List[float], tol: float = 10.0) -> List[float]:
    """Collapse coordinates within tol pixels to single mean."""
    if not coords:
        return []
    coords = sorted(coords)
    clusters = [[coords[0]]]
    for c in coords[1:]:
        if c - clusters[-1][-1] <= tol:
            clusters[-1].append(c)
        else:
            clusters.append([c])
    return [float(np.mean(cl)) for cl in clusters]


def detect_grids(img_bgr: np.ndarray,
                 min_line_length_ratio: float = 0.5) -> Dict[str, list]:
    """
    Returns:
        {
          "x_grids": [{"x": float_px, "label": str}, ...],   # vertical lines
          "y_grids": [{"y": float_px, "label": str}, ...],   # horizontal lines
        }
    """
    h, w = img_bgr.shape[:2]
    mask = _mask_black_lines(img_bgr)
    min_len = int(min(h, w) * min_line_length_ratio)
    lines = cv2.HoughLinesP(mask, rho=1, theta=np.pi / 180,
                            threshold=120, minLineLength=min_len, maxLineGap=20)
    if lines is None:
        return {"x_grids": [], "y_grids": []}

    vertical_x: List[float] = []
    horizontal_y: List[float] = []
    for ln in lines:
        x1, y1, x2, y2 = ln[0]
        dx, dy = abs(x2 - x1), abs(y2 - y1)
        if dx < 5 and dy >= min_len:        # vertical
            vertical_x.append((x1 + x2) / 2)
        elif dy < 5 and dx >= min_len:      # horizontal
            horizontal_y.append((y1 + y2) / 2)

    xs = _cluster_coords(vertical_x, tol=12)
    ys = _cluster_coords(horizontal_y, tol=12)

    # OCR bubble labels — vertical lines: look above top of line (small box)
    x_grids = []
    for x in xs:
        label = ocr_region(img_bgr, int(x) - 25, 0, 50, 60, psm=10) if _HAS_TESS else ""
        x_grids.append({"x": x, "label": label.strip() or ""})

    y_grids = []
    for y in ys:
        label = ocr_region(img_bgr, 0, int(y) - 25, 60, 50, psm=10) if _HAS_TESS else ""
        y_grids.append({"y": y, "label": label.strip() or ""})

    # Auto-label fallback (A,B,C / 1,2,3) where OCR found nothing
    import string
    for i, g in enumerate(x_grids):
        if not g["label"]:
            g["label"] = string.ascii_uppercase[i] if i < 26 else f"X{i+1}"
    for i, g in enumerate(y_grids):
        if not g["label"]:
            g["label"] = str(i + 1)

    return {"x_grids": x_grids, "y_grids": y_grids}
