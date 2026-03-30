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


# ─── RC Beam Section Generator ──────────────────────────────────────────────

def _length_to_mm(value: float, unit_code: int) -> float:
    """Convert a length from ETABS model units to mm."""
    # unit_code: 1=lb-in, 2=lb-ft, 3=kip-in, 4=kip-ft,
    #            5=kN-mm, 6=kN-m, 7=kgf-mm, 8=kgf-m,
    #            9=N-mm, 10=N-m, 11=tonf-mm, 12=tonf-m
    if unit_code in (1, 3):     return value * 25.4        # inches → mm
    if unit_code in (2, 4):     return value * 304.8       # feet → mm
    if unit_code in (6, 8, 10, 12): return value * 1000.0  # m → mm
    return value                                             # already mm


def _mm_to_length(mm: float, unit_code: int) -> float:
    """Convert mm back to ETABS model length units."""
    if unit_code in (1, 3):     return mm / 25.4
    if unit_code in (2, 4):     return mm / 304.8
    if unit_code in (6, 8, 10, 12): return mm / 1000.0
    return mm


def get_frame_materials():
    """Return all material names defined in the active ETABS model."""
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}
    try:
        ret = SapModel.PropMaterial.GetNameList()
        names = list(ret[1]) if ret and len(ret) > 1 else []
        return {"status": "success", "materials": names}
    except Exception as e:
        return {"error": f"Failed to get materials: {str(e)}"}


def get_rc_beam_sections():
    """
    Import all rectangular frame sections from ETABS.
    Returns geometry, rebar basics, and stiffness modifiers.
    """
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}
    try:
        # Determine model length units for conversion
        try:
            unit_code = SapModel.GetDatabaseUnits()
        except Exception:
            try:
                unit_code = SapModel.GetPresentUnits()
            except Exception:
                unit_code = 6  # default: kN-m

        # Get all frame section names
        ret = SapModel.PropFrame.GetNameList()
        if not ret or len(ret) < 2:
            return {"error": "No frame sections found in ETABS model."}
        all_names = list(ret[1])

        sections = []
        num = 1
        for name in all_names:
            try:
                # comtypes returns: ['', MatProp, t3, t2, Color, Notes, GUID, retcode]
                r = SapModel.PropFrame.GetRectangle(name)
                if not r or int(r[-1]) != 0:
                    continue  # not a rectangular (or any valid) section

                sMat  = str(r[1])     # material name at index 1
                t3    = float(r[2])   # depth in model units at index 2
                t2    = float(r[3])   # width in model units at index 3
                depth_mm = round(_length_to_mm(t3, unit_code))
                width_mm = round(_length_to_mm(t2, unit_code))

                # Stiffness modifiers [area, S22, S33, torsion, I22, I33, mass, weight]
                torsion_mod, i22_mod, i33_mod = 0.01, 0.35, 0.35
                # comtypes: GetModifiers(name) → ['', [8 values], retcode] or [[8 values], retcode]
                try:
                    mr = SapModel.PropFrame.GetModifiers(name)
                    if mr and int(mr[-1]) == 0:
                        # Find the array of 8 modifiers — it's the first iterable element
                        mods = None
                        for item in mr[:-1]:
                            if hasattr(item, '__iter__') and not isinstance(item, str):
                                mods = list(item)
                                break
                        if mods and len(mods) >= 6:
                            torsion_mod = round(float(mods[3]), 4)
                            i22_mod     = round(float(mods[4]), 4)
                            i33_mod     = round(float(mods[5]), 4)
                except Exception:
                    pass

                # comtypes: GetRebarBeam(name) → ['', MatRebar, MatRebarShr, CoverTop, CoverBot, ..., retcode]
                fy_main, fy_ties, bar_dia = "", "", 25
                top_cc, bot_cc = 40, 40
                try:
                    rr = SapModel.PropFrame.GetRebarBeam(name)
                    if rr and int(rr[-1]) == 0 and len(rr) >= 5:
                        # Skip leading empty string at index 0; strings=materials, floats=cover
                        str_vals   = [v for v in rr[:-1] if isinstance(v, str) and v]
                        float_vals = [v for v in rr[:-1] if isinstance(v, float)]
                        if str_vals:
                            fy_main = str_vals[0]
                        if len(str_vals) >= 2:
                            fy_ties = str_vals[1]
                        if len(float_vals) >= 2:
                            top_cc = round(_length_to_mm(float_vals[0], unit_code))
                            bot_cc = round(_length_to_mm(float_vals[1], unit_code))
                except Exception:
                    pass

                sections.append({
                    "num":              num,
                    "material":         sMat,
                    "prop_name":        name,
                    "concrete_strength": sMat,
                    "fy_main":          fy_main,
                    "fy_ties":          fy_ties,
                    "depth":            depth_mm,
                    "width":            width_mm,
                    "bar_dia":          bar_dia,
                    "top_cc":           top_cc,
                    "bot_cc":           bot_cc,
                    "nbar_top_i":       0,
                    "nbar_top_j":       0,
                    "nbar_bot_i":       0,
                    "nbar_bot_j":       0,
                    "torsion":          torsion_mod,
                    "i22":              i22_mod,
                    "i33":              i33_mod,
                })
                num += 1
            except Exception:
                continue

        return {"status": "success", "sections": sections}

    except Exception as e:
        return {"error": f"Failed to import beam sections: {str(e)}"}


