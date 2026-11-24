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


def scad_mesh(ctx: Context) -> Result:
    stl = ctx.workdir / "model.stl"
    try:
        p = run_openscad(["--export-format", "binstl", "-o", str(stl), str(ctx.in_path)],
                         float(ctx.options.get("mesh_timeout", OPENSCAD_TIMEOUT)))
    except subprocess.TimeoutExpired:
        return Result(False, "openscad timed out")
    except OSError as e:
        return Result(False, f"cannot run openscad: {e}")
    if p.returncode != 0 or not stl.is_file():
        return Result(False, "openscad failed:\n" + "\n".join((p.stderr or p.stdout).strip().splitlines()[-15:]))
    scene = mesh.Scene([mesh.Part(ctx.in_path.stem, mesh.read_stl(stl.read_bytes()))])
    return _publish(ctx, scene, {"openscad_export": "binstl"})


def threemf_mesh(ctx: Context) -> Result:
    return _publish(ctx, threemf.load(ctx.in_path))


def stl_mesh(ctx: Context) -> Result:
    scene = mesh.Scene([mesh.Part(ctx.in_path.stem, mesh.read_stl(ctx.in_path.read_bytes()))])
    return _publish(ctx, scene)


MESH_PY = {"scad_mesh": scad_mesh, "threemf_mesh": threemf_mesh, "stl_mesh": stl_mesh}
