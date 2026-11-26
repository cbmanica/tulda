"""What a project folder contains: its files and, for every .3mf, the plates, filaments, print
settings, colour changes, slice estimates and post-processing scripts. Read-only; stdlib only.

Used by the Projects tab (`/_p/`: a compact per-row summary) and the project page
(`/_p/<project>/`, `/api/projects/<project>`). The folder is one found by projectspage (a
project folder directly inside a configured destination); files are listed by their path
relative to it, never by absolute path. Symlinks are neither followed nor listed, dot entries
are skipped, and depth and entry counts are capped.

3mf parsing is defensive: members are read through `threemf._read` (declared uncompressed
size capped per member, DTD/entity declarations refused) with small per-member limits, only
the few small metadata members are read (never the meshes or G-code), and every value is
reduced to short plain strings. The page escapes all of it.

Bambu Studio / OrcaSlicer metadata read here (all optional):
  * Metadata/model_settings.config (XML): objects (name, extruder, per-object/per-part setting
    overrides), plates (`plater_id`, `plater_name`, model instances, per-plate overrides);
  * Metadata/project_settings.config (JSON): the process/filament/printer settings;
  * Metadata/custom_gcode_per_layer.xml: colour changes, pauses and custom G-code per layer;
  * Metadata/slice_info.config: Bambu Studio's estimates per plate (print time, grams and
    metres per filament), present when the project was saved after slicing;
  * any `post_process` setting in a *.config member (threemf.find_post_process)."""

from __future__ import annotations

import json
import os
import re
import stat
import threading
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .mesh import MeshError

MAX_DEPTH = 4                  # folder levels listed below the project folder
MAX_FILES = 400                # entries listed per project
MAX_3MF = 20                   # 3mf files parsed per project page
MAX_MEMBERS = 20000            # zip members accepted in one 3mf
MEMBER_LIMITS = {"Metadata/model_settings.config": 8 << 20,
                 "Metadata/project_settings.config": 16 << 20,
                 "Metadata/custom_gcode_per_layer.xml": 4 << 20,
                 "Metadata/slice_info.config": 4 << 20}
MAX_VALUE = 200
MAX_OBJECTS = 200
MAX_PLATES = 100
MAX_CHANGES = 200
HEX = re.compile(r"#[0-9A-Fa-f]{6}")
_CTRL = re.compile(r"[\x00-\x1f\x7f]+")

TYPES = {".3mf": "3mf", ".stl": "stl", ".scad": "scad", ".step": "step", ".stp": "step",
         ".obj": "obj", ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image",
         ".gif": "image", ".pdf": "pdf", ".md": "text", ".txt": "text", ".json": "json",
         ".zip": "zip", ".gcode": "gcode", ".toml": "config", ".csv": "text",
         ".py": "script", ".sh": "script"}

# Process settings shown on the project page: (project_settings key, label).
SETTINGS = (
    ("print_settings_id", "Process profile"), ("printer_settings_id", "Printer profile"),
    ("printer_model", "Printer"), ("nozzle_diameter", "Nozzle (mm)"), ("curr_bed_type", "Bed"),
    ("layer_height", "Layer height (mm)"), ("initial_layer_print_height", "First layer (mm)"),
    ("wall_loops", "Walls"), ("top_shell_layers", "Top layers"),
    ("bottom_shell_layers", "Bottom layers"), ("sparse_infill_density", "Infill"),
    ("sparse_infill_pattern", "Infill pattern"), ("enable_support", "Supports"),
    ("support_type", "Support type"), ("support_threshold_angle", "Support angle"),
    ("brim_type", "Brim"), ("brim_width", "Brim width (mm)"),
    ("elefant_foot_compensation", "Elephant foot comp. (mm)"),
    ("xy_hole_compensation", "Hole compensation (mm)"),
    ("xy_contour_compensation", "Contour compensation (mm)"),
    ("seam_position", "Seam"), ("wall_generator", "Wall generator"),
    ("ironing_type", "Ironing"), ("enable_prime_tower", "Prime tower"),
    ("print_sequence", "Print sequence"), ("spiral_mode", "Spiral vase"),
    ("fuzzy_skin", "Fuzzy skin"),
)
BOOL_KEYS = {"enable_support", "enable_prime_tower", "spiral_mode"}
# model_settings.config metadata that is bookkeeping, not a setting override
OBJECT_STD = {"name", "extruder"}
PART_STD = {"name", "extruder", "matrix", "source_file", "source_object_id", "source_volume_id",
            "source_offset_x", "source_offset_y", "source_offset_z", "volume_type"}
