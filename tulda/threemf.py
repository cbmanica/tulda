"""3MF (zip + XML) -> `mesh.Scene`. Stdlib only.

Handles the core 3MF spec: meshes, `<components>` assemblies with transforms, the `<build>`
item transforms (the plate placement in Bambu/Orca files), the unit attribute, and the
production extension's `p:path` references to other model files.

Colours (verified against Bambu Studio 02.09.01.70 project files):
  * Bambu: `Metadata/model_settings.config` (XML) names each top-level `<object id>` and its
    `<part id>`s (the part id equals the component's object id) with `extruder` N and a
    `subtype` (normal_part, modifier_part, support_blocker, ...); `Metadata/project_settings.config`
    (JSON) has the `filament_colour` list, so part colour = filament_colour[extruder - 1].
    Objects missing from model_settings.config (single-mesh objects often are) use extruder 1.
    The unrelated `extruder_colour` key is NOT used (it was a stale one-entry list in the real file).
  * Generic 3MF / OpenSCAD: `<basematerials>` displaycolor via `pid`/`pindex` (per object) or
    `pid`/`p1` (per triangle). Triangles are split into one part per material.
  * Neither: a default palette indexed by extruder.
Not handled: Bambu paint-on colours (`paint_color` per triangle; absent in every file checked),
texture/colorgroup extensions. `load(path, plate=N)` keeps one Bambu/Orca plate."""

from __future__ import annotations

import html
import json
import re
import zipfile
from array import array
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from .mesh import DEFAULT_COLOR, MeshError, Part, Scene

MAX_XML = 400 * 1024 * 1024        # refuse absurd uncompressed members (zip bombs)
UNIT_MM = {"micron": 0.001, "millimeter": 1.0, "centimeter": 10.0, "inch": 25.4,
           "foot": 304.8, "meter": 1000.0}
IDENT = (1.0, 0, 0, 0, 1.0, 0, 0, 0, 1.0, 0, 0, 0)


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _read(z: zipfile.ZipFile, name: str, limit: int = MAX_XML) -> bytes:
    try:
        info = z.getinfo(name)
    except KeyError:
        raise MeshError(f"missing {name} in 3mf") from None
    if info.file_size > limit:
        raise MeshError(f"{name} is {info.file_size:,} bytes uncompressed (limit {limit:,})")
    data = z.read(info)
    if b"<!ENTITY" in data[:4096] or b"<!DOCTYPE" in data[:512] and b"[" in data[:2048]:
        raise MeshError(f"{name}: DTD/entity declarations are not accepted")
    return data


def parse_transform(s: str | None) -> tuple:
    """3MF transform: 12 numbers m00 m01 m02 m10 m11 m12 m20 m21 m22 m30 m31 m32, applied to
    row vectors: v' = v * M + t."""
    if not s:
        return IDENT
    try:
        v = tuple(float(x) for x in s.split())
    except ValueError as e:
        raise MeshError(f"bad transform {s!r}") from e
    if len(v) != 12:
        raise MeshError(f"transform needs 12 numbers, got {len(v)}")
    return v


def compose(a: tuple, b: tuple) -> tuple:
    """Apply a first, then b (row-vector convention)."""
    r = []
    for i in range(3):
        for j in range(3):
            r.append(a[3 * i] * b[j] + a[3 * i + 1] * b[3 + j] + a[3 * i + 2] * b[6 + j])
    for j in range(3):
        r.append(a[9] * b[j] + a[10] * b[3 + j] + a[11] * b[6 + j] + b[9 + j])
    return tuple(r)


def det3(m: tuple) -> float:
    return (m[0] * (m[4] * m[8] - m[5] * m[7]) - m[1] * (m[3] * m[8] - m[5] * m[6])
            + m[2] * (m[3] * m[7] - m[4] * m[6]))


@dataclass
class Obj:
    id: str
    file: str
    name: str | None = None
    verts: list = field(default_factory=list)       # [(x, y, z)]
    tris: list = field(default_factory=list)        # [(a, b, c, pid, pindex)]
    components: list = field(default_factory=list)  # [(file, objectid, transform)]
    pid: str | None = None
    pindex: int = 0
    is_mesh: bool = False


