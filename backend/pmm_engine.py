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

                # ACI 318 §22.4 maximum axial capacity cap (flat top)
                # Cap P but keep the computed moments so the 3-D surface has a
                # proper flat circular disk at P = φPn,max (not a collapsed spike).
                Pn_max = 0.85 * sec.fc * (Ag - Ast) + sec.fy * Ast  # ACI 318-19 Eq 22.4.2.2 (φ applied separately)
                if Pn > Pn_max:
                    Pn = Pn_max

                # ── Strain in extreme tension steel ─────────────────────
                eps_t = min(eps_bars) if eps_bars else 0.0

                # ── ACI 318 φ factor ────────────────────────────────────
                if sec.include_phi:
                    if eps_t >= -eps_y:
                        phi = 0.65
                    elif eps_t > -(eps_y + 0.003):
                        phi = 0.65 + (-eps_t - eps_y) * (0.25 / 0.003)
                    else:
                        phi = 0.90
                else:
                    phi = 1.0

                # ── Section status ──────────────────────────────────────
                if   eps_t >= -eps_y:                  sc = 'CC'
                elif eps_t <= -(eps_y + 0.003):        sc = 'TC'
                else:                                   sc = 'TZ'

                P_list.append( round(phi * Pn,  2))
                Mx_list.append(round(phi * Mnx, 2))
                My_list.append(round(phi * Mny, 2))
                st_list.append(sc)
                ep_list.append(round(eps_t, 5))

        # Accumulate surface points
        all_P.extend(P_list);  all_Mx.extend(Mx_list)
        all_My.extend(My_list); all_status.extend(st_list); all_eps.extend(ep_list)

        alpha_deg = round(math.degrees(alpha_raw), 1)
        alpha_data[alpha_deg] = {'P': P_list, 'Mx': Mx_list, 'My': My_list}

    # ── 2D cardinal curves ───────────────────────────────────────────────
    curves_2d = {}
    available = list(alpha_data.keys())
    for target in (0, 90, 180, 270):
        best = min(available, key=lambda a: abs(a - target))
        curves_2d[str(target)] = alpha_data[best]

    return {
        'surface':    {'P': all_P, 'Mx': all_Mx, 'My': all_My,
                       'status': all_status, 'eps': all_eps},
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

        # Engine alpha=0° produces moment along the My axis; alpha=90° along Mx.
        # atan2(My,Mx) is 90° ahead of the engine alpha, so subtract pi/2 to align.
        alpha_d = math.pi / 2.0 - math.atan2(Myd, Mxd)
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
