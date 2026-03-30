"""
Unit tests for the three core PMM helper functions.

Run from the backend/ directory:
    python -m pytest tests/ -v

Or run directly:
    python tests/test_pmm_helpers.py
"""

import sys
import os
import math
import unittest

# ── Path setup ────────────────────────────────────────────────────────────────
_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# ── Import check_demands from pmm_engine (no FastAPI side-effects) ─────────────
from pmm_engine import check_demands

# ── Import helpers from main.py without starting the FastAPI server ────────────
import importlib.util as _ilu
import unittest.mock as _mock

_main_path = os.path.join(_BACKEND, 'main.py')
_spec      = _ilu.spec_from_file_location('_main_mod', _main_path)
_stub_mods = {
    'database':           _mock.MagicMock(),
    'config':             _mock.MagicMock(),
    'etabs_api':          _mock.MagicMock(),
    'etabs_api.actions':  _mock.MagicMock(),
    'etabs_api.connection': _mock.MagicMock(),
}
with _mock.patch.dict(sys.modules, _stub_mods):
    _main = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_main)

_normalize_bar_size   = _main._normalize_bar_size
_boundary_dcr_surface = _main._boundary_dcr_surface


# =============================================================================
# 1.  _normalize_bar_size
# =============================================================================

class TestNormalizeBarSize(unittest.TestCase):

    def test_omega_prefix_passthrough(self):
        """Ø-prefixed strings should be preserved and snapped."""
        self.assertEqual(_normalize_bar_size("Ø20"), "Ø20")
        self.assertEqual(_normalize_bar_size("Ø32"), "Ø32")
        self.assertEqual(_normalize_bar_size("Ø8"),  "Ø8")

    def test_d_prefix(self):
        self.assertEqual(_normalize_bar_size("D20"),  "Ø20")
        self.assertEqual(_normalize_bar_size("D-16"), "Ø16")
        self.assertEqual(_normalize_bar_size("d25"),  "Ø25")  # lower-case

    def test_r_prefix(self):
        self.assertEqual(_normalize_bar_size("R12"), "Ø12")
        self.assertEqual(_normalize_bar_size("R10"), "Ø10")

    def test_t_prefix(self):
        self.assertEqual(_normalize_bar_size("T16"), "Ø16")
        self.assertEqual(_normalize_bar_size("T32"), "Ø32")

    def test_us_designation(self):
        # #8 = 25.4 mm → snaps to Ø25
        self.assertEqual(_normalize_bar_size("#8"),  "Ø25")
        # #10 = 32.26 mm → snaps to Ø32
        self.assertEqual(_normalize_bar_size("#10"), "Ø32")
        # #5 = 15.9 mm → snaps to Ø16
        self.assertEqual(_normalize_bar_size("#5"),  "Ø16")

    def test_phib_prefix(self):
        self.assertEqual(_normalize_bar_size("PHIB20"), "Ø20")
        self.assertEqual(_normalize_bar_size("PHIB32"), "Ø32")

    def test_bare_integer(self):
        """Plain integers like ETABS '32' must map to Ø32."""
        self.assertEqual(_normalize_bar_size("32"), "Ø32")
        self.assertEqual(_normalize_bar_size("20"), "Ø20")
        self.assertEqual(_normalize_bar_size("16"), "Ø16")
        self.assertEqual(_normalize_bar_size("25"), "Ø25")

    def test_snaps_to_nearest_known_diameter(self):
        """Values not in the standard list snap to the nearest known diameter."""
        self.assertEqual(_normalize_bar_size("22"), "Ø20")  # nearest to 22 is 20
        self.assertEqual(_normalize_bar_size("30"), "Ø28")  # nearest to 30 is 28

    def test_empty_string_fallback(self):
        self.assertEqual(_normalize_bar_size(""), "Ø20")

    def test_no_digits_fallback(self):
        self.assertEqual(_normalize_bar_size("XYZBAR"), "Ø20")

    def test_omega_lowercase_variant(self):
        """ø (U+00F8) is sometimes used instead of Ø (U+00D8)."""
        result = _normalize_bar_size("\u00f820")
        self.assertEqual(result, "Ø20")


# =============================================================================
# 2.  check_demands  (pmm_engine)
# =============================================================================

def _make_uniaxial_alpha_data():
    """
    Minimal alpha_data for a uniaxial column in the Mx direction.
    Single meridian at alpha=90° (pure-Mx direction per engine convention).
    P: 1000 → -200 kN, M capacity peaks at ~100 kN·m around P=400 kN.
    """
    Plist = [1000.0, 700.0, 400.0, 0.0, -200.0]
    Mlist = [0.0,    60.0,  100.0, 70.0,   0.0]
    return {
        "90.0": {"P": Plist, "Mx": Mlist, "My": [0.0] * 5},
    }