PLATE_STD = {"plater_id", "plater_name", "locked", "filament_map_mode", "filament_maps",
             "filament_volume_maps", "thumbnail_file", "thumbnail_no_light_file", "top_file",
             "pick_file", "gcode_file", "pattern_file", "pattern_bbox_file", "limit_filament_maps"}
# custom_gcode_per_layer.xml `type` (CustomGCode::Type in Bambu Studio / PrusaSlicer)
CHANGE_TYPES = {"0": "colour change (M600)", "1": "pause", "2": "filament change",
                "3": "template", "4": "custom G-code"}


def clean(v, limit: int = MAX_VALUE) -> str | None:
    """A short single-line plain string (None when empty)."""
    if v is None:
        return None
    if isinstance(v, list):
        v = ", ".join(str(x) for x in v if x is not None and str(x) != "")
    v = " ".join(_CTRL.sub(" ", str(v)).split())
    return v[:limit] or None


def colour(v) -> str | None:
    """'#RRGGBB' (upper case) or None: the only form used in a style attribute."""
    if isinstance(v, str) and HEX.match(v.strip()):
        return v.strip()[:7].upper()
    return None


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) < 1e12 else None


def _pos_int(v) -> int | None:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if 0 < n < 100000 else None


def file_type(name: str) -> str:
    return TYPES.get(os.path.splitext(name)[1].lower(), "other")


# ---- folder listing ------------------------------------------------------------------------

def list_files(folder: Path) -> tuple[list[dict], bool]:
    """([{rel, name, type, size, mtime}], truncated) for the regular files below folder (depth
    MAX_DEPTH, at most MAX_FILES), sorted by relative path. Dot entries and symlinks are
    skipped; nothing is followed out of the folder."""
    out: list[dict] = []
    truncated = False

    def walk(d: Path, rel: str, depth: int):
        nonlocal truncated
        try:
            with os.scandir(d) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            return
        for e in entries:
            if e.name.startswith("."):
                continue
            if len(out) >= MAX_FILES:
                truncated = True
                return
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            r = f"{rel}{e.name}"
            if stat.S_ISDIR(st.st_mode):
                if depth < MAX_DEPTH:
                    walk(Path(e.path), r + "/", depth + 1)
                else:
                    truncated = True
            elif stat.S_ISREG(st.st_mode):
                out.append({"rel": r, "name": e.name, "type": file_type(e.name),
                            "size": st.st_size, "mtime": st.st_mtime})

    walk(folder, "", 1)
    out.sort(key=lambda f: f["rel"])
    return out, truncated


def safe_member_path(folder: Path, rel: str) -> Path | None:
    """folder/rel when it is a regular file below folder with no symlink and no dot entry on
    the way (rel as list_files gives it)."""
    parts = rel.split("/")
    if not rel or any(not p or p in (".", "..") or p.startswith(".") for p in parts):
        return None
    p = folder
    try:
        for i, part in enumerate(parts):
            p = p / part
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                return None
            if i < len(parts) - 1 and not stat.S_ISDIR(st.st_mode):
                return None
        return p if stat.S_ISREG(st.st_mode) else None
    except OSError:
        return None


# ---- 3mf details ---------------------------------------------------------------------------

def _xml(z: zipfile.ZipFile, name: str):
    """Parsed XML root of an optional metadata member, or None (missing, too large, DTD)."""
    from .threemf import _read
    try:
        data = _read(z, name, MEMBER_LIMITS[name])
    except MeshError:
        return None
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        return None
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        return None


def _local(tag) -> str:
    return tag.rpartition("}")[2] if isinstance(tag, str) else ""


def _meta(el) -> dict:
    return {m.get("key"): m.get("value") for m in el
            if _local(m.tag) == "metadata" and m.get("key") is not None}


