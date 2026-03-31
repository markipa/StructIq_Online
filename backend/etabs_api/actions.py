from .connection import get_active_etabs
from typing import Optional, List


def check_connection():
    SapModel = get_active_etabs()
    return True if SapModel is not None else False


def _setup_all_output(SapModel, skip_modal: bool = False):
    """Select all load cases and combinations for output."""
    SapModel.Results.Setup.DeselectAllCasesAndCombosForOutput()
    try:
        case_names = SapModel.LoadCases.GetNameList()[1]
        for name in case_names:
            # Modal cases cannot produce story drifts/reactions — skip them when requested
            if skip_modal and name.lower().startswith("modal"):
                continue
            SapModel.Results.Setup.SetCaseSelectedForOutput(name, True)
    except Exception:
        pass
    try:
        combo_names = SapModel.RespCombo.GetNameList()[1]
        for name in combo_names:
            SapModel.Results.Setup.SetComboSelectedForOutput(name, True)
    except Exception:
        pass


def get_load_combinations():
    """Returns all load combination names from the active ETABS model."""
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}
    try:
        # ret = [count, (name1, name2, ...), retcode]
        ret = SapModel.RespCombo.GetNameList()
        combos = list(ret[1])
        return {"status": "success", "combinations": combos}
    except Exception as e:
        return {"error": f"Failed to get load combinations: {str(e)}"}


def generate_load_combinations(
    dead_case: str = "DL",
    live_case: str = "LL",
    comb1_name: str = "Web_Comb_1",
    comb2_name: str = "Web_Comb_2",
):
    """
    Generates two standard load combinations in ETABS:
      comb1: 1.4 * dead_case
      comb2: 1.2 * dead_case + 1.6 * live_case

    Parameters map to actual ETABS load case names (e.g. 'DL', 'LL').
    SetCaseList ItemType=0 means 'Load Case' (not combo).
    """
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running or cannot be connected."}
    try:
        # Verify the load cases actually exist
        available_cases = list(SapModel.LoadCases.GetNameList()[1])
        missing = [c for c in [dead_case, live_case] if c not in available_cases]
        if missing:
            return {
                "error": f"Load case(s) not found in model: {missing}. "
                         f"Available cases: {available_cases}"
            }

        # Remove existing combos if they already exist (to allow re-running)
        for name in [comb1_name, comb2_name]:
            try:
                SapModel.RespCombo.Delete(name)
            except Exception:
                pass

        # ItemType 0 = Load Case, 1 = Load Combo
        # Combo 1: 1.4 * DL
        SapModel.RespCombo.Add(comb1_name, 0)                          # 0 = Linear Add
        SapModel.RespCombo.SetCaseList(comb1_name, 0, dead_case, 1.4)  # 0 = Load Case

        # Combo 2: 1.2 * DL + 1.6 * LL
        SapModel.RespCombo.Add(comb2_name, 0)
        SapModel.RespCombo.SetCaseList(comb2_name, 0, dead_case, 1.2)
        SapModel.RespCombo.SetCaseList(comb2_name, 0, live_case, 1.6)

        return {
            "status": "success",
            "message": (
                f"Generated '{comb1_name}' (1.4×{dead_case}) and "
                f"'{comb2_name}' (1.2×{dead_case} + 1.6×{live_case}) in ETABS."
            )
        }
    except Exception as e:
        return {"error": f"Failed to generate combinations: {str(e)}. Make sure 'Dead' and 'Live' load cases exist."}


def generate_combinations_batch(combinations: list):
    """
    Generate multiple load combinations in ETABS from a list of definitions.

    Each item in combinations:
        name       : str  - combination name
        combo_type : int  - 0=Linear Add, 1=Envelope, 2=Absolute Add, 3=SRSS
        factors    : dict - {load_case_name: factor_value}
    """
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}
    try:
        available_cases = list(SapModel.LoadCases.GetNameList()[1])
        results = []

        for combo in combinations:
            name = combo.get("name", "").strip()
            if not name:
                continue
            combo_type = combo.get("combo_type", 0)
            factors = combo.get("factors", {})

            valid_factors = {
                k: v for k, v in factors.items()
                if v != 0 and k in available_cases
            }

            if not valid_factors:
                results.append({"name": name, "status": "skipped",
                                 "reason": "no valid non-zero factors matching model load cases"})
                continue

            try:
                try:
                    SapModel.RespCombo.Delete(name)
                except Exception:
                    pass

                SapModel.RespCombo.Add(name, combo_type)
                for case_name, factor in valid_factors.items():
                    # ItemType 0 = Load Case
                    SapModel.RespCombo.SetCaseList(name, 0, case_name, float(factor))

                results.append({"name": name, "status": "success"})
            except Exception as e:
                results.append({"name": name, "status": "error", "reason": str(e)})

        success = sum(1 for r in results if r["status"] == "success")
        skipped = sum(1 for r in results if r["status"] == "skipped")
        errors  = sum(1 for r in results if r["status"] == "error")

        return {
            "status": "success",
            "message": f"Generated {success} combination(s). {skipped} skipped. {errors} error(s).",
            "results": results,
            "success_count": success,
            "error_count": errors,
        }
    except Exception as e:
        return {"error": f"Batch generation failed: {str(e)}"}


def get_story_drifts():
    """
    Returns real inter-story drift data from ETABS.

    StoryDrifts() returns a list:
      [0]  int   num_results
      [1]  tuple StoreName
      [2]  tuple LoadCase
      [3]  tuple StepType
      [4]  tuple StepNum
      [5]  tuple Direction  ('X' or 'Y')
      [6]  tuple Drift
      [7]  tuple Label
      [8]  tuple X
      [9]  tuple Y
      [10] tuple Z
      [11] int   retcode  (0 = success)
    """
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}
    try:
        _setup_all_output(SapModel, skip_modal=True)
        ret = SapModel.Results.StoryDrifts()

        retcode = ret[-1]  # retcode is always the last element
        if retcode != 0:
            return {"error": f"ETABS returned error code {retcode} for StoryDrifts. Ensure the model has been analyzed."}

        num         = ret[0]
        story_names = ret[1]
        load_cases  = ret[2]
        directions  = ret[5]
        drifts      = ret[6]

        data = []
        for i in range(num):
            data.append({
                "story":     story_names[i],
                "combo":     load_cases[i],
                "direction": directions[i],
                "drift":     round(float(drifts[i]), 6),
            })

        return {"status": "success", "data": data}
    except Exception as e:
        return {"error": f"Failed to retrieve story drifts: {str(e)}"}


