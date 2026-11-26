"""Model dimensions: the bounding box X x Y x Z in millimetres of a render's source.

Stored in the render sidecar as `dims` and in the catalog. Computed from the actual geometry:

    .stl   the triangles (ASCII or binary)
    .3mf   the build: unit attribute, build-item and component transforms applied, normal parts
           only. Bambu/Orca projects with several plates are measured per plate (the render's
           `plate` option, else plate 1) and list every plate.
    .scad  the interactive render's mesh when the same run produced one, else an OpenSCAD STL
           export of the source (only for chains that already use OpenSCAD).
    .glb   tulda's own GLB: the scene bbox it records (backfill of old renders).

A record: {"size": [x, y, z], "min": [...], "max": [...], "scope": "model" | "plate" | "build",
"from": "stl" | "3mf" | "scad" | "glb" | "mesh", "plate": n, "plates": [{"plate", "size"}]}.
"scope" says what was measured: the whole model; one plate of a multi-plate project; or the
whole build of a 3mf whose plates are unknown (a GLB backfill). Stdlib only."""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

from . import mesh
from .mesh import MeshError

SCOPES = ("model", "plate", "build")
FROMS = ("stl", "3mf", "scad", "glb", "mesh", "sibling")
MAX_MM = 1e7
MAX_PLATES = 64
UNKNOWN = "size unknown"
_cache: dict = {}


def _num3(v) -> list | None:
    if not isinstance(v, (list, tuple)) or len(v) != 3:
        return None
    out = []
    for x in v:
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or abs(x) > MAX_MM:
            return None
        out.append(round(float(x), 4))
    return out


def from_bbox(bb, scope: str = "model", src: str = "mesh") -> dict | None:
    """[[min x, y, z], [max x, y, z]] -> a dims record (None when the box is unusable)."""
    if not isinstance(bb, (list, tuple)) or len(bb) != 2:
        return None
    lo, hi = _num3(bb[0]), _num3(bb[1])
    if lo is None or hi is None or any(h < l for l, h in zip(lo, hi)):
        return None
    return {"size": [round(h - l, 4) for l, h in zip(lo, hi)], "min": lo, "max": hi,
            "scope": scope, "from": src}


def clean(v) -> dict | None:
    """A sidecar's `dims` reduced to validated fields (or None)."""
    if not isinstance(v, dict):
        return None
    size = _num3(v.get("size"))
    if size is None or min(size) < 0:
        return None
    out = {"size": size, "scope": v.get("scope") if v.get("scope") in SCOPES else "model",
           "from": v.get("from") if v.get("from") in FROMS else None}
    for k in ("min", "max"):
        if _num3(v.get(k)) is not None:
            out[k] = _num3(v.get(k))
    if out["scope"] == "plate":
        p = v.get("plate")
        if not isinstance(p, int) or isinstance(p, bool) or not 0 < p < 1000:
            out["scope"] = "build"
        else:
            out["plate"] = p
    plates = []
    for p in (v.get("plates") if isinstance(v.get("plates"), list) else [])[:MAX_PLATES]:
        if isinstance(p, dict) and isinstance(p.get("plate"), int) and not isinstance(p.get("plate"), bool):
            s = _num3(p.get("size"))
            plates.append({"plate": p["plate"], "size": s if s and min(s) >= 0 else None})
    if plates:
        out["plates"] = plates
    return out


def _n(x: float) -> str:
    return f"{x:.1f}"


def size_text(size) -> str:
    return " × ".join(_n(x) for x in size) + " mm"


def scope_text(d: dict) -> str:
    """'' for a whole model; 'plate 1 of 4' / 'whole build' otherwise."""
    if d.get("scope") == "plate":
        n = len(d.get("plates") or [])
        return f"plate {d.get('plate')}" + (f" of {n}" if n else "")
    if d.get("scope") == "build":
        return "whole build"
    return ""


def fmt(d: dict | None) -> str:
    """'60.0 x 46.8 x 13.8 mm', plus '(plate 1 of 4)' or '(whole build)' when that is the scope."""
    if not d or not d.get("size"):
        return UNKNOWN
    s = size_text(d["size"])
    sc = scope_text(d)
    return f"{s} ({sc})" if sc else s


# ---- per format ------------------------------------------------------------------------

