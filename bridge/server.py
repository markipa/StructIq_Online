"""
StructIQ Bridge — local ETABS FastAPI server.
Runs on localhost:19999 (no auth — only reachable from the bridge process itself).
Proxies ETABS COM calls from Railway back to the local ETABS instance.
"""
import sys
import os
import importlib.util as _ilu

# ── Resolve backend dir ───────────────────────────────────────────
_HERE = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
# In dev: bridge/ sits next to backend/
_BACKEND = os.path.join(_HERE, '..', 'backend')
if not os.path.isdir(_BACKEND):
    # In frozen .exe: backend modules are bundled alongside bridge modules
    _BACKEND = _HERE

sys.path.insert(0, _BACKEND)

# ── Dynamic load of pmm_engine (bypass frozen bytecode cache) ─────
_PMM_PATH = os.path.join(_BACKEND, 'pmm_engine.py')
if os.path.isfile(_PMM_PATH):
    _spec = _ilu.spec_from_file_location('pmm_engine', _PMM_PATH)
    _pme = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_pme)
    sys.modules['pmm_engine'] = _pme

# ── Dynamic load of etabs_api/actions.py ─────────────────────────
_ACT_PATH = os.path.join(_BACKEND, 'etabs_api', 'actions.py')
if os.path.isfile(_ACT_PATH):
    _aspec = _ilu.spec_from_file_location('etabs_api.actions', _ACT_PATH)
    _amod = _ilu.module_from_spec(_aspec)
    sys.modules['etabs_api.actions'] = _amod
    _aspec.loader.exec_module(_amod)

# ── Imports ───────────────────────────────────────────────────────
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import time as _time

try:
    import etabs_api.actions as actions
except ImportError:
    actions = None

try:
    from pmm_engine import (PMMSection, compute_pmm, check_demands,
                             rect_coords, rect_bars_grid, REBAR_TABLE)
    _HAS_PMM = True
except ImportError:
    _HAS_PMM = False

# ── Unit conversion ───────────────────────────────────────────────
_MM_TO_IN   = 1.0 / 25.4
_MPA_TO_KSI = 1.0 / 6.89476
_KN_TO_KIPS = 1.0 / 4.44822
_KNM_TO_KIN = 1.0 / 0.112985
_IN_TO_MM   = 25.4
_KSI_TO_MPA = 6.89476
_KIPS_TO_KN = 4.44822
_KIN_TO_KNM = 0.112985
_IN2_TO_MM2 = 645.16

REBAR_TABLE_SI = {
    "Ø8":  50.3,  "Ø10":  78.5,  "Ø12": 113.1,
    "Ø16": 201.1, "Ø20": 314.2,  "Ø25": 490.9,
    "Ø28": 615.8, "Ø32": 804.2,  "Ø36": 1017.9,
    "Ø40": 1256.6,
}

# ── In-process caches (same as backend/main.py) ───────────────────
_pmm_cache: dict = {'alpha_data': None, 'Pmax': None, 'Pmin': None, 'si': True}
_PMM_SURFACE_CACHE: dict = {}
_last_batch_forces: dict = {}
_geometry_cache: dict = {}