@dataclass
class Model:
    objects: dict = field(default_factory=dict)     # (file, id) -> Obj
    build: list = field(default_factory=list)       # [(file, objectid, transform, instance)]
    materials: dict = field(default_factory=dict)   # (file, pid) -> [(name, "#RRGGBB", alpha)]
    unit_scale: float = 1.0
    title: str | None = None
    application: str | None = None


def _parse_model(z, model: Model, name: str, main: bool) -> None:
    root = ET.fromstring(_read(z, name))
    if main:
        model.unit_scale = UNIT_MM.get(root.get("unit", "millimeter"), 1.0)
    for el in root:
        tag = _local(el.tag)
        if tag == "metadata" and main:
            if el.get("name") == "Title":
                model.title = (el.text or "").strip() or None
            elif el.get("name") == "Application":
                model.application = (el.text or "").strip() or None
        elif tag == "resources":
            for r in el:
                rt = _local(r.tag)
                if rt == "basematerials":
                    mats = []
                    for b in r:
                        c = (b.get("displaycolor") or "").lstrip("#")
                        if re.fullmatch(r"[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", c):
                            a = int(c[6:8], 16) / 255 if len(c) == 8 else 1.0
                            mats.append((b.get("name") or "", "#" + c[:6].upper(), a))
                        else:
                            mats.append((b.get("name") or "", DEFAULT_COLOR, 1.0))
                    model.materials[(name, r.get("id"))] = mats
                elif rt == "object":
                    model.objects[(name, r.get("id"))] = _parse_object(r, name)
        elif tag == "build" and main:
            seen: dict = {}
            for it in el:
                if _local(it.tag) == "item":
                    # instance = n-th build item of this object (Bambu plates name instances so)
                    inst = seen[it.get("objectid")] = seen.get(it.get("objectid"), -1) + 1
                    if it.get("printable") == "0":
                        continue
                    model.build.append((name, it.get("objectid"), parse_transform(it.get("transform")), inst))


def _parse_object(el, fname: str) -> Obj:
    o = Obj(id=el.get("id"), file=fname, name=el.get("name"), pid=el.get("pid"))
    try:
        o.pindex = int(el.get("pindex") or 0)
    except ValueError:
        o.pindex = 0
    for ch in el:
        t = _local(ch.tag)
        if t == "mesh":
            o.is_mesh = True
            for m in ch:
                mt = _local(m.tag)
                if mt == "vertices":
                    o.verts = [(float(v.get("x")), float(v.get("y")), float(v.get("z"))) for v in m]
                elif mt == "triangles":
                    for tr in m:
                        pid = tr.get("pid")
                        p1 = tr.get("p1")
                        o.tris.append((int(tr.get("v1")), int(tr.get("v2")), int(tr.get("v3")),
                                       pid, int(p1) if p1 is not None else None))
        elif t == "components":
            for c in ch:
                if _local(c.tag) != "component":
                    continue
                path = None
                for k, v in c.attrib.items():
                    if _local(k) == "path":
                        path = v.lstrip("/")
                o.components.append((path or fname, c.get("objectid"), parse_transform(c.get("transform"))))
    return o


@dataclass
class Leaf:
    """One mesh instance in world space (millimetres)."""
    obj: Obj
    xf: tuple
    top: tuple                  # (file, id) of the build item's top-level object
    comp_id: str | None         # id of the component object when reached through a component
    item: int = 0               # index into Model.build


def _flatten(model: Model):
    leaves: list[Leaf] = []

    def walk(key, xf, top, comp_id, depth, item):
        if depth > 16:
            raise MeshError("component nesting too deep (cycle?)")
        o = model.objects.get(key)
        if o is None:
            raise MeshError(f"component refers to unknown object {key[1]} in {key[0]}")
        if o.is_mesh:
            leaves.append(Leaf(o, xf, top, comp_id, item))
        for f, oid, cxf in o.components:
            walk((f, oid), compose(cxf, xf), top, oid if depth == 0 else comp_id, depth + 1, item)

    for i, (f, oid, xf, _inst) in enumerate(model.build):
        walk((f, oid), xf, (f, oid), None, 0, i)
    return leaves


