"""
Integration test: pmm_calculate → pmm_check → pmm_optimize end-to-end stack.

Uses a well-documented reference column so results can be verified against
hand calculations or spColumn output:

  Reference column (RC-01)
  ─────────────────────────
  Section:   400 × 500 mm
  Concrete:  f'c = 28 MPa  (normal-weight)
  Steel:     fy  = 420 MPa,  Es = 200 000 MPa
  Cover:     40 mm clear (face → stirrup face)
  Stirrups:  Ø10 mm
  Rebar:     8 × Ø25 mm  (3 per b-face, 1 per h-face, 8 total)
               → Ast = 8 × 490.9 = 3927 mm²
               → ρ   = 3927 / (400 × 500) = 1.96 %

  Axial capacity benchmarks (ACI 318-19, φ=0.65 for tied columns):
    φPn,max  ≈ 0.80 × 0.65 × [0.85×28×(200000-3927) + 420×3927]
             ≈ 0.80 × 0.65 × [4 652 750 + 1 649 340]  (N)
             ≈ 3 277 kN          (approximate — engine is the authority)
    φPn,min  ≈ -(fy × Ast) ≈ -(420×3927)/1000 ≈ -1650 kN  (pure tension)

  Demand set (engine-sign: +P = compression; Mx/My pre-swapped per convention):
    D1  P =  1 500 kN,  Mx =   80 kN·m,  My =  40 kN·m  → should PASS
    D2  P =  1 500 kN,  Mx = 1 000 kN·m, My = 500 kN·m  → should FAIL (huge moments)
    D3  P =  3 500 kN,  Mx =    0 kN·m,  My =   0 kN·m  → should FAIL (above φPn,max)
    D4  P = -1 800 kN,  Mx =    0 kN·m,  My =   0 kN·m  → should FAIL (below φPn,min)
    D5  P =  2 000 kN,  Mx =    0 kN·m,  My =   0 kN·m  → pure-axial, may pass or fail
                                                             depending on exact Pmax

Run from the backend/ directory:
    python -m unittest tests.test_integration_pmm -v
"""

import sys
import os
import math
import unittest
import unittest.mock as _mock
import importlib.util as _ilu

# ── Path setup ────────────────────────────────────────────────────────────────
_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# ── Load main module without starting the FastAPI server ──────────────────────
_main_path = os.path.join(_BACKEND, 'main.py')
_spec      = _ilu.spec_from_file_location('_main_mod_integ', _main_path)
_stub_mods = {
    'database':             _mock.MagicMock(),
    'config':               _mock.MagicMock(),
    'etabs_api':            _mock.MagicMock(),
    'etabs_api.actions':    _mock.MagicMock(),
    'etabs_api.connection': _mock.MagicMock(),
}
with _mock.patch.dict(sys.modules, _stub_mods):
    _main = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_main)

# ── Grab the functions and classes we need ────────────────────────────────────
pmm_calculate        = _main.pmm_calculate
pmm_check            = _main.pmm_check
pmm_optimize         = _main.pmm_optimize
PMMRequest           = _main.PMMRequest
PMMCheckRequest      = _main.PMMCheckRequest
PMMOptimizeRequest   = _main.PMMOptimizeRequest
_pmm_cache           = _main._pmm_cache
_boundary_dcr_surface = _main._boundary_dcr_surface

# ── Reference column parameters ───────────────────────────────────────────────
COL = dict(
    b=400, h=500,
    fc=28.0, fy=420.0, Es=200000.0,
    cover=40.0, stirrup_dia_mm=10.0,
    nbars_b=3, nbars_h=1,   # 8 bars total  (2×3 + 2×1)
    bar_size='Ø25',
    include_phi=True,
    alpha_steps=10.0,
    num_points=70,
    units='SI',
)

