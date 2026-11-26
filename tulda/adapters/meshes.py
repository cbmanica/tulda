"""Built-in `mesh` adapters: source files -> binary glTF (.glb) for the interactive viewer.

    scad_mesh   .scad -> OpenSCAD export -> GLB
    threemf_mesh .3mf  -> tulda.threemf -> GLB   (Bambu/Orca/OpenSCAD 3MF)
    stl_mesh    .stl  -> GLB

Each returns a Result whose meta carries `render_info`, which the runner stores in the
sidecar as `info` (triangle count, bounding box, parts, warnings)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .. import mesh, threemf
from .api import Context, Result
from .probes import openscad_backend

OPENSCAD_TIMEOUT = 180


def _info(scene: mesh.Scene, extra: dict | None = None) -> dict:
    info = {"format": "glb", "triangles": scene.n_tris, "bbox": scene.bbox(),
            "parts": [{"name": p.name, "object": p.obj, "color": p.color,
                       "triangles": p.n_tris, "visible": p.visible}
                      for p in scene.parts[:200]]}
    if len(scene.parts) > 200:
        info["parts_truncated"] = len(scene.parts)
    if scene.warnings:
        info["warnings"] = list(scene.warnings)
    info.update(scene.extras)
    info.update(extra or {})
    return info


def _publish(ctx: Context, scene: mesh.Scene, meta: dict | None = None) -> Result:
    scene.check_size()
    ctx.out_path.write_bytes(mesh.write_glb(scene))
    return Result(meta={"render_info": _info(scene), **(meta or {})})


def run_openscad(args: list[str], timeout: float = OPENSCAD_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(["openscad", *openscad_backend(), *args], capture_output=True,
                          text=True, errors="replace", timeout=timeout)


OPENSCAD_DEFAULT = "#F9D72C"        # colour OpenSCAD gives uncoloured geometry in 3MF exports


def _openscad(ctx: Context, out: Path, *extra: str) -> tuple[bool, str]:
    try:
        p = run_openscad([*extra, "-o", str(out), str(ctx.in_path)],
                         float(ctx.options.get("mesh_timeout", OPENSCAD_TIMEOUT)))
    except subprocess.TimeoutExpired:
        return False, "openscad timed out"
    except OSError as e:
        return False, f"cannot run openscad: {e}"
    if p.returncode != 0 or not out.is_file():
        return False, "openscad failed:\n" + "\n".join((p.stderr or p.stdout).strip().splitlines()[-15:])
    return True, ""


def scad_scene(ctx: Context) -> tuple[mesh.Scene | None, str, str]:
    """-> (scene, export format used, error). Tries a 3MF export first (keeps color() as
    per-part colours), then binary STL (single colour)."""
    stem = ctx.in_path.stem
    ok, err3 = _openscad(ctx, ctx.workdir / "model.3mf")
    if ok:
        try:
            sc = threemf.load(ctx.workdir / "model.3mf")
            sc.extras.pop("source", None)
            if {p.color for p in sc.parts} == {OPENSCAD_DEFAULT}:
                for p in sc.parts:
                    p.color = mesh.DEFAULT_COLOR       # no color() calls: neutral grey, not yellow
            for p in sc.parts:
                p.name = stem if len(sc.parts) == 1 else f"{stem} {p.color}"
                p.obj = stem
            return sc, "3mf", ""
        except mesh.MeshError as e:
            err3 = str(e)
    ok, err = _openscad(ctx, ctx.workdir / "model.stl", "--export-format", "binstl")
    if not ok:
        return None, "", err or err3
    sc = mesh.Scene([mesh.Part(stem, mesh.read_stl((ctx.workdir / "model.stl").read_bytes()), obj=stem)])
    sc.extras["colors"] = ["default"]
    return sc, "binstl", ""


def scad_mesh(ctx: Context) -> Result:
    scene, fmt, err = scad_scene(ctx)
    if scene is None:
        return Result(False, err)
    return _publish(ctx, scene, {"openscad_export": fmt})


def threemf_mesh(ctx: Context) -> Result:
    """The whole build, or one plate when the `plate` option is set (`tulda render --plates`);
    the plate is then recorded in the sidecar info."""
    from ..dims import plate_option
    return _publish(ctx, threemf.load(ctx.in_path, plate_option(ctx.options)))


def stl_mesh(ctx: Context) -> Result:
    scene = mesh.Scene([mesh.Part(ctx.in_path.stem, mesh.read_stl(ctx.in_path.read_bytes()))])
    return _publish(ctx, scene)


MESH_PY = {"scad_mesh": scad_mesh, "threemf_mesh": threemf_mesh, "stl_mesh": stl_mesh}