def get_story_drifts_selected(names: list, load_type: str = "combo"):
    """
    Returns story drift data for specified load cases or combinations.
    Includes Z elevation for height-vs-drift plotting.

    StoryDrifts() index layout:
      [0] num  [1] StoreName  [2] LoadCase  [5] Direction
      [6] Drift  [10] Z (elevation)  [-1] retcode

    load_type: 'combo' | 'case'
    names: list of case/combo names to include
    """
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}
    try:
        SapModel.Results.Setup.DeselectAllCasesAndCombosForOutput()
        for name in names:
            if load_type == "combo":
                SapModel.Results.Setup.SetComboSelectedForOutput(name, True)
            else:
                SapModel.Results.Setup.SetCaseSelectedForOutput(name, True)

        ret = SapModel.Results.StoryDrifts()
        retcode = ret[-1]
        if retcode != 0:
            return {"error": f"ETABS returned error code {retcode}. Ensure the model has been analyzed."}

        num         = ret[0]
        story_names = ret[1]
        load_cases  = ret[2]
        directions  = ret[5]
        drifts      = ret[6]
        elevations  = ret[10]  # Z coordinate = floor elevation

        data = []
        for i in range(num):
            data.append({
                "story":     story_names[i],
                "case":      load_cases[i],
                "direction": directions[i],
                "drift":     round(float(drifts[i]), 6),
                "elevation": round(float(elevations[i]), 3),
            })

        return {"status": "success", "data": data}
    except Exception as e:
        return {"error": f"Failed to retrieve story drifts: {str(e)}"}


def check_torsional_irregularity():
    """
    Calculates torsional irregularity from real ETABS drift results.
    Groups drifts by (story, load_case) and computes max/avg ratio.
    Irregularity when ratio > 1.2.
    """
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}
    try:
        _setup_all_output(SapModel, skip_modal=True)
        ret = SapModel.Results.StoryDrifts()

        retcode = ret[-1]
        if retcode != 0:
            return {"error": f"ETABS returned error code {retcode} for StoryDrifts (torsion check)."}

        num         = ret[0]
        story_names = ret[1]
        load_cases  = ret[2]
        drifts      = ret[6]

        # Group X and Y drifts per (story, combo)
        from collections import defaultdict
        grouped: dict = defaultdict(list)
        for i in range(num):
            key = f"{story_names[i]}|||{load_cases[i]}"
            grouped[key].append(abs(float(drifts[i])))

        details = []
        is_irregular = False
        for key, drift_list in grouped.items():
            if len(drift_list) < 2:
                continue
            story, combo = key.split("|||", 1)
            max_drift = max(drift_list)
            avg_drift = sum(drift_list) / len(drift_list)
            ratio = round(float(max_drift / avg_drift), 3) if avg_drift != 0 else 0.0
            if ratio > 1.2:
                is_irregular = True
            details.append({
                "story":    story,
                "combo":    combo,
                "ratio":    ratio,
                "maxDrift": round(float(max_drift), 6),
                "avgDrift": round(float(avg_drift), 6),
            })

        return {
            "status": "success",
            "data": {
                "isIrregular": is_irregular,
                "details": details,
            }
        }
    except Exception as e:
        return {"error": f"Failed to check torsional irregularity: {str(e)}"}


def get_joint_reactions(names: Optional[List[str]] = None, load_type: str = "combo"):
    """
    Returns joint/support/spring reactions from ETABS using the DatabaseTables API
    (single COM call — far faster than per-joint JointReact iteration).

    Flow mirrors the reference VBA approach:
      1. SetLoadCasesSelectedForDisplay / SetLoadCombinationsSelectedForDisplay
         → tells the DB tables engine which cases/combos to include.
      2. GetTableForDisplayArray("Joint Reactions", ...) → one call, all rows.
      3. Parse the flat 1D TableData array using FieldsKeysIncluded for column indices.
      4. GetCoordCartesian only for unique reaction joints (small subset of all joints).

    "Joint Reactions" table field names (ETABS may vary slightly by version):
        Joint | OutputCase | CaseType | StepType | StepNum | F1 | F2 | F3 | M1 | M2 | M3

    names:     list of combo/case names to filter; None = all available
    load_type: 'combo' (default) | 'case'
    """
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}
    try:
        # ── Step 1: Set display selection in DatabaseTables ──
        # Call BOTH set-functions with the target names (matching the VBA pattern).
        # ETABS silently ignores names that don't belong to that pool, so only the
        # right type gets selected — no need for a separate "clear" step.
        if names:
            name_tuple = tuple(names)
        else:
            # Fetch all of the requested type
            if load_type == "case":
                name_tuple = tuple(
                    c for c in SapModel.LoadCases.GetNameList()[1]
                    if not c.startswith("~")
                )
            else:
                name_tuple = tuple(SapModel.RespCombo.GetNameList()[1])

        try:
            SapModel.DatabaseTables.SetLoadCasesSelectedForDisplay(name_tuple)
        except Exception:
            pass
        try:
            SapModel.DatabaseTables.SetLoadCombinationsSelectedForDisplay(name_tuple)
        except Exception:
            pass

        # ── Step 2: Single batch call — all joint reactions at once ──
        ret = SapModel.DatabaseTables.GetTableForDisplayArray(
            "Joint Reactions",   # table name
            [],                  # FieldKeyList (empty = include all fields)
            "All",               # GroupName  ("All" = every object in model)
            0,                   # TableVersion   (out)
            [],                  # FieldsKeysIncluded (out — field names)
            0,                   # NumberRecords      (out)
            [],                  # TableData          (out — flat 1-D string array)
        )

        # comtypes returns out-params in declaration order + retcode last.
        # Layout: (TableVersion, FieldsKeysIncluded, NumberRecords, TableData, retcode)
        retcode  = int(ret[-1])
        tbl_data = list(ret[-2])        # flat 1-D list of strings
        n_rec    = int(ret[-3])         # number of rows
        fields   = [str(f).lower() for f in ret[-4]]  # column names (lower-cased)

        if retcode != 0:
            return {
                "error": (
                    f"GetTableForDisplayArray returned error {retcode}. "
                    "Ensure the model has been analysed and results exist for the "
                    "selected case(s)/combination(s)."
                )
            }

        if n_rec == 0 or not tbl_data:
            # Analysed but no reactions (free joints only, or wrong selection)
            if load_type == "case":
                available = [c for c in SapModel.LoadCases.GetNameList()[1]
                             if not c.startswith("~")]
                return {"status": "success", "data": [], "available_cases": available}
            else:
                all_combos = list(SapModel.RespCombo.GetNameList()[1])
                return {"status": "success", "data": [], "available_combos": all_combos}

        n_flds = len(fields)

        # ── Step 3: Locate column indices (tolerant of ETABS version differences) ──
        def find_col(fmap, *keys):
            for k in keys:
                if k in fmap:
                    return fmap[k]
            return None

        fmap = {f: i for i, f in enumerate(fields)}

        idx_joint = find_col(fmap, "joint", "point", "uniquename", "uniquepoint")
        idx_case  = find_col(fmap, "outputcase", "loadcase", "case", "combo")
        idx_step  = find_col(fmap, "steptype")          # absent for linear analysis
        idx_f1    = find_col(fmap, "f1", "fx", "p")
        idx_f2    = find_col(fmap, "f2", "fy", "v2")
        idx_f3    = find_col(fmap, "f3", "fz", "v3")
        idx_m1    = find_col(fmap, "m1", "mx", "t")
        idx_m2    = find_col(fmap, "m2", "my")
        idx_m3    = find_col(fmap, "m3", "mz")

        if idx_joint is None or idx_case is None or idx_f3 is None:
            return {
                "error": (
                    f"Unexpected 'Joint Reactions' table schema. "
                    f"Got fields: {fields}. "
                    "Expected at least: joint/point, outputcase/loadcase, f3/fz."
                )
            }

        def to_float(val):
            try:   return round(float(val), 2)
            except: return 0.0

        # ── Step 4: Parse rows + fetch XYZ only for unique reaction joints ──
        coord_map: dict = {}
        data: list = []

        for i in range(n_rec):
            base  = i * n_flds
            jname = str(tbl_data[base + idx_joint])

            # Coordinates — cached, so only one COM call per unique joint
            if jname not in coord_map:
                cr = SapModel.PointObj.GetCoordCartesian(jname)
                coord_map[jname] = (round(float(cr[0]), 3),
                                    round(float(cr[1]), 3),
                                    round(float(cr[2]), 3))
            x, y, z = coord_map[jname]

            data.append({
                "joint":     jname,
                "combo":     str(tbl_data[base + idx_case]),
                "step_type": str(tbl_data[base + idx_step]) if idx_step is not None else "Step 1",
                "x": x, "y": y, "z": z,
                "FX": to_float(tbl_data[base + idx_f1]) if idx_f1 is not None else 0.0,
                "FY": to_float(tbl_data[base + idx_f2]) if idx_f2 is not None else 0.0,
                "FZ": to_float(tbl_data[base + idx_f3]),
                "MX": to_float(tbl_data[base + idx_m1]) if idx_m1 is not None else 0.0,
                "MY": to_float(tbl_data[base + idx_m2]) if idx_m2 is not None else 0.0,
                "MZ": to_float(tbl_data[base + idx_m3]) if idx_m3 is not None else 0.0,
            })

        if load_type == "case":
            available = [c for c in SapModel.LoadCases.GetNameList()[1]
                         if not c.startswith("~")]
            return {"status": "success", "data": data, "available_cases": available}
        else:
            all_combos = list(SapModel.RespCombo.GetNameList()[1])
            return {"status": "success", "data": data, "available_combos": all_combos}

    except Exception as e:
        return {"error": f"Failed to retrieve joint reactions: {str(e)}"}