def _tri_array(o: Obj, tri_idx: list[int], xf: tuple, scale: float) -> array:
    m = xf
    out = array("f")
    flip = det3(m) < 0
    V = o.verts
    tv = [((x * m[0] + y * m[3] + z * m[6] + m[9]) * scale,
           (x * m[1] + y * m[4] + z * m[7] + m[10]) * scale,
           (x * m[2] + y * m[5] + z * m[8] + m[11]) * scale) for x, y, z in V]
    for i in tri_idx:
        a, b, c = o.tris[i][:3]
        if flip:
            b, c = c, b
        try:
            out.extend(tv[a]); out.extend(tv[b]); out.extend(tv[c])
        except IndexError:
            raise MeshError(f"object {o.id}: triangle refers to a missing vertex") from None
    return out


PALETTE = ["#9AA0A6", "#F2994A", "#56CCF2", "#6FCF97", "#BB6BD9", "#EB5757", "#F2C94C", "#2D9CDB"]
SUBTYPE_COLORS = {"modifier_part": "#4C8DFF", "negative_part": "#FF8A3D",
                  "support_blocker": "#FF5050", "support_enforcer": "#3DDC84"}
HEX = re.compile(r"#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?")


@dataclass
class Config:
    objects: dict = field(default_factory=dict)     # id -> {"name", "extruder", "parts": {id: {...}}}
    filaments: list = field(default_factory=list)   # ["#RRGGBB", ...] (project filament_colour)
    bed: list | None = None                         # [x0, y0, x1, y1] mm
    printer: str | None = None
    found: bool = False


def _opt_text(z, name: str, limit: int = 64 * 1024 * 1024) -> bytes | None:
    try:
        return _read(z, name, limit)
    except MeshError:
        return None


def _meta(el) -> dict:
    return {m.get("key"): m.get("value") for m in el if _local(m.tag) == "metadata"}


def _int(v, default=None):
    try:
        n = int(v)
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default


def read_config(z: zipfile.ZipFile) -> Config:
    cfg = Config()
    raw = _opt_text(z, "Metadata/model_settings.config")
    if raw:
        try:
            for o in ET.fromstring(raw):
                if _local(o.tag) != "object":
                    continue
                md = _meta(o)
                parts = {}
                for p in o:
                    if _local(p.tag) == "part":
                        pm = _meta(p)
                        parts[p.get("id")] = {"name": pm.get("name"), "extruder": _int(pm.get("extruder")),
                                              "subtype": p.get("subtype") or "normal_part"}
                cfg.objects[o.get("id")] = {"name": md.get("name"), "extruder": _int(md.get("extruder")),
                                            "parts": parts}
            cfg.found = True
        except ET.ParseError:
            cfg.objects = {}
    raw = _opt_text(z, "Metadata/project_settings.config")
    if raw:
        try:
            import json
            js = json.loads(raw)
        except ValueError:
            js = {}
        fc = js.get("filament_colour") if isinstance(js, dict) else None
        if isinstance(fc, list):
            cfg.filaments = ["#" + c.lstrip("#")[:6].upper() if isinstance(c, str) and HEX.fullmatch(c) else None
                             for c in fc]
        pa = js.get("printable_area") if isinstance(js, dict) else None
        if isinstance(pa, list):
            try:
                pts = [tuple(float(n) for n in str(x).split("x")) for x in pa]
                xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                cfg.bed = [min(xs), min(ys), max(xs), max(ys)]
            except (ValueError, IndexError):
                cfg.bed = None
        pm = js.get("printer_model") if isinstance(js, dict) else None
        cfg.printer = pm if isinstance(pm, str) else None
    return cfg


def filament_color(cfg: Config, extruder: int) -> tuple[str, str]:
    """-> (colour, source) for a 1-based extruder number."""
    if 0 < extruder <= len(cfg.filaments) and cfg.filaments[extruder - 1]:
        return cfg.filaments[extruder - 1], "filament_colour"
    return PALETTE[(extruder - 1) % len(PALETTE)], "palette"


