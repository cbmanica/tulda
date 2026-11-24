"""3MF (zip + XML) -> `mesh.Scene`. Stdlib only.

Handles the core 3MF spec: meshes, `<components>` assemblies with transforms, the `<build>`
item transforms (the plate placement in Bambu/Orca files), the unit attribute, and the
production extension's `p:path` references to other model files. Colours: see `colors`
section below (Bambu extruder -> filament colour, 3MF base materials)."""

from __future__ import annotations

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
    build: list = field(default_factory=list)       # [(file, objectid, transform)]
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
            for it in el:
                if _local(it.tag) == "item":
                    if it.get("printable") == "0":
                        continue
                    model.build.append((name, it.get("objectid"), parse_transform(it.get("transform"))))


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


def _flatten(model: Model):
    leaves: list[Leaf] = []

    def walk(key, xf, top, comp_id, depth):
        if depth > 16:
            raise MeshError("component nesting too deep (cycle?)")
        o = model.objects.get(key)
        if o is None:
            raise MeshError(f"component refers to unknown object {key[1]} in {key[0]}")
        if o.is_mesh:
            leaves.append(Leaf(o, xf, top, comp_id))
        for f, oid, cxf in o.components:
            walk((f, oid), compose(cxf, xf), top, oid if depth == 0 else comp_id, depth + 1)

    for f, oid, xf in model.build:
        walk((f, oid), xf, (f, oid), None, 0)
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


def load(path, config: bool = True) -> Scene:
    """Read a .3mf into a Scene: one Part per mesh instance (per colour)."""
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise MeshError(f"not a readable 3mf: {e}") from e
    with z:
        main = "3D/3dmodel.model"
        try:
            rels = _read(z, "_rels/.rels", 1 << 20).decode("utf-8", "replace")
            m = re.search(r'Target="([^"]+)"[^>]*Type="[^"]*3dmodel"|Type="[^"]*3dmodel"[^>]*Target="([^"]+)"', rels)
            if m:
                main = (m.group(1) or m.group(2)).lstrip("/")
        except MeshError:
            pass
        model = Model()
        _parse_model(z, model, main, True)
        # production-extension component files
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
        leaves = _flatten(model)
        scene = Scene()
        scene.extras["source"] = {"title": model.title, "application": model.application}
        scale = model.unit_scale
        for n, lf in enumerate(leaves, 1):
            o = lf.obj
            if not o.tris:
                continue
            scene.parts.append(Part(
                name=o.name or f"object {o.id}", obj=model.objects[lf.top].name or f"object {lf.top[1]}",
                tris=_tri_array(o, list(range(len(o.tris))), lf.xf, scale)))
        scene.check_size()
        return scene