def get_frame_sections():
    """Returns all frame section names and their key properties from the active ETABS model."""
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}
    try:
        ret = SapModel.PropFrame.GetNameList()
        if ret[-1] != 0:
            return {"error": "Failed to retrieve section list from ETABS."}
        names = list(ret[1]) if ret[1] else []
        sections = []
        for name in names:
            sec = {"name": name, "type": "Unknown", "material": "", "b": None, "h": None, "area": None, "I33": None, "I22": None}
            # Computed section properties (area, moment of inertia, etc.)
            try:
                props = SapModel.PropFrame.GetSectProps(name)
                # (Area, As2, As3, I22, I33, J, S22, S33, Z22, Z33, R22, R33, retcode)
                if props[-1] == 0:
                    sec["area"] = round(float(props[0]), 6)
                    sec["I22"]  = round(float(props[3]), 6)
                    sec["I33"]  = round(float(props[4]), 6)
            except Exception:
                pass
            # Rectangular section: try GetRectangle
            try:
                rect = SapModel.PropFrame.GetRectangle(name)
                # (material, t3=depth, t2=width, color, notes, guid, retcode)
                if rect[-1] == 0:
                    sec["type"]     = "Rectangular"
                    sec["material"] = rect[0]
                    sec["h"]        = round(float(rect[1]), 6)  # t3 = depth
                    sec["b"]        = round(float(rect[2]), 6)  # t2 = width
            except Exception:
                pass
            sections.append(sec)
        # Get available materials for the Add Beam dialog
        try:
            mat_ret = SapModel.PropMaterial.GetNameList()
            materials = list(mat_ret[1]) if mat_ret[-1] == 0 else []
        except Exception:
            materials = []
        return {"status": "success", "sections": sections, "materials": materials}
    except Exception as e:
        return {"error": f"Failed to get frame sections: {str(e)}"}


def set_rectangular_section_dims(name: str, b: float, h: float):
    """Modifies width (b) and depth (h) of an existing rectangular frame section in ETABS."""
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}
    try:
        rect = SapModel.PropFrame.GetRectangle(name)
        if rect[-1] != 0:
            return {"error": f"'{name}' is not a rectangular section or was not found."}
        material = rect[0]
        ret = SapModel.PropFrame.SetRectangle(name, material, h, b)
        if ret != 0:
            return {"error": f"ETABS rejected the section update (code {ret})."}
        area = round(b * h, 6)
        return {"status": "success", "message": f"Section '{name}' updated: b={b}, h={h}, A≈{area}", "area": area}
    except Exception as e:
        return {"error": f"Failed to modify section: {str(e)}"}


def add_rectangular_section(name: str, material: str, b: float, h: float):
    """Creates a new rectangular frame section in ETABS."""
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}
    try:
        ret = SapModel.PropFrame.SetRectangle(name, material, h, b)
        if ret != 0:
            return {"error": f"ETABS rejected section creation (code {ret})."}
        area = round(b * h, 6)
        return {"status": "success", "message": f"Section '{name}' created ({material}, b={b}×h={h}, A={area})", "area": area}
    except Exception as e:
        return {"error": f"Failed to add section: {str(e)}"}


def get_base_reactions(combo_name: Optional[str] = None, load_type: str = "combo"):
    """
    Returns real base reactions from ETABS.

    BaseReact() returns a list:
      [0]  int   num_results
      [1]  tuple LoadCase
      [2]  tuple StepType
      [3]  tuple StepNum
      [4]  tuple FX
      [5]  tuple FY
      [6]  tuple FZ
      [7]  tuple MX
      [8]  tuple MY
      [9]  tuple MZ
      [10] float gx  (global X accel - scalar)
      [11] float gy
      [12] float gz
      [13] int   retcode  (0 = success)

    load_type: 'combo' (default) targets load combinations;
               'case'  targets load cases.
    combo_name filters to a single named item of the selected type.
    """
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}
    try:
        SapModel.Results.Setup.DeselectAllCasesAndCombosForOutput()

        if combo_name:
            # Single-item filter
            if load_type == "case":
                SapModel.Results.Setup.SetCaseSelectedForOutput(combo_name, True)
            else:
                SapModel.Results.Setup.SetComboSelectedForOutput(combo_name, True)
        else:
            # Select all of the requested type
            if load_type == "case":
                try:
                    case_names = SapModel.LoadCases.GetNameList()[1]
                    for name in case_names:
                        if not name.startswith("~"):
                            SapModel.Results.Setup.SetCaseSelectedForOutput(name, True)
                except Exception:
                    pass
            else:
                try:
                    combo_names = SapModel.RespCombo.GetNameList()[1]
                    for name in combo_names:
                        SapModel.Results.Setup.SetComboSelectedForOutput(name, True)
                except Exception:
                    pass

        ret = SapModel.Results.BaseReact()

        retcode = ret[13]
        if retcode != 0:
            return {"error": f"ETABS returned error code {retcode} for BaseReact. Ensure the model has been analyzed."}

        num        = ret[0]
        load_cases = ret[1]
        FX_list    = ret[4]
        FY_list    = ret[5]
        FZ_list    = ret[6]
        MX_list    = ret[7]
        MY_list    = ret[8]
        MZ_list    = ret[9]

        data = []
        for i in range(num):
            data.append({
                "combo": load_cases[i],
                "FX":    round(float(FX_list[i]), 2),
                "FY":    round(float(FY_list[i]), 2),
                "FZ":    round(float(FZ_list[i]), 2),
                "MX":    round(float(MX_list[i]), 2),
                "MY":    round(float(MY_list[i]), 2),
                "MZ":    round(float(MZ_list[i]), 2),
            })

        # Return the available picker list matching the selected type
        if load_type == "case":
            available = [c for c in SapModel.LoadCases.GetNameList()[1] if not c.startswith("~")]
            return {"status": "success", "data": data, "available_cases": available}
        else:
            all_combos = list(SapModel.RespCombo.GetNameList()[1])
            return {"status": "success", "data": data, "available_combos": all_combos}

    except Exception as e:
        return {"error": f"Failed to retrieve base reactions: {str(e)}"}


