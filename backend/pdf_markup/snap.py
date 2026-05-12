"""
Geometry snap + topology cleanup pass.

Two operations performed in detection-pixel space:

1. snap_members(): pull beam endpoints + wall vertices to the nearest
   column centroid or grid intersection within a pixel tolerance.

2. split_beams_at_columns(): break each long beam at every column whose
   centroid lies on it, so the pushed ETABS model has frame nodes at
   intermediate columns (otherwise a single 4-column beam becomes one
   ETABS frame with no intermediate connectivity).
"""
from typing import Dict, List, Tuple, Optional
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


def _snap_inside_column(pt: Tuple[float, float], columns: List[Dict],
                         margin_px: float = 8.0) -> Optional[Tuple[float, float]]:
    """
    If `pt` lies inside any column's bounding box (expanded by margin_px),
    return that column's centroid. Otherwise None.
    Catches beams whose endpoints end at the column EDGE rather than the
    centroid — common when engineers draw beams to touch column outline.
    """
    px, py = pt
    for c in columns:
        bbox = c.get("bbox") or []
        if len(bbox) != 4:
            continue
        x, y, w, h = bbox
        if (x - margin_px) <= px <= (x + w + margin_px) and \
           (y - margin_px) <= py <= (y + h + margin_px):
            return (float(c["cx"]), float(c["cy"]))
    return None


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


def snap_members(members: Dict, grids: Dict, tol_px: float = 30.0,
                  min_beam_len_px: float = 25.0) -> Dict:
    """
    Snap beam endpoints + wall polygon vertices to nearest column or grid
    intersection within tol_px. Mutates and returns members.

    Safeguard: if snapping both endpoints would shrink a beam below
    min_beam_len_px (e.g. with overly large tol both endpoints jump to the
    same column), revert that beam to its original endpoints rather than
    destroying it. This is what saves beams when the user dials snap tol
    way up.
    """
    snap_pts = _build_snap_points(members, grids)
    cols = members.get("columns", []) or []
    snapped_count = 0

    def _snap_one(pt: Tuple[float, float]) -> Tuple[float, float]:
        """Bbox snap takes priority — guarantees connectivity when endpoint
        lies on column edge. Falls back to radius snap to grids / far cols."""
        inside = _snap_inside_column(pt, cols)
        if inside is not None:
            return inside
        return _nearest(pt, snap_pts, tol_px)

    for b in members.get("beams", []):
        orig_x1, orig_y1 = b["x1"], b["y1"]
        orig_x2, orig_y2 = b["x2"], b["y2"]
        sx1, sy1 = _snap_one((orig_x1, orig_y1))
        sx2, sy2 = _snap_one((orig_x2, orig_y2))

        new_len = math.hypot(sx2 - sx1, sy2 - sy1)
        if new_len < min_beam_len_px:
            # Snap would degenerate the beam — keep original endpoints.
            b["length"] = math.hypot(orig_x2 - orig_x1, orig_y2 - orig_y1)
            continue

        if (sx1, sy1) != (orig_x1, orig_y1):
            b["x1"], b["y1"] = sx1, sy1
            snapped_count += 1
        if (sx2, sy2) != (orig_x2, orig_y2):
            b["x2"], b["y2"] = sx2, sy2
            snapped_count += 1
        b["length"] = new_len

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


def split_beams_at_columns(members: Dict, grids: Dict,
                           perp_tol_px: float = 15.0,
                           min_segment_px: float = 20.0) -> Dict:
    """
    Split each beam at every column centroid (and grid intersection) that
    lies within perp_tol_px of the beam line. Replaces the original beam
    with N+1 short segments so ETABS gets a frame node at each column.

    Original beam metadata (e.g. 'label') is copied to all segments.
    """
    cols = members.get("columns", []) or []
    col_pts = [(float(c["cx"]), float(c["cy"])) for c in cols]

    # Also include grid intersections — beams resting on a grid line should
    # break at every grid intersection along their length.
    x_lines = [float(g["x"]) for g in grids.get("x_grids", [])]
    y_lines = [float(g["y"]) for g in grids.get("y_grids", [])]
    grid_pts = [(x, y) for x in x_lines for y in y_lines]
    break_pts = col_pts + grid_pts

    new_beams: List[Dict] = []
    for b in members.get("beams", []):
        x1, y1 = float(b["x1"]), float(b["y1"])
        x2, y2 = float(b["x2"]), float(b["y2"])
        dx, dy = x2 - x1, y2 - y1
        L = math.hypot(dx, dy)
        if L < 1:
            continue
        ux, uy = dx / L, dy / L
        nx, ny = -uy, ux

        # Find every break point near the beam, parameterise along axis
        ts: List[float] = []
        for px, py in break_pts:
            perp = abs((px - x1) * nx + (py - y1) * ny)
            if perp > perp_tol_px:
                continue
            t = (px - x1) * ux + (py - y1) * uy
            if min_segment_px < t < L - min_segment_px:
                ts.append(t)
        ts.sort()

        # Build list of break positions: 0, ts..., L
        ts = [0.0] + ts + [L]
        # Dedupe near-equal ts within min_segment_px
        clean: List[float] = [ts[0]]
        for t in ts[1:]:
            if t - clean[-1] >= min_segment_px:
                clean.append(t)
        if clean[-1] != L:
            clean[-1] = L

        # Emit a beam per segment, preserving original label
        base = {k: v for k, v in b.items() if k not in ("x1", "y1", "x2", "y2", "length")}
        for i in range(len(clean) - 1):
            ta, tb = clean[i], clean[i + 1]
            xa, ya = x1 + ta * ux, y1 + ta * uy
            xb, yb = x1 + tb * ux, y1 + tb * uy
            seg = dict(base)
            seg.update({
                "x1": int(round(xa)), "y1": int(round(ya)),
                "x2": int(round(xb)), "y2": int(round(yb)),
                "length": float(tb - ta),
            })
            new_beams.append(seg)

    members["beams"] = new_beams
    return members