def _overrides(md: dict, std: set) -> dict:
    out = {}
    for k, v in md.items():
        if k in std or not isinstance(k, str) or len(out) >= 40:
            continue
        ck = clean(k, 60)
        if ck:
            out[ck] = clean(v) or ""
    return out


def _model_settings(z) -> tuple[dict, list[dict]]:
    """-> ({object id: {name, extruders, overrides, parts: [{name, extruder, overrides}]}},
    [plate {n, name, instances: [object id], overrides, gcode}])."""
    root = _xml(z, "Metadata/model_settings.config")
    objects: dict = {}
    plates: list = []
    if root is None:
        return objects, plates
    for el in root:
        t = _local(el.tag)
        if t == "object" and len(objects) < MAX_OBJECTS:
            md = _meta(el)
            ext = _pos_int(md.get("extruder"))
            parts = []
            for p in el:
                if _local(p.tag) != "part" or len(parts) >= 50:
                    continue
                pm = _meta(p)
                parts.append({"name": clean(pm.get("name"), 100),
                              "subtype": clean(p.get("subtype"), 40) or "normal_part",
                              "extruder": _pos_int(pm.get("extruder")),
                              "overrides": _overrides(pm, PART_STD)})
            objects[str(el.get("id"))] = {"name": clean(md.get("name"), 100), "extruder": ext,
                                          "overrides": _overrides(md, OBJECT_STD), "parts": parts}
        elif t == "plate" and len(plates) < MAX_PLATES:
            md = _meta(el)
            n = _pos_int(md.get("plater_id"))
            if n is None:
                continue
            inst = []
            for mi in el:
                if _local(mi.tag) == "model_instance":
                    oid = _meta(mi).get("object_id")
                    if oid is not None and str(oid) not in inst:
                        inst.append(str(oid))
            plates.append({"n": n, "name": clean(md.get("plater_name"), 100), "objects": inst,
                           "overrides": _overrides(md, PLATE_STD),
                           "gcode": bool(md.get("gcode_file"))})
    plates.sort(key=lambda p: p["n"])
    return objects, plates


def _project_settings(z) -> dict:
    from .threemf import _read
    try:
        raw = _read(z, "Metadata/project_settings.config", MEMBER_LIMITS["Metadata/project_settings.config"])
        js = json.loads(raw.decode("utf-8-sig"))
    except (MeshError, ValueError, UnicodeDecodeError):
        return {}
    return js if isinstance(js, dict) else {}


def _setting_value(key: str, v) -> str | None:
    if isinstance(v, list):
        vals = [clean(x, 60) for x in v[:16]]
        vals = [x for x in vals if x]
        if not vals:
            return None
        v = vals[0] if len(set(vals)) == 1 else ", ".join(vals)
    v = clean(v)
    if v is not None and key in BOOL_KEYS and v in ("0", "1"):
        v = "on" if v == "1" else "off"
    return v


def _changes(z) -> list[dict]:
    root = _xml(z, "Metadata/custom_gcode_per_layer.xml")
    out: list[dict] = []
    if root is None:
        return out

    def layers(el, plate):
        for ly in el:
            if _local(ly.tag) != "layer" or len(out) >= MAX_CHANGES:
                continue
            t = str(ly.get("type") or "")
            g = clean(ly.get("gcode"), 120)
            out.append({"plate": plate, "z": _num(ly.get("top_z")),
                        "type": CHANGE_TYPES.get(t, f"type {clean(t, 10) or '?'}"),
                        "extruder": _pos_int(ly.get("extruder")), "colour": colour(ly.get("color")),
                        "filament": t in ("0", "2"),      # switches filament from this layer on
                        "gcode": g if t in ("3", "4") else None})

    plated = False
    for el in root:
        if _local(el.tag) == "plate":
            plated = True
            n = 1
            for pi in el:
                if _local(pi.tag) == "plate_info":
                    n = _pos_int(pi.get("id")) or 1
            layers(el, n)
    if not plated:
        layers(root, 1)
    out.sort(key=lambda c: (c["plate"], c["z"] if c["z"] is not None else 0))
    return out