# ---------------------------------------------------------------------------
# Helpers for unit conversion (length / stress)
# ---------------------------------------------------------------------------
def _length_to_mm_local(value: float, unit_code: int) -> float:
    if unit_code in (1, 3):     return value * 25.4       # inches → mm
    if unit_code in (2, 4):     return value * 304.8      # feet → mm
    if unit_code in (6, 8, 10, 12): return value * 1000.0  # m → mm
    return value                                            # already mm


_REBAR_DIA_MAP = {
    '#3': 10, '#4': 13, '#5': 16, '#6': 19, '#7': 22,
    '#8': 25, '#9': 29, '#10': 32, '#11': 36,
    'D10': 10, 'D13': 13, 'D16': 16, 'D19': 19,
    'D22': 22, 'D25': 25, 'D29': 29, 'D32': 32,
}


def _bar_label_to_dia(label: str) -> int:
    import re
    s = str(label).strip()
    if s in _REBAR_DIA_MAP:
        return _REBAR_DIA_MAP[s]
    m = re.search(r'\d+', s)
    return int(m.group()) if m else 20


def get_pmm_column_sections():
    """
    Return all rectangular RC column sections from the active ETABS model,
    in the format expected by the PMM panel:
      { status, sections: [{name, b_mm, h_mm, cover_mm, nbars_b, nbars_h,
                             rebar_size, material, fy_main,
                             fc_mpa, fy_mpa, Es_mpa}] }
    """
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}
    try:
        try:
            unit_code = SapModel.GetDatabaseUnits()
        except Exception:
            try:
                unit_code = SapModel.GetPresentUnits()
            except Exception:
                unit_code = 6

        def to_mm(v):
            return _length_to_mm_local(v, unit_code)

        def to_mpa(v):
            if unit_code in (1, 2):   return v * 6.89476e-3
            if unit_code in (3, 4):   return v * 6.89476
            if unit_code in (5, 9):   return v
            if unit_code == 6:        return v * 1e-3
            if unit_code == 10:       return v * 1e-6
            if unit_code == 7:        return v * 9.80665
            if unit_code == 8:        return v * 9.80665e-3
            return v

        mat_cache = {}

        def lookup_mat(mat_name):
            if not mat_name:
                return {"fc_mpa": None, "fy_mpa": None, "Es_mpa": None}
            if mat_name in mat_cache:
                return mat_cache[mat_name]
            result = {"fc_mpa": None, "fy_mpa": None, "Es_mpa": None}
            try:
                cr = SapModel.PropMaterial.GetOConcrete(mat_name)
                if cr and int(cr[-1]) == 0:
                    fvals = [v for v in cr[:-1] if isinstance(v, float)]
                    if fvals:
                        result["fc_mpa"] = round(to_mpa(fvals[0]), 1)
            except Exception:
                pass
            try:
                rr = SapModel.PropMaterial.GetORebar(mat_name)
                if rr and int(rr[-1]) == 0:
                    fvals = [v for v in rr[:-1] if isinstance(v, float)]
                    if fvals:
                        result["fy_mpa"] = round(to_mpa(fvals[0]), 1)
            except Exception:
                pass
            for _em in ("GetMPIsotropic", "GetMPUniaxial"):
                try:
                    er = getattr(SapModel.PropMaterial, _em)(mat_name)
                    if er and int(er[-1]) == 0:
                        fvals = [v for v in er[:-1] if isinstance(v, float)]
                        if fvals:
                            result["Es_mpa"] = round(to_mpa(fvals[0]))
                            break
                except Exception:
                    continue
            if result["Es_mpa"] is None and result["fy_mpa"] is not None:
                result["Es_mpa"] = 200000  # standard rebar Es fallback
            mat_cache[mat_name] = result
            return result

        # ── find sections used by column (vertical) frame elements ──────────────
        # Strategy: iterate frames, classify section on first encounter, stop early
        # once all unique prop-section names have been classified.
        column_sec_names = set()
        try:
            # Get prop section names to know when we've classified everything
            _pret = SapModel.PropFrame.GetNameList()
            _all_prop = set()
            if _pret and int(_pret[-1]) == 0:
                for _item in _pret[:-1]:
                    if hasattr(_item, '__iter__') and not isinstance(_item, str):
                        _all_prop = set(_item)
                        break

            fr = SapModel.FrameObj.GetNameList()
            if fr and int(fr[-1]) == 0:
                names_arr = None
                for item in fr[:-1]:
                    if hasattr(item, '__iter__') and not isinstance(item, str):
                        names_arr = list(item)
                        break
                classified = set()
                for fname in (names_arr or []):
                    # Stop once all prop sections are classified
                    if _all_prop and classified >= _all_prop:
                        break
                    try:
                        sr = SapModel.FrameObj.GetSection(fname)
                        if not sr or int(sr[-1]) != 0 or not sr[0]:
                            continue
                        sec_name = str(sr[0])
                        if sec_name in classified:
                            continue  # already know this section's type
                        classified.add(sec_name)
                        # Check geometry: vertical = column
                        cr = SapModel.FrameObj.GetPoints(fname)
                        if not cr or int(cr[-1]) != 0:
                            continue
                        p1 = SapModel.PointObj.GetCoordCartesian(str(cr[0]))
                        p2 = SapModel.PointObj.GetCoordCartesian(str(cr[1]))
                        if not p1 or not p2 or int(p1[-1]) != 0 or int(p2[-1]) != 0:
                            continue
                        dz = abs(float(p2[2]) - float(p1[2]))
                        dh = ((float(p2[0])-float(p1[0]))**2 + (float(p2[1])-float(p1[1]))**2)**0.5
                        if dz > dh:
                            column_sec_names.add(sec_name)
                    except Exception:
                        continue
        except Exception:
            pass

        ret = SapModel.PropFrame.GetNameList()
        if not ret or len(ret) < 2:
            return {"error": "No frame sections found in ETABS model."}
        all_names = list(ret[1])

        sections = []
        for name in all_names:
            if column_sec_names and name not in column_sec_names:
                continue
            try:
                r = SapModel.PropFrame.GetRectangle(name)
                if not r or int(r[-1]) != 0:
                    continue
                conc_mat = str(r[1])
                h_mm = round(to_mm(float(r[2])))
                b_mm = round(to_mm(float(r[3])))
            except Exception:
                continue

            fy_mat   = ""
            cover_mm = 40
            nbars_b  = 3
            nbars_h  = 3
            bar_dia  = 20
            try:
                rr = SapModel.PropFrame.GetRebarColumn(name)
                if not rr or int(rr[-1]) != 0:
                    continue  # no column rebar → beam section, skip
                str_vals   = [v for v in rr[:-1] if isinstance(v, str) and v]
                float_vals = [v for v in rr[:-1] if isinstance(v, float)]
                int_vals   = [v for v in rr[:-1]
                              if isinstance(v, int) and not isinstance(v, bool)]
                if str_vals:
                    fy_mat = str_vals[0]
                if float_vals:
                    cover_mm = max(20, round(to_mm(float_vals[0])))
                if len(int_vals) >= 5:
                    nbars_b = max(2, int(int_vals[3]))
                    nbars_h = max(0, int(int_vals[4]) - 2)
                elif len(int_vals) >= 4:
                    nbars_b = max(2, int(int_vals[2]))
                    nbars_h = max(0, int(int_vals[3]) - 2)
                sz_label = ""
                if len(str_vals) >= 3:
                    sz_label = str_vals[2]
                elif len(str_vals) >= 2:
                    sz_label = str_vals[1]
                if sz_label:
                    bar_dia = _bar_label_to_dia(sz_label)
            except Exception:
                continue  # if GetRebarColumn raises, treat as non-column → skip

            conc  = lookup_mat(conc_mat)
            rebar = lookup_mat(fy_mat) if fy_mat else {}

            sections.append({
                "name":       name,
                "b_mm":       b_mm,
                "h_mm":       h_mm,
                "cover_mm":   cover_mm,
                "nbars_b":    nbars_b,
                "nbars_h":    nbars_h,
                "rebar_size": bar_dia,
                "material":   conc_mat,
                "fy_main":    fy_mat,
                "fc_mpa":     conc.get("fc_mpa"),
                "fy_mpa":     rebar.get("fy_mpa"),
                "Es_mpa":     rebar.get("Es_mpa"),
            })

        if not sections:
            return {"error": "No rectangular RC column sections found in ETABS model."}
        return {"status": "success", "sections": sections}

    except Exception as e:
        return {"error": f"Failed to get PMM column sections: {str(e)}"}