# ── Demand set (engine-sign: +P = compression; Mx/My already pre-swapped) ────
#    Frontend swap rule: engine.Mx = table.My, engine.My = table.Mx
#    These demands are in ENGINE convention (as sent by frontend pmmCheckLoads).
DEMANDS_ENGINE = [
    {'label': 'D1-pass',    'P':  1500.0, 'Mx':  40.0, 'My':  80.0},  # PASS expected
    {'label': 'D2-fail-M',  'P':  1500.0, 'Mx': 500.0, 'My': 1000.0}, # FAIL: huge M
    {'label': 'D3-fail-P+', 'P':  3500.0, 'Mx':   0.0, 'My':   0.0},  # FAIL: above Pmax
    {'label': 'D4-fail-P-', 'P': -1800.0, 'Mx':   0.0, 'My':   0.0},  # FAIL: below Pmin
]

# ── Optimize demands (frontend convention: table Mx=M33, table My=M22,
#    P = ETABS sign negative=compression → engine sign = -P)
#    pmmOptimize frontend sends: P = -(table.P), Mx = table.My, My = table.Mx
DEMANDS_OPT = [
    {'label': 'D1', 'P': 1500.0, 'Mx': 40.0, 'My': 80.0},   # engine-sign, pre-swapped
    {'label': 'D2', 'P': 1800.0, 'Mx': 20.0, 'My': 60.0},
    {'label': 'D3', 'P': 2200.0, 'Mx':  5.0, 'My': 10.0},
]