def _slice_info(z) -> dict[int, dict]:
    """{plate: {prediction_s, weight_g, filaments: [{id, type, colour, used_g, used_m}]}} for
    the plates whose slice_info.config entry carries Bambu Studio's estimate."""
    root = _xml(z, "Metadata/slice_info.config")
    out: dict[int, dict] = {}
    if root is None:
        return out
    for pl in root:
        if _local(pl.tag) != "plate" or len(out) >= MAX_PLATES:
            continue
        md = _meta(pl)
        n = _pos_int(md.get("index"))
        pred = _num(md.get("prediction"))
        if n is None or pred is None:
            continue
        fils = []
        for f in pl:
            if _local(f.tag) == "filament" and len(fils) < 64:
                fils.append({"id": _pos_int(f.get("id")), "type": clean(f.get("type"), 40),
                             "colour": colour(f.get("color")), "used_g": _num(f.get("used_g")),
                             "used_m": _num(f.get("used_m"))})
        out[n] = {"prediction_s": pred, "weight_g": _num(md.get("weight")), "filaments": fils,
                  "support_used": md.get("support_used") == "true"}
    return out


def _empty() -> dict:
    return {"error": None, "plates": [], "filaments": [], "settings": [], "changed": [],
            "overrides": [], "changes": [], "post_process": [], "bambu": False,
            "variable_layers": False, "application": None}


def read_3mf(path: Path) -> dict:
    """Plates, filaments, settings, colour changes, stored slice estimates and post-processing
    scripts of one 3mf (see module docstring). `error` is set when it is not readable."""
    from .threemf import find_post_process
    out = _empty()
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        out["error"] = f"not a readable 3mf ({clean(e, 120)})"
        return out
    with z:
        names = z.namelist()
        if len(names) > MAX_MEMBERS:
            out["error"] = f"too many zip members ({len(names):,})"
            return out
        nameset = set(names)
        try:
            from .threemf import read_provenance
            out["application"] = clean(read_provenance(path).get("application"), 80)
        except MeshError:
            pass
        objects, plates = _model_settings(z)
        ps = _project_settings(z)
        changes = _changes(z)
        stored = _slice_info(z)
        out["variable_layers"] = "Metadata/layer_heights_profile.txt" in nameset
    out["bambu"] = bool(objects or plates or ps)
    try:
        out["post_process"] = [{"config": clean(s.get("config"), 120), "key": clean(s.get("key"), 40),
                                "command": str(s.get("command") or "")[:2000]}
                               for s in find_post_process(path)]
    except MeshError as e:
        out["post_process"] = [{"config": None, "key": "post_process",
                                "command": f"(could not scan: {clean(e, 120)})"}]

    def obj_extruders(o: dict) -> list[int]:
        exts = [p["extruder"] for p in o["parts"] if p["extruder"] and p["subtype"] == "normal_part"]
        base = o["extruder"] or 1
        if not o["parts"] or any(p["extruder"] is None and p["subtype"] == "normal_part" for p in o["parts"]):
            exts.append(base)
        return sorted(set(exts)) or [base]

    if not plates:
        plates = [{"n": 1, "name": None, "objects": list(objects), "overrides": {}, "gcode": False}]
    for pl in plates:
        objs = []
        used: set[int] = set()
        for oid in pl["objects"]:
            o = objects.get(oid)
            exts = obj_extruders(o) if o else [1]
            used.update(exts)
            objs.append({"id": oid, "name": (o or {}).get("name") or f"object {oid}", "filaments": exts})
        for c in changes:
            if c["plate"] == pl["n"] and c["extruder"] and c["filament"]:
                used.add(c["extruder"])
        pl.update(objects=objs, filaments=sorted(used), stored=stored.get(pl["n"]))
    out["plates"] = plates

    cols = ps.get("filament_colour") if isinstance(ps.get("filament_colour"), list) else []
    types = ps.get("filament_type") if isinstance(ps.get("filament_type"), list) else []
    vendors = ps.get("filament_vendor") if isinstance(ps.get("filament_vendor"), list) else []
    profiles = ps.get("filament_settings_id") if isinstance(ps.get("filament_settings_id"), list) else []
    nfil = min(max(len(cols), len(types)), 64)
    if not nfil and any(pl["filaments"] for pl in plates):
        nfil = min(max(max(pl["filaments"] or [1]) for pl in plates), 64)
    for i in range(nfil):
        n = i + 1
        users = []
        for pl in plates:
            names = [o["name"] for o in pl["objects"] if n in o["filaments"]]
            ch = [c for c in changes if c["plate"] == pl["n"] and c["extruder"] == n and c["filament"]]
            if names:
                users.append(f"plate {pl['n']}: " + ", ".join(names))
            for c in ch:
                users.append(f"plate {pl['n']}: from Z {c['z']:g} mm" if c["z"] is not None
                             else f"plate {pl['n']}: {c['type']}")
        out["filaments"].append({
            "n": n, "colour": colour(cols[i]) if i < len(cols) else None,
            "type": clean(types[i], 40) if i < len(types) else None,
            "vendor": clean(vendors[i], 60) if i < len(vendors) else None,
            "profile": clean(profiles[i], 100) if i < len(profiles) else None,
            "used_by": users[:40]})
    for key, label in SETTINGS:
        if key in ps:
            v = _setting_value(key, ps[key])
            if v is not None:
                out["settings"].append({"key": key, "label": label, "value": v})
    diff = ps.get("different_settings_to_system")
    if isinstance(diff, list) and diff and isinstance(diff[0], str):
        out["changed"] = [k for k in (clean(x, 60) for x in diff[0].split(";")[:60]) if k]
    for oid, o in objects.items():
        if o["overrides"]:
            out["overrides"].append({"object": o["name"] or f"object {oid}", "part": None,
                                     "settings": o["overrides"]})
        for p in o["parts"]:
            if p["overrides"]:
                out["overrides"].append({"object": o["name"] or f"object {oid}",
                                         "part": p["name"], "settings": p["overrides"]})
    out["changes"] = changes
    return out