def _mm_to_model_length(value_mm: float, unit_code: int) -> float:
    """Convert mm back to ETABS model length units."""
    if unit_code in (1, 3):          return value_mm / 25.4        # mm → inches
    if unit_code in (2, 4):          return value_mm / 304.8       # mm → feet
    if unit_code in (6, 8, 10, 12):  return value_mm / 1000.0      # mm → m
    return value_mm                                                  # already mm


def _dia_to_bar_label(dia_mm: int) -> str:
    """Convert rebar diameter (mm) to an ETABS bar-size label."""
    _map = {10:'#3',13:'#4',16:'#5',19:'#6',22:'#7',25:'#8',29:'#9',32:'#10',36:'#11'}
    return _map.get(int(dia_mm), f'D{int(dia_mm)}')


def get_frame_materials():
    """Return all material names defined in the active ETABS model."""
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}
    try:
        ret = SapModel.PropMaterial.GetNameList()
        if not ret or int(ret[-1]) != 0:
            return {"error": "Failed to retrieve material list from ETABS."}
        materials = []
        for item in ret[:-1]:
            if hasattr(item, '__iter__') and not isinstance(item, str):
                materials = list(item)
                break
        return {"status": "success", "materials": materials}
    except Exception as e:
        return {"error": f"Failed to get materials: {str(e)}"}


def get_rc_beam_sections():
    """
    Return all rectangular beam (horizontal) sections from the active ETABS model,
    in the format expected by the RC Beam Section Generator panel.
    """
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}
    try:
        try:
            unit_code = SapModel.GetDatabaseUnits()
        except Exception:
            try:
                unit_code = SapModel.GetPresentUnits()
            except Exception:
                unit_code = 6

        def to_mm(v):
            return _length_to_mm_local(v, unit_code)

        # Classify sections as column or beam by geometry (same logic as PMM)
        column_sec_names = set()
        try:
            _pret = SapModel.PropFrame.GetNameList()
            _all_prop = set()
            if _pret and int(_pret[-1]) == 0:
                for _item in _pret[:-1]:
                    if hasattr(_item, '__iter__') and not isinstance(_item, str):
                        _all_prop = set(_item); break
            fr = SapModel.FrameObj.GetNameList()
            if fr and int(fr[-1]) == 0:
                names_arr = None
                for item in fr[:-1]:
                    if hasattr(item, '__iter__') and not isinstance(item, str):
                        names_arr = list(item); break
                classified = set()
                for fname in (names_arr or []):
                    if _all_prop and classified >= _all_prop: break
                    try:
                        sr = SapModel.FrameObj.GetSection(fname)
                        if not sr or int(sr[-1]) != 0 or not sr[0]: continue
                        sec_name = str(sr[0])
                        if sec_name in classified: continue
                        classified.add(sec_name)
                        cr = SapModel.FrameObj.GetPoints(fname)
                        if not cr or int(cr[-1]) != 0: continue
                        p1 = SapModel.PointObj.GetCoordCartesian(str(cr[0]))
                        p2 = SapModel.PointObj.GetCoordCartesian(str(cr[1]))
                        if not p1 or not p2 or int(p1[-1]) != 0 or int(p2[-1]) != 0: continue
                        dz = abs(float(p2[2]) - float(p1[2]))
                        dh = ((float(p2[0])-float(p1[0]))**2 + (float(p2[1])-float(p1[1]))**2)**0.5
                        if dz > dh: column_sec_names.add(sec_name)
                    except Exception: continue
        except Exception: pass

        ret = SapModel.PropFrame.GetNameList()
        if not ret or len(ret) < 2:
            return {"error": "No frame sections found in ETABS model."}
        all_names = list(ret[1])

        sections = []
        for name in all_names:
            # Skip column sections
            if column_sec_names and name in column_sec_names:
                continue
            try:
                r = SapModel.PropFrame.GetRectangle(name)
                if not r or int(r[-1]) != 0:
                    continue
                conc_mat = str(r[1])
                h_mm = round(to_mm(float(r[2])))
                b_mm = round(to_mm(float(r[3])))
            except Exception:
                continue

            # Try to get section modifiers
            torsion, i22, i33 = 0.35, 0.35, 0.35
            try:
                mr = SapModel.PropFrame.GetModifiers(name)
                if mr and int(mr[-1]) == 0:
                    fvals = [v for v in mr[:-1] if isinstance(v, float)]
                    if len(fvals) >= 6:
                        torsion = round(fvals[2], 3)
                        i22     = round(fvals[3], 3)
                        i33     = round(fvals[4], 3)
            except Exception:
                pass

            sections.append({
                "prop_name":         name,
                "depth":             h_mm,
                "width":             b_mm,
                "concrete_strength": conc_mat,
                "fy_main":           "",
                "fy_ties":           "",
                "bar_dia":           25,
                "top_cc":            40,
                "bot_cc":            40,
                "nbar_top_i":        0,
                "nbar_top_j":        0,
                "nbar_bot_i":        0,
                "nbar_bot_j":        0,
                "torsion":           torsion,
                "i22":               i22,
                "i33":               i33,
            })

        if not sections:
            return {"error": "No rectangular beam sections found in ETABS model."}
        return {"status": "success", "sections": sections}
    except Exception as e:
        return {"error": f"Failed to get beam sections: {str(e)}"}