# ── App ───────────────────────────────────────────────────────────
app = FastAPI(title="StructIQ Bridge — local ETABS server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _require_actions():
    if actions is None:
        raise HTTPException(503, "ETABS API module not available.")


# ═══════════════════════════════════════════════════════════════════
#  STATUS
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/status")
def get_status():
    _require_actions()
    return {"connected": actions.check_connection()}


# ═══════════════════════════════════════════════════════════════════
#  LOAD COMBINATIONS / CASES
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/load-combinations")
def get_load_combinations():
    _require_actions()
    res = actions.get_load_combinations()
    if "error" in res:
        raise HTTPException(500, res["error"])
    return res


@app.get("/api/load-cases")
def get_load_cases():
    _require_actions()
    try:
        from etabs_api.connection import get_active_etabs
        SapModel = get_active_etabs()
        if not SapModel:
            raise HTTPException(503, "ETABS is not connected.")
        ret = SapModel.LoadCases.GetNameList()
        cases = [c for c in ret[1] if not c.startswith("~")]
        return {"status": "success", "cases": cases}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


class BatchGenerateRequest(BaseModel):
    combinations: list


@app.post("/api/load-combinations/generate")
def generate_load_combinations(
    dead_case: str = Query(default="DL"),
    live_case: str = Query(default="LL"),
    comb1_name: str = Query(default="Web_Comb_1"),
    comb2_name: str = Query(default="Web_Comb_2"),
):
    _require_actions()
    res = actions.generate_load_combinations(
        dead_case=dead_case, live_case=live_case,
        comb1_name=comb1_name, comb2_name=comb2_name,
    )
    if "error" in res:
        raise HTTPException(500, res["error"])
    return res


@app.post("/api/load-combinations/generate-batch")
def generate_combinations_batch(request: BatchGenerateRequest):
    _require_actions()
    res = actions.generate_combinations_batch([c if isinstance(c, dict) else c.model_dump() for c in request.combinations])
    if "error" in res:
        raise HTTPException(500, res["error"])
    return res


# ═══════════════════════════════════════════════════════════════════
#  RESULTS
# ═══════════════════════════════════════════════════════════════════

class DriftRequest(BaseModel):
    names: List[str]
    load_type: str = "combo"


@app.post("/api/results/drifts-selected")
def get_drifts_selected(request: DriftRequest):
    _require_actions()
    res = actions.get_story_drifts_selected(request.names, request.load_type)
    if "error" in res:
        raise HTTPException(500, res["error"])
    return res


@app.get("/api/results/drifts")
def get_drifts():
    _require_actions()
    res = actions.get_story_drifts()
    if "error" in res:
        raise HTTPException(500, res["error"])
    return res


@app.get("/api/results/torsional-irregularity")
def check_torsion():
    _require_actions()
    res = actions.check_torsional_irregularity()
    if "error" in res:
        raise HTTPException(500, res["error"])
    return res


@app.get("/api/results/joint-reactions")
def get_joint_reactions(
    names: str = Query(default=None),
    load_type: str = Query(default="combo"),
):
    _require_actions()
    helper = getattr(actions, "get_joint_reactions", None)
    if not callable(helper):
        raise HTTPException(503, "get_joint_reactions not available.")
    res = helper(names, load_type)
    if "error" in res:
        raise HTTPException(500, res["error"])
    return res


@app.get("/api/results/reactions")
def get_reactions(
    names: str = Query(default=None),
    load_type: str = Query(default="combo"),
):
    _require_actions()
    helper = getattr(actions, "get_joint_reactions", None) or getattr(actions, "get_reactions", None)
    if not callable(helper):
        raise HTTPException(503, "reactions not available.")
    res = helper(names, load_type)
    if "error" in res:
        raise HTTPException(500, res["error"])
    return res


# ═══════════════════════════════════════════════════════════════════
#  SECTIONS
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/sections")
def get_sections():
    _require_actions()
    helper = getattr(actions, "get_sections", None)
    if not callable(helper):
        raise HTTPException(503, "get_sections not available.")
    res = helper()
    if isinstance(res, dict) and "error" in res:
        raise HTTPException(500, res["error"])
    return res


@app.post("/api/sections/modify")
def modify_section(body: dict):
    _require_actions()
    helper = getattr(actions, "modify_section", None)
    if not callable(helper):
        raise HTTPException(503, "modify_section not available.")
    res = helper(body)
    if isinstance(res, dict) and "error" in res:
        raise HTTPException(500, res["error"])
    return res


@app.post("/api/sections/add")
def add_section(body: dict):
    _require_actions()
    helper = getattr(actions, "add_section", None)
    if not callable(helper):
        raise HTTPException(503, "add_section not available.")
    res = helper(body)
    if isinstance(res, dict) and "error" in res:
        raise HTTPException(500, res["error"])
    return res


# ═══════════════════════════════════════════════════════════════════
#  RC BEAM / COLUMN
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/rc-beam/materials")
def rc_beam_materials():
    _require_actions()
    helper = getattr(actions, "get_rc_beam_materials", None)
    if not callable(helper):
        raise HTTPException(503, "not available")
    return helper()


@app.get("/api/rc-beam/sections")
def rc_beam_sections():
    _require_actions()
    helper = getattr(actions, "get_rc_beam_sections", None)
    if not callable(helper):
        raise HTTPException(503, "not available")
    return helper()


@app.post("/api/rc-beam/write")
def rc_beam_write(body: dict):
    _require_actions()
    helper = getattr(actions, "write_rc_beam", None)
    if not callable(helper):
        raise HTTPException(503, "not available")
    return helper(body)


@app.get("/api/rc-column/materials")
def rc_col_materials():
    _require_actions()
    helper = getattr(actions, "get_rc_column_materials", None)
    if not callable(helper):
        raise HTTPException(503, "not available")
    return helper()


@app.get("/api/rc-column/sections")
def rc_col_sections():
    _require_actions()
    helper = getattr(actions, "get_rc_column_sections", None)
    if not callable(helper):
        raise HTTPException(503, "not available")
    return helper()


@app.post("/api/rc-column/write")
def rc_col_write(body: dict):
    _require_actions()
    helper = getattr(actions, "write_rc_column", None)
    if not callable(helper):
        raise HTTPException(503, "not available")
    return helper(body)


@app.get("/api/rc-column/debug/{section_name}")
def rc_col_debug(section_name: str):
    _require_actions()
    helper = getattr(actions, "debug_rc_column", None)
    if not callable(helper):
        raise HTTPException(503, "not available")
    return helper(section_name)


# ═══════════════════════════════════════════════════════════════════
#  CLEAN
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/clean/browse-folder")
def browse_folder(path: str = Query(default="")):
    _require_actions()
    helper = getattr(actions, "browse_folder", None)
    if not callable(helper):
        raise HTTPException(503, "not available")
    return helper(path)


@app.post("/api/clean/run-files")
def clean_run_files(body: dict):
    _require_actions()
    helper = getattr(actions, "run_clean_files", None)
    if not callable(helper):
        raise HTTPException(503, "not available")
    return helper(body)


# ═══════════════════════════════════════════════════════════════════
#  PMM + ETABS  (sections, combos, forces, batch check)
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/pmm/etabs-sections")
def pmm_etabs_sections():
    _require_actions()
    helper = getattr(actions, "get_pmm_column_sections", None)
    if not callable(helper):
        raise HTTPException(503, "get_pmm_column_sections not available.")
    res = helper()
    if "error" in res:
        raise HTTPException(503, res["error"])
    return res


@app.get("/api/pmm/etabs-combos")
def pmm_etabs_combos():
    _require_actions()
    helper = getattr(actions, "get_etabs_combos", None)
    if not callable(helper):
        raise HTTPException(503, "get_etabs_combos not available.")
    res = helper()
    if "error" in res:
        raise HTTPException(503, res["error"])
    return res


class ETABSImportForcesRequest(BaseModel):
    combo_names: list
    load_type: str = "combo"


@app.post("/api/pmm/etabs-import-forces")
def pmm_etabs_import_forces(body: ETABSImportForcesRequest):
    _require_actions()
    helper = getattr(actions, "get_etabs_frame_forces", None)
    if not callable(helper):
        raise HTTPException(503, "not available")
    res = helper(body.combo_names, body.load_type)
    if "error" in res:
        raise HTTPException(503, res["error"])
    return res


class ETABSSectionForcesRequest(BaseModel):
    section_name: str
    combo_names: List[str]
    load_type: str = "combo"


@app.post("/api/pmm/etabs-section-forces")
def pmm_etabs_section_forces(body: ETABSSectionForcesRequest):
    _require_actions()
    helper = getattr(actions, "get_etabs_all_column_forces", None)
    if not callable(helper):
        raise HTTPException(503, "not available")
    all_forces = helper([body.section_name], body.combo_names, body.load_type)
    if "error" in all_forces:
        raise HTTPException(503, all_forces["error"])
    by_section = all_forces.get("sections", all_forces)
    rows = by_section.get(body.section_name, [])
    if not rows:
        for key, val in by_section.items():
            if key.lower() == body.section_name.lower():
                rows = val; break
    return {"results": [
        {"frame": r["frame"], "story": r.get("story", "?"), "combo": r["combo"],
         "location": r.get("location", ""), "P_kN": r["P_kN"],
         "M3_kNm": r["M3_kNm"], "M2_kNm": r["M2_kNm"]}
        for r in rows
    ]}


@app.get("/api/pmm/etabs-batch-diag")
def pmm_etabs_batch_diag():
    _require_actions()
    try:
        SapModel = actions.get_active_etabs()
    except Exception:
        SapModel = None
    if not SapModel:
        raise HTTPException(503, "ETABS is not running.")
    try:
        sec_ret = SapModel.PropFrame.GetNameList()
        all_sec = list(sec_ret[1]) if sec_ret and int(sec_ret[-1]) == 0 else []
        fr_ret  = SapModel.FrameObj.GetNameList()
        all_fr  = list(fr_ret[1]) if fr_ret and int(fr_ret[-1]) == 0 else []
        return {"n_section_defs": len(all_sec), "n_frames": len(all_fr),
                "sections": all_sec[:20], "frames_sample": all_fr[:10]}
    except Exception as e:
        raise HTTPException(500, str(e))


class ETABSBatchCheckRequest(BaseModel):
    combo_names: list
    load_type: str = "combo"


def _normalize_bar_size(raw: str) -> str:
    import re as _re
    _known_dia = [8, 10, 12, 16, 20, 25, 28, 32, 36, 40]
    def _snap(n): return "Ø" + str(min(_known_dia, key=lambda d: abs(d - n)))
    if not raw: return "Ø20"
    s = raw.strip()
    if s[0] in ("Ø", "ø"):
        nums = _re.findall(r'\d+', s[1:].split()[0])
        return _snap(int(nums[0])) if nums else "Ø20"
    su = s.upper()
    m = _re.match(r'^D-?(\d+)', su)
    if m: return _snap(int(m.group(1)))
    m = _re.match(r'^[RT](\d+)$', su)
    if m: return _snap(int(m.group(1)))
    _us = {"#3": 10, "#4": 13, "#5": 16, "#6": 19, "#7": 22, "#8": 25, "#9": 29, "#10": 32, "#11": 36}
    if s in _us: return _snap(_us[s])
    nums = [int(n) for n in _re.findall(r'\d+', s) if int(n) >= 6]
    if nums:
        best = min(nums, key=lambda n: min(abs(n - d) for d in _known_dia))
        return _snap(best)
    return "Ø20"


def _build_surface(sec_data: dict, bar_key: str, a_steps=10, n_pts=70) -> dict:
    """Build PMM surface for a section + bar, cached by fingerprint."""
    b_mm = float(sec_data.get("width") or sec_data.get("b_mm") or 400)
    h_mm = float(sec_data.get("depth") or sec_data.get("h_mm") or 400)
    fc   = float(sec_data.get("fc_mpa") or 28.0)
    fy   = float(sec_data.get("fy_mpa") or 420.0)
    Es   = float(sec_data.get("Es_mpa") or 200000.0)
    cov  = float(sec_data.get("cover_mm") or sec_data.get("cover") or 40.0)
    nb   = max(2, int(sec_data.get("nbars_b") or sec_data.get("nbars_2") or 3))
    nh   = max(0, int(sec_data.get("nbars_h") or 0))
    name = sec_data.get("prop_name") or sec_data.get("name", "?")

    ck = (name, b_mm, h_mm, fc, fy, nb, nh, bar_key, a_steps, n_pts)
    if ck in _PMM_SURFACE_CACHE:
        return _PMM_SURFACE_CACHE[ck]

    bar_dia = {"Ø8": 8.0, "Ø10": 10.0, "Ø12": 12.0, "Ø16": 16.0, "Ø20": 20.0,
               "Ø25": 25.0, "Ø28": 28.0, "Ø32": 32.0, "Ø36": 36.0, "Ø40": 40.0}.get(bar_key, 20.0)
    ba_mm2 = REBAR_TABLE_SI.get(bar_key, 314.2)
    eff_cov = cov + 10.0 + bar_dia / 2.0
    b_in = b_mm * _MM_TO_IN; h_in = h_mm * _MM_TO_IN
    fc_k = fc * _MPA_TO_KSI; fy_k = fy * _MPA_TO_KSI; Es_k = Es * _MPA_TO_KSI
    cov_in = eff_cov * _MM_TO_IN; ba = ba_mm2 * (_MM_TO_IN ** 2)
    corners = rect_coords(b_in, h_in)
    areas, positions = rect_bars_grid(b_in, h_in, cov_in, nb, nh, ba)
    sec_obj = PMMSection(corner_coords=corners, fc=fc_k, fy=fy_k, Es=Es_k,
                         alpha_steps=a_steps, num_points=n_pts, include_phi=True,
                         bar_areas=areas, bar_positions=positions)
    raw = compute_pmm(sec_obj)
    ad = raw.get("alpha_data", {})
    for curve in ad.values():
        curve["P"]  = [v * _KIPS_TO_KN  for v in curve["P"]]
        curve["Mx"] = [v * _KIN_TO_KNM for v in curve["Mx"]]
        curve["My"] = [v * _KIN_TO_KNM for v in curve["My"]]
    result = {
        "alpha_data": ad,
        "Pmax_kN": raw["Pmax"] * _KIPS_TO_KN,
        "Pmin_kN": raw["Pmin"] * _KIPS_TO_KN,
    }
    _PMM_SURFACE_CACHE[ck] = result
    return result


@app.post("/api/pmm/etabs-batch-check")
def pmm_etabs_batch_check(body: ETABSBatchCheckRequest):
    _require_actions()
    if not _HAS_PMM:
        raise HTTPException(503, "PMM engine not available.")

    _PMM_SURFACE_CACHE.clear()
    _geometry_cache.clear()

    helper = getattr(actions, "get_pmm_column_sections", None)
    if not callable(helper):
        raise HTTPException(503, "get_pmm_column_sections not available.")
    sec_res = helper()
    if "error" in sec_res:
        raise HTTPException(503, sec_res["error"])
    sections = sec_res.get("sections", [])

    force_helper = getattr(actions, "get_etabs_all_column_forces", None)
    if not callable(force_helper):
        raise HTTPException(503, "get_etabs_all_column_forces not available.")

    sec_names = list({s.get("prop_name") or s.get("name") for s in sections if s.get("prop_name") or s.get("name")})
    all_forces_res = force_helper(sec_names, body.combo_names, body.load_type)
    if "error" in all_forces_res:
        raise HTTPException(503, all_forces_res["error"])

    by_section = all_forces_res.get("sections", all_forces_res)
    _last_batch_forces.clear()
    _last_batch_forces["sections"] = sections
    _last_batch_forces["by_section"] = by_section
    _last_batch_forces["combo_names"] = body.combo_names

    results = []
    for sec in sections:
        sname = sec.get("prop_name") or sec.get("name", "?")
        raw_bar = sec.get("rebar_size") or sec.get("bar_size") or "Ø20"
        bar_key = _normalize_bar_size(str(raw_bar))
        forces = by_section.get(sname, [])
        if not forces:
            for k, v in by_section.items():
                if k.lower() == sname.lower():
                    forces = v; break

        surface = _build_surface(sec, bar_key)
        ad   = surface["alpha_data"]
        Pmax = surface["Pmax_kN"]
        Pmin = surface["Pmin_kN"]

        Ag_mm2 = float(sec.get("width") or 400) * float(sec.get("depth") or 400)
        ba_mm2 = REBAR_TABLE_SI.get(bar_key, 314.2)
        nb = max(2, int(sec.get("nbars_b") or 3))
        nh = max(0, int(sec.get("nbars_h") or 0))
        n_bars = 2 * nb + 2 * nh
        rho_pct = round(n_bars * ba_mm2 / Ag_mm2 * 100, 2) if Ag_mm2 else 0.0

        demands_kn = [
            {"label": f"{r['frame']}/{r['combo']}",
             "P": r["P_kN"], "Mx": r["M3_kNm"], "My": r["M2_kNm"]}
            for r in forces
        ]
        checked = check_demands(ad, Pmax, Pmin, demands_kn) if demands_kn else []
        max_dcr = max((c.get("dcr", 0) for c in checked), default=0.0)

        results.append({
            "section": sname,
            "bar_size": bar_key,
            "rho_pct": rho_pct,
            "max_dcr": round(max_dcr, 3),
            "status": "FAIL" if max_dcr > 1.0 else "OK",
            "n_demands": len(demands_kn),
            "demands": checked,
            "Pmax_kN": round(Pmax, 1),
            "Pmin_kN": round(Pmin, 1),
        })

    return {"results": results, "sections_checked": len(results)}


class BatchOptimizeRequest(BaseModel):
    target_dcr:  float = 0.90
    min_rho_pct: float = 1.0
    max_rho_pct: float = 4.0


@app.post("/api/pmm/batch-optimize")
def pmm_batch_optimize(body: BatchOptimizeRequest):
    if not _HAS_PMM:
        raise HTTPException(503, "PMM engine not available.")
    if not _last_batch_forces.get("sections"):
        raise HTTPException(400, "No batch data cached. Run Batch Check first.")

    STANDARD_BARS = ["Ø12", "Ø16", "Ø20", "Ø25", "Ø28", "Ø32", "Ø36", "Ø40"]
    sections   = _last_batch_forces["sections"]
    by_section = _last_batch_forces["by_section"]

    t0 = _time.perf_counter()
    results = []

    for sec in sections:
        sname   = sec.get("prop_name") or sec.get("name", "?")
        raw_bar = sec.get("rebar_size") or sec.get("bar_size") or "Ø20"
        cur_bar = _normalize_bar_size(str(raw_bar))
        forces  = by_section.get(sname, [])
        if not forces:
            for k, v in by_section.items():
                if k.lower() == sname.lower():
                    forces = v; break

        demands_kn = [
            {"label": f"{r['frame']}/{r['combo']}",
             "P": r["P_kN"], "Mx": r["M3_kNm"], "My": r["M2_kNm"]}
            for r in forces
        ]
        Ag_mm2 = float(sec.get("width") or 400) * float(sec.get("depth") or 400)

        # Current bar DCR (fine resolution)
        cur_surf = _build_surface(sec, cur_bar, a_steps=10, n_pts=70)
        cur_checked = check_demands(cur_surf["alpha_data"],
                                    cur_surf["Pmax_kN"], cur_surf["Pmin_kN"],
                                    demands_kn) if demands_kn else []
        cur_dcr = max((c.get("dcr", 0) for c in cur_checked), default=0.0)
        cur_ba  = REBAR_TABLE_SI.get(cur_bar, 314.2)
        nb = max(2, int(sec.get("nbars_b") or 3))
        nh = max(0, int(sec.get("nbars_h") or 0))
        n_bars = 2 * nb + 2 * nh
        cur_rho = round(n_bars * cur_ba / Ag_mm2 * 100, 2) if Ag_mm2 else 0.0

        # Find smallest bar that satisfies target_dcr and rho limits
        opt_bar = None; opt_dcr = None; opt_rho = None; note = ""

        for bar_key in STANDARD_BARS:
            ba_mm2 = REBAR_TABLE_SI.get(bar_key, 314.2)
            rho = round(n_bars * ba_mm2 / Ag_mm2 * 100, 2) if Ag_mm2 else 0.0
            if rho < body.min_rho_pct or rho > body.max_rho_pct:
                continue
            # Coarse check first
            coarse = _build_surface(sec, bar_key, a_steps=30, n_pts=30)
            chk = check_demands(coarse["alpha_data"], coarse["Pmax_kN"], coarse["Pmin_kN"], demands_kn) if demands_kn else []
            max_dcr = max((c.get("dcr", 0) for c in chk), default=0.0)
            if max_dcr > body.target_dcr * 1.05:
                continue
            # Fine verify
            fine = _build_surface(sec, bar_key, a_steps=10, n_pts=70)
            chk_f = check_demands(fine["alpha_data"], fine["Pmax_kN"], fine["Pmin_kN"], demands_kn) if demands_kn else []
            max_dcr_f = max((c.get("dcr", 0) for c in chk_f), default=0.0)
            if max_dcr_f <= body.target_dcr:
                opt_bar = bar_key; opt_dcr = max_dcr_f; opt_rho = rho
                break

        if opt_bar is None:
            # Try largest bar
            for bar_key in reversed(STANDARD_BARS):
                ba_mm2 = REBAR_TABLE_SI.get(bar_key, 314.2)
                rho = round(n_bars * ba_mm2 / Ag_mm2 * 100, 2) if Ag_mm2 else 0.0
                if rho > body.max_rho_pct:
                    continue
                fine = _build_surface(sec, bar_key, a_steps=10, n_pts=70)
                chk_f = check_demands(fine["alpha_data"], fine["Pmax_kN"], fine["Pmin_kN"], demands_kn) if demands_kn else []
                max_dcr_f = max((c.get("dcr", 0) for c in chk_f), default=0.0)
                opt_bar = bar_key; opt_dcr = max_dcr_f; opt_rho = rho
                note = "OVERSTRESSED — increase section size"
                break

        if opt_bar is None:
            status = "NO DATA"
        elif (opt_dcr or 0) > 1.0:
            status = "OVERSTRESSED"
        elif opt_bar == cur_bar:
            status = "OPTIMAL"
        elif STANDARD_BARS.index(opt_bar) > STANDARD_BARS.index(cur_bar) if cur_bar in STANDARD_BARS else False:
            status = "UPSIZE"
        else:
            status = "DOWNSIZE"

        results.append({
            "section":          sname,
            "current_bar":      cur_bar,
            "current_rho_pct":  cur_rho,
            "current_dcr":      round(cur_dcr, 3),
            "optimized_bar":    opt_bar,
            "optimized_rho_pct": round(opt_rho, 2) if opt_rho is not None else None,
            "optimized_dcr":    round(opt_dcr, 3) if opt_dcr is not None else None,
            "status":           status,
            "note":             note,
        })

    _order = {"OVERSTRESSED": 0, "UPSIZE": 1, "DOWNSIZE": 2, "OPTIMAL": 3, "NO DATA": 4}
    results.sort(key=lambda r: _order.get(r.get("status", "NO DATA"), 5))
    return {"results": results, "elapsed_s": round(_time.perf_counter() - t0, 2)}


# ═══════════════════════════════════════════════════════════════════
#  ETABS GEOMETRY / COLUMN AXIAL
# ═══════════════════════════════════════════════════════════════════

class _AxialReq(BaseModel):
    combo: str
    col_frames: list
    load_type: str = "combo"


@app.post("/api/etabs/column-axial")
def etabs_column_axial(req: _AxialReq):
    _require_actions()
    helper = getattr(actions, "get_column_axial_for_combo", None)
    if not callable(helper):
        raise HTTPException(503, "not available")
    result = helper(req.combo, req.col_frames, req.load_type)
    if "error" in result:
        raise HTTPException(503, result["error"])
    return result


@app.get("/api/etabs/geometry")
def etabs_geometry():
    if _geometry_cache.get("data"):
        return _geometry_cache["data"]
    _require_actions()
    helper = getattr(actions, "get_building_geometry", None)
    if not callable(helper):
        raise HTTPException(503, "not available")
    result = helper()
    if "error" in result:
        raise HTTPException(503, result["error"])
    _geometry_cache["data"] = result
    return result
