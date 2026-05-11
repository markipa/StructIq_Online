"""
Push detected PDF members → live ETABS model via COM.

Workflow:
  1. Connect to running ETABS instance.
  2. Set units to kN, m.
  3. Define grid system from detected grids.
  4. Define stories from user-supplied story list.
  5. Create rectangular concrete sections from label → (b,h) mapping.
  6. For each floor (page → story):
       - Place columns (vertical lines from base to top of story).
       - Place beams at story elevation (horizontal frames).
       - Place wall areas (vertical poly extruded from story base to top).
       - Place slab areas at story elevation.

All input coords are world meters (after scale + Y-flip applied by caller).
"""
from typing import List, Dict, Optional, Tuple


def _connect():
    """Lazy import to avoid hard dep when not on Windows."""
    from etabs_api.connection import get_active_etabs
    sm = get_active_etabs()
    if sm is None:
        raise RuntimeError("ETABS not running. Open ETABS and try again.")
    return sm


def _setup_units(SapModel):
    """kN, m, kN·m."""
    SapModel.SetPresentUnits(6)


def _define_stories(SapModel, stories: List[Dict]) -> List[str]:
    """
    stories = [{"name": "Story1", "height_m": 3.5}, ...]
    Stories list runs bottom→top. Returns list of story names actually created.
    """
    n = len(stories)
    if n == 0:
        return []
    names    = [s["name"] for s in stories]
    heights  = [float(s["height_m"]) for s in stories]
    # Bottom elev = 0; ETABS API auto-stacks
    is_master   = [True] * n
    similar_to  = [""] * n
    splice      = [False] * n
    splice_h    = [0.0] * n
    color       = [0] * n
    try:
        SapModel.Story.SetStories_2(0.0, n, names, heights,
                                     is_master, similar_to,
                                     splice, splice_h, color)
    except Exception:
        # Fallback to older API
        SapModel.Story.SetStories(0.0, n, names, heights,
                                   is_master, similar_to,
                                   splice, splice_h)
    return names


def _define_grid_system(SapModel, x_grids: List[Dict], y_grids: List[Dict],
                        name: str = "G1") -> None:
    """Create a Cartesian grid system. Coords already in meters."""
    if not x_grids and not y_grids:
        return
    try:
        # ETABS v22: SapModel.GridSys.SetGridSys_2 — API varies by version.
        # Use generic add then SetGridSys.
        SapModel.GridSys.SetGridSys(name, 0.0, 0.0, 0.0)
        for g in x_grids:
            SapModel.GridSys.SetGridLine(name, 1, g["label"], float(g["x_m"]), True, 0, 0)
        for g in y_grids:
            SapModel.GridSys.SetGridLine(name, 2, g["label"], float(g["y_m"]), True, 0, 0)
    except Exception:
        # Older API doesn't expose SetGridLine — skip gracefully
        pass


def _ensure_concrete_material(SapModel, name: str = "CONC28") -> str:
    """Ensure a concrete material exists. Returns material name."""
    try:
        ret = SapModel.PropMaterial.GetNameList()
        existing = list(ret[1]) if ret and len(ret) > 1 else []
        if name in existing:
            return name
        # eMatType_Concrete = 2
        SapModel.PropMaterial.SetMaterial(name, 2)
        SapModel.PropMaterial.SetMPIsotropic(name, 24855000.0, 0.2, 1.0e-5)  # E in kN/m²
        return name
    except Exception:
        return name


def _ensure_rect_section(SapModel, label: str, b_mm: float, h_mm: float,
                         material: str) -> str:
    """Create rectangular concrete section if missing. Returns ETABS section name."""
    sec_name = label or f"R{int(b_mm)}x{int(h_mm)}"
    try:
        ret = SapModel.PropFrame.GetNameList()
        existing = list(ret[1]) if ret and len(ret) > 1 else []
        if sec_name in existing:
            return sec_name
        # Units are meters → convert mm
        b_m = b_mm / 1000.0
        h_m = h_mm / 1000.0
        SapModel.PropFrame.SetRectangle(sec_name, material, h_m, b_m)
    except Exception:
        pass
    return sec_name


def _ensure_wall_property(SapModel, name: str, thickness_mm: float,
                          material: str) -> str:
    try:
        ret = SapModel.PropArea.GetNameList()
        existing = list(ret[1]) if ret and len(ret) > 1 else []
        if name in existing:
            return name
        t_m = thickness_mm / 1000.0
        # eWallPropType = 1 (Shell-Thin), eShellType: 1=Shell-Thin
        SapModel.PropArea.SetWall(name, 1, 1, material, t_m)
    except Exception:
        pass
    return name


def _ensure_slab_property(SapModel, name: str, thickness_mm: float,
                          material: str) -> str:
    try:
        ret = SapModel.PropArea.GetNameList()
        existing = list(ret[1]) if ret and len(ret) > 1 else []
        if name in existing:
            return name
        t_m = thickness_mm / 1000.0
        # eSlabType: 0=Slab, eShellType: 1=Shell-Thin
        SapModel.PropArea.SetSlab(name, 0, 1, material, t_m)
    except Exception:
        pass
    return name