def get_rc_column_sections():
    """
    Return all rectangular RC column sections from the active ETABS model,
    in the format expected by the RC Column Section Generator panel.
    """
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}
    try:
        try:
            unit_code = SapModel.GetDatabaseUnits()
        except Exception:
            try:
                unit_code = SapModel.GetPresentUnits()
            except Exception:
                unit_code = 6

        def to_mm(v):
            return _length_to_mm_local(v, unit_code)

        # Classify column sections by geometry (same as PMM)
        column_sec_names = set()
        try:
            _pret = SapModel.PropFrame.GetNameList()
            _all_prop = set()
            if _pret and int(_pret[-1]) == 0:
                for _item in _pret[:-1]:
                    if hasattr(_item, '__iter__') and not isinstance(_item, str):
                        _all_prop = set(_item); break
            fr = SapModel.FrameObj.GetNameList()
            if fr and int(fr[-1]) == 0:
                names_arr = None
                for item in fr[:-1]:
                    if hasattr(item, '__iter__') and not isinstance(item, str):
                        names_arr = list(item); break
                classified = set()
                for fname in (names_arr or []):
                    if _all_prop and classified >= _all_prop: break
                    try:
                        sr = SapModel.FrameObj.GetSection(fname)
                        if not sr or int(sr[-1]) != 0 or not sr[0]: continue
                        sec_name = str(sr[0])
                        if sec_name in classified: continue
                        classified.add(sec_name)
                        cr = SapModel.FrameObj.GetPoints(fname)
                        if not cr or int(cr[-1]) != 0: continue
                        p1 = SapModel.PointObj.GetCoordCartesian(str(cr[0]))
                        p2 = SapModel.PointObj.GetCoordCartesian(str(cr[1]))
                        if not p1 or not p2 or int(p1[-1]) != 0 or int(p2[-1]) != 0: continue
                        dz = abs(float(p2[2]) - float(p1[2]))
                        dh = ((float(p2[0])-float(p1[0]))**2 + (float(p2[1])-float(p1[1]))**2)**0.5
                        if dz > dh: column_sec_names.add(sec_name)
                    except Exception: continue
        except Exception: pass

        ret = SapModel.PropFrame.GetNameList()
        if not ret or len(ret) < 2:
            return {"error": "No frame sections found in ETABS model."}
        all_names = list(ret[1])

        sections = []
        for name in all_names:
            if column_sec_names and name not in column_sec_names:
                continue
            try:
                r = SapModel.PropFrame.GetRectangle(name)
                if not r or int(r[-1]) != 0:
                    continue
                conc_mat = str(r[1])
                h_mm = round(to_mm(float(r[2])))
                b_mm = round(to_mm(float(r[3])))
            except Exception:
                continue

            fy_mat        = ""
            cover_mm      = 40
            nbars_3       = 3
            nbars_2       = 3
            bar_dia       = 20
            tie_spacing   = 150
            try:
                rr = SapModel.PropFrame.GetRebarColumn(name)
                if not rr or int(rr[-1]) != 0:
                    continue
                str_vals   = [v for v in rr[:-1] if isinstance(v, str) and v]
                float_vals = [v for v in rr[:-1] if isinstance(v, float)]
                int_vals   = [v for v in rr[:-1]
                              if isinstance(v, int) and not isinstance(v, bool)]
                if str_vals:
                    fy_mat = str_vals[0]
                if float_vals:
                    cover_mm = max(20, round(to_mm(float_vals[0])))
                if len(float_vals) >= 2:
                    tie_spacing = max(50, round(to_mm(float_vals[1])))
                if len(int_vals) >= 5:
                    nbars_3 = max(2, int(int_vals[3]))
                    nbars_2 = max(2, int(int_vals[4]))
                elif len(int_vals) >= 4:
                    nbars_3 = max(2, int(int_vals[2]))
                    nbars_2 = max(2, int(int_vals[3]))
                sz_label = ""
                if len(str_vals) >= 3:
                    sz_label = str_vals[2]
                elif len(str_vals) >= 2:
                    sz_label = str_vals[1]
                if sz_label:
                    bar_dia = _bar_label_to_dia(sz_label)
            except Exception:
                continue

            # Tie size: try second string value, else default
            tie_dia = 12
            try:
                if len(str_vals) >= 4:
                    tie_dia = _bar_label_to_dia(str_vals[3])
                elif len(str_vals) >= 2 and fy_mat and str_vals[1] != fy_mat:
                    tie_dia = _bar_label_to_dia(str_vals[1])
            except Exception:
                pass

            # Section modifiers
            torsion, i22, i33 = 0.01, 0.70, 0.70
            try:
                mr = SapModel.PropFrame.GetModifiers(name)
                if mr and int(mr[-1]) == 0:
                    fvals = [v for v in mr[:-1] if isinstance(v, float)]
                    if len(fvals) >= 6:
                        torsion = round(fvals[2], 3)
                        i22     = round(fvals[3], 3)
                        i33     = round(fvals[4], 3)
            except Exception:
                pass

            sections.append({
                "prop_name":         name,
                "depth":             h_mm,
                "width":             b_mm,
                "concrete_strength": conc_mat,
                "fy_main":           fy_mat,
                "fy_ties":           fy_mat,
                "cover":             cover_mm,
                "rebar_size":        bar_dia,
                "nbars_3":           nbars_3,
                "nbars_2":           nbars_2,
                "tie_size":          tie_dia,
                "tie_spacing":       tie_spacing,
                "num_tie_3":         3,
                "num_tie_2":         3,
                "to_be_designed":    False,
                "torsion":           torsion,
                "i22":               i22,
                "i33":               i33,
            })

        if not sections:
            return {"error": "No rectangular RC column sections found in ETABS model."}
        return {"status": "success", "sections": sections}
    except Exception as e:
        return {"error": f"Failed to get RC column sections: {str(e)}"}


def write_rc_beam_sections(sections_list: list):
    """Create or update rectangular RC beam sections in ETABS."""
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}
    try:
        unit_code = SapModel.GetDatabaseUnits()
    except Exception:
        try:
            unit_code = SapModel.GetPresentUnits()
        except Exception:
            unit_code = 6

    success_count = 0
    error_count   = 0
    errors        = []

    for s in sections_list:
        name  = str(s.get("prop_name", "")).strip()
        conc  = str(s.get("concrete_strength", "")).strip()
        depth = float(s.get("depth") or 0)
        width = float(s.get("width") or 0)
        if not name or not conc or depth <= 0 or width <= 0:
            error_count += 1
            errors.append(f"'{name}': missing or invalid data")
            continue
        try:
            t3 = _mm_to_model_length(depth, unit_code)
            t2 = _mm_to_model_length(width, unit_code)
            ret = SapModel.PropFrame.SetRectangle(name, conc, t3, t2)
            if int(ret) != 0:
                raise RuntimeError(f"SetRectangle returned {ret}")
            # Apply modifiers if provided
            torsion = float(s.get("torsion") or 0.35)
            i22     = float(s.get("i22")     or 0.35)
            i33     = float(s.get("i33")     or 0.35)
            try:
                # Modifiers order: (A, AS2, AS3, I22, I33, J, M22, M33, W, Mass, Weight)
                # Most common: index 2=J(torsion), 3=I22, 4=I33
                SapModel.PropFrame.SetModifiers(name, [1,1,torsion,i22,i33,1,1,1])
            except Exception:
                pass
            success_count += 1
        except Exception as e:
            error_count += 1
            errors.append(f"'{name}': {str(e)}")

    return {"status": "success", "success_count": success_count,
            "error_count": error_count, "errors": errors}