def for_stl(path) -> dict:
    tris = mesh.read_stl(Path(path).read_bytes())
    bb = mesh.Scene([mesh.Part("m", tris)]).bbox()
    d = from_bbox(bb, "model", "stl")
    if d is None:
        raise MeshError("STL has no triangles")
    return d


def for_3mf(path, plate: int | None = None) -> dict:
    from .threemf import build_bboxes
    r = build_bboxes(path)
    plates = [(n, bb) for n, bb in r["plates"]]
    if len(plates) > 1:
        want = plate if any(n == plate for n, _ in plates) else plates[0][0]
        bb = next(b for n, b in plates if n == want)
        d = from_bbox(bb, "plate", "3mf") if bb else None
        if d is None:                       # empty plate: report the build, labelled
            d = from_bbox(r["build"], "build", "3mf")
        else:
            d["plate"] = want
        d["plates"] = [{"plate": n, "size": (from_bbox(b) or {}).get("size") if b else None}
                       for n, b in plates[:MAX_PLATES]]
        return d
    d = from_bbox(r["build"], "model", "3mf")
    if d is None:
        raise MeshError("3mf has no measurable geometry")
    return d


def for_glb(path, source_type: str | None = None) -> dict | None:
    """tulda's GLB: the scene's recorded bbox (visible parts), else the accessors' min/max of
    visible nodes. A 3mf's GLB holds every plate, so it is labelled the whole build."""
    data = Path(path).read_bytes()
    js = mesh.read_glb_json(data)
    scope = "build" if source_type == "3mf" else "model"
    bb = ((js.get("scenes") or [{}])[0].get("extras") or {}).get("bbox")
    d = from_bbox(bb, scope, "glb")
    if d:
        return d
    lo, hi = [math.inf] * 3, [-math.inf] * 3
    acc = js.get("accessors") or []
    for node in js.get("nodes") or []:
        if "mesh" not in node or (node.get("extras") or {}).get("visible") is False:
            continue
        for prim in js["meshes"][node["mesh"]].get("primitives", []):
            a = acc[prim["attributes"]["POSITION"]]
            for k in range(3):
                lo[k] = min(lo[k], a["min"][k])
                hi[k] = max(hi[k], a["max"][k])
    return from_bbox([lo, hi], scope, "glb") if lo[0] != math.inf else None


def for_scad(path, timeout: float = 180) -> dict:
    """OpenSCAD binary STL export of the source, measured."""
    from .adapters.meshes import run_openscad
    import subprocess
    with tempfile.TemporaryDirectory(prefix="tulda-dims-") as td:
        out = Path(td) / "m.stl"
        try:
            p = run_openscad(["--export-format", "binstl", "-o", str(out), str(path)], timeout)
        except subprocess.TimeoutExpired:
            raise MeshError("openscad timed out") from None
        except OSError as e:
            raise MeshError(f"cannot run openscad: {e}") from e
        if p.returncode != 0 or not out.is_file():
            raise MeshError("openscad STL export failed")
        d = for_stl(out)
    d["from"] = "scad"
    return d


def compute(src: Path, sha: str | None = None, plate: int | None = None,
            mesh_bbox=None, scad: bool = False) -> dict:
    """Dimensions of a source file (cached per path + content hash + plate for one process,
    so the views of one request measure once). mesh_bbox: the bbox of a mesh this run made
    from the source (used for .scad instead of a second OpenSCAD run). Raises MeshError."""
    src = Path(src)
    ext = src.suffix.lower()
    key = (str(src), sha, plate if ext == ".3mf" else None)
    if sha and key in _cache:
        return dict(_cache[key])
    if ext == ".3mf":
        d = for_3mf(src, plate)
    elif ext == ".stl":
        d = for_stl(src)
    elif ext == ".scad" and from_bbox(mesh_bbox):
        d = from_bbox(mesh_bbox, "model", "mesh")
    elif ext == ".scad" and scad:
        d = for_scad(src)
    elif from_bbox(mesh_bbox):
        d = from_bbox(mesh_bbox, "model", "mesh")
    else:
        raise MeshError(f"no way to measure a {ext or 'extensionless'} source here")
    if sha:
        _cache[key] = dict(d)
    return d


def plate_option(options: dict | None) -> int | None:
    try:
        n = int((options or {}).get("plate"))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


