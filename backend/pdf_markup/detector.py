"""
OpenCV detection pipeline for engineer's marked-up architectural PDFs.

Color convention (HSV ranges, tuned for typical Bluebeam/Adobe markups):
    RED    → columns          (filled rect/circle)
    BLUE   → beams            (line)
    GREEN  → slabs            (closed polygon)
    YELLOW → shear walls      (hatched polygon, treated as poly w/ thick boundary)

Output coords are in PDF image pixels (origin top-left).
Caller converts to ETABS world coords via scale (mm/px) and Y-flip
(PDF top-left → ETABS bottom-left = 0,0).
"""
import io
import math
from typing import List, Dict, Tuple, Optional

import numpy as np
import cv2
import fitz  # PyMuPDF


# ── HSV color ranges (Hue 0–179 in OpenCV) ────────────────────────────────────
COLOR_RANGES = {
    "column":   [(np.array([0,   80,  80]),  np.array([10,  255, 255])),
                 (np.array([170, 80,  80]),  np.array([179, 255, 255]))],   # red wraps hue
    "beam":     [(np.array([100, 80,  80]),  np.array([130, 255, 255]))],   # blue
    "slab":     [(np.array([40,  50,  50]),  np.array([85,  255, 255]))],   # green
    "wall":     [(np.array([20,  80,  80]),  np.array([35,  255, 255]))],   # yellow/orange
}

# Minimum contour area (px²) — filters noise specks
MIN_AREA = {"column": 80, "beam": 40, "slab": 400, "wall": 400}