def get_rc_column_sections():
    """
    Import all rectangular frame sections from ETABS as column data.
    Returns geometry, column rebar basics, and stiffness modifiers.
    """
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}
    try:
        # Determine model length units for conversion
        try:
            unit_code = SapModel.GetDatabaseUnits()
        except Exception:
            try:
                unit_code = SapModel.GetPresentUnits()
            except Exception:
                unit_code = 6  # default: kN-m

        # ── Build a set of section names actually used by column frame objects ──
        # GetDesignOrientation returns (orientation, retcode) where 1=Column.
        # This filters out rectangular beam/brace sections that also happen to
        # have GetRebarColumn data (which can happen in ETABS when a beam section
        # was previously used for columns or was defined with column rebar).
        column_sections: set = set()
        try:
            fr_ret = SapModel.FrameObj.GetNameList()
            if fr_ret and int(fr_ret[-1]) == 0 and fr_ret[1]:
                for _fn in fr_ret[1]:
                    try:
                        _fn = str(_fn)
                        _ori = SapModel.FrameObj.GetDesignOrientation(_fn)
                        # Returns (DesignOrientation, retcode)
                        if not _ori or int(_ori[-1]) != 0:
                            continue
                        if int(_ori[0]) == 1:   # 1 = Column
                            _sec = SapModel.FrameObj.GetSection(_fn)
                            if _sec and int(_sec[-1]) == 0:
                                column_sections.add(str(_sec[0]))
                    except Exception:
                        continue
        except Exception:
            pass
        _filter_by_frame = bool(column_sections)  # False → fall back, include all rebar sections

        # Get all frame section names
        ret = SapModel.PropFrame.GetNameList()
        if not ret or len(ret) < 2:
            return {"error": "No frame sections found in ETABS model."}
        all_names = list(ret[1])

        sections = []
        num = 1
        for name in all_names:
            try:
                # Check if rectangular section
                r = SapModel.PropFrame.GetRectangle(name)
                if not r or int(r[-1]) != 0:
                    continue

                sMat     = str(r[1])
                t3       = float(r[2])   # depth in model units
                t2       = float(r[3])   # width in model units
                depth_mm = round(_length_to_mm(t3, unit_code))
                width_mm = round(_length_to_mm(t2, unit_code))

                # Stiffness modifiers [area, S22, S33, torsion, I22, I33, mass, weight]
                torsion_mod, i22_mod, i33_mod = 0.01, 0.70, 0.70
                try:
                    mr = SapModel.PropFrame.GetModifiers(name)
                    if mr and int(mr[-1]) == 0:
                        mods = None
                        for item in mr[:-1]:
                            if hasattr(item, '__iter__') and not isinstance(item, str):
                                mods = list(item)
                                break
                        if mods and len(mods) >= 6:
                            torsion_mod = round(float(mods[3]), 4)
                            i22_mod     = round(float(mods[4]), 4)
                            i33_mod     = round(float(mods[5]), 4)
                except Exception:
                    pass

                # GetRebarColumn — positional parse (type-based filtering is
                # unreliable because comtypes may return ints as floats).
                #
                # ETABS comtypes return order (after optional leading ''):
                #  0: MatRebar      (str)
                #  1: MatRebarConf  (str)
                #  2: Pattern       (int)   1=Rect, 2=Circ
                #  3: ConfineType   (int)   1=Ties, 2=Spiral
                #  4: Cover         (float) model-units
                #  5: CoverTo       (float) model-units
                #  6: ToBeDesigned  (bool/int)
                #  7: NumBars2Dir   (int)
                #  8: NumBars3Dir   (int)
                #  9: RebarSize     (str)
                # 10: TieSize       (str)
                # 11: TieSpacing    (float) model-units
                # 12: NumTieBarsD2  (int)
                # 13: NumTieBarsD3  (int)
                # ── Only include sections that have RC column rebar defined ──────
                # GetRebarColumn must succeed (retcode == 0) AND have a valid
                # rebar material name.  Rectangular beam/steel sections that were
                # never assigned column rebar will either fail or return retcode≠0,
                # and are skipped so the picker only shows true RC columns.
                fy_main, fy_ties = "", ""
                cover_mm, tie_spacing_mm = 40, 150
                rebar_size, tie_size = "", ""
                nbars_3, nbars_2 = 3, 3
                num_tie_3, num_tie_2 = 3, 3
                to_be_designed = False
                _rebar_ok = False
                _log_rebar = (num <= 3)   # log first 3 sections to structiq.log for debug
                try:
                    cr = SapModel.PropFrame.GetRebarColumn(name)
                    if cr and int(cr[-1]) == 0:
                        # Strip retcode, then strip optional leading empty string
                        vals = list(cr[:-1])
                        if vals and isinstance(vals[0], str) and vals[0].strip() == '':
                            vals = vals[1:]

                        # ── Diagnostic: log raw vals for the first few sections ──
                        if _log_rebar:
                            try:
                                import sys as _s, os as _o
                                _lp = _o.path.join(
                                    _o.path.dirname(_s.executable)
                                    if getattr(_s, 'frozen', False)
                                    else _o.path.dirname(_o.path.abspath(__file__)),
                                    '..', 'structiq.log')
                                with open(_lp, 'a', encoding='utf-8') as _lf:
                                    _lf.write(
                                        f'[GetRebarColumn] {name}: len={len(vals)} '
                                        f'vals={[repr(v) for v in vals]}\n')
                            except Exception:
                                pass

                        # Actual ETABS return order (confirmed from diagnostic log):
                        # [0]=MatRebar  [1]=MatRebarConf  [2]=Pattern  [3]=ConfineType
                        # [4]=Cover     [5]=CoverTo
                        # [6]=NumBars2Dir  [7]=NumBars3Dir  [8]=RebarSize
                        # [9]=TieSize   [10]=TieSpacing
                        # [11]=NumTieBarsD2  [12]=NumTieBarsD3  [13]=ToBeDesigned
                        if len(vals) >= 11:
                            fy_main        = str(vals[0]) if vals[0] else ''
                            fy_ties        = str(vals[1]) if vals[1] else ''
                            # vals[2]=Pattern, vals[3]=ConfineType (skip)
                            cover_mm       = round(_length_to_mm(float(vals[4]), unit_code))
                            # vals[5]=CoverTo (skip)
                            nbars_2        = int(round(float(vals[6])))   # NumBars2Dir: side-face bars
                            nbars_3        = int(round(float(vals[7])))   # NumBars3Dir: top/bot-face bars
                            rebar_size     = str(vals[8])  if vals[8]  else ''
                            tie_size       = str(vals[9])  if vals[9]  else ''
                            tie_spacing_mm = round(_length_to_mm(float(vals[10]), unit_code))
                        if len(vals) >= 13:
                            num_tie_2 = int(round(float(vals[11])))
                            num_tie_3 = int(round(float(vals[12])))
                        if len(vals) >= 14:
                            to_be_designed = bool(vals[13])
                        if fy_main:
                            # Must have a rebar material to count as an RC column
                            _rebar_ok = True
                except Exception:
                    pass

                # Skip any rectangular section that has no column rebar assignment
                if not _rebar_ok:
                    continue

                # Skip sections not actually used by any column frame in the model
                if _filter_by_frame and name not in column_sections:
                    continue

                sections.append({
                    "num":              num,
                    "material":         sMat,
                    "prop_name":        name,
                    "concrete_strength": sMat,
                    "fy_main":          fy_main,
                    "fy_ties":          fy_ties,
                    "depth":            depth_mm,
                    "width":            width_mm,
                    "cover":            cover_mm,
                    "rebar_size":       rebar_size,
                    "nbars_3":          nbars_3,
                    "nbars_2":          nbars_2,
                    "tie_size":         tie_size,
                    "tie_spacing":      tie_spacing_mm,
                    "num_tie_3":        num_tie_3,
                    "num_tie_2":        num_tie_2,
                    "to_be_designed":   to_be_designed,
                    "torsion":          torsion_mod,
                    "i22":              i22_mod,
                    "i33":              i33_mod,
                })
                num += 1
            except Exception:
                continue

        return {"status": "success", "sections": sections}

    except Exception as e:
        return {"error": f"Failed to import column sections: {str(e)}"}