# ---- backfill (`tulda db dims`) ------------------------------------------------------------

def _source_candidates(meta: dict) -> list[Path]:
    """Absolute file arguments of the recorded step commands that carry the source's name."""
    name = meta.get("source")
    out = []
    for st in meta.get("steps") if isinstance(meta.get("steps"), list) else []:
        cmd = st.get("cmd") if isinstance(st, dict) else None
        for a in cmd if isinstance(cmd, list) else []:
            if isinstance(a, str) and a.startswith("/") and isinstance(name, str) and Path(a).name == name:
                out.append(Path(a))
    return out


def backfill(root: Path, scad: bool = True, dry_run: bool = False, log=print) -> dict:
    """Add `dims` to render sidecars that lack it. Per render, in order: the source file named
    in the recorded command (only while its sha256 still equals the recorded source_sha256);
    the render's own GLB; another render of the same request and source content. Renders with
    none of these stay "size unknown". Returns counts."""
    import json
    from .items import RENDER_EXTS, is_project_dir
    from .runner import _atomic_write, sha256
    root = Path(root).resolve()
    st = {"renders": 0, "had": 0, "source": 0, "glb": 0, "sibling": 0, "unknown": 0, "errors": 0}
    todo, have = [], {}
    paths: dict = {}                    # (source name, sha) -> verified source path
    hashed: dict = {}
    for pdir in sorted(root.iterdir()) if root.is_dir() else []:
        if not is_project_dir(root, pdir):
            continue
        for side in sorted(pdir.glob("*.json")):
            if side.name.endswith(".fail.json") or side.name.startswith("."):
                continue
            rend = next((side.with_suffix(e) for e in RENDER_EXTS if side.with_suffix(e).is_file()), None)
            if rend is None:
                continue
            try:
                meta = json.loads(side.read_text())
            except (OSError, ValueError):
                st["errors"] += 1
                continue
            if not isinstance(meta, dict):
                continue
            st["renders"] += 1
            key = (meta.get("group_id"), meta.get("source"), meta.get("source_sha256"))
            sha = meta.get("source_sha256")
            for cand in _source_candidates(meta):
                if (meta.get("source"), sha) in paths or not sha:
                    break
                try:
                    if cand not in hashed:
                        hashed[cand] = sha256(cand) if cand.is_file() else None
                except OSError:
                    hashed[cand] = None
                if hashed[cand] == sha:
                    paths[(meta.get("source"), sha)] = cand
            d = clean(meta.get("dims"))
            if d:
                st["had"] += 1
                if d.get("scope") != "plate":
                    have.setdefault(key, d)
                continue
            todo.append((side, rend, meta, key))
    pending = []
    for side, rend, meta, key in todo:
        d, how = None, None
        src = paths.get((meta.get("source"), meta.get("source_sha256")))
        info = meta.get("info") if isinstance(meta.get("info"), dict) else {}
        if src is not None:
            ext = src.suffix.lower()
            try:
                if ext in (".3mf", ".stl") or (ext == ".scad" and (scad or from_bbox(info.get("bbox")))):
                    d = compute(src, meta.get("source_sha256"), plate_option(meta.get("options")),
                                info.get("bbox"), scad)
                    how = "source"
            except (MeshError, OSError, ValueError) as e:
                log(f"{side.parent.name}/{side.name}: {e}")
        if d is None and rend.suffix.lower() == ".glb":
            try:
                d = for_glb(rend, meta.get("source_type"))
                how = "glb" if d else None
            except (MeshError, OSError, ValueError, KeyError, IndexError, TypeError) as e:
                log(f"{side.parent.name}/{side.name}: {e}")
        if d is not None and d.get("scope") != "plate":
            have.setdefault(key, d)
        pending.append((side, meta, d, how, key))
    for side, meta, d, how, key in pending:
        if d is None:
            sib = have.get(key)
            if sib is None:
                st["unknown"] += 1
                continue
            d, how = {**sib, "from": "sibling"}, "sibling"
        st[how] += 1
        if dry_run:
            continue
        meta["dims"] = d
        try:
            _atomic_write(side, json.dumps(meta, indent=2).encode())
        except OSError as e:
            st["errors"] += 1
            log(f"{side.parent.name}/{side.name}: cannot write ({e})")
    return st