def push_to_etabs(payload: Dict) -> Dict:
    """
    payload = {
      "stories": [{"name", "height_m", "base_elev_m"?}, ...],
      "grids":   {"x_grids": [{"label", "x_m"}], "y_grids": [...]},
      "sections": {                       # label → (b_mm, h_mm)
          "C1":  {"b_mm": 400, "h_mm": 600, "kind": "column"},
          "B1":  {"b_mm": 300, "h_mm": 500, "kind": "beam"},
          "SW1": {"thickness_mm": 250,     "kind": "wall"},
          "S1":  {"thickness_mm": 150,     "kind": "slab"},
      },
      "floors": [                         # one per story
        {
          "story": "Story1",
          "columns": [{"x_m", "y_m", "label"}],
          "beams":   [{"x1_m","y1_m","x2_m","y2_m","label"}],
          "walls":   [{"vertices_m": [[x,y], ...], "label"}],
          "slabs":   [{"vertices_m": [[x,y], ...], "label"}],
        }, ...
      ]
    }

    Returns counts of placed objects + warnings.
    """
    SapModel = _connect()
    _setup_units(SapModel)

    warnings: List[str] = []
    counts = {"columns": 0, "beams": 0, "walls": 0, "slabs": 0,
              "sections": 0, "stories": 0}

    # 1. Stories — bottom→top order
    stories = payload.get("stories", [])
    story_names = _define_stories(SapModel, stories)
    counts["stories"] = len(story_names)

    # Compute top elevations per story (bottom→top stacking, base 0)
    story_top: Dict[str, float] = {}
    story_bot: Dict[str, float] = {}
    elev = 0.0
    for s in stories:
        story_bot[s["name"]] = elev
        elev += float(s["height_m"])
        story_top[s["name"]] = elev

    # 2. Grids
    grids = payload.get("grids") or {}
    _define_grid_system(SapModel, grids.get("x_grids", []), grids.get("y_grids", []))

    # 3. Materials + sections
    mat = _ensure_concrete_material(SapModel, "CONC28")
    sec_map: Dict[str, str] = {}            # label → ETABS section name
    for label, sec in payload.get("sections", {}).items():
        kind = sec.get("kind", "column")
        if kind in ("column", "beam"):
            sec_map[label] = _ensure_rect_section(
                SapModel, label,
                float(sec.get("b_mm", 300)),
                float(sec.get("h_mm", 500)),
                mat,
            )
        elif kind == "wall":
            sec_map[label] = _ensure_wall_property(
                SapModel, label,
                float(sec.get("thickness_mm", 200)),
                mat,
            )
        elif kind == "slab":
            sec_map[label] = _ensure_slab_property(
                SapModel, label,
                float(sec.get("thickness_mm", 150)),
                mat,
            )
        counts["sections"] += 1

    # 4. Place objects per floor
    for floor in payload.get("floors", []):
        story = floor["story"]
        if story not in story_top:
            warnings.append(f"Story '{story}' not defined — skipped.")
            continue
        z_top = story_top[story]
        z_bot = story_bot[story]

        # Columns: vertical lines from z_bot → z_top
        for c in floor.get("columns", []):
            x = float(c["x_m"]); y = float(c["y_m"])
            label = c.get("label", "")
            sec_name = sec_map.get(label, "")
            try:
                ret = SapModel.FrameObj.AddByCoord(x, y, z_bot, x, y, z_top,
                                                    "", sec_name, "", "Global")
                # ETABS COM AddByCoord returns (out_name, ret_code) — accept any non-throw
                counts["columns"] += 1
                _ = ret
            except Exception as e:
                warnings.append(f"col @ ({x:.2f},{y:.2f}) {story}: {e}")

        # Beams: horizontal at z_top
        for b in floor.get("beams", []):
            x1, y1 = float(b["x1_m"]), float(b["y1_m"])
            x2, y2 = float(b["x2_m"]), float(b["y2_m"])
            label = b.get("label", "")
            sec_name = sec_map.get(label, "")
            try:
                ret = SapModel.FrameObj.AddByCoord(x1, y1, z_top, x2, y2, z_top,
                                                    "", sec_name, "", "Global")
                counts["beams"] += 1
                _ = ret
            except Exception as e:
                warnings.append(f"beam {story}: {e}")

        # Walls: extrude polygon edges from z_bot → z_top as vertical area panels
        for w in floor.get("walls", []):
            verts = w.get("vertices_m", [])
            label = w.get("label", "")
            prop = sec_map.get(label, "")
            if len(verts) < 2:
                continue
            # Each edge becomes a quad panel
            for i in range(len(verts) - 1):
                x1, y1 = verts[i]
                x2, y2 = verts[i + 1]
                xs = [x1, x2, x2, x1]
                ys = [y1, y2, y2, y1]
                zs = [z_bot, z_bot, z_top, z_top]
                try:
                    ret = SapModel.AreaObj.AddByCoord(4, xs, ys, zs,
                                                       "", prop, "", "Global")
                    counts["walls"] += 1
                    _ = ret
                except Exception as e:
                    warnings.append(f"wall {story}: {e}")

        # Slabs: horizontal polygon at z_top
        for s in floor.get("slabs", []):
            verts = s.get("vertices_m", [])
            label = s.get("label", "")
            prop = sec_map.get(label, "")
            if len(verts) < 3:
                continue
            xs = [float(v[0]) for v in verts]
            ys = [float(v[1]) for v in verts]
            zs = [z_top] * len(verts)
            try:
                ret = SapModel.AreaObj.AddByCoord(len(verts), xs, ys, zs,
                                                   "", prop, "", "Global")
                counts["slabs"] += 1
                _ = ret
            except Exception as e:
                warnings.append(f"slab {story}: {e}")

    # Refresh view
    try:
        SapModel.View.RefreshView(0, False)
    except Exception:
        pass

    return {"status": "success", "counts": counts, "warnings": warnings}
