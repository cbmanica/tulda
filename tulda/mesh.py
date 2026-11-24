"""Triangle meshes in memory, STL reading/writing and a binary glTF (GLB) writer. Stdlib only.

A `Part` is world-space geometry as a flat float array, 9 floats (3 vertices) per triangle,
plus a name and a colour. CAD files are Z-up; the GLB keeps the CAD coordinates and puts a
single root node on top that rotates the scene to glTF's Y-up convention."""

from __future__ import annotations

import json
import math
import struct
import sys
from array import array
from dataclasses import dataclass, field

# Guardrails (see docs/PLAN.md, "Interactive viewer"): above WARN_TRIANGLES the sidecar
# records a warning (phones struggle around 1-2M triangles); above MAX_TRIANGLES the mesh
# step refuses. Decimation is future work.
WARN_TRIANGLES = 1_500_000
MAX_TRIANGLES = 6_000_000

DEFAULT_COLOR = "#B0B4BA"          # neutral grey for uncoloured geometry
GLB_MAGIC = b"glTF"


class MeshError(Exception):
    pass


@dataclass
class Part:
    name: str
    tris: array                    # 'f', 9 floats per triangle, world space (CAD units: mm, Z-up)
    color: str = DEFAULT_COLOR     # "#RRGGBB"
    alpha: float = 1.0
    obj: str | None = None         # owning object name (Bambu object / OpenSCAD model)
    extruder: int | None = None
    subtype: str = "normal_part"   # Bambu: modifier_part, support_blocker, ...
    visible: bool = True

    @property
    def n_tris(self) -> int:
        return len(self.tris) // 9


@dataclass
class Scene:
    parts: list[Part] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    extras: dict = field(default_factory=dict)      # e.g. {"bed": [x0, y0, x1, y1]}

    @property
    def n_tris(self) -> int:
        return sum(p.n_tris for p in self.parts)

    def bbox(self, only_visible: bool = True):
        lo = [math.inf] * 3
        hi = [-math.inf] * 3
        for p in self.parts:
            if only_visible and not p.visible:
                continue
            t = p.tris
            for k in range(3):
                col = t[k::3]
                if col:
                    lo[k] = min(lo[k], min(col))
                    hi[k] = max(hi[k], max(col))
        if lo[0] == math.inf:
            return None
        return [lo, hi]

    def check_size(self) -> None:
        n = self.n_tris
        if n == 0:
            raise MeshError("no triangles in the model")
        if n > MAX_TRIANGLES:
            raise MeshError(f"{n:,} triangles exceeds the {MAX_TRIANGLES:,} limit "
                            "(decimation is not implemented)")
        if n > WARN_TRIANGLES:
            self.warnings.append(f"large mesh: {n:,} triangles (over {WARN_TRIANGLES:,}); "
                                 "the interactive viewer may be slow on phones")


# ---- STL ---------------------------------------------------------------------------

def read_stl(data: bytes) -> array:
    """Binary or ASCII STL -> flat float array (9 per triangle)."""
    if len(data) >= 84:
        n = struct.unpack_from("<I", data, 80)[0]
        if len(data) == 84 + 50 * n:         # exact size match: binary
            out = array("f")
            if n:
                out.extend(v for i in range(n)
                           for v in struct.unpack_from("<9f", data, 84 + 50 * i + 12))
            return out
    text = data.decode("ascii", "replace")
    if "facet" not in text[:4096] and "solid" not in text[:256]:
        raise MeshError("not an STL file")
    toks = text.split()
    out = array("f")
    i = 0
    while i < len(toks):
        if toks[i] == "vertex":
            try:
                out.extend((float(toks[i + 1]), float(toks[i + 2]), float(toks[i + 3])))
            except (ValueError, IndexError) as e:
                raise MeshError("bad vertex in ASCII STL") from e
            i += 4
        else:
            i += 1
    if len(out) % 9:
        raise MeshError("ASCII STL vertex count is not a multiple of 3")
    return out


def write_stl(tris: array) -> bytes:
    """Flat float array -> binary STL (zero normals; readers recompute)."""
    n = len(tris) // 9
    buf = bytearray(b"tulda".ljust(80, b"\0"))
    buf += struct.pack("<I", n)
    z = b"\0" * 12
    for i in range(n):
        buf += z + struct.pack("<9fH", *tris[9 * i:9 * i + 9], 0)
    return bytes(buf)


# ---- GLB ---------------------------------------------------------------------------

