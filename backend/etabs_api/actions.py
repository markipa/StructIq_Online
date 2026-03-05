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