def render_pdf_page(pdf_bytes: bytes, page_index: int = 0, dpi: int = 200) -> np.ndarray:
    """Render PDF page → BGR numpy image (OpenCV format)."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[page_index]
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    img_bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    doc.close()
    return img_bgr


def _mask_color(hsv: np.ndarray, member_type: str) -> np.ndarray:
    """Build binary mask for given color category. Handles red hue wrap-around."""
    masks = [cv2.inRange(hsv, lo, hi) for lo, hi in COLOR_RANGES[member_type]]
    m = masks[0]
    for extra in masks[1:]:
        m = cv2.bitwise_or(m, extra)
    # Close small gaps (e.g. hatch interior) so polygon detection sees one blob
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, kernel, iterations=2)
    return m


def _detect_columns(mask: np.ndarray) -> List[Dict]:
    """Return list of {centroid: (x,y), bbox: (x,y,w,h), area: float}."""
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < MIN_AREA["column"]:
            continue
        M = cv2.moments(c)
        if M["m00"] == 0:
            continue
        cx = M["m10"] / M["m00"]
        cy = M["m01"] / M["m00"]
        x, y, w, h = cv2.boundingRect(c)
        out.append({"cx": float(cx), "cy": float(cy),
                    "bbox": [int(x), int(y), int(w), int(h)],
                    "area": float(area)})
    return out


def _fit_beam_endpoints(contour: np.ndarray) -> Optional[Tuple[float, float, float, float, float]]:
    """Return (x1, y1, x2, y2, length) from a single contour, else None."""
    pts = contour.reshape(-1, 2).astype(np.float32)
    if len(pts) < 5:
        return None
    vx, vy, _, _ = cv2.fitLine(pts, cv2.DIST_L2, 0, 0.01, 0.01)
    vx, vy = float(vx), float(vy)
    cx, cy = float(pts[:, 0].mean()), float(pts[:, 1].mean())
    proj = (pts[:, 0] - cx) * vx + (pts[:, 1] - cy) * vy
    t_min, t_max = float(proj.min()), float(proj.max())
    length = t_max - t_min
    if length < 30:
        return None
    x1 = cx + t_min * vx; y1 = cy + t_min * vy
    x2 = cx + t_max * vx; y2 = cy + t_max * vy
    return x1, y1, x2, y2, length


def _detect_beams(mask: np.ndarray) -> List[Dict]:
    """
    One beam per connected blue region. Detected separately for horizontal
    and vertical directions so that beams crossing red column squares (which
    chop the blue mask into pieces) get reconnected along their axis.

    Pipeline:
      1. Directional morphological close along X → bridges horizontal gaps
         (including breaks where beams pass through columns).
      2. findContours + cv2.fitLine on each contour → endpoints of horizontal
         beams (filtered by |vx| > |vy|).
      3. Repeat with Y-direction close for vertical beams.
      4. Plus a generic close for diagonal members.
      5. Deduplicate hits that overlap heavily (same axis, near identical line).
    """
    H, W = mask.shape[:2]

    # Bridge width: cover typical column width (~30 px @ 200 DPI)
    BRIDGE = 35

    kernel_h = cv2.getStructuringElement(cv2.MORPH_RECT, (BRIDGE, 3))
    kernel_v = cv2.getStructuringElement(cv2.MORPH_RECT, (3, BRIDGE))
    kernel_g = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))

    masks = [
        ("h", cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel_h, iterations=1)),
        ("v", cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel_v, iterations=1)),
        ("g", cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel_g, iterations=2)),
    ]

    out: List[Dict] = []
    for tag, m in masks:
        contours, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        for c in contours:
            if cv2.contourArea(c) < MIN_AREA["beam"]:
                continue
            res = _fit_beam_endpoints(c)
            if res is None:
                continue
            x1, y1, x2, y2, length = res
            if length < _MIN_BEAM_LEN:
                continue
            dx, dy = abs(x2 - x1), abs(y2 - y1)
            # Only keep horizontals from h-pass, verticals from v-pass.
            # The generic pass keeps diagonals (neither dx>>dy nor dy>>dx).
            if tag == "h" and dx < dy * 1.5:
                continue
            if tag == "v" and dy < dx * 1.5:
                continue
            if tag == "g" and (dx > dy * 1.5 or dy > dx * 1.5):
                continue
            out.append({"x1": int(round(x1)), "y1": int(round(y1)),
                        "x2": int(round(x2)), "y2": int(round(y2)),
                        "length": float(length)})

    return _dedupe_beams(out)


def _dedupe_beams(beams: List[Dict], pos_tol: float = 12.0,
                  ang_tol_deg: float = 8.0) -> List[Dict]:
    """
    Remove beams that lie almost on top of each other (same axis, parallel,
    overlapping). Keeps the longer of each pair.
    """
    keep = [True] * len(beams)
    beams = sorted(beams, key=lambda b: -b["length"])  # longest first
    for i, a in enumerate(beams):
        if not keep[i]:
            continue
        ax = (a["x1"] + a["x2"]) / 2
        ay = (a["y1"] + a["y2"]) / 2
        a_ang = math.degrees(math.atan2(a["y2"] - a["y1"], a["x2"] - a["x1"])) % 180
        for j in range(i + 1, len(beams)):
            if not keep[j]:
                continue
            b = beams[j]
            bx = (b["x1"] + b["x2"]) / 2
            by = (b["y1"] + b["y2"]) / 2
            b_ang = math.degrees(math.atan2(b["y2"] - b["y1"], b["x2"] - b["x1"])) % 180
            d_ang = min(abs(a_ang - b_ang), 180 - abs(a_ang - b_ang))
            if d_ang > ang_tol_deg:
                continue
            # Perpendicular distance from b's midpoint to a's line
            # For near-horizontal: compare y; for near-vertical: compare x
            if a_ang < 45 or a_ang > 135:   # ~horizontal
                if abs(ay - by) > pos_tol: continue
            else:                            # ~vertical
                if abs(ax - bx) > pos_tol: continue
            keep[j] = False     # drop the shorter duplicate

    return [b for b, k in zip(beams, keep) if k]


def _merge_collinear(lines: List[Dict], angle_tol: float = 5.0,
                     dist_tol: float = 8.0) -> List[Dict]:
    """Merge near-collinear, near-touching line fragments into single beams."""
    if not lines:
        return lines
    used = [False] * len(lines)
    merged = []
    for i, a in enumerate(lines):
        if used[i]:
            continue
        ax, ay = (a["x1"] + a["x2"]) / 2, (a["y1"] + a["y2"]) / 2
        ang_a = np.degrees(np.arctan2(a["y2"] - a["y1"], a["x2"] - a["x1"])) % 180
        pts = [(a["x1"], a["y1"]), (a["x2"], a["y2"])]
        used[i] = True
        for j, b in enumerate(lines[i + 1:], start=i + 1):
            if used[j]:
                continue
            ang_b = np.degrees(np.arctan2(b["y2"] - b["y1"], b["x2"] - b["x1"])) % 180
            if abs(ang_a - ang_b) > angle_tol and abs(ang_a - ang_b) < 180 - angle_tol:
                continue
            bx, by = (b["x1"] + b["x2"]) / 2, (b["y1"] + b["y2"]) / 2
            if np.hypot(ax - bx, ay - by) > 60:
                continue
            pts += [(b["x1"], b["y1"]), (b["x2"], b["y2"])]
            used[j] = True
        # Take farthest pair as merged endpoints
        far_a, far_b, max_d = pts[0], pts[1], 0.0
        for p in pts:
            for q in pts:
                d = (p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2
                if d > max_d:
                    max_d = d
                    far_a, far_b = p, q
        merged.append({"x1": int(far_a[0]), "y1": int(far_a[1]),
                       "x2": int(far_b[0]), "y2": int(far_b[1]),
                       "length": float(np.sqrt(max_d))})
    return merged


def _detect_polygons(mask: np.ndarray, member_type: str,
                      simplify_px: float = 2.0) -> List[Dict]:
    """
    Return polygon vertex lists for slabs / walls (closed regions).

    simplify_px is the absolute pixel tolerance passed to approxPolyDP.
    A small constant (≈2 px) keeps the actual PDF outline shape — every
    corner the engineer drew is preserved — while dropping per-pixel
    noise along otherwise straight edges. Pass 0 to keep the raw contour.
    """
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    out = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < MIN_AREA[member_type]:
            continue
        if simplify_px > 0:
            approx = cv2.approxPolyDP(c, float(simplify_px), True)
        else:
            approx = c
        verts = [[int(p[0][0]), int(p[0][1])] for p in approx]
        if len(verts) < 3:
            continue
        out.append({"vertices": verts, "area": float(area)})
    return out


def configure_detection(min_beam_len: int = None,
                         beam_min_area: int = None,
                         column_min_area: int = None,
                         poly_min_area: int = None) -> None:
    """Adjust detection thresholds at runtime (used by /detect query params)."""
    global _MIN_BEAM_LEN
    if min_beam_len is not None:
        _MIN_BEAM_LEN = int(min_beam_len)
    if beam_min_area is not None:
        MIN_AREA["beam"] = int(beam_min_area)
    if column_min_area is not None:
        MIN_AREA["column"] = int(column_min_area)
    if poly_min_area is not None:
        MIN_AREA["slab"] = int(poly_min_area)
        MIN_AREA["wall"] = int(poly_min_area)


_MIN_BEAM_LEN = 30  # px, default — overridable via configure_detection()


def detect_members(img_bgr: np.ndarray) -> Dict[str, list]:
    """
    Run full color-mask pipeline on a single PDF page image.

    Returns:
        {
          "columns":  [{cx, cy, bbox, area}, ...],
          "beams":    [{x1, y1, x2, y2, length}, ...],
          "slabs":    [{vertices: [[x,y], ...], area}, ...],
          "walls":    [{vertices: [[x,y], ...], area}, ...],
          "image_size": [width, height],
        }
    """
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    h, w = img_bgr.shape[:2]
    return {
        "columns":    _detect_columns(_mask_color(hsv, "column")),
        "beams":      _detect_beams(_mask_color(hsv, "beam")),
        "slabs":      _detect_polygons(_mask_color(hsv, "slab"), "slab"),
        "walls":      _detect_polygons(_mask_color(hsv, "wall"), "wall"),
        "image_size": [w, h],
    }