_cache: dict = {}
_cache_lock = threading.Lock()


def details(path: Path) -> dict:
    """read_3mf, cached on (size, mtime, inode)."""
    try:
        st = os.stat(path)
    except OSError:
        return {**_empty(), "error": "unreadable"}
    ident = (st.st_size, st.st_mtime_ns, st.st_ino)
    key = str(path)
    with _cache_lock:
        hit = _cache.get(key)
    if hit and hit[0] == ident:
        return hit[1]
    d = read_3mf(path)
    with _cache_lock:
        if len(_cache) > 512:
            _cache.clear()
        _cache[key] = (ident, d)
    return d


# ---- summaries -----------------------------------------------------------------------------

def folder_summary(folder: Path) -> dict:
    """Compact facts for the Projects list: {counts: {type: n}, files, truncated, threemf,
    plates, post_process}."""
    files, truncated = list_files(folder)
    counts: dict[str, int] = {}
    for f in files:
        counts[f["type"]] = counts.get(f["type"], 0) + 1
    plates = 0
    scripts = False
    for f in [f for f in files if f["type"] == "3mf"][:MAX_3MF]:
        p = safe_member_path(folder, f["rel"])
        if p is None:
            continue
        d = details(p)
        plates += len(d["plates"]) if not d["error"] else 0
        scripts = scripts or bool(d["post_process"])
    return {"counts": counts, "files": len(files), "truncated": truncated,
            "threemf": counts.get("3mf", 0), "plates": plates, "post_process": scripts}


TYPE_ORDER = ("3mf", "stl", "scad", "step", "obj", "gcode", "image", "pdf", "text", "json",
              "config", "script", "zip", "other")


def summary_text(s: dict | None) -> str:
    """'2 × 3mf (3 plates), 9 × scad, 1 × text' (plain text)."""
    if not s or not s.get("files"):
        return "no files" if s else ""
    parts = []
    for t in sorted(s["counts"], key=lambda t: (TYPE_ORDER.index(t) if t in TYPE_ORDER else 99, t)):
        n = s["counts"][t]
        p = f"{n} × {t}"
        if t == "3mf":
            p += f" ({s['plates']} plate{'s' * (s['plates'] != 1)})"
        parts.append(p)
    return ", ".join(parts) + (" (listing truncated)" if s.get("truncated") else "")