def write_rc_column_sections(sections_list: list):
    """Create or update rectangular RC column sections in ETABS."""
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}
    try:
        unit_code = SapModel.GetDatabaseUnits()
    except Exception:
        try:
            unit_code = SapModel.GetPresentUnits()
        except Exception:
            unit_code = 6

    def to_model(mm):
        return _mm_to_model_length(float(mm), unit_code)

    success_count = 0
    error_count   = 0
    errors        = []

    for s in sections_list:
        name  = str(s.get("prop_name", "")).strip()
        conc  = str(s.get("concrete_strength", "")).strip()
        depth = float(s.get("depth") or 0)
        width = float(s.get("width") or 0)
        if not name or not conc or depth <= 0 or width <= 0:
            error_count += 1
            errors.append(f"'{name}': missing or invalid data")
            continue
        try:
            t3 = to_model(depth)
            t2 = to_model(width)
            ret = SapModel.PropFrame.SetRectangle(name, conc, t3, t2)
            if int(ret) != 0:
                raise RuntimeError(f"SetRectangle returned {ret}")

            # Set column rebar if material info is present
            fy_main = str(s.get("fy_main", "") or "").strip()
            fy_ties = str(s.get("fy_ties", "") or fy_main).strip()
            if fy_main and fy_ties:
                try:
                    cover       = to_model(float(s.get("cover") or 40))
                    bar_dia     = int(s.get("rebar_size") or 20)
                    nbars_3     = max(2, int(s.get("nbars_3") or 3))
                    nbars_2     = max(2, int(s.get("nbars_2") or 3))
                    tie_dia     = int(s.get("tie_size") or 12)
                    tie_sp      = to_model(float(s.get("tie_spacing") or 150))
                    num_tie_3   = max(1, int(s.get("num_tie_3") or 3))
                    num_tie_2   = max(1, int(s.get("num_tie_2") or 3))
                    to_be_des   = bool(s.get("to_be_designed", False))
                    bar_label   = _dia_to_bar_label(bar_dia)
                    tie_label   = _dia_to_bar_label(tie_dia)
                    SapModel.PropFrame.SetRebarColumn(
                        name, fy_main, fy_ties,
                        1,          # Pattern: 1=Rectangular
                        0,          # ConfineType: 0=Ties
                        cover,
                        0,          # (reserved slot)
                        nbars_3,    # bars on short faces (incl. corners)
                        nbars_2,    # bars on long faces (incl. corners)
                        bar_label,
                        tie_label,
                        tie_sp,
                        num_tie_3,
                        num_tie_2,
                        to_be_des,
                    )
                except Exception:
                    pass  # geometry written successfully; rebar is best-effort

            # Apply modifiers
            torsion = float(s.get("torsion") or 0.01)
            i22     = float(s.get("i22")     or 0.70)
            i33     = float(s.get("i33")     or 0.70)
            try:
                SapModel.PropFrame.SetModifiers(name, [1,1,torsion,i22,i33,1,1,1])
            except Exception:
                pass

            success_count += 1
        except Exception as e:
            error_count += 1
            errors.append(f"'{name}': {str(e)}")

    return {"status": "success", "success_count": success_count,
            "error_count": error_count, "errors": errors}


def _to_kN(force: float, unit_code: int) -> float:
    """Convert force from ETABS present units to kN.
    ETABS eUnits: 1=lb_in 2=lb_ft 3=kip_in 4=kip_ft
                  5=kN_mm 6=kN_m  7=kgf_mm 8=kgf_m
                  9=N_mm  10=N_m  11=Ton_mm 12=Ton_m
    """
    if unit_code in (1, 2):   return force * 0.00444822   # lb → kN
    if unit_code in (3, 4):   return force * 4.44822      # kip → kN
    if unit_code in (5, 6):   return force                # kN_mm / kN_m: already kN
    if unit_code in (7, 8):   return force * 0.00980665   # kgf → kN
    if unit_code in (9, 10):  return force * 0.001        # N → kN
    if unit_code in (11, 12): return force * 9.80665      # metric ton-force → kN
    return force


def _to_kNm(moment: float, unit_code: int) -> float:
    """Convert moment from ETABS present units to kN·m.
    ETABS eUnits: 1=lb_in 2=lb_ft 3=kip_in 4=kip_ft
                  5=kN_mm 6=kN_m  7=kgf_mm 8=kgf_m
                  9=N_mm  10=N_m  11=Ton_mm 12=Ton_m
    """
    if unit_code == 1:    return moment * 0.000112985   # lb·in   → kN·m
    if unit_code == 2:    return moment * 0.00135582    # lb·ft   → kN·m
    if unit_code == 3:    return moment * 0.112985      # kip·in  → kN·m
    if unit_code == 4:    return moment * 1.35582       # kip·ft  → kN·m
    if unit_code == 5:    return moment * 0.001         # kN·mm   → kN·m
    if unit_code == 6:    return moment                 # kN·m    → kN·m
    if unit_code == 7:    return moment * 9.80665e-6    # kgf·mm  → kN·m
    if unit_code == 8:    return moment * 0.00980665    # kgf·m   → kN·m
    if unit_code == 9:    return moment * 1e-6          # N·mm    → kN·m
    if unit_code == 10:   return moment * 0.001         # N·m     → kN·m
    if unit_code == 11:   return moment * 0.00980665    # Ton·mm  → kN·m  (1 t·mm = 9.80665 kN·mm = 9.80665e-3 kN·m)
    if unit_code == 12:   return moment * 9.80665       # Ton·m   → kN·m
    return moment


def get_etabs_combos():
    """Return all load combinations and load cases from the active ETABS model."""
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}
    try:
        combo_ret = SapModel.RespCombo.GetNameList()
        combos = list(combo_ret[1]) if combo_ret and int(combo_ret[-1]) == 0 else []
        case_ret = SapModel.LoadCases.GetNameList()
        cases = [c for c in list(case_ret[1]) if not c.startswith("~")] \
                if case_ret and int(case_ret[-1]) == 0 else []
        return {"status": "success", "combinations": combos, "cases": cases}
    except Exception as e:
        return {"error": f"Failed to get load combinations: {str(e)}"}


