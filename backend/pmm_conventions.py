"""
PMM Column App — Axis Convention Reference
==========================================

THREE naming spaces coexist in this codebase.  This file is the single source
of truth.  Every place that swaps Mx/My should cite it.

ETABS OUTPUT  (SapModel.FrameObj.GetInternalForces / M22/M33):
  M22  = bending about the local 2-axis  (weak-axis bending for rect. columns)
  M33  = bending about the local 3-axis  (strong-axis bending)

ENGINE CONVENTION  (pmm_engine.py — PMMSection / compute_pmm):
  engine.Mx = bending about the b-face plane   = ETABS M33 (strong-axis)
  engine.My = bending about the h-face plane   = ETABS M22 (weak-axis)
  engine.P  = POSITIVE for compression

TABLE / DISPLAY CONVENTION  (frontend load table, batch results,
                              pmmCheckLoads, pmmUpdateDCRFromBoundary):
  Table "Mx" column = M33  (strong-axis)   ← same as engine.Mx
  Table "My" column = M22  (weak-axis)     ← same as engine.My
  Table "P"  column = NEGATIVE for compression  (ETABS sign)

SWAP RULE — engine ↔ frontend pmmOptimize / pmmCheckLoads
──────────────────────────────────────────────────────────
  engine_demand.Mx = table.My          # swap!
  engine_demand.My = table.Mx          # swap!
  engine_demand.P  = -(table.P)        # sign flip

This swap appears at FOUR canonical sites in the code.
Any future change to the convention must update all four:

  Site 1 — main.py  _boundary_dcr_surface()
       dx_r = d['My']   # engine My (M33) → table Mx → ray x
       dy_r = d['Mx']   # engine Mx (M22) → table My → ray y

  Site 2 — main.py  pmm_optimize()  demand pre-processing
       'Mx': float(d.get('Mx', 0)), 'My': float(d.get('My', 0))
       (demands arrive pre-swapped from the frontend pmmOptimize call)

  Site 3 — main.py  pmm_etabs_batch_check()  demand assembly
       "Mx": r["M3_kNm"],   # M33 → engine Mx slot
       "My": r["M2_kNm"],   # M22 → engine My slot

  Site 4 — frontend app.js  pmmCheckLoads / pmmOptimize
       demands: loads.map(l => ({ P: -(+l.P),
                                   Mx: +(l.My||0),
                                   My: +(l.Mx||0) }))
"""

from __future__ import annotations

# ── Canonical rebar diameters (mm) ───────────────────────────────────────────
REBAR_DIAMETERS_MM: list[int] = [8, 10, 12, 16, 20, 25, 28, 32, 36, 40]

# ── SI rebar cross-sectional areas (mm²) ─────────────────────────────────────
# Master definition — main.py REBAR_TABLE_SI mirrors this.
REBAR_AREA_MM2: dict[str, float] = {
    "Ø8":  50.3,   "Ø10":  78.5,  "Ø12": 113.1,
    "Ø16": 201.1,  "Ø20": 314.2,  "Ø25": 490.9,
    "Ø28": 615.8,  "Ø32": 804.2,  "Ø36": 1017.9, "Ø40": 1256.6,
}

# ── Nominal bar diameters (mm) keyed by Ø-format string ──────────────────────
REBAR_DIA_MM: dict[str, float] = {f"\u00d8{d}": float(d) for d in REBAR_DIAMETERS_MM}

# ── Ordered list smallest → largest — used by bar-size sweep ─────────────────
ALL_BAR_SIZES: list[str] = [f"\u00d8{d}" for d in REBAR_DIAMETERS_MM]
# = ["Ø8", "Ø10", "Ø12", "Ø16", "Ø20", "Ø25", "Ø28", "Ø32", "Ø36", "Ø40"]

# ── ACI 318-19 spacing limits (mm) ───────────────────────────────────────────
ACI_MAX_CLEAR_MM: float = 150.0   # §25.7.2.3
ACI_RHO_MIN:      float = 0.01    # §10.6.1.1  (1 %)
ACI_RHO_MAX:      float = 0.08    # §10.6.1.1  (8 %)
