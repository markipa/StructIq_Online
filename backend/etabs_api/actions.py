import os
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

    _KN_M = 6   # ETABS unit code: kN, m, kN·m
    try:
        orig_units = SapModel.GetPresentUnits()
    except Exception:
        orig_units = _KN_M
    try:
        SapModel.SetPresentUnits(_KN_M)   # forces → kN, moments → kN·m
    except Exception:
        pass

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
            _restore_units(SapModel, orig_units)
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
                _restore_units(SapModel, orig_units)
                return {"status": "success", "data": [], "available_cases": available}
            else:
                all_combos = list(SapModel.RespCombo.GetNameList()[1])
                _restore_units(SapModel, orig_units)
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
            _restore_units(SapModel, orig_units)
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
            _restore_units(SapModel, orig_units)
            return {"status": "success", "data": data, "available_cases": available}
        else:
            all_combos = list(SapModel.RespCombo.GetNameList()[1])
            _restore_units(SapModel, orig_units)
            return {"status": "success", "data": data, "available_combos": all_combos}

    except Exception as e:
        try:
            _restore_units(SapModel, orig_units)
        except Exception:
            pass
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
    Returns global base reactions from ETABS (kN·m), matching the ETABS
    'Base Reactions' display table exactly — including step-by-step results.

    Strategy:
      PRIMARY  — DatabaseTables "Base Reactions" (per-joint rows).
                 Group + sum over all joints for each (case, case_type, step_type,
                 step_num) key → global total per step.  This is identical to what
                 ETABS does internally for its own display.  Correctly returns
                 Step-by-Step steps, RS Max rows, and single-step LinStatic rows.
                 Rows with step_type == "Mode" (individual RS modal contributions,
                 not the final CQC/SRSS result) are skipped.

      FALLBACK — Results.BaseReact() when table is empty or unavailable.
                 BaseReact() returns envelope (Max/Min) for multi-step cases but
                 is always correct for single-step LinStatic and RS combined results.

    load_type : 'combo' (default) | 'case'
    combo_name: optional — filter to single named item.
    """
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}

    _KN_M = 6
    try:
        orig_units = SapModel.GetPresentUnits()
    except Exception:
        orig_units = _KN_M
    try:
        SapModel.SetPresentUnits(_KN_M)
    except Exception:
        pass

    try:
        # ── 1. Select cases/combos for output ──────────────────────────
        SapModel.Results.Setup.DeselectAllCasesAndCombosForOutput()

        if combo_name:
            if load_type == "case":
                SapModel.Results.Setup.SetCaseSelectedForOutput(combo_name, True)
            else:
                SapModel.Results.Setup.SetComboSelectedForOutput(combo_name, True)
        else:
            if load_type == "case":
                nr = SapModel.LoadCases.GetNameList()
                if nr and int(nr[-1]) == 0:
                    for n in list(nr[1]):
                        if not str(n).startswith("~"):
                            SapModel.Results.Setup.SetCaseSelectedForOutput(str(n), True)
            else:
                nr = SapModel.RespCombo.GetNameList()
                if nr and int(nr[-1]) == 0:
                    for n in list(nr[1]):
                        if not str(n).startswith("~"):
                            SapModel.Results.Setup.SetComboSelectedForOutput(str(n), True)

        # ── 2. PRIMARY: DatabaseTables "Base Reactions" ─────────────────
        #   Returns per-support-joint rows.  Sum over joints per unique key
        #   to obtain the global structure total — same as ETABS display.
        data = []
        tbl = SapModel.DatabaseTables.GetTableForDisplayArray(
            "Base Reactions", [], "All", 1, [], 0, []
        )

        if tbl and int(tbl[5]) == 0 and int(tbl[3]) > 0:
            fields = [str(f).strip() for f in list(tbl[2])]
            n_rec  = int(tbl[3])
            raw    = tbl[4]
            nf     = len(fields)
            fl     = [f.lower() for f in fields]

            def _col(*keys):
                for k in keys:
                    try: return fl.index(k.lower())
                    except ValueError: pass
                return -1

            case_i  = _col("outputcase", "casename")
            ctype_i = _col("casetype")
            step_i  = _col("steptype")
            snum_i  = _col("stepnumber", "stepnum", "step number")
            fx_i    = _col("fx")
            fy_i    = _col("fy")
            fz_i    = _col("fz")
            mx_i    = _col("mx")
            my_i    = _col("my")
            mz_i    = _col("mz")

            if all(i >= 0 for i in [case_i, fx_i, fy_i, fz_i, mx_i, my_i, mz_i]):
                # Ordered dict preserves the ETABS table row order
                from collections import OrderedDict
                groups    = OrderedDict()
                key_meta  = {}

                for r in range(n_rec):
                    b         = r * nf
                    cname     = str(raw[b + case_i]).strip()
                    case_type = str(raw[b + ctype_i]).strip() if ctype_i >= 0 else ""
                    step_type = str(raw[b + step_i]).strip()  if step_i  >= 0 else ""

                    # Skip individual RS modal contributions — not a meaningful result.
                    # The combined (CQC/SRSS) result appears as step_type "Max".
                    if step_type.lower() == "mode":
                        continue

                    # Step number
                    try:
                        snum_f = float(raw[b + snum_i]) if snum_i >= 0 else 0.0
                        if snum_f == 0:
                            step_num = ""
                        elif snum_f == int(snum_f):
                            step_num = int(snum_f)
                        else:
                            step_num = round(snum_f, 4)
                    except (ValueError, TypeError):
                        step_num = ""

                    key = (cname, case_type, step_type, step_num)
                    if key not in groups:
                        groups[key]   = {"FX": 0.0, "FY": 0.0, "FZ": 0.0,
                                         "MX": 0.0, "MY": 0.0, "MZ": 0.0}
                        key_meta[key] = (cname, case_type, step_type, step_num)

                    try:
                        groups[key]["FX"] += float(raw[b + fx_i])
                        groups[key]["FY"] += float(raw[b + fy_i])
                        groups[key]["FZ"] += float(raw[b + fz_i])
                        groups[key]["MX"] += float(raw[b + mx_i])
                        groups[key]["MY"] += float(raw[b + my_i])
                        groups[key]["MZ"] += float(raw[b + mz_i])
                    except (ValueError, TypeError):
                        continue

                for key, s in groups.items():
                    cname, case_type, step_type, step_num = key_meta[key]
                    data.append({
                        "combo":     cname,
                        "case_type": case_type,
                        "step_type": step_type,
                        "step_num":  step_num,
                        "FX": round(s["FX"], 2),
                        "FY": round(s["FY"], 2),
                        "FZ": round(s["FZ"], 2),
                        "MX": round(s["MX"], 2),
                        "MY": round(s["MY"], 2),
                        "MZ": round(s["MZ"], 2),
                    })

        # ── 3. FALLBACK: BaseReact() when table is empty / unavailable ──
        #   Returns envelope Max/Min for multi-step — no individual steps.
        if not data:
            ret = SapModel.Results.BaseReact()
            rc  = int(ret[-1]) if hasattr(ret, '__len__') else int(ret)
            if rc == 0 and int(ret[0]) > 0:
                num        = int(ret[0])
                case_names = list(ret[1])
                step_types = list(ret[2])
                step_nums  = list(ret[3])
                FX_list    = list(ret[4])
                FY_list    = list(ret[5])
                FZ_list    = list(ret[6])
                MX_list    = list(ret[7])
                MY_list    = list(ret[8])
                MZ_list    = list(ret[9])

                # Build case_type map from collections (fallback only)
                case_type_map = {}
                for attr, ts in [("StaticLinear","LinStatic"),("StaticNonlinear","NonlinStatic"),
                                  ("ModalEigen","LinModal"),("ResponseSpectrum","LinRespSpec"),
                                  ("DirHistLinear","LinTimeHistory"),("DirHistNonlinear","NonlinTimeHistory")]:
                    try:
                        coll = getattr(SapModel.LoadCases, attr, None)
                        if coll:
                            nr2 = coll.GetNameList()
                            if nr2 and int(nr2[-1]) == 0:
                                for n in list(nr2[1]):
                                    case_type_map[str(n).strip()] = ts
                    except Exception:
                        pass

                for i in range(num):
                    cname     = str(case_names[i]).strip()
                    step_type = str(step_types[i]).strip() if step_types else ""
                    try:
                        snum_f   = float(step_nums[i])
                        step_num = "" if snum_f == 0 else (int(snum_f) if snum_f == int(snum_f) else round(snum_f, 4))
                    except (ValueError, TypeError, IndexError):
                        step_num = ""
                    data.append({
                        "combo":     cname,
                        "case_type": case_type_map.get(cname, ""),
                        "step_type": step_type,
                        "step_num":  step_num,
                        "FX": round(float(FX_list[i]), 2),
                        "FY": round(float(FY_list[i]), 2),
                        "FZ": round(float(FZ_list[i]), 2),
                        "MX": round(float(MX_list[i]), 2),
                        "MY": round(float(MY_list[i]), 2),
                        "MZ": round(float(MZ_list[i]), 2),
                    })

        if not data:
            _restore_units(SapModel, orig_units)
            return {"error": "No base reaction results. Ensure model has been analyzed."}

        # ── 4. Available picker list ────────────────────────────────────
        _restore_units(SapModel, orig_units)

        if load_type == "case":
            nr = SapModel.LoadCases.GetNameList()
            available = [str(n) for n in list(nr[1]) if not str(n).startswith("~")] \
                        if nr and int(nr[-1]) == 0 else []
            return {"status": "success", "data": data, "available_cases": available}
        else:
            nr = SapModel.RespCombo.GetNameList()
            available = [str(n) for n in list(nr[1]) if not str(n).startswith("~")] \
                        if nr and int(nr[-1]) == 0 else []
            return {"status": "success", "data": data, "available_combos": available}

    except Exception as e:
        try: _restore_units(SapModel, orig_units)
        except Exception: pass
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
        # Switch to kN·mm (code 5): dimensions come back in mm, stresses in N/mm² = MPa.
        # This eliminates all unit-conversion guesswork regardless of model settings.
        _KN_MM = 5
        try:
            _sec_orig_units = SapModel.GetPresentUnits()
        except Exception:
            _sec_orig_units = _KN_MM
        try:
            SapModel.SetPresentUnits(_KN_MM)
        except Exception:
            pass

        # With kN·mm units: lengths → mm (identity), stress → kN/mm² (× 1000 → MPa).
        def to_mm(v):
            return float(v)

        def to_mpa(v):
            return float(v) * 1000.0   # kN/mm² → N/mm² = MPa

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
            _restore_units(SapModel, _sec_orig_units)
            return {"error": "No rectangular RC column sections found in ETABS model."}
        _restore_units(SapModel, _sec_orig_units)
        return {"status": "success", "sections": sections}

    except Exception as e:
        try:
            _restore_units(SapModel, _sec_orig_units)
        except Exception:
            pass
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
    """
    Return all material names defined in the active ETABS model, classified by type.
    ETABS MatType codes: 1=Steel, 2=Concrete, 3=NoDesign, 4=Aluminum,
                         5=ColdFormed, 6=Rebar, 7=Tendon, 8=Masonry
    Returns: {status, materials (all names), steel_materials (type 1+6), concrete_materials (type 2)}
    """
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

        steel_materials    = []
        concrete_materials = []
        for name in materials:
            try:
                mr = SapModel.PropMaterial.GetMaterial(name)
                # tuple: (MatType, Color, Notes, GUID, retcode) — retcode last
                rc = int(mr[-1]) if hasattr(mr, '__len__') else int(mr)
                if rc != 0:
                    continue
                mat_type = int(mr[0])
                if mat_type in (1, 6):   # Steel or Rebar
                    steel_materials.append(name)
                elif mat_type == 2:       # Concrete
                    concrete_materials.append(name)
            except Exception:
                pass  # leave unclassified

        return {
            "status":             "success",
            "materials":          materials,
            "steel_materials":    steel_materials,
            "concrete_materials": concrete_materials,
        }
    except Exception as e:
        return {"error": f"Failed to get materials: {str(e)}"}


# Bridge aliases — server.py calls these names
get_rc_beam_materials    = get_frame_materials
get_rc_column_materials  = get_frame_materials
# NOTE: write_rc_beam_sections / write_rc_column_sections are used directly
# by server.py — do NOT add forward-reference aliases here (NameError at load)


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

            # Read beam rebar assignment from ETABS section definition
            # GetRebarBeam returns: ['', MatRebar, MatRebarShr, CoverTop, CoverBot, ..., retcode]
            fy_main, fy_ties = "", ""
            top_cc, bot_cc   = 40, 40
            bar_dia          = 25
            try:
                rr = SapModel.PropFrame.GetRebarBeam(name)
                if rr and int(rr[-1]) == 0 and len(rr) >= 5:
                    str_vals   = [v for v in rr[:-1] if isinstance(v, str) and v]
                    float_vals = [v for v in rr[:-1] if isinstance(v, float)]
                    if str_vals:
                        fy_main = str_vals[0]
                    if len(str_vals) >= 2:
                        fy_ties = str_vals[1]
                    if len(float_vals) >= 2:
                        top_cc = max(1, round(to_mm(float_vals[0])))
                        bot_cc = max(1, round(to_mm(float_vals[1])))
            except Exception:
                pass

            sections.append({
                "prop_name":         name,
                "depth":             h_mm,
                "width":             b_mm,
                "concrete_strength": conc_mat,
                "fy_main":           fy_main,
                "fy_ties":           fy_ties,
                "bar_dia":           bar_dia,
                "top_cc":            top_cc,
                "bot_cc":            bot_cc,
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

    # Unlock model if locked (required to modify section properties)
    try:
        if SapModel.GetModelIsLocked():
            SapModel.SetModelIsLocked(False)
    except Exception:
        pass

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
            ret = SapModel.PropFrame.SetRectangle(name, conc, t3, t2, -1, "", "")
            if int(ret) != 0:
                raise RuntimeError(f"SetRectangle returned {ret}")
            # Set rebar materials and cover
            fy_main = str(s.get("fy_main", "") or "").strip()
            fy_ties = str(s.get("fy_ties", "") or fy_main).strip()
            if fy_main or fy_ties:
                try:
                    rebar_mat = fy_main or fy_ties
                    tie_mat   = fy_ties or fy_main
                    top_cov   = _mm_to_model_length(float(s.get("top_cc") or 40), unit_code)
                    bot_cov   = _mm_to_model_length(float(s.get("bot_cc") or 40), unit_code)
                    # SetRebarBeam(Name, MatRebar, MatRebarShr, CoverTop, CoverBot,
                    #              AreaTop_I, AreaTop_J, AreaBot_I, AreaBot_J)
                    SapModel.PropFrame.SetRebarBeam(
                        name, rebar_mat, tie_mat, top_cov, bot_cov,
                        0.0, 0.0, 0.0, 0.0
                    )
                except Exception:
                    pass  # geometry written; rebar best-effort
            # Apply modifiers — ETABS order: [Area, V2, V3, Torsion(J), I22, I33, Mass, Weight]
            torsion = float(s.get("torsion") or 0.35)
            i22     = float(s.get("i22")     or 0.35)
            i33     = float(s.get("i33")     or 0.35)
            try:
                SapModel.PropFrame.SetModifiers(name, [1, 1, 1, torsion, i22, i33, 1, 1])
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

    # Unlock model if locked (required to modify section properties)
    try:
        if SapModel.GetModelIsLocked():
            SapModel.SetModelIsLocked(False)
    except Exception:
        pass

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
            ret = SapModel.PropFrame.SetRectangle(name, conc, t3, t2, -1, "", "")
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
                # ETABS order: [Area, V2, V3, Torsion(J), I22, I33, Mass, Weight]
                SapModel.PropFrame.SetModifiers(name, [1, 1, 1, torsion, i22, i33, 1, 1])
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


def _restore_units(SapModel, orig_units: int) -> None:
    """Restore ETABS present units to their original value (best-effort, never raises)."""
    try:
        SapModel.SetPresentUnits(orig_units)
    except Exception:
        pass


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
    _err_log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "etabs_import_err.txt")
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}
    # Switch to kN-m (unit_code=6) so FrameForce returns values directly in kN / kN·m.
    # Restore original units after reading regardless of success or failure.
    _KN_M = 6
    try:
        _orig_units = SapModel.GetPresentUnits()
    except Exception:
        _orig_units = _KN_M
    try:
        SapModel.SetPresentUnits(_KN_M)
    except Exception:
        pass
    unit_code = _KN_M  # values will now be in kN / kN·m — no further conversion needed

    try:
        # --- 1. Get selected frame objects ---
        try:
            sel_ret = SapModel.SelectObj.GetSelected()
        except Exception as _se:
            with open(_err_log, "w") as _f: _f.write(f"GetSelected() raised: {_se!r}\n")
            return {"error": f"GetSelected failed: {_se}"}
        # Returns: (count, type_array, name_array, retcode)
        with open(_err_log, "w") as _f:
            _f.write(f"sel_ret={sel_ret!r}\n  retcode={sel_ret[-1] if sel_ret else 'N/A'}, count={sel_ret[0] if sel_ret else 'N/A'}\n")
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

        # --- 3. Build frame → story map from "Column Object Connectivity" table ---
        # This table is shown in ETABS and directly maps UniqueName → Story.
        # Read "Column Object Connectivity" table: tbl[2]=fields, tbl[3]=nRec, tbl[4]=data, tbl[5]=retcode
        frame_to_story = {}
        try:
            tbl = SapModel.DatabaseTables.GetTableForDisplayArray(
                "Column Object Connectivity", [], "All", 1, [], 0, []
            )
            if tbl and int(tbl[5]) == 0 and int(tbl[3]) > 0:
                fields = list(tbl[2])
                n_rec  = int(tbl[3])
                data   = tbl[4]
                n_flds = len(fields)
                name_idx  = next((i for i, f in enumerate(fields) if f.lower() in ("uniquename", "unique name")), -1)
                story_idx = next((i for i, f in enumerate(fields) if f.lower() == "story"), -1)
                if name_idx >= 0 and story_idx >= 0:
                    for r in range(n_rec):
                        base = r * n_flds
                        frame_to_story[str(data[base + name_idx])] = str(data[base + story_idx])
        except Exception:
            pass

        # --- 4. Loop frames and extract forces ---
        results = []
        for frame_name in frame_names:
            # ── Determine which physical end is Bottom vs Top ─────────────────
            # ObjSta=0 corresponds to the I-end of the frame; ObjSta=max to J-end.
            # We cannot assume I-end is always the lower node — it depends on how
            # the column was drawn in ETABS.  Look up the Z-coordinates of both
            # joints and let gravity decide.
            i_is_bottom = True   # default fallback: treat I-end as bottom
            try:
                pts_ret = SapModel.FrameObj.GetPoints(frame_name)
                # pts_ret = (Point1_name, Point2_name, retcode)
                if pts_ret and int(pts_ret[-1]) == 0:
                    pt_i_name = pts_ret[0]
                    pt_j_name = pts_ret[1]
                    coord_i = SapModel.PointObj.GetCoordCartesian(pt_i_name)
                    coord_j = SapModel.PointObj.GetCoordCartesian(pt_j_name)
                    # coord = (x, y, z, retcode)
                    if (coord_i and int(coord_i[-1]) == 0 and
                            coord_j and int(coord_j[-1]) == 0):
                        z_i = float(coord_i[2])
                        z_j = float(coord_j[2])
                        # I-end is the bottom when its Z is less than or equal to J-end Z
                        i_is_bottom = z_i <= z_j
            except Exception:
                i_is_bottom = True  # safe fallback

            # eItemType=0 means Object (frame by name)
            ret = SapModel.Results.FrameForce(frame_name, 0)
            # ret = (NumberResults, Obj, ObjSta, Elm, ElmSta, LoadCase,
            #         StepType, StepNum, P, V2, V3, T, M2, M3, retcode)
            if not ret or int(ret[-1]) != 0 or int(ret[0]) == 0:
                continue

            n        = int(ret[0])
            stations = ret[2]   # ObjSta — distance from I-end along the member
            lc_names = ret[5]   # LoadCase label per result row
            P_raw    = ret[8]   # axial force (ETABS: compression = negative)
            M2_raw   = ret[12]  # weak-axis moment
            M3_raw   = ret[13]  # strong-axis moment

            # Group all stations by load case, then identify the two physical ends
            by_lc: dict = {}
            for i in range(n):
                lc = lc_names[i]
                if lc not in by_lc:
                    by_lc[lc] = []
                by_lc[lc].append((float(stations[i]), P_raw[i], M2_raw[i], M3_raw[i]))

            for lc, pts in by_lc.items():
                pts.sort(key=lambda x: x[0])   # ascending station → I-end first
                i_end_pt = pts[0]               # ObjSta = 0  →  I-end of member
                j_end_pt = pts[-1]              # ObjSta = max →  J-end of member

                if i_end_pt[0] == j_end_pt[0]:
                    # Only one station returned — emit once as Bot
                    locs = [("Bot", i_end_pt)]
                elif i_is_bottom:
                    # I-end has lower Z → I-end is physically the bottom
                    locs = [("Bot", i_end_pt), ("Top", j_end_pt)]
                else:
                    # J-end has lower Z → J-end is physically the bottom
                    locs = [("Bot", j_end_pt), ("Top", i_end_pt)]

                for loc_label, (_, P_v, M2_v, M3_v) in locs:
                    results.append({
                        "frame":    frame_name,
                        "story":    frame_to_story.get(frame_name, "?"),
                        "combo":    lc,
                        "location": loc_label,
                        "P_kN":     round(_to_kN  (P_v,  unit_code), 2),
                        "M3_kNm":   round(_to_kNm (M3_v, unit_code), 2),
                        "M2_kNm":   round(_to_kNm (M2_v, unit_code), 2),
                    })

        if not results:
            _restore_units(SapModel, _orig_units)
            return {"error": "No force results returned. Ensure selected frames have results for the chosen combinations."}

        _restore_units(SapModel, _orig_units)
        return {"status": "success", "results": results}
    except Exception as e:
        _restore_units(SapModel, _orig_units)
        try:
            with open(_err_log, "a") as _f: _f.write(f"OUTER EXCEPTION: {e!r}\n")
        except Exception:
            pass
        return {"error": f"Failed to get frame forces: {str(e)}"}


def get_etabs_all_column_forces(sec_names: list, combo_names: list, load_type: str = "combo"):
    """
    Get frame forces for every column frame in the model, grouped by section name.

    Optimised implementation — uses DB table bulk reads (≈5 COM calls total) instead
    of per-frame FrameForce calls (previously O(N) COM calls).  Typical speedup:
    30–50× for large buildings.

    Fast path:
      1. "Frame Assignments - Sections" table  → frame→section map  (1 call)
      2. "Point Object Coordinates" table       → joint Z coords     (1 call)
      3. "Frame Object Connectivity" table      → I/J joints         (1 call)
      4. "Column Object Connectivity" table     → story labels       (1 call)
      5. "Element Forces - Frames" table        → ALL forces at once (1 call)

    Falls back to per-frame FrameForce if any table call fails.

    Returns:
      {"sections": {sec_name: [{"frame", "story", "combo", "location",
                                 "P_kN", "M3_kNm", "M2_kNm"}, ...]}}
    """
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}

    _KN_M2 = 6
    try:
        _orig_units2 = SapModel.GetPresentUnits()
    except Exception:
        _orig_units2 = _KN_M2
    try:
        SapModel.SetPresentUnits(_KN_M2)
    except Exception:
        pass
    unit_code = _KN_M2

    try:
        sec_set   = set(str(s) for s in sec_names)
        combo_set = set(str(c) for c in combo_names)

        # ── 1. Frame → section map (1 DB table call, fallback to per-frame) ─
        frame_to_sec: dict = {}
        for _tname in ("Frame Assignments - Sections",
                       "Frame Section Assignments",
                       "Frame Props Assignment"):
            try:
                _t = SapModel.DatabaseTables.GetTableForDisplayArray(
                    _tname, [], "All", 1, [], 0, []
                )
                if not (_t and int(_t[5]) == 0 and int(_t[3]) > 0):
                    continue
                _fields = list(_t[2]); _data = _t[4]; _nf = len(_fields)
                _ni = next((i for i, f in enumerate(_fields)
                            if f.lower() in ("uniquename", "frame", "name")), -1)
                _si = next((i for i, f in enumerate(_fields)
                            if f.lower() in ("sectionprop", "section", "propname",
                                             "property", "analysisprop")), -1)
                if _ni >= 0 and _si >= 0:
                    for _r in range(int(_t[3])):
                        _b = _r * _nf
                        _sn = str(_data[_b + _si])
                        if _sn in sec_set:
                            frame_to_sec[str(_data[_b + _ni])] = _sn
                if frame_to_sec:
                    break
            except Exception:
                continue

        if not frame_to_sec:   # fallback: per-frame GetSection
            try:
                _fr = SapModel.FrameObj.GetNameList()
                if _fr and int(_fr[-1]) == 0:
                    _arr = next((list(x) for x in _fr[:-1]
                                 if hasattr(x, '__iter__') and not isinstance(x, str)), [])
                    for _fn in _arr:
                        try:
                            _sr = SapModel.FrameObj.GetSection(_fn)
                            if _sr and int(_sr[-1]) == 0 and _sr[0]:
                                _sn = str(_sr[0])
                                if _sn in sec_set:
                                    frame_to_sec[_fn] = _sn
                        except Exception:
                            continue
            except Exception as _e:
                return {"error": f"Could not enumerate frame objects: {_e}"}

        if not frame_to_sec:
            return {"error": "No column frames found for the given section names."}

        col_frame_set = set(frame_to_sec.keys())

        # ── 2. Joint Z-coordinates (1 DB table call) ─────────────────────────
        _z_map: dict = {}   # joint_name → Z in metres
        try:
            _pt = SapModel.DatabaseTables.GetTableForDisplayArray(
                "Point Object Coordinates", [], "All", 1, [], 0, []
            )
            if _pt and int(_pt[5]) == 0 and int(_pt[3]) > 0:
                _fields = list(_pt[2]); _data = _pt[4]; _nf = len(_fields)
                _ni = next((i for i, f in enumerate(_fields)
                            if f.lower() in ("uniquename", "point", "name")), -1)
                _zi = next((i for i, f in enumerate(_fields) if f.lower() == "z"), -1)
                if _ni >= 0 and _zi >= 0:
                    for _r in range(int(_pt[3])):
                        _b = _r * _nf
                        try:
                            _z_map[str(_data[_b + _ni])] = float(_data[_b + _zi])
                        except (ValueError, TypeError):
                            pass
        except Exception:
            pass

        # ── 3. Frame connectivity → top/bottom orientation (1 DB table call) ─
        frame_i_is_bottom: dict = {}   # frame_name → bool
        try:
            _fc = SapModel.DatabaseTables.GetTableForDisplayArray(
                "Frame Object Connectivity", [], "All", 1, [], 0, []
            )
            if _fc and int(_fc[5]) == 0 and int(_fc[3]) > 0:
                _fields = list(_fc[2]); _data = _fc[4]; _nf = len(_fields)
                _ni = next((i for i, f in enumerate(_fields)
                            if f.lower() in ("uniquename", "frame")), -1)
                _ii = next((i for i, f in enumerate(_fields)
                            if f.lower() in ("jointi", "pointi", "joint1", "point1")), -1)
                _ji = next((i for i, f in enumerate(_fields)
                            if f.lower() in ("jointj", "pointj", "joint2", "point2")), -1)
                if _ni >= 0 and _ii >= 0 and _ji >= 0:
                    for _r in range(int(_fc[3])):
                        _b = _r * _nf
                        _fn = str(_data[_b + _ni])
                        if _fn not in col_frame_set:
                            continue
                        _zi2 = _z_map.get(str(_data[_b + _ii]))
                        _zj2 = _z_map.get(str(_data[_b + _ji]))
                        if _zi2 is not None and _zj2 is not None:
                            frame_i_is_bottom[_fn] = _zi2 <= _zj2
        except Exception:
            pass

        # ── 4. Story labels (1 DB table call) ────────────────────────────────
        frame_to_story: dict = {}
        try:
            _ct = SapModel.DatabaseTables.GetTableForDisplayArray(
                "Column Object Connectivity", [], "All", 1, [], 0, []
            )
            if _ct and int(_ct[5]) == 0 and int(_ct[3]) > 0:
                _fields = list(_ct[2]); _data = _ct[4]; _nf = len(_fields)
                _ni = next((i for i, f in enumerate(_fields)
                            if f.lower() in ("uniquename", "unique name")), -1)
                _si = next((i for i, f in enumerate(_fields) if f.lower() == "story"), -1)
                if _ni >= 0 and _si >= 0:
                    for _r in range(int(_ct[3])):
                        _b = _r * _nf
                        frame_to_story[str(_data[_b + _ni])] = str(_data[_b + _si])
        except Exception:
            pass

        # ── 5. Select combos/cases for output ────────────────────────────────
        SapModel.Results.Setup.DeselectAllCasesAndCombosForOutput()
        for name in combo_names:
            if load_type == "case":
                SapModel.Results.Setup.SetCaseSelectedForOutput(name, True)
            else:
                SapModel.Results.Setup.SetComboSelectedForOutput(name, True)

        by_section: dict = {s: [] for s in sec_set}
        _forces_via_table = False

        # ── 6. ALL forces in one table call ──────────────────────────────────
        try:
            _ef = SapModel.DatabaseTables.GetTableForDisplayArray(
                "Element Forces - Frames", [], "All", 1, [], 0, []
            )
            if _ef and int(_ef[5]) == 0 and int(_ef[3]) > 0:
                _fields = list(_ef[2]); _data = _ef[4]; _nf = len(_fields)

                _fi_name = next((i for i, f in enumerate(_fields)
                                 if f.lower() in ("uniquename", "frame")), -1)
                _fi_case = next((i for i, f in enumerate(_fields)
                                 if f.lower() in ("outputcase", "loadcase", "case")), -1)
                _fi_step = next((i for i, f in enumerate(_fields)
                                 if f.lower() in ("steptype", "casetype")), -1)
                _fi_sta  = next((i for i, f in enumerate(_fields)
                                 if f.lower() in ("station", "stationnum", "objsta")), -1)
                _fi_P    = next((i for i, f in enumerate(_fields) if f.lower() == "p"), -1)
                _fi_M2   = next((i for i, f in enumerate(_fields) if f.lower() == "m2"), -1)
                _fi_M3   = next((i for i, f in enumerate(_fields) if f.lower() == "m3"), -1)

                if _fi_name >= 0 and _fi_case >= 0 and _fi_P >= 0 and _fi_M2 >= 0 and _fi_M3 >= 0:
                    _VALID_STEPS = {"", "max", "min", "linear", "mode"}
                    _raw: dict = {}   # (frame, combo) → [(station, P, M2, M3)]

                    for _r in range(int(_ef[3])):
                        _b = _r * _nf
                        _fn = str(_data[_b + _fi_name])
                        if _fn not in col_frame_set:
                            continue
                        _lc = str(_data[_b + _fi_case])
                        if _lc not in combo_set:
                            continue
                        if _fi_step >= 0:
                            _step = str(_data[_b + _fi_step]).strip().lower()
                            if _step and _step not in _VALID_STEPS:
                                continue
                        try:
                            _sta = float(_data[_b + _fi_sta]) if _fi_sta >= 0 else 0.0
                            _P  = float(_data[_b + _fi_P])
                            _M2 = float(_data[_b + _fi_M2])
                            _M3 = float(_data[_b + _fi_M3])
                        except (ValueError, TypeError):
                            continue
                        _key = (_fn, _lc)
                        if _key not in _raw:
                            _raw[_key] = []
                        _raw[_key].append((_sta, _P, _M2, _M3))

                    for (_fn, _lc), _pts in _raw.items():
                        _sn = frame_to_sec.get(_fn)
                        if not _sn:
                            continue
                        _story      = frame_to_story.get(_fn, "?")
                        _i_bot      = frame_i_is_bottom.get(_fn, True)
                        _pts.sort(key=lambda x: x[0])
                        _ie = _pts[0]; _je = _pts[-1]
                        if _ie[0] == _je[0]:
                            _locs = [("Bot", _ie)]
                        elif _i_bot:
                            _locs = [("Bot", _ie), ("Top", _je)]
                        else:
                            _locs = [("Bot", _je), ("Top", _ie)]
                        for _loc, (_, _Pv, _M2v, _M3v) in _locs:
                            by_section[_sn].append({
                                "frame":    _fn,
                                "story":    _story,
                                "combo":    _lc,
                                "location": _loc,
                                "P_kN":     round(float(_Pv),  2),
                                "M3_kNm":   round(float(_M3v), 2),
                                "M2_kNm":   round(float(_M2v), 2),
                            })
                    _forces_via_table = True
        except Exception:
            pass

        # ── Fallback: per-frame FrameForce ────────────────────────────────────
        if not _forces_via_table:
            for frame_name, sec_name in frame_to_sec.items():
                try:
                    story       = frame_to_story.get(frame_name, "?")
                    i_is_bottom = frame_i_is_bottom.get(frame_name, True)
                    ret = SapModel.Results.FrameForce(frame_name, 0)
                    if not ret or int(ret[-1]) != 0 or int(ret[0]) == 0:
                        continue
                    n = int(ret[0])
                    stations = ret[2]; lc_names = ret[5]
                    P_raw = ret[8]; M2_raw = ret[12]; M3_raw = ret[13]
                    by_lc: dict = {}
                    for i in range(n):
                        lc = lc_names[i]
                        if lc not in by_lc:
                            by_lc[lc] = []
                        by_lc[lc].append((float(stations[i]), P_raw[i], M2_raw[i], M3_raw[i]))
                    for lc, pts in by_lc.items():
                        pts.sort(key=lambda x: x[0])
                        ie = pts[0]; je = pts[-1]
                        if ie[0] == je[0]:
                            locs = [("Bot", ie)]
                        elif i_is_bottom:
                            locs = [("Bot", ie), ("Top", je)]
                        else:
                            locs = [("Bot", je), ("Top", ie)]
                        for loc_label, (_, Pv, M2v, M3v) in locs:
                            by_section[sec_name].append({
                                "frame":    frame_name,
                                "story":    story,
                                "combo":    lc,
                                "location": loc_label,
                                "P_kN":     round(_to_kN  (Pv,  unit_code), 2),
                                "M3_kNm":   round(_to_kNm (M3v, unit_code), 2),
                                "M2_kNm":   round(_to_kNm (M2v, unit_code), 2),
                            })
                except Exception:
                    continue

        _restore_units(SapModel, _orig_units2)
        return {"status": "success", "sections": by_section}

    except Exception as e:
        try:
            _restore_units(SapModel, _orig_units2)
        except Exception:
            pass
        return {"error": f"get_etabs_all_column_forces failed: {e}"}


def get_column_axial_for_combo(combo_name: str, col_frames: list,
                               load_type: str = "combo"):
    """
    Extract axial force (kN) at station-0 (I-end) for each supplied column frame.

    col_frames: list of ETABS frame names already classified as columns by the
                frontend geometry cache — avoids re-classifying here.

    Returns {"axial": {frame_name: abs_P_kN}, "combo": combo_name}
    """
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}
    if not col_frames:
        return {"error": "No column frames supplied."}

    _KN_M = 6
    try:
        orig_units = SapModel.GetPresentUnits()
    except Exception:
        orig_units = _KN_M
    try:
        SapModel.SetPresentUnits(_KN_M)
    except Exception:
        pass

    try:
        # ── 1. Select the single combo/case for output ────────────────────────
        SapModel.Results.Setup.DeselectAllCasesAndCombosForOutput()
        if load_type == "case":
            SapModel.Results.Setup.SetCaseSelectedForOutput(combo_name, True)
        else:
            rc = SapModel.Results.Setup.SetComboSelectedForOutput(combo_name, True)
            if rc != 0:
                SapModel.Results.Setup.SetCaseSelectedForOutput(combo_name, True)

        # ── 2. FrameForce at station-0 (bottom end) for each column ──────────
        # Station 0 = I-end of the element.  For gravity-loaded columns the
        # axial force is nearly constant along the height, so station 0 gives
        # the bottom-end (maximum compression) value in the standard ETABS
        # orientation where the I-joint is the lower floor level.
        axial = {}
        for fname in col_frames:
            try:
                ret = SapModel.Results.FrameForce(fname, 0)
                # ret tuple: (nResults, Obj[], ObjSta[], Elm[], ElmSta[],
                #             LoadCase[], StepType[], StepNum[],
                #             P[], V2[], V3[], T[], M2[], M3[], retcode)
                if not ret or int(ret[-1]) != 0 or int(ret[0]) == 0:
                    continue
                n        = int(ret[0])
                stations = [float(ret[2][k]) for k in range(n)]
                p_vals   = [float(ret[8][k]) for k in range(n)]
                # Sort ascending by station → index 0 = I-end (bottom)
                pairs    = sorted(zip(stations, p_vals), key=lambda x: x[0])
                p_bottom = pairs[0][1]   # ETABS sign: compression = negative
                axial[fname] = round(abs(p_bottom), 1)
            except Exception:
                continue

        return {"axial": axial, "combo": combo_name}

    except Exception as e:
        return {"error": f"get_column_axial_for_combo failed: {e}"}
    finally:
        try:
            SapModel.SetPresentUnits(orig_units)
        except Exception:
            pass


def get_building_geometry():
    """
    Return frame (column/beam) and wall geometry for the 3D building view.
    Uses ETABS database tables for batch retrieval (fast).
    Falls back to per-element COM calls if tables are unavailable.
    All coordinates are in metres (kN·m unit system).
    """
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}

    _KN_M = 6
    try:
        orig_units = SapModel.GetPresentUnits()
    except Exception:
        orig_units = _KN_M
    try:
        SapModel.SetPresentUnits(_KN_M)
    except Exception:
        pass

    try:
        # ── 1. Joint coordinates (batch via DB table) ────────────────────────
        coord_map = {}   # joint_name -> (x, y, z) in metres

        try:
            pt_tbl = SapModel.DatabaseTables.GetTableForDisplayArray(
                "Point Object Coordinates", [], "All", 1, [], 0, []
            )
            if pt_tbl and int(pt_tbl[5]) == 0 and int(pt_tbl[3]) > 0:
                fields  = list(pt_tbl[2])
                n_rec   = int(pt_tbl[3])
                data    = pt_tbl[4]
                nf      = len(fields)
                ni  = next((i for i, f in enumerate(fields) if f.lower() in ("point","uniquename","unique name","joint")), -1)
                xi  = next((i for i, f in enumerate(fields) if f.lower() in ("globalx","x")), -1)
                yi  = next((i for i, f in enumerate(fields) if f.lower() in ("globaly","y")), -1)
                zi  = next((i for i, f in enumerate(fields) if f.lower() in ("globalz","z")), -1)
                if ni >= 0 and xi >= 0 and yi >= 0 and zi >= 0:
                    for r in range(n_rec):
                        b = r * nf
                        try:
                            coord_map[str(data[b + ni])] = (
                                float(data[b + xi]),
                                float(data[b + yi]),
                                float(data[b + zi]),
                            )
                        except (ValueError, TypeError):
                            pass
        except Exception:
            pass   # will fill lazily below

        def _get_coord(jname):
            if jname in coord_map:
                return coord_map[jname]
            try:
                cr = SapModel.PointObj.GetCoordCartesian(jname)
                if cr and int(cr[-1]) == 0:
                    coord_map[jname] = (float(cr[0]), float(cr[1]), float(cr[2]))
                    return coord_map[jname]
            except Exception:
                pass
            return (0.0, 0.0, 0.0)

        # ── 2. Frame connectivity ────────────────────────────────────────────
        frame_joints = {}   # frame_name -> (joint_i, joint_j)
        try:
            fc_tbl = SapModel.DatabaseTables.GetTableForDisplayArray(
                "Frame Object Connectivity", [], "All", 1, [], 0, []
            )
            if fc_tbl and int(fc_tbl[5]) == 0 and int(fc_tbl[3]) > 0:
                flds = list(fc_tbl[2])
                n_r  = int(fc_tbl[3])
                dat  = fc_tbl[4]
                nf2  = len(flds)
                fi_n = next((i for i, f in enumerate(flds) if f.lower() in ("frame","uniquename","unique name")), -1)
                ji_n = next((i for i, f in enumerate(flds) if f.lower() in ("jointi","pointi","jpointi")), -1)
                jj_n = next((i for i, f in enumerate(flds) if f.lower() in ("jointj","pointj","jpointj")), -1)
                if fi_n >= 0 and ji_n >= 0 and jj_n >= 0:
                    for r in range(n_r):
                        b = r * nf2
                        frame_joints[str(dat[b + fi_n])] = (
                            str(dat[b + ji_n]),
                            str(dat[b + jj_n]),
                        )
        except Exception:
            pass

        # Fallback: per-frame GetPoints
        if not frame_joints:
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
                            pts = SapModel.FrameObj.GetPoints(fname)
                            if pts and int(pts[-1]) == 0:
                                frame_joints[fname] = (str(pts[0]), str(pts[1]))
                        except Exception:
                            continue
            except Exception:
                pass

        # ── 3. Frame section assignment ──────────────────────────────────────
        frame_section = {}   # frame_name -> section_name
        try:
            fs_tbl = SapModel.DatabaseTables.GetTableForDisplayArray(
                "Frame Assignments - Sections", [], "All", 1, [], 0, []
            )
            if fs_tbl and int(fs_tbl[5]) == 0 and int(fs_tbl[3]) > 0:
                flds3 = list(fs_tbl[2])
                n_r3  = int(fs_tbl[3])
                dat3  = fs_tbl[4]
                nf3   = len(flds3)
                fi3 = next((i for i, f in enumerate(flds3) if f.lower() in ("frame","uniquename","unique name")), -1)
                si3 = next((i for i, f in enumerate(flds3) if f.lower() in ("analysissection","section","sectionname")), -1)
                if fi3 >= 0 and si3 >= 0:
                    for r in range(n_r3):
                        b = r * nf3
                        frame_section[str(dat3[b + fi3])] = str(dat3[b + si3])
        except Exception:
            pass

        # Fallback: per-frame GetSection
        if not frame_section:
            for fname in frame_joints:
                try:
                    sr = SapModel.FrameObj.GetSection(fname)
                    if sr and int(sr[-1]) == 0 and sr[0]:
                        frame_section[fname] = str(sr[0])
                except Exception:
                    continue

        # ── 4. Section dimensions ─────────────────────────────────────────────
        import sys as _sys_dbg, os as _os_dbg
        _log_p = _os_dbg.path.join(
            _os_dbg.path.dirname(_sys_dbg.executable) if getattr(_sys_dbg, 'frozen', False)
            else _os_dbg.path.dirname(_os_dbg.path.abspath(__file__)),
            'structiq.log')
        def _dbg(m):
            try:
                with open(_log_p, 'a', encoding='utf-8') as _f: _f.write(m + '\n')
            except Exception: pass

        try:
            db_unit = SapModel.GetDatabaseUnits()
            _dbg(f"[geom] GetDatabaseUnits={db_unit}  GetPresentUnits={SapModel.GetPresentUnits()}")
        except Exception:
            db_unit = 6
            _dbg("[geom] GetDatabaseUnits FAILED — defaulting to 6")

        def _to_m(val):
            """Convert a length from ETABS database units to metres."""
            # unit codes where 1 unit = 1 mm: 5(kN_mm),8(kgf_mm),11(N_mm),14(Ton_mm)
            if db_unit in (5, 8, 11, 14):   return val / 1000.0
            # unit codes where 1 unit = 1 cm: 6(kN_cm),9(kgf_cm),12(N_cm),15(Ton_cm)
            if db_unit in (6, 9, 12, 15):   return val / 100.0
            # unit codes where 1 unit = 1 m: 7(kN_m),10(kgf_m),13(N_m),16(Ton_m)
            if db_unit in (7, 10, 13, 16):  return val
            # unit codes in inches: 1(lb_in),3(kip_in)
            if db_unit in (1, 3):           return val * 0.0254
            # unit codes in feet: 2(lb_ft),4(kip_ft)
            if db_unit in (2, 4):           return val * 0.3048
            return val   # unknown — pass through

        sec_dims = {}   # section_name -> (b_m, h_m) in metres

        # Primary: database table for ALL section types (fast, reliable)
        _dbg(f"[geom] unique sections to resolve: {list(set(frame_section.values()))}")
        _sec_tbl_names = [
            "Frame Section Property Definitions 01 - General",
            "Frame Section Property Definitions",
            "Frame Section Properties 01 - General",
        ]
        for _tbl in _sec_tbl_names:
            try:
                sp_tbl = SapModel.DatabaseTables.GetTableForDisplayArray(
                    _tbl, [], "All", 1, [], 0, []
                )
                if sp_tbl and int(sp_tbl[5]) == 0 and int(sp_tbl[3]) > 0:
                    sp_flds = list(sp_tbl[2])
                    sp_n    = int(sp_tbl[3])
                    sp_dat  = sp_tbl[4]
                    sp_nf   = len(sp_flds)
                    _dbg(f"[geom] table '{_tbl}' found — fields={sp_flds}")
                    sp_ni = next((i for i, f in enumerate(sp_flds)
                                  if f.lower() in ("section","sectionname","uniquename","name","unique name")), -1)
                    sp_t3 = next((i for i, f in enumerate(sp_flds)
                                  if f.lower() in ("t3","depth","d","t3 (depth)","t3depth")), -1)
                    sp_t2 = next((i for i, f in enumerate(sp_flds)
                                  if f.lower() in ("t2","width","b","t2 (width)","t2width")), -1)
                    _dbg(f"[geom] name_idx={sp_ni} t3_idx={sp_t3} t2_idx={sp_t2}")
                    if sp_ni >= 0 and sp_t3 >= 0 and sp_t2 >= 0:
                        for r in range(sp_n):
                            b2 = r * sp_nf
                            try:
                                sn  = str(sp_dat[b2 + sp_ni])
                                t3v = float(sp_dat[b2 + sp_t3])
                                t2v = float(sp_dat[b2 + sp_t2])
                                sec_dims[sn] = (t2v, t3v)  # (b_m=T2, h_m=T3) — already in metres
                            except (ValueError, TypeError):
                                pass
                    _dbg(f"[geom] sec_dims after table: {dict(sec_dims)}")
                    if sec_dims:
                        break   # got data from this table
            except Exception as _et:
                _dbg(f"[geom] table '{_tbl}' exception: {_et}")
                continue

        # Fallback: PropFrame.GetRectangle per unique section
        # Return tuple: (FileName, MatProp, T3, T2, Color, Notes, GUID, retcode)
        # rect[2]=T3 (depth/h), rect[3]=T2 (width/b).
        # SetPresentUnits was already called above — values already in metres,
        # no further conversion needed (same as coordinate values).
        missing = [s for s in set(frame_section.values()) if s not in sec_dims]
        _dbg(f"[geom] missing after table path: {missing}")
        for sname in missing:
            try:
                rect = SapModel.PropFrame.GetRectangle(sname)
                _dbg(f"[geom] GetRectangle({sname!r}) raw={[str(v) for v in rect]}")
                if rect and int(rect[-1]) == 0:
                    sec_dims[sname] = (float(rect[3]), float(rect[2]))  # (b_m=T2, h_m=T3)
                else:
                    sec_dims[sname] = (0.0, 0.0)
            except Exception as _ex:
                _dbg(f"[geom] GetRectangle({sname!r}) EXCEPTION: {_ex}")
                sec_dims[sname] = (0.0, 0.0)
        _dbg(f"[geom] final sec_dims={dict(sec_dims)}")

        # ── 4b. Local axis angles (batch via DB table) ───────────────────────
        # angle = rotation (deg) of local 2-3 plane about local 1 (length axis)
        # For a vertical column: local-2 default = global X; angle rotates CCW
        frame_angle = {}   # frame_name -> angle_deg
        try:
            la_tbl = SapModel.DatabaseTables.GetTableForDisplayArray(
                "Frame Local Axes", [], "All", 1, [], 0, []
            )
            if la_tbl and int(la_tbl[5]) == 0 and int(la_tbl[3]) > 0:
                la_flds = list(la_tbl[2])
                la_n    = int(la_tbl[3])
                la_dat  = la_tbl[4]
                la_nf   = len(la_flds)
                la_fi = next((i for i, f in enumerate(la_flds) if f.lower() in ("frame","uniquename","unique name")), -1)
                la_ai = next((i for i, f in enumerate(la_flds) if f.lower() in ("angle","axisangle","localangle","ang")), -1)
                if la_fi >= 0 and la_ai >= 0:
                    for r in range(la_n):
                        b2 = r * la_nf
                        try:
                            frame_angle[str(la_dat[b2 + la_fi])] = float(la_dat[b2 + la_ai])
                        except (ValueError, TypeError):
                            pass
        except Exception:
            pass   # fallback: query per-element below if angle missing

        # ── 5. Build frame list with column/beam classification ──────────────
        frames = []
        for fname, (ji, jj) in frame_joints.items():
            x1, y1, z1 = _get_coord(ji)
            x2, y2, z2 = _get_coord(jj)
            # Ensure bottom→top ordering for columns
            if z1 > z2:
                x1, y1, z1, x2, y2, z2 = x2, y2, z2, x1, y1, z1
            dz = abs(z2 - z1)
            dh = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
            is_col = dz > dh

            sec_name = frame_section.get(fname, "")
            b_m, h_m = sec_dims.get(sec_name, (0.0, 0.0))

            # Per-element angle fallback if not in table
            angle = frame_angle.get(fname)
            if angle is None and is_col:
                try:
                    la = SapModel.FrameObj.GetLocalAxes(fname)
                    angle = float(la[0]) if la and int(la[-1]) == 0 else 0.0
                except Exception:
                    angle = 0.0
            if angle is None:
                angle = 0.0

            frames.append({
                "name":    fname,
                "section": sec_name,
                "type":    "column" if is_col else "beam",
                "x1": round(x1, 4), "y1": round(y1, 4), "z1": round(z1, 4),
                "x2": round(x2, 4), "y2": round(y2, 4), "z2": round(z2, 4),
                "b_m":   round(b_m, 4),
                "h_m":   round(h_m, 4),
                "angle": round(float(angle), 2),
            })

        # ── 6. Area (wall / slab) geometry ──────────────────────────────────
        # Classify: z_range < 0.3 m → slab (horizontal), else → wall (vertical)
        walls = []
        slabs = []

        def _classify_area(aname, pts):
            if len(pts) < 3:
                return
            zs = [p[2] for p in pts]
            if (max(zs) - min(zs)) < 0.3:
                slabs.append({"name": aname, "points": pts})
            else:
                walls.append({"name": aname, "points": pts})

        try:
            ao_tbl = SapModel.DatabaseTables.GetTableForDisplayArray(
                "Area Object Connectivity", [], "All", 1, [], 0, []
            )
            if ao_tbl and int(ao_tbl[5]) == 0 and int(ao_tbl[3]) > 0:
                flds4 = list(ao_tbl[2])
                n_r4  = int(ao_tbl[3])
                dat4  = ao_tbl[4]
                nf4   = len(flds4)
                ai4 = next((i for i, f in enumerate(flds4) if f.lower() in ("area","uniquename","unique name")), -1)
                jcols = [(i, f) for i, f in enumerate(flds4)
                         if f.lower().startswith(("joint","point")) and f[-1].isdigit()]
                jcols.sort(key=lambda t: int(''.join(c for c in t[1] if c.isdigit()) or '0'))
                if ai4 >= 0 and jcols:
                    for r in range(n_r4):
                        b = r * nf4
                        aname = str(dat4[b + ai4])
                        pts = []
                        for (ci, _) in jcols:
                            jn = str(dat4[b + ci]).strip()
                            if jn:
                                cx, cy, cz = _get_coord(jn)
                                pts.append([round(cx, 4), round(cy, 4), round(cz, 4)])
                        _classify_area(aname, pts)
        except Exception:
            pass

        # Fallback: per-area GetPoints
        if not walls and not slabs:
            try:
                ar = SapModel.AreaObj.GetNameList()
                if ar and int(ar[-1]) == 0:
                    names_arr2 = None
                    for item in ar[:-1]:
                        if hasattr(item, '__iter__') and not isinstance(item, str):
                            names_arr2 = list(item)
                            break
                    for aname in (names_arr2 or []):
                        try:
                            ap = SapModel.AreaObj.GetPoints(aname)
                            if ap and int(ap[-1]) == 0:
                                n_pts = int(ap[0])
                                jnames = list(ap[1])[:n_pts]
                                pts = []
                                for jn in jnames:
                                    cx, cy, cz = _get_coord(str(jn))
                                    pts.append([round(cx, 4), round(cy, 4), round(cz, 4)])
                                _classify_area(aname, pts)
                        except Exception:
                            continue
            except Exception:
                pass

        _restore_units(SapModel, orig_units)
        return {"frames": frames, "walls": walls, "slabs": slabs}

    except Exception as e:
        try:
            _restore_units(SapModel, orig_units)
        except Exception:
            pass
        return {"error": f"get_building_geometry failed: {e}"}


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


def _siq_write_log(path: str, msg: str) -> None:
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def _fill_cases_from_db(SapModel, combo_map: dict, log_path: str) -> None:
    """
    Fallback: read ETABS database table to populate combo_map with case lists.
    Tries several known table names — stops at the first that returns data.
    Mutates combo_map in place.
    """
    CANDIDATE_TABLES = [
        "Response Combination Definitions",
        "Response Combination Load Cases",
        "Load Combination Definitions - Response Load Cases",
        "Response Combo - Load Case Factors",
    ]

    def _fi(fields, *names):
        clean = [f.lower().replace(" ", "").replace("_", "") for f in fields]
        for nm in names:
            c = nm.lower().replace(" ", "").replace("_", "")
            if c in clean:
                return clean.index(c)
        return -1

    for tname in CANDIDATE_TABLES:
        try:
            tbl = SapModel.DatabaseTables.GetTableForDisplayArray(
                tname, [], "All", 1, [], 0, []
            )
            if not (tbl and len(tbl) >= 6 and int(tbl[5]) == 0 and int(tbl[3]) > 0):
                continue

            fields = list(tbl[2])
            n_rec  = int(tbl[3])
            data   = list(tbl[4])
            n_flds = len(fields)

            _siq_write_log(log_path, f"  Table '{tname}': {n_rec} rows, fields={fields}")

            i_name = _fi(fields, "Name", "ComboName", "Combination", "CombinationName")
            i_case = _fi(fields, "LoadCase", "Case", "CaseName", "LoadCaseName")
            i_type = _fi(fields, "CaseType", "Type", "ItemType", "LoadCaseType")
            i_sf   = _fi(fields, "ScaleFactor", "SF", "Factor", "Scale", "F")

            if i_name < 0 or i_case < 0:
                continue

            def cell(row, col):
                idx = row * n_flds + col
                return str(data[idx]) if 0 <= col < n_flds and idx < len(data) else ""

            for r in range(n_rec):
                cname = cell(r, i_name)
                if cname not in combo_map:
                    continue
                case_nm = cell(r, i_case)
                if not case_nm:
                    continue
                item_type = 0
                if i_type >= 0:
                    t = cell(r, i_type).lower()
                    item_type = 1 if ("combo" in t or "combination" in t or t == "1") else 0
                sf = 1.0
                if i_sf >= 0:
                    try:
                        sf = float(cell(r, i_sf))
                    except ValueError:
                        pass
                combo_map[cname]["cases"].append({
                    "name": case_nm, "item_type": item_type, "factor": sf,
                })

            if any(v["cases"] for v in combo_map.values()):
                _siq_write_log(log_path, f"  DB fallback succeeded via '{tname}'.")
                return
        except Exception as e:
            _siq_write_log(log_path, f"  Table '{tname}' exception: {e}")

    _siq_write_log(log_path, "DB fallback: all table attempts failed.")


def get_etabs_combo_details(source: str = "etabs"):
    """
    Return all load combinations with their constituent case lists and scale factors.

    Response: {"combinations": [{"name": str, "cases": [{"name", "item_type", "factor"}]}]}
    item_type: 0 = Load Case, 1 = Load Combination reference

    Strategy:
      1. Primary  — RespCombo.GetCaseList() per combination
      2. Fallback — DatabaseTables batch call if primary returns 0 (ETABS only)

    source: "etabs" | "safe"
    """
    import tempfile
    _log = os.path.join(tempfile.gettempdir(), "siq_lc_import_debug.txt")

    if source == "safe":
        try:
            SapModel = get_active_safe()
        except Exception:
            SapModel = None
        if not SapModel:
            return {"error": "SAFE is not currently running or no model is open."}
    else:
        SapModel = get_active_etabs()
        if not SapModel:
            return {"error": "ETABS is not currently running."}

    try:
        name_ret  = SapModel.RespCombo.GetNameList()
        all_names = [n for n in list(name_ret[1]) if not str(n).startswith("~")] \
                    if name_ret and int(name_ret[-1]) == 0 else []

        _siq_write_log(_log, f"Source: {source.upper()}  Combos: {len(all_names)}")

        combo_map = {n: {"name": n, "cases": []} for n in all_names}

        # -- Primary: GetCaseList per combo ----------------------------------
        # CONFIRMED format (verified 2026-04-24):
        #   cr = (NumberItems, (ItemType[int]...), (CaseName[str]...), (SF[float]...), retcode)
        # cr[4] is retcode — use explicit index, NOT cr[-1] (tuple may have extra elements).
        # ItemType (cr[1]) PRECEDES CaseName (cr[2]).
        for name in all_names:
            try:
                cr = SapModel.RespCombo.GetCaseList(name)
                if not cr or len(cr) < 5:
                    continue

                count = int(cr[0]) if cr[0] is not None else 0
                rc    = int(cr[4]) if cr[4] is not None else -1

                if rc != 0 or count == 0:
                    continue

                item_types = list(cr[1]) if cr[1] is not None else []
                cnames     = list(cr[2]) if cr[2] is not None else []
                csfs       = list(cr[3]) if cr[3] is not None else []

                for i in range(min(count, len(cnames))):
                    item_type = int(item_types[i]) if i < len(item_types) else 0
                    sf        = float(csfs[i])      if i < len(csfs)       else 1.0
                    combo_map[name]["cases"].append({
                        "name":      str(cnames[i]),
                        "item_type": item_type,
                        "factor":    sf,
                    })
            except Exception as e:
                _siq_write_log(_log, f"  {name}: GetCaseList exception: {e}")

        # -- Fallback: DB table (ETABS only) ---------------------------------
        total_primary = sum(len(v["cases"]) for v in combo_map.values())
        _siq_write_log(_log, f"Primary total cases: {total_primary}")

        if total_primary == 0 and all_names and source == "etabs":
            _siq_write_log(_log, "Primary=0 — trying DB table fallback.")
            _fill_cases_from_db(SapModel, combo_map, _log)

        return {"combinations": list(combo_map.values())}

    except Exception as e:
        _siq_write_log(_log, f"FATAL: {e}")
        return {"error": str(e)}


def create_load_envelope(name: str, combo_names: list, targets: list):
    """
    Create an Envelope-type load combination in ETABS (and/or SAFE) from a
    list of existing combination names.

    targets: list of "etabs" | "safe"  (currently only "etabs" is implemented)
    """
    SapModel = get_active_etabs()
    if not SapModel:
        return {"error": "ETABS is not currently running."}

    results = []

    if "etabs" in [t.lower() for t in targets]:
        try:
            # Delete if already exists so we can re-create cleanly
            try:
                SapModel.RespCombo.Delete(name)
            except Exception:
                pass

            # combo_type 1 = Envelope
            SapModel.RespCombo.Add(name, 1)

            for combo in combo_names:
                # CaseComboType 1 = Load Combination reference
                SapModel.RespCombo.SetCaseList(name, 1, combo, 1.0)

            results.append({"target": "etabs", "status": "success"})
        except Exception as e:
            results.append({"target": "etabs", "status": "error", "detail": str(e)})

    if "safe" in [t.lower() for t in targets]:
        results.append({
            "target": "safe",
            "status": "error",
            "detail": "SAFE connection not yet implemented.",
        })

    errors = [r for r in results if r["status"] == "error"]
    if errors and len(errors) == len(results):
        return {"error": errors[0]["detail"], "results": results}

    return {"status": "success", "name": name, "results": results}