def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def hex_to_rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    if len(h) not in (6, 8):
        raise MeshError(f"bad colour {h!r}")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _pad4(b: bytes, fill: bytes) -> bytes:
    return b + fill * (-len(b) % 4)


def write_glb(scene: Scene) -> bytes:
    """One glTF node per part (flat colour PBR material, no normals so viewers shade flat),
    grouped under one node per object, under a Z-up -> Y-up root. Node names are the part and
    object names; part details are in node `extras`."""
    if not scene.parts:
        raise MeshError("no parts to write")
    bin_chunk = bytearray()
    views, accessors, meshes, materials, nodes = [], [], [], [], []
    mat_index: dict[tuple, int] = {}
    obj_children: dict[str, list[int]] = {}
    obj_order: list[str] = []
    for p in scene.parts:
        if not p.n_tris:
            continue
        data = p.tris if sys.byteorder == "little" else array("f", p.tris)
        if sys.byteorder != "little":
            data.byteswap()
        raw = data.tobytes()
        off = len(bin_chunk)
        bin_chunk += raw
        bin_chunk += b"\0" * (-len(bin_chunk) % 4)
        views.append({"buffer": 0, "byteOffset": off, "byteLength": len(raw), "target": 34962})
        lo = [min(p.tris[k::3]) for k in range(3)]
        hi = [max(p.tris[k::3]) for k in range(3)]
        accessors.append({"bufferView": len(views) - 1, "componentType": 5126,
                          "count": len(p.tris) // 3, "type": "VEC3", "min": lo, "max": hi})
        key = (p.color.upper(), round(p.alpha, 3))
        if key not in mat_index:
            r, g, b = (_srgb_to_linear(c) for c in hex_to_rgb(p.color))
            m = {"name": p.color.upper(),
                 "pbrMetallicRoughness": {"baseColorFactor": [r, g, b, p.alpha],
                                          "metallicFactor": 0.0, "roughnessFactor": 0.55},
                 "doubleSided": True}
            if p.alpha < 1:
                m["alphaMode"] = "BLEND"
            mat_index[key] = len(materials)
            materials.append(m)
        meshes.append({"name": p.name, "primitives": [
            {"attributes": {"POSITION": len(accessors) - 1}, "material": mat_index[key], "mode": 4}]})
        extras = {"color": p.color.upper(), "triangles": p.n_tris, "visible": p.visible}
        if p.extruder is not None:
            extras["extruder"] = p.extruder
        if p.subtype != "normal_part":
            extras["subtype"] = p.subtype
        nodes.append({"name": p.name, "mesh": len(meshes) - 1, "extras": extras})
        oname = p.obj or p.name
        if oname not in obj_children:
            obj_children[oname] = []
            obj_order.append(oname)
        obj_children[oname].append(len(nodes) - 1)
    if not meshes:
        raise MeshError("no triangles to write")
    obj_nodes = []
    for oname in obj_order:
        nodes.append({"name": oname, "children": obj_children[oname]})
        obj_nodes.append(len(nodes) - 1)
    s = math.sqrt(0.5)
    nodes.append({"name": "tulda-root (Z-up to Y-up)", "rotation": [-s, 0, 0, s], "children": obj_nodes})
    root = len(nodes) - 1
    scene_extras = dict(scene.extras)
    bb = scene.bbox()
    if bb:
        scene_extras["bbox"] = bb
    scene_extras["triangles"] = scene.n_tris
    doc = {"asset": {"version": "2.0", "generator": "tulda"},
           "scene": 0, "scenes": [{"nodes": [root], "extras": scene_extras}],
           "nodes": nodes, "meshes": meshes, "materials": materials, "accessors": accessors,
           "bufferViews": views, "buffers": [{"byteLength": len(bin_chunk)}]}
    js = _pad4(json.dumps(doc, separators=(",", ":")).encode(), b" ")
    bn = bytes(bin_chunk)
    total = 12 + 8 + len(js) + 8 + len(bn)
    return (GLB_MAGIC + struct.pack("<II", 2, total)
            + struct.pack("<I4s", len(js), b"JSON") + js
            + struct.pack("<I4s", len(bn), b"BIN\0") + bn)


def read_glb_json(data: bytes) -> dict:
    """Parse the JSON chunk of a GLB (used by tests and sanity checks)."""
    if data[:4] != GLB_MAGIC:
        raise MeshError("not a GLB")
    ln, kind = struct.unpack_from("<I4s", data, 12)
    if kind != b"JSON":
        raise MeshError("first GLB chunk is not JSON")
    return json.loads(data[20:20 + ln])