class TestCheckDemands(unittest.TestCase):

    def test_pass_inside_surface(self):
        ad  = _make_uniaxial_alpha_data()
        out = check_demands(ad, 1000.0, -200.0,
                            [{"label": "D1", "P": 400.0, "Mx": 50.0, "My": 0.0}])
        self.assertEqual(out[0]["status"], "PASS")
        self.assertLessEqual(out[0]["DCR"], 1.0)

    def test_fail_outside_surface(self):
        ad  = _make_uniaxial_alpha_data()
        out = check_demands(ad, 1000.0, -200.0,
                            [{"label": "D2", "P": 400.0, "Mx": 200.0, "My": 0.0}])
        self.assertEqual(out[0]["status"], "FAIL")
        self.assertGreater(out[0]["DCR"], 1.0)

    def test_pure_axial_compression_pass(self):
        ad  = _make_uniaxial_alpha_data()
        out = check_demands(ad, 1000.0, -200.0,
                            [{"label": "D3", "P": 800.0, "Mx": 0.0, "My": 0.0}])
        self.assertEqual(out[0]["status"], "PASS")
        self.assertAlmostEqual(out[0]["DCR"], 0.8, delta=0.01)

    def test_pure_axial_exceeds_pmax(self):
        ad  = _make_uniaxial_alpha_data()
        out = check_demands(ad, 1000.0, -200.0,
                            [{"label": "D4", "P": 1200.0, "Mx": 0.0, "My": 0.0}])
        self.assertEqual(out[0]["status"], "FAIL")

    def test_label_passthrough(self):
        ad  = _make_uniaxial_alpha_data()
        out = check_demands(ad, 1000.0, -200.0,
                            [{"label": "COMBO1", "P": 400.0, "Mx": 30.0, "My": 0.0}])
        self.assertEqual(out[0]["label"], "COMBO1")

    def test_output_keys_present(self):
        ad  = _make_uniaxial_alpha_data()
        out = check_demands(ad, 1000.0, -200.0,
                            [{"label": "X", "P": 400.0, "Mx": 50.0, "My": 0.0}])
        for key in ("label", "P", "Mx", "My", "M_demand",
                    "alpha_deg", "M_cap", "DCR", "status"):
            self.assertIn(key, out[0], f"Missing key: {key}")

    def test_multiple_demands_all_returned(self):
        ad = _make_uniaxial_alpha_data()
        demands = [
            {"label": "A", "P": 400.0, "Mx": 50.0,  "My": 0.0},
            {"label": "B", "P": 400.0, "Mx": 200.0, "My": 0.0},
        ]
        out = check_demands(ad, 1000.0, -200.0, demands)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["status"], "PASS")
        self.assertEqual(out[1]["status"], "FAIL")

    def test_dcr_monotonic_with_demand(self):
        """Larger moment → larger DCR."""
        ad = _make_uniaxial_alpha_data()
        d1 = check_demands(ad, 1000.0, -200.0,
                           [{"label": "lo", "P": 400.0, "Mx": 30.0, "My": 0.0}])
        d2 = check_demands(ad, 1000.0, -200.0,
                           [{"label": "hi", "P": 400.0, "Mx": 80.0, "My": 0.0}])
        self.assertLess(d1[0]["DCR"], d2[0]["DCR"])


# =============================================================================
# 3.  _boundary_dcr_surface
# =============================================================================

def _make_circular_surface(n_alpha: int = 36, n_pts: int = 10):
    """
    Synthetic circular-symmetric PMM surface.
    Pmax = 1000 kN, Pmin = -200 kN.
    Moment capacity R(P) follows a parabola peaking at 100 kN·m around P=400 kN.
    """
    P_vals = [1000.0 - 1200.0 * k / (n_pts - 1) for k in range(n_pts)]

    def R(p):
        t = (p - (-200.0)) / 1200.0        # 0 → 1
        return 100.0 * 4.0 * t * (1.0 - t)  # parabola, peak = 100 at t=0.5

    allP, allMx, allMy = [], [], []
    for a in range(n_alpha):
        ang = 2.0 * math.pi * a / n_alpha
        for p in P_vals:
            r = R(p)
            allP .append(p)
            allMx.append(r * math.cos(ang))
            allMy.append(r * math.sin(ang))

    return {"P": allP, "Mx": allMx, "My": allMy}, n_pts


