"""
Geometry snap pass.

Engineers drawing freehand on PDFs rarely land beam endpoints exactly on
column centroids or grid intersections. This module post-processes detected
members and pulls every loose endpoint to the nearest reference point
(column centroid or grid line crossing) within a pixel tolerance.

Operates entirely in detection-pixel space — caller already knows the
coordinate system.
"""
from typing import Dict, List, Tuple
import math


def _build_snap_points(members: Dict, grids: Dict) -> List[Tuple[float, float]]:
    """Reference points: column centroids + every grid-line intersection."""
    pts: List[Tuple[float, float]] = []

    for c in members.get("columns", []):
        pts.append((float(c["cx"]), float(c["cy"])))

    x_lines = [float(g["x"]) for g in grids.get("x_grids", [])]
    y_lines = [float(g["y"]) for g in grids.get("y_grids", [])]
    for x in x_lines:
        for y in y_lines:
            pts.append((x, y))
    return pts


def _nearest(pt: Tuple[float, float],
             candidates: List[Tuple[float, float]],
             tol: float) -> Tuple[float, float]:
    """Return nearest candidate within tol pixels, else original point."""
    if not candidates:
        return pt
    px, py = pt
    best = None
    best_d = tol * tol
    for cx, cy in candidates:
        d = (cx - px) ** 2 + (cy - py) ** 2
        if d <= best_d:
            best_d = d
            best = (cx, cy)
    return best if best is not None else pt


def snap_members(members: Dict, grids: Dict, tol_px: float = 30.0) -> Dict:
    """
    Snap beam endpoints + wall polygon vertices to nearest column or grid
    intersection within tol_px. Mutates and returns members.

    Tracks how many endpoints moved so the caller can report it.
    """
    snap_pts = _build_snap_points(members, grids)
    snapped_count = 0

    for b in members.get("beams", []):
        before = (b["x1"], b["y1"])
        sx1, sy1 = _nearest(before, snap_pts, tol_px)
        if (sx1, sy1) != before:
            b["x1"], b["y1"] = sx1, sy1
            snapped_count += 1

        before = (b["x2"], b["y2"])
        sx2, sy2 = _nearest(before, snap_pts, tol_px)
        if (sx2, sy2) != before:
            b["x2"], b["y2"] = sx2, sy2
            snapped_count += 1

        # Recompute length after snap (used in overlay tooltips)
        b["length"] = math.hypot(b["x2"] - b["x1"], b["y2"] - b["y1"])

    for w in members.get("walls", []):
        new_verts = []
        for v in w["vertices"]:
            sx, sy = _nearest((float(v[0]), float(v[1])), snap_pts, tol_px)
            if (sx, sy) != (v[0], v[1]):
                snapped_count += 1
            new_verts.append([sx, sy])
        w["vertices"] = new_verts

    members["snapped_endpoints"] = snapped_count
    return members
