"""
P-M-M Interaction Diagram Engine (ACI 318-19)
Adapted from PMMCurve3D.py by Karim Laknejadi, Ph.D. in Structural Engineering.
Refactored for use as a FastAPI backend module with no global state.

Units: kips and inches (US customary)
"""

import math
import os
from dataclasses import dataclass
from typing import List, Tuple


# ---------------------------------------------------------------------------
# Rebar look-up table  (#3 … #18, areas in in²)
# ---------------------------------------------------------------------------
REBAR_TABLE = {
    "#3":  0.11, "#4":  0.20, "#5":  0.31, "#6":  0.44,
    "#7":  0.60, "#8":  0.79, "#9":  1.00, "#10": 1.27,
    "#11": 1.56, "#14": 2.25, "#18": 4.00,
}


@dataclass
class PMMSection:
    corner_coords: List[Tuple[float, float]]  # CCW polygon vertices (in)
    fc:            float   # concrete compressive strength (ksi)
    fy:            float   # steel yield strength (ksi)
    Es:            float   # steel elastic modulus (ksi)
    alpha_steps:   float   # angular increment of neutral-axis rotation (degrees)
    num_points:    int     # number of c values per angle
    include_phi:   bool    # apply ACI 318 strength-reduction factors
    bar_areas:     List[float]               # area of each bar (in²)
    bar_positions: List[Tuple[float, float]] # (x, y) centre of each bar (in)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _polygon_area(v: list) -> float:
    n = len(v)
    a = 0.0
    for i in range(n):
        x1, y1 = v[i];  x2, y2 = v[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def _polygon_centroid(v: list, area: float) -> Tuple[float, float]:
    n = len(v);  cx = cy = 0.0
    for i in range(n):
        x1, y1 = v[i];  x2, y2 = v[(i + 1) % n]
        f = x1 * y2 - x2 * y1
        cx += (x1 + x2) * f;  cy += (y1 + y2) * f
    return cx / (6 * area), cy / (6 * area)


def _gross_centroid(corners, bar_areas, bar_positions) -> Tuple[float, float]:
    Ac = _polygon_area(corners)
    xc, yc = _polygon_centroid(corners, Ac)
    Ast = sum(bar_areas)
    rx = sum(a * p[0] for a, p in zip(bar_areas, bar_positions))
    ry = sum(a * p[1] for a, p in zip(bar_areas, bar_positions))
    cx = (Ac * xc + rx) / (Ac + Ast)
    cy = (Ac * yc + ry) / (Ac + Ast)
    return cx, cy


def _dist_from_line(line, point) -> Tuple[float, str]:
    """
    Signed distance from point to line  y = m*x + b  (line = [m, b]).
    Returns (distance, 'top' | 'bottom').
    """
    m, b = line
    num = -m * point[0] + point[1] - b
    den = math.sqrt(m * m + 1.0)
    return abs(num) / den, ('top' if num >= 0 else 'bottom')


def _split_area(polygon_coords, line_coords):
    """
    Split a convex polygon with an infinite line and return (top_part, bot_part)
    dicts with keys 'area', 'xc', 'yc'.  Returns None if no intersection.

    Pure-Python implementation — no shapely or numpy required.
    Uses the Sutherland-Hodgman edge-clipping algorithm.
    """
    lp1, lp2 = line_coords[0], line_coords[-1]
    dx = lp2[0] - lp1[0];  dy = lp2[1] - lp1[1]

    def _side(p):
        """Positive = left of lp1→lp2 ('above' when line goes left→right)."""
        return dx * (p[1] - lp1[1]) - dy * (p[0] - lp1[0])

    def _intersect(p1, p2):
        """Intersection of segment p1-p2 with the infinite line lp1-lp2."""
        s1, s2 = _side(p1), _side(p2)
        if abs(s1 - s2) < 1e-14:
            return None
        t = s1 / (s1 - s2)
        return (p1[0] + t * (p2[0] - p1[0]),
                p1[1] + t * (p2[1] - p1[1]))

    # Split polygon vertices into two lists using the splitting line
    n = len(polygon_coords)
    sides = [_side(p) for p in polygon_coords]

    # Check any vertex crosses to the other side (real intersection)
    if all(s >= 0 for s in sides) or all(s <= 0 for s in sides):
        return None  # Line doesn't split the polygon

    left, right = [], []   # left = positive side of the directed line
    for i in range(n):
        j = (i + 1) % n
        p1, p2 = polygon_coords[i], polygon_coords[j]
        s1, s2 = sides[i], sides[j]
        (left if s1 >= 0 else right).append(p1)
        if (s1 > 0 > s2) or (s1 < 0 < s2):
            ix = _intersect(p1, p2)
            if ix:
                left.append(ix)
                right.append(ix)

    if len(left) < 3 or len(right) < 3:
        return None

    def _area_centroid(pts):
        A = _polygon_area(pts)
        if A < 1e-14:
            return A, pts[0][0], pts[0][1]
        cx, cy = _polygon_centroid(pts, A)
        return A, cx, cy

    A1, cx1, cy1 = _area_centroid(left)
    A2, cx2, cy2 = _area_centroid(right)

    # "top" = centroid above the line (positive side when line goes left→right)
    if _side((cx1, cy1)) >= 0:
        top = {'area': A1, 'xc': cx1, 'yc': cy1}
        bot = {'area': A2, 'xc': cx2, 'yc': cy2}
    else:
        top = {'area': A2, 'xc': cx2, 'yc': cy2}
        bot = {'area': A1, 'xc': cx1, 'yc': cy1}

    return top, bot


def _outer_envelope_curve(P_raw, Mx_raw, My_raw, n_out=None,
                           P_lo=None, P_hi=None):
    """
    Extract the outer-envelope of a φ-reduced P-M meridian curve.

    Background (ACI 318-19):
        The φ factor transitions from 0.90 (TC) down to 0.65 (CC) through the
        tension-transition zone.  While φ is decreasing, φ·Pn can temporarily
        *decrease* even though the nominal Pn is still rising.  In (P, M) space
        this makes the curve fold back on itself, forming a closed loop — the
        so-called "nose" of the ACI φ-reduced diagram.  At any P level inside
        this loop there are two moment values: one on the TC branch (φ=0.90,
        high M) and one on the CC branch (φ=0.65, low M).

        For design only the *outer* boundary matters.  Every production column
        program (spColumn, ColumnBase …) shows the outer envelope, not the raw
        folded curve.

    Method:
        Resample to a uniform P grid [P_min … P_max]; at each P level scan ALL
        segments of the raw curve and keep the interpolation with the largest
        moment magnitude.  Identical to the ACI "max-M at each P" convention.

    Args:
        P_raw, Mx_raw, My_raw : raw φ-reduced output from the engine sweep
        n_out                  : number of output points (default = len(P_raw))
        P_lo, P_hi             : override the P range for the output grid.
                                 Pass the *global* P_min/P_max when rebuilding
                                 the surface so every meridian shares the same
                                 uniform P grid → perfect 3-D triangulation.

    Returns:
        (P_out, Mx_out, My_out) – outer-envelope lists, uniformly spaced in P
    """
    n = len(P_raw)
    if n < 2:
        return list(P_raw), list(Mx_raw), list(My_raw)
    n_out   = n_out if n_out is not None else n
    P_min   = P_lo  if P_lo  is not None else min(P_raw)
    P_max   = P_hi  if P_hi  is not None else max(P_raw)
    P_range = P_max - P_min
    if P_range < 1e-12:
        return list(P_raw), list(Mx_raw), list(My_raw)

    P_out, Mx_out, My_out = [], [], []
    for k in range(n_out):
        Pt      = P_min + P_range * k / (n_out - 1)
        best_M2 = -1.0
        best_mx = best_my = 0.0
        for j in range(n - 1):
            p1, p2 = P_raw[j], P_raw[j + 1]
            dp = p2 - p1
            if abs(dp) < 1e-12:
                continue
            t = (Pt - p1) / dp
            if t < -1e-9 or t > 1 + 1e-9:
                continue
            tc   = max(0.0, min(1.0, t))
            cMx  = Mx_raw[j] + tc * (Mx_raw[j + 1] - Mx_raw[j])
            cMy  = My_raw[j] + tc * (My_raw[j + 1] - My_raw[j])
            M2   = cMx * cMx + cMy * cMy
            if M2 > best_M2:
                best_M2 = M2
                best_mx = cMx
                best_my = cMy
        if best_M2 >= 0:
            P_out.append(round(Pt,      2))
            Mx_out.append(round(best_mx, 2))
            My_out.append(round(best_my, 2))

    return P_out, Mx_out, My_out


def _quadrant_corners(coords):
    """
    Return corner indices ordered [Q3, Q4, Q1, Q2] from centroid quadrants.
    Used to initialise the 'max-compression corner' for each α sweep.
    """
    Ac = _polygon_area(coords)
    cx, cy = _polygon_centroid(coords, Ac)

    def farthest(pts):
        return max(pts, key=lambda p: math.hypot(p[0] - cx, p[1] - cy)) if pts else None

    Q = {'Q1': [], 'Q2': [], 'Q3': [], 'Q4': []}
    for p in coords:
        x, y = p
        if   x >= cx and y >= cy: Q['Q1'].append(p)
        elif x <  cx and y >= cy: Q['Q2'].append(p)
        elif x <  cx and y <  cy: Q['Q3'].append(p)
        else:                      Q['Q4'].append(p)

    result = []
    for q in ('Q3', 'Q4', 'Q1', 'Q2'):
        rep = farthest(Q[q])
        if rep is not None:
            for i, p in enumerate(coords):
                if p == rep and i not in result:
                    result.append(i); break
    return result


# ---------------------------------------------------------------------------
# Bar layout helpers
# ---------------------------------------------------------------------------

def rect_coords(b: float, h: float) -> List[Tuple[float, float]]:
    """CCW corner coordinates of a b×h rectangle with origin at (0,0)."""
    return [(0.0, 0.0), (b, 0.0), (b, h), (0.0, h)]


def perimeter_bars(b: float, h: float, cover: float,
                   n_bars: int, bar_area: float
                   ) -> Tuple[List[float], List[Tuple[float, float]]]:
    """
    Distribute n_bars evenly around the inner perimeter of a rectangular
    section (cover to bar centre).  Returns (areas[], positions[]).

    When n_bars is divisible by 4, bars are placed n/4 per side and centered
    on each side — this guarantees biaxial symmetry so that the P-M-M surface
    is symmetric under 0°/180° and 90°/270° reversal.
    For other counts the original uniform-perimeter-spacing method is used.
    """
    n_bars = max(4, n_bars)
    x0, y0 = cover, cover
    x1, y1 = b - cover, h - cover
    bot_len = x1 - x0
    rgt_len = y1 - y0

    positions = []

    if n_bars % 4 == 0:
        # n per side — bars centered on each side (not corner-anchored)
        n = n_bars // 4
        for i in range(n):          # bottom: left → right
            positions.append((x0 + bot_len * (i + 1) / (n + 1), y0))
        for i in range(n):          # right:  bottom → top
            positions.append((x1, y0 + rgt_len * (i + 1) / (n + 1)))
        for i in range(n):          # top:    right → left
            positions.append((x1 - bot_len * (i + 1) / (n + 1), y1))
        for i in range(n):          # left:   top → bottom
            positions.append((x0, y1 - rgt_len * (i + 1) / (n + 1)))
    else:
        # Fallback: uniform perimeter spacing, offset by half-spacing so bars
        # are centered between corners rather than corner-anchored.
        perim   = 2 * (bot_len + rgt_len)
        spacing = perim / n_bars
        for i in range(n_bars):
            d = (spacing / 2 + i * spacing) % perim
            if d < bot_len:
                positions.append((x0 + d, y0))
            elif d < bot_len + rgt_len:
                positions.append((x1, y0 + (d - bot_len)))
            elif d < 2 * bot_len + rgt_len:
                positions.append((x1 - (d - bot_len - rgt_len), y1))
            else:
                positions.append((x0, y1 - (d - 2 * bot_len - rgt_len)))

    return [bar_area] * n_bars, positions


def rect_bars_grid(b: float, h: float, cover: float,
                   nbars_b: int, nbars_h: int, bar_area: float
                   ) -> Tuple[List[float], List[Tuple[float, float]]]:
    """
    Place bars explicitly per face for a rectangular section.

    nbars_b : bars on bottom and top faces, INCLUDING the two corner bars (≥2)
    nbars_h : bars on each side face, NOT counting the corner bars (≥0)

    Total bars = 2*nbars_b + 2*nbars_h

    Bars are placed at equal spacing on each face, which guarantees exact
    biaxial symmetry about both the x and y centroidal axes.
    """
    nbars_b = max(2, nbars_b)
    nbars_h = max(0, nbars_h)
    x0, y0 = cover, cover
    x1, y1 = b - cover, h - cover

    positions: List[Tuple[float, float]] = []

    # Bottom face (y = y0): nbars_b evenly spaced including corners
    for i in range(nbars_b):
        x = x0 + (x1 - x0) * i / (nbars_b - 1) if nbars_b > 1 else (x0 + x1) / 2
        positions.append((x, y0))

    # Right face (x = x1): nbars_h intermediate bars (no corners)
    for j in range(nbars_h):
        y = y0 + (y1 - y0) * (j + 1) / (nbars_h + 1)
        positions.append((x1, y))

    # Top face (y = y1): nbars_b bars, right to left
    for i in range(nbars_b - 1, -1, -1):
        x = x0 + (x1 - x0) * i / (nbars_b - 1) if nbars_b > 1 else (x0 + x1) / 2
        positions.append((x, y1))

    # Left face (x = x0): nbars_h intermediate bars, top to bottom
    for j in range(nbars_h - 1, -1, -1):
        y = y0 + (y1 - y0) * (j + 1) / (nbars_h + 1)
        positions.append((x0, y))

    total = 2 * nbars_b + 2 * nbars_h
    return [bar_area] * total, positions


# ---------------------------------------------------------------------------
# Core P-M-M engine
# ---------------------------------------------------------------------------

def compute_pmm(sec: PMMSection) -> dict:
    """
    Compute the P-M-M interaction surface.

    Returns a JSON-serialisable dict:
    {
      'surface':   { 'P', 'Mx', 'My', 'status', 'eps' },
      'curves_2d': { '0': {P,Mx,My}, '90': ..., '180': ..., '270': ... },
      'Pmax', 'Pmin', 'Ag', 'Ast', 'rho', 'centroid',
    }
    """
    coords   = sec.corner_coords
    Ag       = _polygon_area(coords)
    Ast      = sum(sec.bar_areas)
    centroid = _gross_centroid(coords, sec.bar_areas, sec.bar_positions)
    eps_y    = sec.fy / sec.Es
    beta1    = max(0.65, 0.85 - 0.05 * max(0.0, sec.fc - 4.0))

    xs = [p[0] for p in coords];  ys = [p[1] for p in coords]
    b_dim  = max(xs) - min(xs)
    h_dim  = max(ys) - min(ys)
    ext    = max(b_dim, h_dim) * 3   # line extension beyond section

    c_corners = _quadrant_corners(coords)
    nc        = len(c_corners)

    n_c = sec.num_points
    # Four-zone c-list: dense in tension/balanced region, then tapering smoothly
    # all the way to pure-compression so the top of the interaction diagram is
    # smooth (no visible kink from a single large jump to the anchor point).
    #   Zone 1 (68 %): c = 0.001 → c_scale        — balanced + φ-transition region
    #   Zone 2 (18 %): c = c_scale → 1.5×c_scale  — Pmax cap kicks in here
    #   Zone 3 (~13%): c = 1.5×c_scale → 8×c_scale — smooth M→0 taper
    #   Anchor (1 pt): c = 20×c_scale              — guarantees Pn_max plateau
    c_scale = max(b_dim, h_dim)
    _n_lo  = max(2, round(n_c * 0.68))
    _n_mid = max(1, round(n_c * 0.18))
    _n_hi  = max(1, n_c - _n_lo - _n_mid - 1)
    c_list = (
        [0.001 + c_scale * i / (_n_lo - 1) for i in range(_n_lo)]
        + [c_scale  + (0.5 * c_scale) * (i + 1) / _n_mid for i in range(_n_mid)]
        + [1.5 * c_scale + (6.5 * c_scale) * (i + 1) / _n_hi  for i in range(_n_hi)]
        + [c_scale * 20]                           # pure-compression anchor
    )

    # ── Bar-transition c-clustering ──────────────────────────────────────
    # Each rebar layer causes a kink in the P-M curve because the bar's
    # contribution abruptly switches from tension to compression as c passes
    # through its depth.  Inserting a tight 3-point cluster (d-δ, d, d+δ)
    # around every bar's transition depth for both horizontal (α≈0°) and
    # vertical (α≈90°) neutral-axis orientations adds at most ~3×n_bars extra
    # c values but gives sub-millimetre resolution exactly where kinks are
    # steepest — equivalent to what spColumn achieves with a very fine c-grid.
    _delta = 0.06   # cluster half-width in inches (≈1.5 mm)
    _extra_c: set = set()
    for bx, by in sec.bar_positions:
        for d in (by, h_dim - by, bx, b_dim - bx):
            if 0.01 < d < c_scale:
                _extra_c.update({
                    round(d - _delta, 5),
                    round(d,          5),
                    round(d + _delta, 5),
                })
    c_list = sorted(set(c_list) | _extra_c)
    n_c    = len(c_list)   # update: may be larger than sec.num_points
    n_a = int(360 / sec.alpha_steps)
    alpha_arr = [2 * math.pi * i / n_a for i in range(n_a)]

    all_P, all_Mx, all_My, all_status, all_eps = [], [], [], [], []
    alpha_data = {}   # alpha_deg → {P, Mx, My}

    _SING = 0.015  # absolute offset (rad) applied identically at π/2 and 3π/2
    for alpha_raw in alpha_arr:
        # Avoid tan() singularity at ±π/2 — use the SAME offset so that the two
        # near-vertical sweeps remain exactly π rad apart, preserving symmetry.
        alpha = alpha_raw
        if abs(alpha - math.pi / 2)     < 0.001: alpha = math.pi / 2     - _SING
        if abs(alpha - 3 * math.pi / 2) < 0.001: alpha = 3 * math.pi / 2 - _SING

        mi    = math.tan(alpha)
        cos_a = math.cos(alpha)

        # Compression side for this neutral-axis angle
        comp_side = 'top' if (alpha <= math.pi / 2 or alpha > 3 * math.pi / 2) else 'bottom'

        # Initial corner guess based on quadrant
        if   alpha < math.pi / 2:     corner = coords[c_corners[3 % nc]]
        elif alpha < math.pi:          corner = coords[c_corners[0 % nc]]
        elif alpha < 3 * math.pi / 2: corner = coords[c_corners[1 % nc]]
        else:                          corner = coords[c_corners[2 % nc]]

        P_list, Mx_list, My_list, st_list, ep_list = [], [], [], [], []
        run_status = True
        max_iters  = 5

        while run_status and max_iters > 0:
            max_iters  -= 1
            run_status  = True    # reset each iteration
            P_list, Mx_list, My_list, st_list, ep_list = [], [], [], [], []

            for c in c_list:
                a    = beta1 * c
                y0a  = corner[1] - a / cos_a - corner[0] * mi
                y0c  = corner[1] - c / cos_a - corner[0] * mi
                cline = [mi, y0c]

                # Compression block boundary line
                x_lo = min(xs) - ext;  x_hi = max(xs) + ext
                line_nodes = [(x_lo, x_lo * mi + y0a), (x_hi, x_hi * mi + y0a)]

                split = _split_area(coords, line_nodes)
                if split is not None:
                    tp, bp = split
                    part   = tp if comp_side == 'top' else bp
                    comp_A, Xc, Yc = part['area'], part['xc'], part['yc']
                else:
                    comp_A, Xc, Yc = Ag, centroid[0], centroid[1]

                # Check if a farther corner exists on comp side (update if so)
                if run_status:
                    d0, _ = _dist_from_line(cline, list(corner))
                    changed = False
                    for p in coords:
                        d, loc = _dist_from_line(cline, list(p))
                        if loc == comp_side and d > d0 + 1e-9:
                            corner = p;  d0 = d;  changed = True
                    if changed:
                        run_status = True
                        break        # restart while loop with new corner
                    else:
                        run_status = False   # corner stable → compute forces

                # ── Steel contribution ──────────────────────────────────
                Ps = Mxs = Mys = 0.0
                eps_bars = []
                comp_A_adj = comp_A

                for pos, Asi in zip(sec.bar_positions, sec.bar_areas):
                    xi, yi  = pos
                    di, loci = _dist_from_line(cline, [xi, yi])
                    eps_i   = (0.003 * di / c) if loci == comp_side else (-0.003 * di / c)
                    if loci == comp_side:
                        comp_A_adj -= Asi   # deduct steel from concrete area
                    stress_i = (sec.Es * eps_i
                                if abs(eps_i) < eps_y
                                else sec.fy * math.copysign(1.0, eps_i))
                    Fsi  = Asi * stress_i
                    Ps  += Fsi
                    Mxs += Fsi * (xi - centroid[0])
                    Mys += Fsi * (yi - centroid[1])
                    eps_bars.append(eps_i)

                # ── Concrete contribution ───────────────────────────────
                Fc  = 0.85 * sec.fc * max(0.0, comp_A_adj)
                Mcx = Fc * (Xc - centroid[0])
                Mcy = Fc * (Yc - centroid[1])

                Pn  = Fc  + Ps
                Mnx = Mcx + Mxs
                Mny = Mcy + Mys

                # ACI 318-19 §22.4.2.2: Tied column Pn,max = 0.80 × Po
                # (0.80 accounts for accidental eccentricity and is NOT part of φ)
                Po     = 0.85 * sec.fc * (Ag - Ast) + sec.fy * Ast
                Pn_max = 0.80 * Po                   # ACI 318-19 Eq 22.4.2.2 tied
                if Pn > Pn_max:
                    Pn = Pn_max

                # ── Strain in extreme tension steel ─────────────────────
                eps_t = min(eps_bars) if eps_bars else 0.0

                # ── ACI 318-19 §21.2.2 φ factor ─────────────────────────
                # Transition denominator is (0.005 − εy), not a hard-coded 0.003,
                # so the formula works correctly for any steel grade.
                eps_tc = 0.005   # ACI 318 tension-controlled strain limit
                if sec.include_phi:
                    if eps_t >= -eps_y:
                        phi = 0.65                   # compression-controlled
                    elif eps_t > -eps_tc:
                        phi = 0.65 + (-eps_t - eps_y) * (0.25 / (eps_tc - eps_y))
                    else:
                        phi = 0.90                   # tension-controlled
                else:
                    phi = 1.0

                # ── Section status ──────────────────────────────────────
                if   eps_t >= -eps_y:   sc = 'CC'
                elif eps_t <= -eps_tc:  sc = 'TC'
                else:                   sc = 'TZ'

                P_list.append( round(phi * Pn,  2))
                Mx_list.append(round(phi * Mny, 2))   # Mny = from y-displacements = moment about X-axis ✓
                My_list.append(round(phi * Mnx, 2))   # Mnx = from x-displacements = moment about Y-axis ✓
                st_list.append(sc)
                ep_list.append(round(eps_t, 5))

        # Accumulate surface points
        all_P.extend(P_list);  all_Mx.extend(Mx_list)
        all_My.extend(My_list); all_status.extend(st_list); all_eps.extend(ep_list)

        alpha_deg = round(math.degrees(alpha_raw), 1)
        alpha_data[alpha_deg] = {'P': P_list, 'Mx': Mx_list, 'My': My_list}

    # ── Surface rebuild: project every meridian onto a GLOBAL P grid ────────
    # After computing all meridians, the P values differ per alpha at the same
    # c index because the compression-block area varies with neutral-axis angle.
    # Re-sampling every meridian to the same global P range with
    # _outer_envelope_curve simultaneously:
    #   1. Gives all meridians identical P values at the same index
    #      → perfect 3-D triangulation without jagged rings or wavy fill
    #   2. Removes the ACI phi-factor "nose" loop (outer-envelope selection)
    #   3. Resolves bar-transition kinks via the max-M scan across all segments
    # After this pass the surface arrays contain n_a × n_c monotonic values.
    Pglo_min_r = min(all_P)
    Pglo_max_r = max(all_P)
    new_all_P, new_all_Mx, new_all_My = [], [], []
    for a_idx, alpha_raw in enumerate(alpha_arr):
        base = a_idx * n_c
        P_env, Mx_env, My_env = _outer_envelope_curve(
            all_P [base : base + n_c],
            all_Mx[base : base + n_c],
            all_My[base : base + n_c],
            n_out=n_c,
            P_lo=Pglo_min_r,
            P_hi=Pglo_max_r,
        )
        new_all_P .extend(P_env)
        new_all_Mx.extend(Mx_env)
        new_all_My.extend(My_env)
        # Keep alpha_data in sync with the cleaned uniform-P meridian
        alpha_deg = round(math.degrees(alpha_raw), 1)
        alpha_data[alpha_deg] = {'P': P_env, 'Mx': Mx_env, 'My': My_env}
    all_P  = new_all_P
    all_Mx = new_all_Mx
    all_My = new_all_My

    # ── 2D cardinal curves ───────────────────────────────────────────────────
    # alpha_data already contains cleaned uniform-P meridians.  Assign by
    # direct reference so that the caller's unit-conversion loop (which
    # mutates alpha_data dicts in-place) also updates curves_2d automatically.
    curves_2d = {}
    available = list(alpha_data.keys())
    for target in (0, 90, 180, 270):
        best = min(available, key=lambda a: abs(a - target))
        curves_2d[str(target)] = alpha_data[best]   # shared reference — converted by caller

    return {
        'surface':    {'P': all_P, 'Mx': all_Mx, 'My': all_My,
                       'num_points': n_c,   # actual pts/meridian (may differ from
                                            # sec.num_points after bar clustering)
                       'status': [], 'eps': []},
        'curves_2d':  curves_2d,
        'alpha_data': alpha_data,          # full sweep — used by check_demands
        'Pmax':       round(max(all_P),  2) if all_P else 0,
        'Pmin':       round(min(all_P),  2) if all_P else 0,
        'centroid':   [round(centroid[0], 3), round(centroid[1], 3)],
        'Ag':         round(Ag,  3),
        'Ast':        round(Ast, 3),
        'rho':        round(Ast / Ag * 100, 2) if Ag else 0,
    }


# ── Demand-point checker ──────────────────────────────────────────────────────

def check_demands(alpha_data: dict, Pmax: float, Pmin: float,
                  demands: list) -> list:
    """
    Check demand points against a computed PMM surface.

    All values must be in the same units as the ``compute_pmm`` output
    (kips / kip·in for the engine; kN / kN·m after SI conversion).
    """
    curves: List[Tuple] = []
    for adeg_str, curve in alpha_data.items():
        arad  = math.radians(float(adeg_str))
        Plist = curve['P']
        M_tot = [math.sqrt(mx * mx + my * my)
                 for mx, my in zip(curve['Mx'], curve['My'])]
        curves.append((arad, Plist, M_tot))
    curves.sort(key=lambda x: x[0])

    def _M_at_P(P_list: list, M_list: list, Pd: float) -> float:
        best = 0.0
        for i in range(len(P_list) - 1):
            p1, p2 = P_list[i], P_list[i + 1]
            m1, m2 = M_list[i], M_list[i + 1]
            lo, hi = min(p1, p2), max(p1, p2)
            if lo - 1e-6 <= Pd <= hi + 1e-6:
                t = (Pd - p1) / (p2 - p1) if abs(p2 - p1) > 1e-12 else 0.5
                best = max(best, m1 + max(0.0, min(1.0, t)) * (m2 - m1))
        return best

    def _M_cap(alpha_d: float, Pd: float) -> float:
        n = len(curves)
        if n == 0:
            return 0.0
        arad_list = [c[0] for c in curves]
        idx = next((i for i, a in enumerate(arad_list) if a > alpha_d + 1e-9), None)
        if idx is None or idx == 0:
            c1, c2 = curves[-1], curves[0]
            span   = (2.0 * math.pi - c1[0]) + c2[0]
            offset = (alpha_d - c1[0]) if idx is None else (alpha_d + 2.0 * math.pi - c1[0])
        else:
            c1, c2 = curves[idx - 1], curves[idx]
            span   = c2[0] - c1[0]
            offset = alpha_d - c1[0]
        w2 = max(0.0, min(1.0, offset / span)) if span > 1e-9 else 0.5
        w1 = 1.0 - w2
        return w1 * _M_at_P(c1[1], c1[2], Pd) + w2 * _M_at_P(c2[1], c2[2], Pd)

    out = []
    for d in demands:
        label = d.get('label', '')
        Pd    = float(d.get('P',  0.0))
        Mxd   = float(d.get('Mx', 0.0))
        Myd   = float(d.get('My', 0.0))
        Md    = math.sqrt(Mxd * Mxd + Myd * Myd)

        if Md < 1e-9:
            inside = (Pmin - 1e-6) <= Pd <= (Pmax + 1e-6)
            if Pmax > 1e-9 and Pd >= 0:
                dcr = Pd / Pmax
            elif Pmin < -1e-9 and Pd < 0:
                dcr = abs(Pd / Pmin)
            else:
                dcr = 0.0
            out.append({'label': label, 'P': Pd, 'Mx': Mxd, 'My': Myd,
                        'M_demand': 0.0, 'alpha_deg': None, 'M_cap': None,
                        'DCR': round(dcr, 3), 'status': 'PASS' if inside else 'FAIL'})
            continue

        # After Mx/My convention fix: alpha=0° → horizontal NA → Mx axis; alpha=90° → My axis.
        # atan2(Myd, Mxd) directly gives the demand angle from the Mx axis = engine alpha.
        alpha_d = math.pi / 2.0 - math.atan2(Mxd, Myd)
        if alpha_d < 0:
            alpha_d += 2.0 * math.pi

        if Pd > Pmax + 1e-6 or Pd < Pmin - 1e-6:
            out.append({'label': label, 'P': Pd, 'Mx': Mxd, 'My': Myd,
                        'M_demand': round(Md, 3),
                        'alpha_deg': round(math.degrees(alpha_d), 1),
                        'M_cap': 0.0, 'DCR': 999.0, 'status': 'FAIL'})
            continue

        Mc  = _M_cap(alpha_d, Pd)
        dcr = (Md / Mc) if Mc > 1e-9 else 999.0
        out.append({'label': label, 'P': Pd, 'Mx': Mxd, 'My': Myd,
                    'M_demand':  round(Md,  3),
                    'alpha_deg': round(math.degrees(alpha_d), 1),
                    'M_cap':     round(Mc,  3),
                    'DCR':       round(dcr, 3),
                    'status':    'PASS' if dcr <= 1.0 else 'FAIL'})
    return out