class TestBoundaryDcrSurface(unittest.TestCase):

    def test_demand_inside_returns_dcr_lt1(self):
        surf, npts = _make_circular_surface()
        # At P=400 capacity R≈100; demand |M|=50 → DCR≈0.5
        dcr = _boundary_dcr_surface(surf, npts,
                                     [{"P": 400.0, "Mx": 50.0, "My": 0.0}])
        self.assertLess(dcr, 1.0, f"Expected DCR < 1, got {dcr:.3f}")

    def test_demand_outside_returns_dcr_gt1(self):
        surf, npts = _make_circular_surface()
        dcr = _boundary_dcr_surface(surf, npts,
                                     [{"P": 400.0, "Mx": 0.0, "My": 150.0}])
        self.assertGreater(dcr, 1.0, f"Expected DCR > 1, got {dcr:.3f}")

    def test_p_above_pmax_returns_999(self):
        surf, npts = _make_circular_surface()
        dcr = _boundary_dcr_surface(surf, npts,
                                     [{"P": 2000.0, "Mx": 0.0, "My": 50.0}])
        self.assertGreaterEqual(dcr, 999.0)

    def test_pure_axial_demand(self):
        """Mx=My=0 → pure-axial DCR = P / Pmax."""
        surf, npts = _make_circular_surface()
        dcr = _boundary_dcr_surface(surf, npts,
                                     [{"P": 800.0, "Mx": 0.0, "My": 0.0}])
        self.assertAlmostEqual(dcr, 0.8, delta=0.05)

    def test_symmetric_surface_mx_my_equal(self):
        """
        For a circular surface, a demand with only Mx should give the same DCR
        as a demand with only My of the same magnitude.
        (Verifies the Mx/My convention un-swap doesn't break symmetry.)
        """
        surf, npts = _make_circular_surface()
        dcr_mx = _boundary_dcr_surface(surf, npts,
                                        [{"P": 400.0, "Mx": 60.0, "My": 0.0}])
        dcr_my = _boundary_dcr_surface(surf, npts,
                                        [{"P": 400.0, "Mx": 0.0,  "My": 60.0}])
        self.assertAlmostEqual(dcr_mx, dcr_my, delta=0.05,
                               msg="Symmetric surface: Mx-only vs My-only DCR should match")

    def test_multiple_demands_returns_max(self):
        """Return value must be the maximum DCR across all demands."""
        surf, npts = _make_circular_surface()
        demands = [
            {"P": 400.0, "Mx": 30.0, "My": 0.0},   # inside
            {"P": 400.0, "Mx": 0.0,  "My": 150.0},  # outside
        ]
        dcr = _boundary_dcr_surface(surf, npts, demands)
        self.assertGreater(dcr, 1.0)

    def test_empty_surface_returns_zero(self):
        surf = {"P": [], "Mx": [], "My": []}
        dcr  = _boundary_dcr_surface(surf, 10,
                                      [{"P": 400.0, "Mx": 50.0, "My": 0.0}])
        self.assertEqual(dcr, 0.0)

    def test_dcr_proportional_to_demand_magnitude(self):
        """Doubling the moment demand should roughly double the DCR."""
        surf, npts = _make_circular_surface()
        dcr1 = _boundary_dcr_surface(surf, npts,
                                      [{"P": 400.0, "Mx": 30.0, "My": 0.0}])
        dcr2 = _boundary_dcr_surface(surf, npts,
                                      [{"P": 400.0, "Mx": 60.0, "My": 0.0}])
        self.assertAlmostEqual(dcr2, dcr1 * 2.0, delta=dcr1 * 0.15,
                               msg="DCR should scale roughly linearly with moment demand")


# =============================================================================
# 4.  Integration: check_demands vs _boundary_dcr_surface consistency
# =============================================================================

class TestDCRMethodConsistency(unittest.TestCase):
    """
    Both methods should give broadly consistent results (within ~20%) on the
    same synthetic data.  Exact agreement is not expected because they use
    different boundary representations, but they should agree on PASS/FAIL.
    """

    def _build_alpha_data_from_surface(self, surf, npts):
        """Convert surface dict → alpha_data dict that check_demands expects."""
        allP  = surf['P']
        allMx = surf['Mx']
        allMy = surf['My']
        n_total = len(allP)
        n_alpha = round(n_total / npts)
        alpha_data = {}
        for a in range(n_alpha):
            base  = a * npts
            ang_d = round(360.0 * a / n_alpha, 1)
            alpha_data[str(ang_d)] = {
                'P':  allP [base:base + npts],
                'Mx': allMx[base:base + npts],
                'My': allMy[base:base + npts],
            }
        return alpha_data

    def test_pass_fail_agree(self):
        surf, npts = _make_circular_surface(n_alpha=36, n_pts=20)
        ad = self._build_alpha_data_from_surface(surf, npts)

        demands = [
            {"label": "inside", "P": 400.0, "Mx": 0.0, "My": 40.0},
            {"label": "outside","P": 400.0, "Mx": 0.0, "My": 140.0},
        ]

        # check_demands (engine convention — no swap needed here)
        eng_results = check_demands(ad, 1000.0, -200.0, demands)

        # _boundary_dcr_surface per-demand (engine convention demands)
        for i, d in enumerate(demands):
            dcr_bnd = _boundary_dcr_surface(surf, npts, [d])
            dcr_eng = float(eng_results[i]["DCR"])
            status_bnd = "PASS" if dcr_bnd <= 1.0 else "FAIL"
            status_eng = eng_results[i]["status"]
            self.assertEqual(status_bnd, status_eng,
                             f"Demand '{d['label']}': methods disagree on PASS/FAIL "
                             f"(bnd={dcr_bnd:.3f}, eng={dcr_eng:.3f})")


# =============================================================================
# Runner
# =============================================================================

if __name__ == '__main__':
    unittest.main(verbosity=2)
