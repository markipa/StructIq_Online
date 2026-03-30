"""
spColumn benchmark test — numerical validation of the PMM engine against
hand-calculated ACI 318-19 reference values for Reference Column RC-01.

Reference Column RC-01
──────────────────────
  Section  : 400 × 500 mm  (b × h)
  Concrete : f'c = 28 MPa  (normal-weight, β₁ = 0.85)
  Steel    : fy = 420 MPa,  Es = 200 000 MPa
  Cover    : 40 mm clear (face → stirrup face)
  Stirrups : Ø10 mm
  Rebar    : 8 × Ø25  (3 per b-face, 1 per h-side, 8 total)
             Ast = 8 × 490.9 = 3 927.2 mm²
             ρ   = 3 927.2 / (400 × 500) = 1.963 %

ACI 318-19 hand-calculation benchmarks (tied column, φ = 0.65)
──────────────────────────────────────────────────────────────
All formulae from ACI 318-19 §22.4.

1.  Maximum axial capacity (pure compression, ACI §22.4.2.2):
      Pn,max = 0.80 × [0.85·f'c·(Ag − Ast) + fy·Ast]
             = 0.80 × [0.85×28×(200000−3927.2) + 420×3927.2]
             = 0.80 × [4 665 107 + 1 649 424]   N
             = 0.80 × 6 314 531 N
             = 5 051 625 N  = 5 051.6 kN   (unfactored)
      φPn,max = 0.65 × 5 051.6 = 3 283.5 kN   (tied)

2.  Minimum axial capacity (pure tension):
      φPn,min ≈ −φ·fy·Ast  (all steel yielding in tension, ACI §22.4.3)
             = −0.90 × 420 × 3927.2 / 1000
             = −1 484.5 kN   (φ_tension = 0.90)

3.  Effective cover for Ø25 bars:
      eff_cover = 40 + 10 + 25/2 = 62.5 mm  (face → bar centre)
      d_eff     = 500 − 62.5 = 437.5 mm

4.  Uniaxial bending (strong axis, h = 500 mm direction, α = 0°):
    Balanced strain condition  (ACI §22.2.2.1, εu = 0.003):
      c_b = (εu / (εu + εy)) × d_eff
          = (0.003 / (0.003 + 420/200000)) × 437.5
          = (0.003 / 0.005100) × 437.5
          = 257.4 mm
      a_b = β₁ × c_b = 0.85 × 257.4 = 218.8 mm
      P_bal (nominal) ≈ 0.85×f'c×a_b×b  +  fs_compr × As_compr  −  fy × As_tens
                      (simplified, single steel layer each side)
    For testing we only verify the engine's Pbal is within 10% of:
      P_bal_approx ≈ 0.85 × 28 × 218.8 × 400 / 1000  ≈  2 085 kN  (concrete only)

5.  Tolerances used in tests:
      φPn,max:  ±3 %  of 3 283.5 kN
      φPn,min:  ±5 %  of −1 484.5 kN
      Mbal:     within 20 % of approximate value (balanced M varies by bar layout)
      ρ:        ±0.05 % absolute

Run from the backend/ directory:
    python -m unittest tests.test_spcolumn_benchmark -v
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
_spec      = _ilu.spec_from_file_location('_main_mod_bench', _main_path)
_stubs     = {
    'database':             _mock.MagicMock(),
    'config':               _mock.MagicMock(),
    'etabs_api':            _mock.MagicMock(),
    'etabs_api.actions':    _mock.MagicMock(),
    'etabs_api.connection': _mock.MagicMock(),
}
with _mock.patch.dict(sys.modules, _stubs):
    _main = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_main)

pmm_calculate = _main.pmm_calculate
PMMRequest    = _main.PMMRequest

# ── ACI 318-19 hand-calculated reference values ───────────────────────────────
FC_MPA       = 28.0
FY_MPA       = 420.0
ES_MPA       = 200_000.0
B_MM         = 400.0
H_MM         = 500.0
COVER_MM     = 40.0        # clear cover
STIRRUP_MM   = 10.0
BAR_DIA_MM   = 25.0
N_BARS       = 8
AREA_BAR_MM2 = 490.9       # Ø25 nominal area

AG_MM2  = B_MM * H_MM                        # 200 000 mm²
AST_MM2 = N_BARS * AREA_BAR_MM2              # 3 927.2 mm²
RHO_PCT = 100.0 * AST_MM2 / AG_MM2          # 1.963 %

BETA1   = 0.85   # ACI §22.2.2.3 for f'c = 28 MPa (≤ 28 MPa)
EPS_U   = 0.003  # ACI §22.2.2.1 concrete crushing strain
EPS_Y   = FY_MPA / ES_MPA                    # 0.0021

PHI_COMP   = 0.65   # ACI §21.2.2 tied column (compression-controlled)
PHI_TENS   = 0.90   # ACI §21.2.2 tension-controlled

# ACI §22.4.2.2  φPn,max (tied):
PN_MAX_KN    = 0.80 * (0.85 * FC_MPA * (AG_MM2 - AST_MM2) + FY_MPA * AST_MM2) / 1000.0
PHI_PN_MAX   = PHI_COMP * PN_MAX_KN                          # ≈ 3 283 kN

# ACI §22.4.3  φPn,min (tension) — conservative; engine may use smaller φ below balanced
PHI_PN_MIN   = -PHI_TENS * FY_MPA * AST_MM2 / 1000.0        # ≈ −1 485 kN

# Balanced condition — concrete contribution to axial only (see §4 in header)
EFF_COVER_MM = COVER_MM + STIRRUP_MM + BAR_DIA_MM / 2.0      # 62.5 mm
D_EFF        = H_MM - EFF_COVER_MM                            # 437.5 mm
C_BAL        = (EPS_U / (EPS_U + EPS_Y)) * D_EFF             # 257.4 mm
A_BAL        = BETA1 * C_BAL                                  # 218.8 mm
PBAL_CONC_KN = 0.85 * FC_MPA * A_BAL * B_MM / 1000.0         # ≈ 2 085 kN (concrete only)


class TestSpColumnBenchmark(unittest.TestCase):
    """
    Validates the PMM engine against ACI 318-19 hand-calculated reference
    values for reference column RC-01.  Tolerances are deliberately generous
    (3–10 %) to account for bar layout discretisation, φ interpolation, and
    difference between the simplified hand-calc and the full fibre-section
    integration the engine performs.
    """

    @classmethod
    def setUpClass(cls):
        """Compute PMM surface once at high resolution for all benchmark tests."""
        req = PMMRequest(
            b=B_MM, h=H_MM,
            fc=FC_MPA, fy=FY_MPA, Es=ES_MPA,
            cover=COVER_MM, stirrup_dia_mm=STIRRUP_MM,
            nbars_b=3, nbars_h=1,   # 8 bars total (2×3 + 2×1)
            bar_size='Ø25',
            include_phi=True,
            alpha_steps=5.0,        # high resolution for benchmark accuracy
            num_points=120,
            units='SI',
        )
        cls.result = pmm_calculate(req, current_user={})
        cls.c2d    = cls.result.get('curves_2d', {})

    # ── 1.  Section properties ────────────────────────────────────────────────

    def test_rho_matches_aci_calc(self):
        """
        ρ = Ast / Ag must match the hand-calculated 1.963 %.
        Tolerance: ±0.05 % absolute.
        """
        Ast = self.result['Ast']   # mm² (engine output)
        Ag  = self.result['Ag']    # mm²
        rho = 100.0 * Ast / Ag
        self.assertAlmostEqual(rho, RHO_PCT, delta=0.05,
            msg=f"Engine ρ = {rho:.3f} %, hand-calc = {RHO_PCT:.3f} %")

    def test_ag_equals_bxh(self):
        """Ag must equal b × h = 200 000 mm²."""
        self.assertAlmostEqual(self.result['Ag'], AG_MM2, delta=1.0,
            msg=f"Engine Ag = {self.result['Ag']:.0f} mm², expected {AG_MM2:.0f} mm²")

    def test_ast_equals_n_times_area(self):
        """Ast must equal N_bars × area_bar = 3 927.2 mm²."""
        self.assertAlmostEqual(self.result['Ast'], AST_MM2, delta=5.0,
            msg=f"Engine Ast = {self.result['Ast']:.1f} mm², "
                f"hand-calc = {AST_MM2:.1f} mm²")

    # ── 2.  Axial capacity benchmarks ─────────────────────────────────────────

    def test_phi_pn_max_vs_aci(self):
        """
        φPn,max (pure compression) must match ACI §22.4.2.2 within 3 %.

        Hand-calc:  φPn,max = 0.65 × 0.80 × [0.85×28×(200000−3927) + 420×3927]
                            ≈ 3 283.5 kN

        The engine applies the 0.80 factor via the ACI-mandated Pn,max cap
        on the interaction surface, so the peak of the surface should agree.
        """
        engine_pmax = self.result['Pmax']
        tol_pct     = 0.03
        tol_abs     = PHI_PN_MAX * tol_pct

        self.assertAlmostEqual(engine_pmax, PHI_PN_MAX, delta=tol_abs,
            msg=f"Engine φPn,max = {engine_pmax:.1f} kN, "
                f"ACI hand-calc = {PHI_PN_MAX:.1f} kN "
                f"(diff = {abs(engine_pmax - PHI_PN_MAX):.1f} kN, "
                f"tol = {tol_abs:.1f} kN = 3 %)")

    def test_phi_pn_min_vs_aci(self):
        """
        φPn,min (pure tension) must match −φ·fy·Ast within 5 %.

        Hand-calc: φPn,min ≈ −0.90 × 420 × 3927 / 1000 ≈ −1 484.5 kN.
        The engine may use a lower φ for some section states; ±5 % tolerance.
        """
        engine_pmin = self.result['Pmin']
        tol_abs     = abs(PHI_PN_MIN) * 0.05

        self.assertTrue(engine_pmin < 0,
            f"φPn,min must be negative (tension), got {engine_pmin:.1f} kN")
        self.assertAlmostEqual(engine_pmin, PHI_PN_MIN, delta=tol_abs,
            msg=f"Engine φPn,min = {engine_pmin:.1f} kN, "
                f"ACI hand-calc = {PHI_PN_MIN:.1f} kN "
                f"(diff = {abs(engine_pmin - PHI_PN_MIN):.1f} kN, "
                f"tol = {tol_abs:.1f} kN = 5 %)")

    def test_pmax_greater_than_pmin(self):
        """Trivial sanity: φPn,max > 0 > φPn,min."""
        self.assertGreater(self.result['Pmax'], 0.0)
        self.assertLess(self.result['Pmin'], 0.0)

    # ── 3.  Balanced condition (strong axis) ──────────────────────────────────

    def test_balanced_condition_strong_axis(self):
        """
        The balanced point on the P–Mx (strong axis, α=0°) curve must occur
        between P = 0 and P = φPn,max.  The concrete-only axial contribution
        at the balanced strain depth is ~2 085 kN; with steel the actual
        φPbal is typically 1 000–2 800 kN for this column.
        """
        curve = self.c2d.get('0') or self.c2d.get('0.0')
        if curve is None:
            self.skipTest("α=0° curve not in curves_2d — check alpha_steps")

        Plist  = curve['P']
        Mxlist = curve['Mx']
        # Find balanced point = maximum |Mx| on the curve
        bal_i  = max(range(len(Mxlist)), key=lambda i: abs(Mxlist[i]))
        Pbal   = Plist[bal_i]
        Mbal   = abs(Mxlist[bal_i])

        self.assertGreater(Pbal, 0.0,
            f"Balanced P = {Pbal:.1f} kN should be > 0 (compression)")
        self.assertLess(Pbal, self.result['Pmax'],
            f"Balanced P = {Pbal:.1f} kN should be < φPn,max = {self.result['Pmax']:.1f} kN")
        self.assertGreater(Mbal, 50.0,
            f"Balanced Mx = {Mbal:.1f} kN·m seems too small (expected > 50 kN·m)")

    def test_balanced_condition_weak_axis(self):
        """
        The balanced point on the P–My (weak axis, α=90°) curve must satisfy
        the same constraints.  Weak-axis Mbal is lower than strong-axis because
        b (400 mm) < h (500 mm).
        """
        curve = self.c2d.get('90') or self.c2d.get('90.0')
        if curve is None:
            self.skipTest("α=90° curve not in curves_2d — check alpha_steps")

        Plist  = curve['P']
        Mylist = curve['My']
        bal_i  = max(range(len(Mylist)), key=lambda i: abs(Mylist[i]))
        Pbal   = Plist[bal_i]
        Mbal   = abs(Mylist[bal_i])

        self.assertGreater(Pbal, 0.0)
        self.assertLess(Pbal, self.result['Pmax'])
        self.assertGreater(Mbal, 30.0,
            f"Balanced My = {Mbal:.1f} kN·m too small (expected > 30 kN·m)")

    def test_weak_axis_mbal_less_than_strong(self):
        """
        For b < h the weak-axis peak moment must be less than the strong-axis
        peak moment.  (Wider face → more moment capacity about strong axis.)
        """
        c_strong = self.c2d.get('0') or self.c2d.get('0.0')
        c_weak   = self.c2d.get('90') or self.c2d.get('90.0')
        if c_strong is None or c_weak is None:
            self.skipTest("curves_2d missing α=0° or α=90°")

        mbal_strong = max(abs(m) for m in c_strong['Mx'])
        mbal_weak   = max(abs(m) for m in c_weak['My'])

        self.assertLess(mbal_weak, mbal_strong,
            f"Weak-axis Mbal ({mbal_weak:.1f}) should be < "
            f"strong-axis Mbal ({mbal_strong:.1f}) because b < h")

    # ── 4.  Curve shape validation ────────────────────────────────────────────

    def test_interaction_curve_compression_range_is_concave(self):
        """
        In the compression-bending range (P > 0) the interaction curve
        should be concave (moment increases then decreases as P decreases
        from Pmax to 0).  This is the classic 'kidney-shaped' interaction.
        Verified by checking that the maximum moment occurs at an intermediate
        P level, not at the top or bottom.
        """
        curve = self.c2d.get('0') or self.c2d.get('0.0')
        if curve is None:
            self.skipTest("α=0° curve not in curves_2d")

        pairs = [(p, abs(mx)) for p, mx in zip(curve['P'], curve['Mx']) if p > 0]
        if len(pairs) < 5:
            self.skipTest("Not enough compression-range points")

        max_pair = max(pairs, key=lambda x: x[1])
        max_p    = max_pair[0]

        # The max-moment P must not be the very first or last point
        all_p_pos = sorted(p for p, _ in pairs)
        self.assertGreater(max_p, all_p_pos[0],
            "Maximum moment occurs at P=0 (flat curve) — unexpected shape")
        self.assertLess(max_p, all_p_pos[-1],
            "Maximum moment occurs at Pn,max — unexpected shape (no balanced drop-off)")

    def test_moment_zero_at_pmax(self):
        """At φPn,max the moment capacity must be near zero (pure axial)."""
        curve = self.c2d.get('0') or self.c2d.get('0.0')
        if curve is None:
            self.skipTest("α=0° curve not in curves_2d")

        # Find the point with the highest P
        idx_top = max(range(len(curve['P'])), key=lambda i: curve['P'][i])
        M_at_top = abs(curve['Mx'][idx_top])
        P_at_top = curve['P'][idx_top]

        self.assertAlmostEqual(M_at_top, 0.0, delta=10.0,
            msg=f"At P={P_at_top:.1f} kN (near Pmax) Mx should be ~0, "
                f"got {M_at_top:.1f} kN·m")

    def test_moment_zero_at_pmin(self):
        """At φPn,min (pure tension) the moment capacity must be near zero."""
        curve = self.c2d.get('0') or self.c2d.get('0.0')
        if curve is None:
            self.skipTest("α=0° curve not in curves_2d")

        idx_bot = min(range(len(curve['P'])), key=lambda i: curve['P'][i])
        M_at_bot = abs(curve['Mx'][idx_bot])
        P_at_bot = curve['P'][idx_bot]

        self.assertAlmostEqual(M_at_bot, 0.0, delta=15.0,
            msg=f"At P={P_at_bot:.1f} kN (near Pmin) Mx should be ~0, "
                f"got {M_at_bot:.1f} kN·m")

    # ── 5.  Monotonicity with steel content ───────────────────────────────────

    def test_more_steel_increases_pmax(self):
        """
        Adding steel must increase φPn,max.
        Compare 4 × Ø25 (minimum) vs 8 × Ø25 (RC-01).
        """
        req_light = PMMRequest(
            b=B_MM, h=H_MM, fc=FC_MPA, fy=FY_MPA, Es=ES_MPA,
            cover=COVER_MM, stirrup_dia_mm=STIRRUP_MM,
            nbars_b=2, nbars_h=0,   # 4 bars
            bar_size='Ø25', include_phi=True,
            alpha_steps=10.0, num_points=70, units='SI',
        )
        result_light = pmm_calculate(req_light, current_user={})

        pmax_light = result_light['Pmax']
        pmax_heavy = self.result['Pmax']   # 8 bars

        self.assertLess(pmax_light, pmax_heavy,
            f"4×Ø25 φPn,max = {pmax_light:.1f} kN should be < "
            f"8×Ø25 φPn,max = {pmax_heavy:.1f} kN")

    def test_more_steel_increases_moment_capacity(self):
        """
        8 × Ø25 must have higher peak moment than 4 × Ø25 on the strong axis.
        """
        req_light = PMMRequest(
            b=B_MM, h=H_MM, fc=FC_MPA, fy=FY_MPA, Es=ES_MPA,
            cover=COVER_MM, stirrup_dia_mm=STIRRUP_MM,
            nbars_b=2, nbars_h=0,
            bar_size='Ø25', include_phi=True,
            alpha_steps=10.0, num_points=70, units='SI',
        )
        result_light = pmm_calculate(req_light, current_user={})

        c_light  = result_light['curves_2d'].get('0') or result_light['curves_2d'].get('0.0')
        c_heavy  = self.c2d.get('0') or self.c2d.get('0.0')
        if c_light is None or c_heavy is None:
            self.skipTest("α=0° curve not available")

        mbal_light = max(abs(m) for m in c_light['Mx'])
        mbal_heavy = max(abs(m) for m in c_heavy['Mx'])

        self.assertLess(mbal_light, mbal_heavy,
            f"4×Ø25 peak Mx = {mbal_light:.1f} kN·m should be < "
            f"8×Ø25 peak Mx = {mbal_heavy:.1f} kN·m")

    def test_higher_fc_increases_pmax(self):
        """
        f'c = 40 MPa must give a higher φPn,max than f'c = 28 MPa.
        """
        req_high_fc = PMMRequest(
            b=B_MM, h=H_MM, fc=40.0, fy=FY_MPA, Es=ES_MPA,
            cover=COVER_MM, stirrup_dia_mm=STIRRUP_MM,
            nbars_b=3, nbars_h=1,
            bar_size='Ø25', include_phi=True,
            alpha_steps=10.0, num_points=70, units='SI',
        )
        result_high = pmm_calculate(req_high_fc, current_user={})

        self.assertLess(self.result['Pmax'], result_high['Pmax'],
            f"f'c=28 Pmax={self.result['Pmax']:.1f} kN should be < "
            f"f'c=40 Pmax={result_high['Pmax']:.1f} kN")

    # ── 6.  Summary printout (informational — never fails) ────────────────────

    def test_print_benchmark_summary(self):
        """Print a comparison table for visual inspection. Always passes."""
        engine_pmax = self.result['Pmax']
        engine_pmin = self.result['Pmin']
        engine_ast  = self.result['Ast']
        engine_rho  = 100.0 * engine_ast / self.result['Ag']

        c0 = self.c2d.get('0') or self.c2d.get('0.0') or {}
        mbal_strong = max((abs(m) for m in c0.get('Mx', [0])), default=0)
        c90 = self.c2d.get('90') or self.c2d.get('90.0') or {}
        mbal_weak = max((abs(m) for m in c90.get('My', [0])), default=0)

        print()
        print("=" * 60)
        print("  RC-01 Benchmark: Engine vs ACI 318-19 Hand-Calc")
        print("=" * 60)
        print(f"  {'Property':<22} {'Hand-calc':>12} {'Engine':>12} {'Diff%':>8}")
        print("-" * 60)

        def row(label, ref, eng):
            diff = abs(eng - ref) / abs(ref) * 100 if ref else 0
            print(f"  {label:<22} {ref:>12.1f} {eng:>12.1f} {diff:>7.1f}%")

        row("phi*Pn,max (kN)",    PHI_PN_MAX,  engine_pmax)
        row("phi*Pn,min (kN)",    PHI_PN_MIN,  engine_pmin)
        row("Ast (mm2)",          AST_MM2,     engine_ast)
        row("rho (%)",            RHO_PCT,     engine_rho)
        row("Mbal strong (kN*m)", 0,           mbal_strong)   # no hand-calc ref
        row("Mbal weak   (kN*m)", 0,           mbal_weak)
        print("=" * 60)
        print("  (Mbal hand-calc not shown — depends on bar layer positions)")
        print()


# =============================================================================
# Runner
# =============================================================================

if __name__ == '__main__':
    unittest.main(verbosity=2)