def get_etabs_frame_forces(combo_names: list, load_type: str = "combo"):
    """
    Get frame forces for currently selected frame objects in ETABS,
    for the specified load combinations or load cases.
    Returns {results: [{label, P_kN, M3_kNm, M2_kNm}, ...]}
    P sign: ETABS compression = negative P; we negate so PMM receives positive compression.
    """
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}
    # Frame force results are returned in the PRESENT units, not database units
    try:
        unit_code = SapModel.GetPresentUnits()
    except Exception:
        unit_code = 6  # assume kN-m

    try:
        # --- 1. Get selected frame objects ---
        sel_ret = SapModel.SelectObj.GetSelected()
        # Returns: (count, type_array, name_array, retcode)
        if not sel_ret or int(sel_ret[-1]) != 0 or sel_ret[0] == 0:
            return {"error": "No objects selected in ETABS. Please select columns first."}

        obj_count = int(sel_ret[0])
        obj_types = sel_ret[1]   # 1=point, 2=frame, 3=area, 4=solid, 5=link
        obj_names = sel_ret[2]
        frame_names = [obj_names[i] for i in range(obj_count) if int(obj_types[i]) == 2]

        if not frame_names:
            return {"error": "No frame objects selected. Please select columns in ETABS first."}

        # --- 2. Set up output results ---
        SapModel.Results.Setup.DeselectAllCasesAndCombosForOutput()
        for name in combo_names:
            if load_type == "case":
                SapModel.Results.Setup.SetCaseSelectedForOutput(name, True)
            else:
                SapModel.Results.Setup.SetComboSelectedForOutput(name, True)

        # --- 3. Loop frames and extract forces ---
        results = []
        for frame_name in frame_names:
            # eItemType=0 means Object (frame by name)
            ret = SapModel.Results.FrameForce(frame_name, 0)
            # ret = (NumberResults, Obj, ObjSta, Elm, ElmSta, LoadCase,
            #         StepType, StepNum, P, V2, V3, T, M2, M3, retcode)
            if not ret or int(ret[-1]) != 0 or int(ret[0]) == 0:
                continue

            n        = int(ret[0])
            stations = ret[2]   # station positions along element
            lc_names = ret[5]   # LoadCase label per result row
            P_raw    = ret[8]   # axial force (ETABS: compression = negative)
            M2_raw   = ret[12]  # weak-axis moment
            M3_raw   = ret[13]  # strong-axis moment

            # Group by load case, pick the station with maximum |P|
            best: dict = {}
            for i in range(n):
                lc = lc_names[i]
                if lc not in best or abs(P_raw[i]) > abs(best[lc]["P"]):
                    best[lc] = {"P": P_raw[i], "M2": M2_raw[i], "M3": M3_raw[i]}

            for lc, forces in best.items():
                P_kN   = _to_kN(forces["P"],  unit_code)
                M3_kNm = _to_kNm(forces["M3"], unit_code)
                M2_kNm = _to_kNm(forces["M2"], unit_code)
                results.append({
                    "label":   f"{frame_name}/{lc}",
                    "P_kN":    round(P_kN, 2),     # ETABS sign: compression = negative P
                    "M3_kNm":  round(M3_kNm, 2),
                    "M2_kNm":  round(M2_kNm, 2),
                })

        if not results:
            return {"error": "No force results returned. Ensure selected frames have results for the chosen combinations."}

        return {"status": "success", "results": results}
    except Exception as e:
        return {"error": f"Failed to get frame forces: {str(e)}"}


def get_etabs_all_column_forces(sec_names: list, combo_names: list, load_type: str = "combo"):
    """
    Get frame forces for every column frame in the model, grouped by section name.

    For each frame that uses one of the sections in `sec_names`, query FrameForce
    for all requested combos/cases and keep the result with maximum |P| per
    frame × load combination.

    Returns:
      {"sections": {sec_name: [{"label": "frame/combo", "P_kN": ...,
                                "M3_kNm": ..., "M2_kNm": ...}, ...]}}
    """
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}

    try:
        try:
            unit_code = SapModel.GetPresentUnits()
        except Exception:
            unit_code = 6  # kN-m

        sec_set = set(str(s) for s in sec_names)

        # ── 1. Build frame → section mapping ────────────────────────────────
        frame_to_sec = {}
        try:
            fr = SapModel.FrameObj.GetNameList()
            if fr and int(fr[-1]) == 0:
                names_arr = None
                for item in fr[:-1]:
                    if hasattr(item, '__iter__') and not isinstance(item, str):
                        names_arr = list(item)
                        break
                for fname in (names_arr or []):
                    try:
                        sr = SapModel.FrameObj.GetSection(fname)
                        if not sr or int(sr[-1]) != 0 or not sr[0]:
                            continue
                        sname = str(sr[0])
                        if sname in sec_set:
                            frame_to_sec[fname] = sname
                    except Exception:
                        continue
        except Exception as e:
            return {"error": f"Could not enumerate frame objects: {e}"}

        if not frame_to_sec:
            return {"error": "No column frames found for the given section names."}

        # ── 2. Set up output combinations/cases ─────────────────────────────
        SapModel.Results.Setup.DeselectAllCasesAndCombosForOutput()
        for name in combo_names:
            if load_type == "case":
                SapModel.Results.Setup.SetCaseSelectedForOutput(name, True)
            else:
                SapModel.Results.Setup.SetComboSelectedForOutput(name, True)

        # ── 3. Loop frames and extract forces ────────────────────────────────
        by_section: dict = {s: [] for s in sec_set}

        for frame_name, sec_name in frame_to_sec.items():
            try:
                ret = SapModel.Results.FrameForce(frame_name, 0)
                # ret = (NumberResults, Obj, ObjSta, Elm, ElmSta, LoadCase,
                #         StepType, StepNum, P, V2, V3, T, M2, M3, retcode)
                if not ret or int(ret[-1]) != 0 or int(ret[0]) == 0:
                    continue

                n        = int(ret[0])
                lc_names = ret[5]
                P_raw    = ret[8]
                M2_raw   = ret[12]
                M3_raw   = ret[13]

                # Per frame: keep worst (max |P|) station per load case
                best: dict = {}
                for i in range(n):
                    lc = lc_names[i]
                    if lc not in best or abs(P_raw[i]) > abs(best[lc]["P"]):
                        best[lc] = {"P": P_raw[i], "M2": M2_raw[i], "M3": M3_raw[i]}

                for lc, forces in best.items():
                    by_section[sec_name].append({
                        "frame":  frame_name,
                        "combo":  lc,
                        "P_kN":   round(_to_kN  (forces["P"],  unit_code), 2),
                        "M3_kNm": round(_to_kNm (forces["M3"], unit_code), 2),
                        "M2_kNm": round(_to_kNm (forces["M2"], unit_code), 2),
                    })
            except Exception:
                continue

        return {"status": "success", "sections": by_section}

    except Exception as e:
        return {"error": f"get_etabs_all_column_forces failed: {e}"}


def debug_rc_column_raw(section_name: str):
    """Return raw comtypes output for GetRectangle + GetRebarColumn on one section."""
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}
    try:
        unit_code = SapModel.GetDatabaseUnits()
    except Exception:
        unit_code = 6
    try:
        rect = SapModel.PropFrame.GetRectangle(section_name)
        rect_out = [str(v) for v in rect] if rect else None
    except Exception as e:
        rect_out = [f"ERROR: {e}"]
    try:
        rebar = SapModel.PropFrame.GetRebarColumn(section_name)
        rebar_out = [str(v) for v in rebar] if rebar else None
    except Exception as e:
        rebar_out = [f"ERROR: {e}"]
    return {"section_name": section_name, "unit_code": unit_code,
            "GetRectangle": rect_out, "GetRebarColumn": rebar_out}