def autofill_grid_beams(members: Dict,
                         row_tol_px: float = 25.0) -> Dict:
    """
    Generate orthogonal beams between every pair of adjacent columns on the
    same row or column (i.e. columns whose y- or x-coordinate matches within
    row_tol_px). Existing beams are left alone — autofill only adds beams
    where a connection is missing between adjacent collinear columns.

    Use case: engineer drew only horizontal beams on the PDF; verticals
    are implied by the column layout but never inked.
    """
    cols = members.get("columns", []) or []
    if len(cols) < 2:
        return members

    existing = members.get("beams", []) or []
    # Build a quick lookup of existing beam axes (rounded coord pairs)
    existing_axes = set()
    for b in existing:
        x1, y1, x2, y2 = b["x1"], b["y1"], b["x2"], b["y2"]
        # Use sorted tuple so direction doesn't matter
        existing_axes.add((min(x1, x2), min(y1, y2),
                            max(x1, x2), max(y1, y2)))

    def _have_beam(a, b) -> bool:
        x1, y1 = a; x2, y2 = b
        for ex in existing_axes:
            ex1, ey1, ex2, ey2 = ex
            # Check both axes overlap within tol
            if (abs(x1 - ex1) < row_tol_px and abs(y1 - ey1) < row_tol_px
                and abs(x2 - ex2) < row_tol_px and abs(y2 - ey2) < row_tol_px):
                return True
            if (abs(x1 - ex2) < row_tol_px and abs(y1 - ey2) < row_tol_px
                and abs(x2 - ex1) < row_tol_px and abs(y2 - ey1) < row_tol_px):
                return True
        return False

    pts = [(float(c["cx"]), float(c["cy"])) for c in cols]
    added = 0

    # Group by row (similar y) → sort by x → connect adjacent
    by_row: Dict[int, List[Tuple[float, float]]] = {}
    for x, y in pts:
        key = int(round(y / row_tol_px))
        by_row.setdefault(key, []).append((x, y))
    for row in by_row.values():
        if len(row) < 2: continue
        row.sort(key=lambda p: p[0])
        for i in range(len(row) - 1):
            a, b = row[i], row[i + 1]
            if _have_beam(a, b): continue
            existing.append({"x1": int(round(a[0])), "y1": int(round(a[1])),
                              "x2": int(round(b[0])), "y2": int(round(b[1])),
                              "length": float(math.hypot(b[0]-a[0], b[1]-a[1])),
                              "label": "", "auto": True})
            added += 1

    # Group by column (similar x) → sort by y → connect adjacent
    by_col: Dict[int, List[Tuple[float, float]]] = {}
    for x, y in pts:
        key = int(round(x / row_tol_px))
        by_col.setdefault(key, []).append((x, y))
    for col in by_col.values():
        if len(col) < 2: continue
        col.sort(key=lambda p: p[1])
        for i in range(len(col) - 1):
            a, b = col[i], col[i + 1]
            if _have_beam(a, b): continue
            existing.append({"x1": int(round(a[0])), "y1": int(round(a[1])),
                              "x2": int(round(b[0])), "y2": int(round(b[1])),
                              "length": float(math.hypot(b[0]-a[0], b[1]-a[1])),
                              "label": "", "auto": True})
            added += 1

    members["beams"] = existing
    members["autofilled_beams"] = added
    return members