def main_model_name(z: zipfile.ZipFile) -> str:
    """The root model part named by `_rels/.rels` (default `3D/3dmodel.model`)."""
    main = "3D/3dmodel.model"
    try:
        rels = _read(z, "_rels/.rels", 1 << 20).decode("utf-8", "replace")
        m = re.search(r'Target="([^"]+)"[^>]*Type="[^"]*3dmodel"|Type="[^"]*3dmodel"[^>]*Target="([^"]+)"', rels)
        if m:
            main = (m.group(1) or m.group(2)).lstrip("/")
    except MeshError:
        pass
    return main


# ---- provenance (self-reported <metadata> of the root model) ------------------------------

# 3mf metadata name -> provenance key. Bambu writes both "Copyright" and "CopyRight" (one of them
# often empty); the first non-empty one wins.
PROVENANCE_FIELDS = {"Title": "title", "Designer": "designer", "License": "license",
                     "Origin": "origin", "Copyright": "copyright", "CopyRight": "copyright",
                     "DesignModelId": "design_model_id", "DesignProfileId": "design_profile_id",
                     "DesignRegion": "design_region", "Application": "application"}
PROVENANCE_KEYS = ("title", "designer", "license", "copyright", "origin", "design_model_id",
                   "design_profile_id", "design_region", "application")
MAX_META_VALUE = 500               # characters kept per field
MAX_META_SCAN = 256 * 1024 * 1024  # uncompressed bytes of the root model streamed for metadata
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean(v: str | None) -> str | None:
    v = _CTRL.sub("", v or "").strip()
    if v[:1] == "[" and v[-1:] == "]" or v[:1] == "{" and v[-1:] == "}":
        # Bambu writes an empty list for unset fields ("[]"); an empty container is no value.
        # A non-empty list of strings reads as its items joined with ", ".
        try:
            js = json.loads(v)
        except ValueError:
            js = None
        if isinstance(js, dict) and not js:
            return None
        if isinstance(js, list):
            if not js:
                return None
            if all(isinstance(x, str) for x in js):
                v = ", ".join(x.strip() for x in js if x.strip())
    return v[:MAX_META_VALUE] or None


def source_kind(design_model_id: str | None) -> str | None:
    """'parametric' for Parametric Model Maker outputs (FMI_ ids), 'makerworld' for other
    MakerWorld design ids, None when there is no id. Never used to build a URL."""
    if not design_model_id:
        return None
    return "parametric" if design_model_id.startswith("FMI_") else "makerworld"


def read_provenance(path) -> dict:
    """Self-reported provenance of a .3mf: {key: str | None} for PROVENANCE_KEYS plus `kind`
    (see source_kind). Bambu writes metadata both before <resources> and after <build>, so the
    whole root model is streamed, dropping finished elements (flat memory); DTDs/entities are
    refused and the uncompressed size is capped (declared and actual). Raises MeshError when
    the file is not a readable 3mf."""
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise MeshError(f"not a readable 3mf: {e}") from e
    out: dict = dict.fromkeys(PROVENANCE_KEYS)
    with z:
        name = main_model_name(z)
        try:
            info = z.getinfo(name)
        except KeyError:
            raise MeshError(f"missing {name} in 3mf") from None
        if info.file_size > MAX_META_SCAN:
            raise MeshError(f"{name} is {info.file_size:,} bytes uncompressed (limit {MAX_META_SCAN:,})")
        parser = ET.XMLPullParser(events=("start", "end"))
        stack: list = []
        read = 0
        tail = b""
        try:
            with z.open(info) as f:
                while read < MAX_META_SCAN:
                    chunk = f.read(min(256 * 1024, MAX_META_SCAN - read))
                    if not chunk:
                        break
                    probe = tail + chunk          # catch markers split across chunks
                    if b"<!DOCTYPE" in probe or b"<!ENTITY" in probe:
                        raise MeshError(f"{name}: DTD/entity declarations are not accepted")
                    tail = chunk[-16:]
                    read += len(chunk)
                    parser.feed(chunk)
                    for ev, el in parser.read_events():
                        if ev == "start":
                            stack.append(el)
                            continue
                        stack.pop()
                        if len(stack) == 1 and _local(el.tag) == "metadata":
                            key = PROVENANCE_FIELDS.get(el.get("name") or "")
                            if key and not out[key]:
                                out[key] = _clean(el.text)
                        if stack:                 # drop finished elements: memory stays flat
                            stack[-1].remove(el)
                if read >= MAX_META_SCAN and f.read(1):
                    raise MeshError(f"{name} exceeds {MAX_META_SCAN:,} bytes uncompressed")
                parser.close()
        except ET.ParseError as e:
            # a truncated or malformed head: keep what was read before the error
            if not any(out.values()):
                raise MeshError(f"{name}: malformed XML ({e})") from e
        except (zipfile.BadZipFile, OSError, EOFError, RuntimeError) as e:
            raise MeshError(f"{name}: unreadable ({e})") from e
    out["kind"] = source_kind(out["design_model_id"])
    return out