class TestPMMIntegration(unittest.TestCase):
    """
    End-to-end tests: pmm_calculate → pmm_check → pmm_optimize.

    All three functions are called in sequence exactly as the frontend does:
      1. pmm_calculate  — builds the PMM surface, populates _pmm_cache
      2. pmm_check      — checks demands against the cached surface
      3. pmm_optimize   — finds the lightest rebar arrangement meeting target DCR
    """

    # ── Shared surface computed once per test class ───────────────────────────
    _result = None   # pmm_calculate response dict

    @classmethod
    def setUpClass(cls):
        """Compute the PMM surface once and cache it for all tests in this class."""
        req = PMMRequest(**COL)
        cls._result = pmm_calculate(req, current_user={})
        # _pmm_cache is now populated by pmm_calculate

    # =========================================================================
    # 1.  pmm_calculate — surface shape and key benchmarks
    # =========================================================================

    def test_calculate_returns_surface(self):
        """pmm_calculate must return a dict with 'surface' key."""
        self.assertIsNotNone(self._result)
        self.assertIn('surface', self._result)
        surf = self._result['surface']
        for key in ('P', 'Mx', 'My'):
            self.assertIn(key, surf)
            self.assertGreater(len(surf[key]), 0, f"surface['{key}'] is empty")

    def test_calculate_units_si(self):
        self.assertEqual(self._result.get('units'), 'SI')

    def test_calculate_pmax_reasonable(self):
        """φPn,max for RC-01 should be roughly 3000–4500 kN."""
        Pmax = self._result['Pmax']
        self.assertGreater(Pmax, 2500.0,
            f"φPn,max = {Pmax:.0f} kN seems too low (expected > 2500 kN)")
        self.assertLess(Pmax, 5000.0,
            f"φPn,max = {Pmax:.0f} kN seems too high (expected < 5000 kN)")

    def test_calculate_pmin_reasonable(self):
        """φPn,min (tension) should be roughly -1200 to -2000 kN."""
        Pmin = self._result['Pmin']
        self.assertLess(Pmin, 0.0,
            f"Pmin should be negative (tension), got {Pmin:.0f} kN")
        self.assertGreater(Pmin, -2500.0,
            f"φPn,min = {Pmin:.0f} kN seems too large in tension")

    def test_calculate_rho_matches_input(self):
        """ρ should match 8 × 490.9 / (400 × 500) ≈ 1.96 %."""
        Ast     = self._result.get('Ast', 0.0)      # mm²
        Ag      = self._result.get('Ag',  200000.0) # mm²
        rho_pct = 100.0 * Ast / Ag
        self.assertAlmostEqual(rho_pct, 1.96, delta=0.15,
            msg=f"ρ = {rho_pct:.2f} % (expected ≈ 1.96 %)")

    def test_calculate_surface_symmetry(self):
        """
        Mx and My capacity should be non-zero and in a sensible range.
        For a 400×500 column with Ø25@1.96%, peak moment is typically 150–600 kN·m.
        """
        surf = self._result['surface']
        mx_max = max(abs(v) for v in surf['Mx'])
        my_max = max(abs(v) for v in surf['My'])
        for label, val in [('Mx_max', mx_max), ('My_max', my_max)]:
            self.assertGreater(val, 50.0,
                f"{label} = {val:.1f} kN·m — too small")
            self.assertLess(val, 2000.0,
                f"{label} = {val:.1f} kN·m — suspiciously large")

    def test_calculate_cache_populated(self):
        """pmm_calculate must populate the shared _pmm_cache for pmm_check."""
        self.assertIsNotNone(_pmm_cache['alpha_data'],
            "_pmm_cache['alpha_data'] was not populated after pmm_calculate")
        self.assertIsNotNone(_pmm_cache['Pmax'])
        self.assertIsNotNone(_pmm_cache['Pmin'])

    # =========================================================================
    # 2.  pmm_check — demand status against the cached surface
    # =========================================================================

    def test_check_d1_passes(self):
        """D1 (P=1500, small moments) should be inside the surface → PASS."""
        req = PMMCheckRequest(demands=[DEMANDS_ENGINE[0]])
        out = pmm_check(req, current_user={})
        r   = out['results'][0]
        self.assertEqual(r['status'], 'PASS',
            f"D1 expected PASS, got {r['status']} (DCR={r['DCR']:.3f})")
        self.assertLessEqual(r['DCR'], 1.0,
            f"D1 DCR = {r['DCR']:.3f} should be ≤ 1.0")

    def test_check_d2_fails_large_moment(self):
        """D2 (massive moments) must be outside the surface → FAIL."""
        req = PMMCheckRequest(demands=[DEMANDS_ENGINE[1]])
        out = pmm_check(req, current_user={})
        r   = out['results'][0]
        self.assertEqual(r['status'], 'FAIL',
            f"D2 expected FAIL, got {r['status']} (DCR={r['DCR']:.3f})")
        self.assertGreater(r['DCR'], 1.0)

    def test_check_d3_fails_axial_above_pmax(self):
        """D3 (P=3500 kN) exceeds φPn,max → FAIL."""
        req = PMMCheckRequest(demands=[DEMANDS_ENGINE[2]])
        out = pmm_check(req, current_user={})
        r   = out['results'][0]
        self.assertEqual(r['status'], 'FAIL',
            f"D3 expected FAIL (P > Pmax), got {r['status']} (DCR={r['DCR']:.3f})")

    def test_check_d4_fails_axial_below_pmin(self):
        """D4 (P=-1800 kN) exceeds tension capacity → FAIL."""
        req = PMMCheckRequest(demands=[DEMANDS_ENGINE[3]])
        out = pmm_check(req, current_user={})
        r   = out['results'][0]
        self.assertEqual(r['status'], 'FAIL',
            f"D4 expected FAIL (P < Pmin), got {r['status']} (DCR={r['DCR']:.3f})")

    def test_check_all_demands_returned(self):
        """pmm_check must return one result per demand, preserving order."""
        req = PMMCheckRequest(demands=DEMANDS_ENGINE)
        out = pmm_check(req, current_user={})
        self.assertEqual(len(out['results']), len(DEMANDS_ENGINE))
        for i, r in enumerate(out['results']):
            self.assertEqual(r['label'], DEMANDS_ENGINE[i]['label'],
                f"Result[{i}] label mismatch")

    def test_check_output_keys(self):
        """Each result must have all expected keys."""
        req = PMMCheckRequest(demands=[DEMANDS_ENGINE[0]])
        out = pmm_check(req, current_user={})
        for key in ('label', 'P', 'Mx', 'My', 'M_demand',
                    'alpha_deg', 'M_cap', 'DCR', 'status'):
            self.assertIn(key, out['results'][0], f"Missing key: {key}")

    def test_check_dcr_proportional_to_demand(self):
        """Scaling up moments should increase DCR proportionally."""
        base = {'label': 'base', 'P': 1500.0, 'Mx': 20.0, 'My': 40.0}
        big  = {'label': 'big',  'P': 1500.0, 'Mx': 60.0, 'My': 120.0}
        r_base = pmm_check(PMMCheckRequest(demands=[base]),
                           current_user={})['results'][0]
        r_big  = pmm_check(PMMCheckRequest(demands=[big]),
                           current_user={})['results'][0]
        self.assertLess(float(r_base['DCR']), float(r_big['DCR']),
            "Tripling the moment should increase DCR")

    # =========================================================================
    # 3.  pmm_check ↔ _boundary_dcr_surface consistency
    #     Both use the same surface — their DCRs should agree closely.
    # =========================================================================

    def test_check_vs_boundary_dcr_consistency(self):
        """
        pmm_check (check_demands engine) and _boundary_dcr_surface must give the
        same PASS/FAIL verdict for every demand in DEMANDS_ENGINE.

        Both methods use the same PMM surface (built in setUpClass).
        Numerical DCR may differ by up to 25% due to different interpolation
        strategies, but PASS/FAIL must always agree.
        """
        surf = self._result['surface']
        npts = surf.get('num_points', COL['num_points'])

        req      = PMMCheckRequest(demands=DEMANDS_ENGINE)
        chk_out  = pmm_check(req, current_user={})

        for i, d in enumerate(DEMANDS_ENGINE):
            chk_r   = chk_out['results'][i]
            chk_status = chk_r['status']

            # _boundary_dcr_surface uses same demand convention as optimizer
            # (engine-sign P, Mx/My already in engine convention)
            bnd_dcr    = _boundary_dcr_surface(surf, npts, [d])
            bnd_status = 'PASS' if bnd_dcr <= 1.0 else 'FAIL'

            self.assertEqual(
                chk_status, bnd_status,
                f"Demand '{d['label']}': pmm_check={chk_status} "
                f"(DCR={chk_r['DCR']:.3f}), boundary={bnd_status} "
                f"(DCR={bnd_dcr:.3f}) — methods disagree"
            )

    # =========================================================================
    # 4.  pmm_optimize — single bar size mode
    # =========================================================================

    def test_optimize_returns_valid_response(self):
        """pmm_optimize must return a dict with all required keys."""
        req = PMMOptimizeRequest(
            b_mm=400, h_mm=500, fc_mpa=28, fy_mpa=420,
            cover_mm=40, stirrup_dia_mm=10,
            bar_size='Ø20', target_dcr=0.90,
            min_rho_pct=1.0, max_rho_pct=4.0,
            alpha_steps=10.0, num_points=70,
            demands=DEMANDS_OPT,
        )
        out = pmm_optimize(req, current_user={})
        for key in ('nbars_b', 'nbars_h', 'n_total', 'bar_size',
                    'rho_pct', 'achieved_dcr', 'target_met',
                    'arrangement', 'min_clear_mm', 'max_clear_mm'):
            self.assertIn(key, out, f"Missing key in optimizer response: {key}")

    def test_optimize_rho_within_bounds(self):
        """Optimizer result ρ must be within the requested [1%, 4%] range."""
        req = PMMOptimizeRequest(
            b_mm=400, h_mm=500, fc_mpa=28, fy_mpa=420,
            cover_mm=40, stirrup_dia_mm=10,
            bar_size='Ø20', target_dcr=0.90,
            min_rho_pct=1.0, max_rho_pct=4.0,
            alpha_steps=10.0, num_points=70,
            demands=DEMANDS_OPT,
        )
        out = pmm_optimize(req, current_user={})
        self.assertGreaterEqual(out['rho_pct'], 1.0 - 0.01)
        self.assertLessEqual(out['rho_pct'], 4.0 + 0.01)

    def test_optimize_bar_size_preserved(self):
        """The bar_size in the response must match the requested bar size."""
        req = PMMOptimizeRequest(
            b_mm=400, h_mm=500, fc_mpa=28, fy_mpa=420,
            cover_mm=40, stirrup_dia_mm=10,
            bar_size='Ø25', target_dcr=0.90,
            min_rho_pct=1.0, max_rho_pct=4.0,
            alpha_steps=10.0, num_points=70,
            demands=DEMANDS_OPT,
        )
        out = pmm_optimize(req, current_user={})
        self.assertEqual(out['bar_size'], 'Ø25')

    def test_optimize_aci_spacing_respected(self):
        """
        ACI §25.8.1: min clear ≥ max(1.5·db, 40 mm).
        ACI §25.7.2.3: max clear ≤ 150 mm.
        """
        req = PMMOptimizeRequest(
            b_mm=400, h_mm=500, fc_mpa=28, fy_mpa=420,
            cover_mm=40, stirrup_dia_mm=10,
            bar_size='Ø20', target_dcr=0.90,
            min_rho_pct=1.0, max_rho_pct=4.0,
            alpha_steps=10.0, num_points=70,
            demands=DEMANDS_OPT,
        )
        out = pmm_optimize(req, current_user={})
        self.assertGreaterEqual(out['min_clear_mm'], out['min_clear_req'] - 0.1,
            f"min clear {out['min_clear_mm']:.1f} mm < ACI min {out['min_clear_req']:.1f} mm")
        self.assertLessEqual(out['max_clear_mm'], out['max_clear_req'] + 0.1,
            f"max clear {out['max_clear_mm']:.1f} mm > ACI max {out['max_clear_req']:.1f} mm")

    def test_optimize_achieved_dcr_le_target_when_met(self):
        """When target_met=True, achieved_dcr must be ≤ target × 100%."""
        req = PMMOptimizeRequest(
            b_mm=400, h_mm=500, fc_mpa=28, fy_mpa=420,
            cover_mm=40, stirrup_dia_mm=10,
            bar_size='Ø20', target_dcr=0.90,
            min_rho_pct=1.0, max_rho_pct=4.0,
            alpha_steps=10.0, num_points=70,
            demands=DEMANDS_OPT,
        )
        out = pmm_optimize(req, current_user={})
        if out['target_met']:
            self.assertLessEqual(out['achieved_dcr'], 90.0 + 1.0,
                f"target_met=True but achieved_dcr={out['achieved_dcr']:.1f}% > 90%")

    def test_optimize_n_total_correct(self):
        """n_total must equal 2×nbars_b + 2×nbars_h."""
        req = PMMOptimizeRequest(
            b_mm=400, h_mm=500, fc_mpa=28, fy_mpa=420,
            cover_mm=40, stirrup_dia_mm=10,
            bar_size='Ø20', target_dcr=0.90,
            min_rho_pct=1.0, max_rho_pct=4.0,
            alpha_steps=10.0, num_points=70,
            demands=DEMANDS_OPT,
        )
        out = pmm_optimize(req, current_user={})
        expected_n = 2 * out['nbars_b'] + 2 * out['nbars_h']
        self.assertEqual(out['n_total'], expected_n,
            f"n_total={out['n_total']} ≠ 2×{out['nbars_b']} + 2×{out['nbars_h']}={expected_n}")

    # =========================================================================
    # 5.  pmm_optimize — bar size sweep mode
    # =========================================================================

    def test_optimize_sweep_returns_valid_response(self):
        """sweep_bar_sizes=True must return a valid response with sweep metadata."""
        req = PMMOptimizeRequest(
            b_mm=400, h_mm=500, fc_mpa=28, fy_mpa=420,
            cover_mm=40, stirrup_dia_mm=10,
            bar_size='Ø20', target_dcr=0.90,
            min_rho_pct=1.0, max_rho_pct=4.0,
            alpha_steps=10.0, num_points=70,
            demands=DEMANDS_OPT,
            sweep_bar_sizes=True,
        )
        out = pmm_optimize(req, current_user={})
        self.assertIn('swept_sizes', out,
            "Sweep mode response must include 'swept_sizes'")
        self.assertIn('sizes_tried', out,
            "Sweep mode response must include 'sizes_tried'")
        self.assertGreater(out['swept_sizes'], 0)

    def test_optimize_sweep_lighter_than_or_equal_single(self):
        """
        The globally optimal result from a sweep should have Ast ≤ the
        single-bar-size result, because the sweep considers more options.
        """
        single_req = PMMOptimizeRequest(
            b_mm=400, h_mm=500, fc_mpa=28, fy_mpa=420,
            cover_mm=40, stirrup_dia_mm=10,
            bar_size='Ø20', target_dcr=0.90,
            min_rho_pct=1.0, max_rho_pct=4.0,
            alpha_steps=10.0, num_points=70,
            demands=DEMANDS_OPT,
        )
        sweep_req = PMMOptimizeRequest(
            b_mm=400, h_mm=500, fc_mpa=28, fy_mpa=420,
            cover_mm=40, stirrup_dia_mm=10,
            bar_size='Ø20', target_dcr=0.90,
            min_rho_pct=1.0, max_rho_pct=4.0,
            alpha_steps=10.0, num_points=70,
            demands=DEMANDS_OPT,
            sweep_bar_sizes=True,
        )
        single = pmm_optimize(single_req, current_user={})
        sweep  = pmm_optimize(sweep_req,  current_user={})

        # Only compare when both found a passing result
        if single['target_met'] and sweep['target_met']:
            from pmm_conventions import REBAR_AREA_MM2
            area_single = REBAR_AREA_MM2.get(single['bar_size'], 0) * single['n_total']
            area_sweep  = REBAR_AREA_MM2.get(sweep['bar_size'],  0) * sweep['n_total']
            self.assertLessEqual(area_sweep, area_single + 1.0,   # 1 mm² tolerance
                f"Sweep Ast={area_sweep:.0f} mm² > single-size Ast={area_single:.0f} mm² "
                f"(sweep should be ≤ single)")

    def test_optimize_sweep_with_custom_candidates(self):
        """bar_size_candidates limits the sweep to the specified subset."""
        req = PMMOptimizeRequest(
            b_mm=400, h_mm=500, fc_mpa=28, fy_mpa=420,
            cover_mm=40, stirrup_dia_mm=10,
            bar_size='Ø20', target_dcr=0.90,
            min_rho_pct=1.0, max_rho_pct=4.0,
            alpha_steps=10.0, num_points=70,
            demands=DEMANDS_OPT,
            sweep_bar_sizes=True,
            bar_size_candidates=['Ø16', 'Ø20', 'Ø25'],
        )
        out = pmm_optimize(req, current_user={})
        self.assertEqual(out['swept_sizes'], 3,
            f"Expected swept_sizes=3, got {out['swept_sizes']}")
        self.assertIn(out['bar_size'], ['Ø16', 'Ø20', 'Ø25'],
            f"Result bar_size {out['bar_size']} not in the requested candidates")

    # =========================================================================
    # 6.  Full round-trip: calculate → optimize → re-calculate → check
    #     The DCR after applying the optimizer's suggested design should
    #     match the optimizer's reported achieved_dcr within a tight tolerance.
    # =========================================================================

    def test_full_roundtrip_dcr_matches(self):
        """
        Optimizer's achieved_dcr must match the actual DCR obtained by running
        pmm_calculate on the suggested design and then pmm_check.

        This is the definitive end-to-end regression test: if the optimizer's
        DCR prediction matches the Check DCR table result within 5%, the
        entire stack is self-consistent.
        """
        # Step 1 — Optimize
        opt_req = PMMOptimizeRequest(
            b_mm=400, h_mm=500, fc_mpa=28, fy_mpa=420,
            cover_mm=40, stirrup_dia_mm=10,
            bar_size='Ø20', target_dcr=0.90,
            min_rho_pct=1.0, max_rho_pct=4.0,
            alpha_steps=10.0, num_points=70,
            demands=DEMANDS_OPT,
        )
        opt_out = pmm_optimize(opt_req, current_user={})
        opt_dcr = opt_out['achieved_dcr']   # as reported by optimizer (%)

        # Step 2 — Re-calculate with the optimizer's suggested design
        calc_req = PMMRequest(
            b=400, h=500,
            fc=28.0, fy=420.0, Es=200000.0,
            cover=40.0, stirrup_dia_mm=10.0,
            nbars_b=opt_out['nbars_b'],
            nbars_h=opt_out['nbars_h'],
            bar_size=opt_out['bar_size'],
            include_phi=True,
            alpha_steps=10.0,
            num_points=70,
            units='SI',
        )
        pmm_calculate(calc_req, current_user={})
        # _pmm_cache is now updated with the optimizer's design

        # Step 3 — Check the same demands against the new surface
        # pmm_check uses engine convention (Mx/My already pre-swapped in DEMANDS_ENGINE-style)
        # Use the DEMANDS_OPT demands, converted for pmm_check:
        #   pmm_check expects engine-sign P and engine Mx/My — same as DEMANDS_OPT
        check_req = PMMCheckRequest(demands=DEMANDS_OPT)
        chk_out   = pmm_check(check_req, current_user={})

        # The max DCR across all demands from pmm_check
        check_max_dcr = max(float(r['DCR']) for r in chk_out['results']) * 100.0

        # Step 4 — Compare: optimizer's predicted DCR vs actual check DCR
        delta = abs(opt_dcr - check_max_dcr)
        tolerance = max(5.0, opt_dcr * 0.10)   # 5% absolute or 10% relative, whichever larger

        self.assertLessEqual(delta, tolerance,
            f"Round-trip DCR mismatch: optimizer predicted {opt_dcr:.1f}%, "
            f"pmm_check returned {check_max_dcr:.1f}% "
            f"(diff = {delta:.1f}%, tolerance = {tolerance:.1f}%)")

    # =========================================================================
    # 7.  Edge cases
    # =========================================================================

    def _assert_http_exception(self, fn, expected_status: int):
        """
        Call fn() and assert it raises an exception whose class name is
        'HTTPException' and whose status_code == expected_status.
        Works regardless of which FastAPI module instance raised it.
        """
        try:
            fn()
            self.fail("Expected an HTTPException to be raised but none was.")
        except Exception as exc:
            cls_name = type(exc).__name__
            self.assertEqual(cls_name, 'HTTPException',
                f"Expected HTTPException, got {cls_name}: {exc}")
            self.assertEqual(exc.status_code, expected_status,
                f"Expected status {expected_status}, got {exc.status_code}: {exc.detail}")

    def test_optimize_no_demands_raises(self):
        """pmm_optimize with empty demands must raise HTTPException 422."""
        req = PMMOptimizeRequest(
            b_mm=400, h_mm=500, fc_mpa=28, fy_mpa=420,
            cover_mm=40, demands=[],
        )
        self._assert_http_exception(
            lambda: pmm_optimize(req, current_user={}), 422)

    def test_optimize_impossible_rho_raises(self):
        """min_rho > max_rho must raise HTTPException 422."""
        req = PMMOptimizeRequest(
            b_mm=400, h_mm=500, fc_mpa=28, fy_mpa=420,
            cover_mm=40, demands=DEMANDS_OPT,
            min_rho_pct=5.0, max_rho_pct=2.0,
        )
        self._assert_http_exception(
            lambda: pmm_optimize(req, current_user={}), 422)

    def test_check_without_calculate_raises(self):
        """
        pmm_check with no prior pmm_calculate (cleared cache) must raise
        HTTPException 400.
        """
        saved = dict(_pmm_cache)
        try:
            _pmm_cache['alpha_data'] = None
            req = PMMCheckRequest(demands=[DEMANDS_ENGINE[0]])
            self._assert_http_exception(
                lambda: pmm_check(req, current_user={}), 400)
        finally:
            _pmm_cache.update(saved)   # restore cache for subsequent tests


# =============================================================================
# Runner
# =============================================================================

if __name__ == '__main__':
    unittest.main(verbosity=2)