def write_rc_column_sections(sections: list):
    """
    Create or update rectangular RC column sections in ETABS.
    Sets geometry (SetRectangle), stiffness modifiers, and rebar (SetRebarColumn).
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

        try:
            if SapModel.GetModelIsLocked():
                SapModel.SetModelIsLocked(False)
        except Exception:
            pass

        results = []
        for sec in sections:
            name = str(sec.get("prop_name", "")).strip()
            if not name:
                continue

            mat              = str(sec.get("concrete_strength") or sec.get("material", "")).strip()
            fy_main          = str(sec.get("fy_main", "")).strip()
            fy_ties          = str(sec.get("fy_ties", "")).strip()
            depth_mm         = float(sec.get("depth",        500))
            width_mm         = float(sec.get("width",        500))
            cover_mm         = float(sec.get("cover",         40))
            rebar_size       = str(sec.get("rebar_size",      "")).strip()
            nbars_3          = int(sec.get("nbars_3",           3))
            nbars_2          = int(sec.get("nbars_2",           3))
            tie_size         = str(sec.get("tie_size",        "")).strip()
            tie_spacing_mm   = float(sec.get("tie_spacing",  150))
            num_tie_3        = int(sec.get("num_tie_3",         3))
            num_tie_2        = int(sec.get("num_tie_2",         3))
            to_be_designed   = bool(sec.get("to_be_designed", False))
            torsion          = float(sec.get("torsion",      0.01))
            i22              = float(sec.get("i22",          0.70))
            i33              = float(sec.get("i33",          0.70))

            t3 = _mm_to_length(depth_mm, unit_code)
            t2 = _mm_to_length(width_mm, unit_code)

            try:
                ret = SapModel.PropFrame.SetRectangle(name, mat, t3, t2, -1, "", "")
                if int(ret) != 0:
                    results.append({"name": name, "status": "error",
                                    "reason": f"SetRectangle returned {ret}"})
                    continue

                # Stiffness modifiers
                mods = [1.0, 1.0, 1.0, torsion, i22, i33, 1.0, 1.0]
                SapModel.PropFrame.SetModifiers(name, mods)

                # Column rebar
                if fy_main or fy_ties:
                    try:
                        long_mat   = fy_main or fy_ties
                        conf_mat   = fy_ties or fy_main
                        cover_len  = _mm_to_length(cover_mm, unit_code)
                        # CoverTo = cover to bar center (approx cover + half bar dia, use cover*1.5)
                        cover_to   = cover_len * 1.5
                        spacing    = _mm_to_length(tie_spacing_mm, unit_code)
                        # SetRebarColumn(Name, MatRebar, MatRebarConf, Pattern, ConfineType,
                        #                Cover, CoverTo, ToBeDesigned, NumBars2Dir, NumBars3Dir,
                        #                RebarSize, TieSize, TieSpacing, NumTieBarsD2, NumTieBarsD3)
                        SapModel.PropFrame.SetRebarColumn(
                            name, long_mat, conf_mat,
                            1,          # Pattern=1 (Rectangular)
                            1,          # ConfineType=1 (Ties)
                            cover_len, cover_to,
                            to_be_designed,
                            nbars_2, nbars_3,
                            rebar_size, tie_size, spacing,
                            num_tie_2, num_tie_3
                        )
                    except Exception:
                        pass

                results.append({"name": name, "status": "success"})

            except Exception as e:
                results.append({"name": name, "status": "error", "reason": str(e)})

        success = sum(1 for r in results if r["status"] == "success")
        errors  = sum(1 for r in results if r["status"] == "error")

        return {
            "status":        "success",
            "message":       f"Wrote {success} column section(s) to ETABS. {errors} error(s).",
            "results":       results,
            "success_count": success,
            "error_count":   errors,
        }

    except Exception as e:
        return {"error": f"Failed to write column sections: {str(e)}"}


def debug_rc_column_raw(section_name: str):
    """
    Return the raw comtypes tuple from GetRebarColumn for a single section,
    annotated with positional indices, types, and converted mm values.
    Useful for diagnosing unit/parsing issues.
    """
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}
    try:
        try:
            unit_code = SapModel.GetDatabaseUnits()
        except Exception:
            unit_code = 6

        unit_names = {
            1:'lb-in', 2:'lb-ft', 3:'kip-in', 4:'kip-ft',
            5:'kN-mm', 6:'kN-m', 7:'kgf-mm', 8:'kgf-m',
            9:'N-mm', 10:'N-m', 11:'tonf-mm', 12:'tonf-m'
        }

        # GetRectangle raw
        rect_raw = None
        try:
            r = SapModel.PropFrame.GetRectangle(section_name)
            rect_raw = {
                "repr": repr(r)[:400],
                "unit_code": unit_code,
                "unit_name": unit_names.get(unit_code, f"unknown({unit_code})"),
                "items": {str(i): {"val": repr(r[i])[:60], "type": type(r[i]).__name__}
                          for i in range(len(r))} if hasattr(r, '__len__') else {},
            }
            if r and int(r[-1]) == 0:
                rect_raw["t3_raw"]    = float(r[2])
                rect_raw["t2_raw"]    = float(r[3])
                rect_raw["depth_mm"]  = round(_length_to_mm(float(r[2]), unit_code))
                rect_raw["width_mm"]  = round(_length_to_mm(float(r[3]), unit_code))
        except Exception as e:
            rect_raw = {"error": str(e)}

        # GetRebarColumn raw
        rebar_raw = None
        parsed = {}
        try:
            cr = SapModel.PropFrame.GetRebarColumn(section_name)
            rebar_raw = {
                "repr": repr(cr)[:600],
                "items": {str(i): {"val": repr(cr[i])[:80], "type": type(cr[i]).__name__}
                          for i in range(len(cr))} if hasattr(cr, '__len__') else {},
            }
            if cr and int(cr[-1]) == 0:
                vals = list(cr[:-1])
                if vals and isinstance(vals[0], str) and vals[0].strip() == '':
                    vals = vals[1:]
                parsed["stripped_vals"] = [
                    {"idx": i, "val": repr(v)[:60], "type": type(v).__name__}
                    for i, v in enumerate(vals)
                ]
                if len(vals) >= 12:
                    parsed["MatRebar"]     = str(vals[0])
                    parsed["MatRebarConf"] = str(vals[1])
                    parsed["Pattern"]      = repr(vals[2])
                    parsed["ConfineType"]  = repr(vals[3])
                    parsed["Cover_raw"]    = repr(vals[4])
                    parsed["Cover_mm"]     = round(_length_to_mm(float(vals[4]), unit_code))
                    parsed["CoverTo_raw"]  = repr(vals[5])
                    parsed["ToBeDesigned"] = repr(vals[6])
                    parsed["NumBars2"]     = int(round(float(vals[7])))
                    parsed["NumBars3"]     = int(round(float(vals[8])))
                    parsed["RebarSize"]    = str(vals[9])
                    parsed["TieSize"]      = str(vals[10])
                    parsed["TieSpacing_raw"] = repr(vals[11])
                    parsed["TieSpacing_mm"]  = round(_length_to_mm(float(vals[11]), unit_code))
                if len(vals) >= 14:
                    parsed["NumTieBarsD2"] = int(round(float(vals[12])))
                    parsed["NumTieBarsD3"] = int(round(float(vals[13])))
        except Exception as e:
            rebar_raw = {"error": str(e)}

        return {
            "status":     "success",
            "section":    section_name,
            "unit_code":  unit_code,
            "unit_name":  unit_names.get(unit_code, f"unknown({unit_code})"),
            "GetRectangle": rect_raw,
            "GetRebarColumn_raw": rebar_raw,
            "GetRebarColumn_parsed": parsed,
        }
    except Exception as e:
        return {"error": f"Debug failed: {str(e)}"}


def debug_all_frame_sections():
    """Return all frame section names and whether GetRectangle succeeds for each."""
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}
    try:
        try:
            unit_code = SapModel.GetDatabaseUnits()
        except Exception:
            unit_code = 6

        ret = SapModel.PropFrame.GetNameList()
        if not ret or len(ret) < 2:
            return {"status": "success", "all_sections": [], "total": 0}
        all_names = list(ret[1])

        results = []
        sample_raw = {}  # raw return values for first 5 sections
        for name in all_names:
            info = {"name": name, "is_rect": False, "rect_error": None, "t3": None, "t2": None}
            try:
                # comtypes: pass [in] param only; [out] params returned in tuple
                r = SapModel.PropFrame.GetRectangle(name)
                retcode = int(r[-1]) if r else -1
                info["rect_retcode"] = retcode
                # Capture raw repr for first 5 sections
                if len(sample_raw) < 5:
                    sample_raw[name] = {
                        "repr": repr(r)[:300],
                        "len": len(r) if hasattr(r, '__len__') else None,
                        "indices": {str(i): repr(r[i])[:60] for i in range(len(r))} if hasattr(r, '__len__') else {}
                    }
                if retcode == 0:
                    info["is_rect"] = True
                    # Find numeric t3 and t2 among the tuple elements
                    floats = [(i, v) for i, v in enumerate(r[:-1]) if isinstance(v, (int, float)) and not isinstance(v, bool)]
                    if len(floats) >= 2:
                        info["t3"] = float(floats[0][1])
                        info["t2"] = float(floats[1][1])
                        info["t3_idx"] = floats[0][0]
                        info["t2_idx"] = floats[1][0]
                        info["depth_mm"] = round(_length_to_mm(float(floats[0][1]), unit_code))
                        info["width_mm"] = round(_length_to_mm(float(floats[1][1]), unit_code))
                else:
                    info["rect_error"] = f"retcode={retcode}"
            except Exception as ex:
                info["rect_error"] = str(ex)
                if len(sample_raw) < 5:
                    sample_raw[name] = {"exception": str(ex)}
            results.append(info)

        return {
            "status": "success",
            "unit_code": unit_code,
            "total": len(results),
            "sample_raw": sample_raw,
            "rectangular": [r for r in results if r["is_rect"]],
            "non_rectangular": [r["name"] for r in results if not r["is_rect"]],
        }
    except Exception as e:
        return {"error": f"Debug failed: {str(e)}"}


def write_rc_beam_sections(sections: list):
    """
    Create or update rectangular RC beam sections in ETABS.
    Sets geometry (SetRectangle), stiffness modifiers, and rebar cover (SetRebarBeam).
    """
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}
    try:
        # Determine model units
        try:
            unit_code = SapModel.GetDatabaseUnits()
        except Exception:
            try:
                unit_code = SapModel.GetPresentUnits()
            except Exception:
                unit_code = 6  # default kN-m

        # Unlock model if locked
        try:
            if SapModel.GetModelIsLocked():
                SapModel.SetModelIsLocked(False)
        except Exception:
            pass

        results = []
        for sec in sections:
            name = str(sec.get("prop_name", "")).strip()
            if not name:
                continue

            mat       = str(sec.get("concrete_strength") or sec.get("material", "")).strip()
            fy_main   = str(sec.get("fy_main", "")).strip()
            fy_ties   = str(sec.get("fy_ties", "")).strip()
            depth_mm  = float(sec.get("depth", 400))
            width_mm  = float(sec.get("width", 300))
            top_cc_mm = float(sec.get("top_cc", 40))
            bot_cc_mm = float(sec.get("bot_cc", 40))
            torsion   = float(sec.get("torsion", 0.01))
            i22       = float(sec.get("i22",     0.35))
            i33       = float(sec.get("i33",     0.35))

            t3 = _mm_to_length(depth_mm, unit_code)
            t2 = _mm_to_length(width_mm, unit_code)

            try:
                # Create / overwrite rectangular section
                ret = SapModel.PropFrame.SetRectangle(name, mat, t3, t2, -1, "", "")
                if int(ret) != 0:
                    results.append({"name": name, "status": "error",
                                    "reason": f"SetRectangle returned {ret}"})
                    continue

                # Stiffness modifiers: [area, S22, S33, torsion, I22, I33, mass, weight]
                mods = [1.0, 1.0, 1.0, torsion, i22, i33, 1.0, 1.0]
                SapModel.PropFrame.SetModifiers(name, mods)

                # Rebar (only if material specified)
                if fy_main or fy_ties:
                    try:
                        rebar_mat = fy_main or fy_ties
                        tie_mat   = fy_ties or fy_main
                        top_cov   = _mm_to_length(top_cc_mm, unit_code)
                        bot_cov   = _mm_to_length(bot_cc_mm, unit_code)
                        # SetRebarBeam(Name, MatRebar, MatRebarShr, CoverTop, CoverBot,
                        #              AreaTop_I, AreaTop_J, AreaBot_I, AreaBot_J)
                        SapModel.PropFrame.SetRebarBeam(
                            name, rebar_mat, tie_mat, top_cov, bot_cov,
                            0.0, 0.0, 0.0, 0.0
                        )
                    except Exception:
                        pass  # rebar optional; continue without

                results.append({"name": name, "status": "success"})

            except Exception as e:
                results.append({"name": name, "status": "error", "reason": str(e)})

        success = sum(1 for r in results if r["status"] == "success")
        errors  = sum(1 for r in results if r["status"] == "error")

        return {
            "status":        "success",
            "message":       f"Wrote {success} section(s) to ETABS. {errors} error(s).",
            "results":       results,
            "success_count": success,
            "error_count":   errors,
        }

    except Exception as e:
        return {"error": f"Failed to write beam sections: {str(e)}"}


# ─── PMM / Batch helpers ──────────────────────────────────────────────────────

def _stress_to_mpa(v: float, unit_code: int) -> float:
    """Convert stress from ETABS model units to MPa."""
    if unit_code in (1, 2):   return v * 6.89476e-3   # psi → MPa
    if unit_code in (3, 4):   return v * 6.89476       # ksi → MPa
    if unit_code == 5:        return v * 1e3            # kN/mm² → MPa
    if unit_code == 6:        return v * 1e-3           # kN/m²  → MPa
    if unit_code in (7,):     return v * 9.80665        # kgf/mm² → MPa
    if unit_code in (8,):     return v * 9.80665e-3     # kgf/m²  → MPa
    if unit_code in (9, 10):  return v                  # N/mm² = MPa
    if unit_code in (11, 12): return v * 9.80665        # tonf/mm² or tonf/m²
    return v


def _force_to_kN(v: float, unit_code: int) -> float:
    """Convert force from ETABS model units to kN."""
    if unit_code in (1, 2):   return v * 4.44822e-3    # lb  → kN
    if unit_code in (3, 4):   return v * 4.44822        # kip → kN
    if unit_code in (5, 6):   return v                  # kN  → kN
    if unit_code in (7, 8):   return v * 9.80665e-3     # kgf → kN
    if unit_code in (9, 10):  return v * 1e-3           # N   → kN
    if unit_code in (11, 12): return v * 9.80665        # tonf → kN
    return v


def _moment_to_kNm(v: float, unit_code: int) -> float:
    """Convert moment from ETABS model units to kN·m."""
    if unit_code == 1:   return v * 0.000112985         # lb·in   → kN·m
    if unit_code == 2:   return v * 0.00135582          # lb·ft   → kN·m
    if unit_code == 3:   return v * 0.112985            # kip·in  → kN·m
    if unit_code == 4:   return v * 1.35582             # kip·ft  → kN·m
    if unit_code == 5:   return v * 0.001               # kN·mm   → kN·m
    if unit_code == 6:   return v                       # kN·m    → kN·m
    if unit_code == 7:   return v * 9.80665e-6          # kgf·mm  → kN·m
    if unit_code == 8:   return v * 9.80665e-3          # kgf·m   → kN·m
    if unit_code == 9:   return v * 1e-6                # N·mm    → kN·m
    if unit_code == 10:  return v * 1e-3                # N·m     → kN·m
    if unit_code == 11:  return v * 9.80665e-3          # tonf·mm → kN·m
    if unit_code == 12:  return v * 9.80665             # tonf·m  → kN·m
    return v


def get_etabs_combos() -> dict:
    """Return all load combinations and load cases from the active ETABS model."""
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}
    try:
        combinations = []
        try:
            c_ret = SapModel.RespCombo.GetNameList()
            if c_ret and int(c_ret[-1]) == 0 and c_ret[1]:
                combinations = list(c_ret[1])
        except Exception:
            pass
        cases = []
        try:
            lc_ret = SapModel.LoadCases.GetNameList()
            if lc_ret and int(lc_ret[-1]) == 0 and lc_ret[1]:
                cases = [n for n in lc_ret[1] if n and not str(n).startswith("~")]
        except Exception:
            pass
        return {"combinations": combinations, "cases": cases}
    except Exception as e:
        return {"error": str(e)}


def get_etabs_frame_forces(combo_names: list, load_type: str = "combo") -> dict:
    """
    Get frame forces for currently selected column frames in ETABS.
    Returns {results: [{label, P_kN, M3_kNm, M2_kNm}]}
    """
    import math as _math
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

        # Setup output cases
        SapModel.Results.Setup.DeselectAllCasesAndCombosForOutput()
        for cname in combo_names:
            try:
                if load_type == "case":
                    SapModel.Results.Setup.SetCaseSelectedForOutput(cname, True)
                else:
                    SapModel.Results.Setup.SetComboSelectedForOutput(cname, True)
            except Exception:
                continue

        # Collect selected frames by iterating all frames
        selected_frames = []
        try:
            all_ret = SapModel.FrameObj.GetNameList()
            if all_ret and int(all_ret[-1]) == 0 and all_ret[1]:
                for frame in all_ret[1]:
                    try:
                        sel_ret = SapModel.FrameObj.GetSelected(str(frame))
                        if sel_ret and sel_ret[0]:
                            selected_frames.append(str(frame))
                    except Exception:
                        continue
        except Exception:
            pass

        if not selected_frames:
            return {"error": "No column frames selected in ETABS. Select frames first."}

        results = []
        for frame in selected_frames:
            try:
                ret = SapModel.Results.FrameForce(frame, 0)
                if not ret or int(ret[-1]) != 0 or int(ret[0]) == 0:
                    continue
                n = int(ret[0])
                # ETABS FrameForce return layout (with ObjSta + ElmSta):
                # [0]=N, [1]=Obj, [2]=ObjSta, [3]=Elm, [4]=ElmSta,
                # [5]=ACase, [6]=StepType, [7]=StepNum,
                # [8]=P, [9]=V2, [10]=V3, [11]=T, [12]=M2, [13]=M3, [14]=retcode
                load_cases = ret[5]
                P_arr  = ret[8]
                M2_arr = ret[12]
                M3_arr = ret[13]

                # Per load case: take station with max resultant moment
                case_best: dict = {}
                for i in range(n):
                    case = str(load_cases[i])
                    P  = _force_to_kN(float(P_arr[i]),  unit_code)
                    M3 = _moment_to_kNm(float(M3_arr[i]), unit_code)
                    M2 = _moment_to_kNm(float(M2_arr[i]), unit_code)
                    Md = _math.sqrt(M3**2 + M2**2)
                    existing = case_best.get(case)
                    if existing is None or Md > existing[3]:
                        case_best[case] = (P, M3, M2, Md)

                for case, (P, M3, M2, _) in case_best.items():
                    results.append({
                        "label":   f"{frame} / {case}",
                        "P_kN":   round(P,  2),
                        "M3_kNm": round(M3, 2),
                        "M2_kNm": round(M2, 2),
                    })
            except Exception:
                continue

        if not results:
            return {"error": "No force results returned. Ensure the model has been analyzed."}
        return {"results": results}
    except Exception as e:
        return {"error": str(e)}


def get_etabs_all_column_forces(section_names: list,
                                 combo_names: list,
                                 load_type: str = "combo") -> dict:
    """
    Get frame forces for ALL column frames that are assigned to any section in
    section_names, under the requested load combinations / cases.

    Returns {sections: {section_name: [{frame, combo, P_kN, M3_kNm, M2_kNm}]}}
    M3 = strong-axis moment (→ Mx in PMM),  M2 = weak-axis moment (→ My in PMM).
    For each (frame, combo) pair only the station with the largest resultant
    moment is kept.
    """
    import math as _math
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

        # 1. Map frame → section name (only for matching sections)
        section_set = set(section_names)
        frames_by_section: dict = {}

        try:
            all_ret = SapModel.FrameObj.GetNameList()
            if all_ret and int(all_ret[-1]) == 0 and all_ret[1]:
                for frame in all_ret[1]:
                    try:
                        sec_ret = SapModel.FrameObj.GetSection(str(frame))
                        # Returns (PropName, SAuto, retcode) or (PropName, retcode)
                        if not sec_ret or int(sec_ret[-1]) != 0:
                            continue
                        sec_name = str(sec_ret[0])
                        if sec_name in section_set:
                            frames_by_section.setdefault(sec_name, []).append(str(frame))
                    except Exception:
                        continue
        except Exception as e:
            return {"error": f"Cannot retrieve frame list: {e}"}

        if not frames_by_section:
            return {"error": "No column frames found matching the given section names."}

        # 2. Setup output cases
        SapModel.Results.Setup.DeselectAllCasesAndCombosForOutput()
        for cname in combo_names:
            try:
                if load_type == "case":
                    SapModel.Results.Setup.SetCaseSelectedForOutput(cname, True)
                else:
                    SapModel.Results.Setup.SetComboSelectedForOutput(cname, True)
            except Exception:
                continue

        # 3. Extract forces per section
        by_section: dict = {}
        for sec_name, frames in frames_by_section.items():
            rows = []
            for frame in frames:
                try:
                    ret = SapModel.Results.FrameForce(frame, 0)  # 0 = ObjectElm
                    if not ret or int(ret[-1]) != 0 or int(ret[0]) == 0:
                        continue
                    n = int(ret[0])
                    # ETABS FrameForce layout (with ObjSta + ElmSta):
                    # [0]=N, [1]=Obj, [2]=ObjSta, [3]=Elm, [4]=ElmSta,
                    # [5]=ACase, [6]=StepType, [7]=StepNum,
                    # [8]=P, [9]=V2, [10]=V3, [11]=T, [12]=M2, [13]=M3, [14]=retcode
                    load_cases = ret[5]
                    P_arr  = ret[8]
                    M2_arr = ret[12]
                    M3_arr = ret[13]

                    # Per load case: keep station with max resultant moment
                    case_best: dict = {}
                    for i in range(n):
                        case = str(load_cases[i])
                        P  = _force_to_kN(float(P_arr[i]),   unit_code)
                        M3 = _moment_to_kNm(float(M3_arr[i]), unit_code)
                        M2 = _moment_to_kNm(float(M2_arr[i]), unit_code)
                        Md = _math.sqrt(M3**2 + M2**2)
                        existing = case_best.get(case)
                        if existing is None or Md > existing[3]:
                            case_best[case] = (P, M3, M2, Md)

                    for case, (P, M3, M2, _) in case_best.items():
                        rows.append({
                            "frame":  frame,
                            "combo":  case,
                            "P_kN":   round(P,  2),
                            "M3_kNm": round(M3, 2),
                            "M2_kNm": round(M2, 2),
                        })
                except Exception:
                    continue
            by_section[sec_name] = rows

        return {"sections": by_section}
    except Exception as e:
        return {"error": str(e)}