def _load_model(z: zipfile.ZipFile) -> Model:
    """The root model plus the production extension's component files."""
    main = main_model_name(z)
    model = Model()
    _parse_model(z, model, main, True)
    seen = {main}
    todo = [c[0] for o in list(model.objects.values()) for c in o.components]
    while todo:
        f = todo.pop()
        if f in seen:
            continue
        seen.add(f)
        _parse_model(z, model, f, False)
        todo += [c[0] for o in model.objects.values() if o.file == f for c in o.components]
    if not model.build:
        raise MeshError("3mf has no printable build items")
    return model


def _leaf_subtype(model: Model, cfg: Config, lf: Leaf) -> str:
    ocfg = cfg.objects.get(model.objects[lf.top].id) or {}
    pcfg = (ocfg.get("parts") or {}).get(lf.comp_id) if lf.comp_id else None
    return (pcfg or {}).get("subtype") or "normal_part"


def read_plates(z: zipfile.ZipFile) -> list[tuple[int, set]]:
    """Bambu/Orca plates from Metadata/model_settings.config: [(plate number,
    {(object id, instance index)})] in plate order. [] when the file has none."""
    raw = _opt_text(z, "Metadata/model_settings.config")
    out = []
    if not raw:
        return out
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return out
    for pl in root:
        if _local(pl.tag) != "plate":
            continue
        n = _int(_meta(pl).get("plater_id"))
        if n is None:
            continue
        inst = set()
        for mi in pl:
            if _local(mi.tag) == "model_instance":
                md = _meta(mi)
                try:
                    inst.add((md.get("object_id"), int(md.get("instance_id") or 0)))
                except ValueError:
                    continue
        out.append((n, inst))
    return sorted(out, key=lambda p: p[0])


def _union(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return [[min(a[0][k], b[0][k]) for k in range(3)], [max(a[1][k], b[1][k]) for k in range(3)]]


def build_bboxes(path) -> dict:
    """Bounding boxes in millimetres (unit and every transform applied) of the printable
    geometry (normal parts; modifiers and blockers are left out, as in the viewer):
    {"build": [[min], [max]] of all build items, "plates": [(n, bbox or None)] for Bambu/Orca
    plates (empty when the file has none)}. Raises MeshError for unreadable files."""
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise MeshError(f"not a readable 3mf: {e}") from e
    with z:
        model = _load_model(z)
        cfg = read_config(z)
        plates = read_plates(z)
        leaves = _flatten(model)
    s = model.unit_scale
    per_item: dict[int, list] = {}
    for lf in leaves:
        o = lf.obj
        if not o.tris or not o.verts or _leaf_subtype(model, cfg, lf) != "normal_part":
            continue
        m = lf.xf
        used = {i for t in o.tris for i in t[:3]}
        try:
            pts = [o.verts[i] for i in used]
        except IndexError:
            raise MeshError(f"object {o.id}: triangle refers to a missing vertex") from None
        lo, hi = [], []
        for k in range(3):
            vals = [x * m[k] + y * m[3 + k] + z * m[6 + k] + m[9 + k] for x, y, z in pts]
            lo.append(min(vals) * s)
            hi.append(max(vals) * s)
        per_item[lf.item] = _union(per_item.get(lf.item), [lo, hi])
    build = None
    for bb in per_item.values():
        build = _union(build, bb)
    if build is None:
        raise MeshError("no printable geometry in the 3mf")
    out_plates = []
    for n, inst in plates:
        bb = None
        for i, (_f, oid, _xf, k) in enumerate(model.build):
            if (oid, k) in inst:
                bb = _union(bb, per_item.get(i))
        out_plates.append((n, bb))
    return {"build": build, "plates": out_plates}


def load(path, plate: int | None = None) -> Scene:
    """Read a .3mf into a Scene: one Part per mesh instance (per material). plate: keep only the
    build items of that Bambu/Orca plate (a file without plates has just plate 1); MeshError
    when the file has no such plate."""
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise MeshError(f"not a readable 3mf: {e}") from e
    with z:
        model = _load_model(z)
        cfg = read_config(z)
        leaves = _flatten(model)
        plates = read_plates(z) if plate is not None else []
    if plate is not None and (plates or plate != 1):
        inst = next((s for n, s in plates if n == plate), None)
        if inst is None:
            raise MeshError(f"the 3mf has no plate {plate}")
        keep = {i for i, (_f, oid, _xf, k) in enumerate(model.build) if (oid, k) in inst}
        leaves = [lf for lf in leaves if lf.item in keep]
        if not leaves:
            raise MeshError(f"plate {plate} is empty")
    scene = Scene()
    scene.extras["source"] = {"title": model.title, "application": model.application}
    sources: set[str] = set()
    scale = model.unit_scale
    for lf in leaves:
        o = lf.obj
        if not o.tris:
            continue
        top = model.objects[lf.top]
        ocfg = cfg.objects.get(top.id) or {}
        pcfg = (ocfg.get("parts") or {}).get(lf.comp_id) if lf.comp_id else None
        pcfg = pcfg or {}
        obj_name = ocfg.get("name") or top.name or f"object {top.id}"
        name = pcfg.get("name") or (ocfg.get("name") if not lf.comp_id else None) or o.name or f"object {o.id}"
        subtype = pcfg.get("subtype") or "normal_part"
        extruder = pcfg.get("extruder") or ocfg.get("extruder") or 1
        groups = _material_groups(model, o)
        if groups is not None:
            # generic 3MF / OpenSCAD: colours come from base materials
            multi = len(groups) > 1
            sources.add("3mf-materials")
            for (mname, col, alpha), idx in groups.items():
                scene.parts.append(Part(
                    name=f"{name} ({mname or col})" if multi else name, obj=obj_name, extruder=None,
                    tris=_tri_array(o, idx, lf.xf, scale), color=col, alpha=alpha,
                    subtype=subtype, visible=subtype == "normal_part"))
            continue
        if subtype != "normal_part":
            col, alpha = SUBTYPE_COLORS.get(subtype, "#8888FF"), 0.4
        else:
            col, src = filament_color(cfg, extruder)
            sources.add(src)
            alpha = 1.0
        scene.parts.append(Part(name=name, obj=obj_name, extruder=extruder, color=col, alpha=alpha,
                                tris=_tri_array(o, list(range(len(o.tris))), lf.xf, scale),
                                subtype=subtype, visible=subtype == "normal_part"))
    if plate is not None:
        scene.extras["plate"] = plate
    scene.extras["colors"] = sorted(sources)
    if "palette" in sources:
        scene.warnings.append("no usable filament_colour for some extruders; used a default palette")
    if cfg.filaments:
        scene.extras["filament_colours"] = [c for c in cfg.filaments if c]
    if cfg.bed:
        scene.extras["bed"] = cfg.bed
    if cfg.printer:
        scene.extras["printer"] = cfg.printer
    scene.check_size()
    return scene


def _material_groups(model: Model, o: Obj):
    """{(name, colour, alpha): [triangle index]} when the object carries base materials, else None."""
    if not (o.pid or any(t[3] for t in o.tris)):
        return None
    groups: dict = {}
    for i, t in enumerate(o.tris):
        pid = t[3] or o.pid
        pidx = t[4] if t[4] is not None else o.pindex
        mats = model.materials.get((o.file, pid)) if pid else None
        key = mats[pidx] if mats and 0 <= pidx < len(mats) else ("", DEFAULT_COLOR, 1.0)
        groups.setdefault(key, []).append(i)
    return groups


# ---- embedded post-processing scripts --------------------------------------------------------

# Slicer settings whose values are shell commands the slicer GUI runs after exporting G-code
# (Bambu Studio / OrcaSlicer / PrusaSlicer "Post-processing scripts"). Print-host settings
# (print_host, printhost_*) only name upload targets and are not commands.
SCRIPT_KEYS = ("post_process",)
MAX_CONFIG_SCAN = 64 * 1024 * 1024      # bytes of one *.config member scanned
_XML_KV = re.compile(r"""<[^<>]*\bkey\s*=\s*["']({k})["'][^<>]*\bvalue\s*=\s*(["'])(.*?)\2"""
                     r"""|<[^<>]*\bvalue\s*=\s*(["'])(.*?)\4[^<>]*\bkey\s*=\s*["']({k})["']""",
                     re.S)
_INI_KV = re.compile(r"^[ \t;#]*({k})[ \t]*=[ \t]*(.*?)[ \t]*$", re.M)


def _json_hits(obj, path: str = "") -> list[tuple[str, str]]:
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in SCRIPT_KEYS:
                vals = v if isinstance(v, list) else [v]
                text = "\n".join(str(x) for x in vals if x is not None and str(x).strip())
                if text.strip():
                    out.append((k, text))
            elif isinstance(v, (dict, list)):
                out += _json_hits(v)
    elif isinstance(obj, list):
        for v in obj:
            out += _json_hits(v)
    return out


def config_post_process(data: bytes) -> list[tuple[str, str]]:
    """[(key, command text)] for non-empty script settings in one *.config member, in any of the
    formats slicers write: JSON (Bambu/Orca project and process settings), XML metadata
    key/value (model_settings.config) or `key = value` lines (PrusaSlicer). A mention of a
    script key that none of these parsers can read is reported as unparsed (conservative)."""
    names = [k.encode() for k in SCRIPT_KEYS]
    if not any(n in data for n in names):
        return []
    text = data.decode("utf-8", "replace").lstrip("﻿")
    try:
        return _json_hits(json.loads(text))
    except ValueError:
        pass
    keys = "|".join(re.escape(k) for k in SCRIPT_KEYS)
    hits, recognised = [], False
    for m in re.compile(_XML_KV.pattern.format(k=keys), re.S).finditer(text):
        recognised = True
        key, val = (m.group(1), m.group(3)) if m.group(1) else (m.group(6), m.group(5))
        val = html.unescape(val)
        if val.strip():
            hits.append((key, val))
    for m in re.compile(_INI_KV.pattern.format(k=keys), re.M).finditer(text):
        recognised = True
        val = m.group(2).strip().strip('"')
        if val:
            hits.append((m.group(1), val.replace("\\n", "\n")))
    if not recognised:
        hits.append((SCRIPT_KEYS[0], "(present in a format tulda cannot parse; inspect the file)"))
    return hits


def find_post_process(path) -> list[dict]:
    """[{"config": member name, "key": ..., "command": text}] for every non-empty
    post-processing script setting in a 3mf's *.config members. Raises MeshError when the file
    is not a readable zip."""
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise MeshError(f"not a readable 3mf: {e}") from e
    out = []
    with z:
        for info in z.infolist():
            if info.is_dir() or not info.filename.lower().endswith(".config"):
                continue
            try:
                with z.open(info) as f:
                    data = f.read(MAX_CONFIG_SCAN)
            except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, EOFError) as e:
                out.append({"config": info.filename, "key": SCRIPT_KEYS[0],
                            "command": f"(member cannot be read: {e}; inspect the file)"})
                continue
            for key, cmd in config_post_process(data):
                out.append({"config": info.filename, "key": key, "command": cmd})
    return out
