"""Built-in `turntable` adapter: .scad -> N PNG frames orbiting the model (no-WebGL viewer
fallback and drag-to-rotate widget).

One OpenSCAD export measures the bounding box, then every frame uses the same fixed camera
centre and distance (--camera without --viewall) so the model does not change size while it
turns. Frames are written to <workdir>/frames/f000.png ...; frame 0 is also the poster."""

from __future__ import annotations

import math
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .api import Context, Result
from .meshes import OPENSCAD_TIMEOUT, _openscad, run_openscad, scad_scene

DEFAULT_FRAMES = 36
MAX_FRAMES = 120
FOV_HALF = math.radians(22.5 / 2)     # OpenSCAD's perspective camera is 22.5 degrees
ELEVATION = 55                         # same tilt as the `iso` view
START_ANGLE = 25                       # iso's azimuth, so frame 0 matches `iso`


def camera_args(bbox, i: int, n: int, elevation: float = ELEVATION, start: float = START_ANGLE) -> list[str]:
    lo, hi = bbox
    c = [(a + b) / 2 for a, b in zip(lo, hi)]
    r = max(math.dist(lo, hi) / 2, 0.5)
    dist = r / math.sin(FOV_HALF) * 1.12
    rz = (start + 360.0 * i / n) % 360
    return [f"--camera={c[0]:.4f},{c[1]:.4f},{c[2]:.4f},{elevation:g},0,{rz:.3f},{dist:.3f}"]


def _frame(ctx: Context, bbox, i: int, n: int, out: Path, w: int, h: int, timeout: float) -> str:
    try:
        p = run_openscad(["-o", str(out), f"--imgsize={w},{h}", *camera_args(bbox, i, n),
                          str(ctx.in_path)], timeout)
    except subprocess.TimeoutExpired:
        return f"frame {i}: openscad timed out"
    except OSError as e:
        return f"frame {i}: cannot run openscad: {e}"
    if p.returncode != 0 or not out.is_file():
        return f"frame {i}: openscad failed: " + "\n".join((p.stderr or p.stdout).strip().splitlines()[-5:])
    return ""


def scad_turntable(ctx: Context) -> Result:
    try:
        n = int(ctx.options.get("frames", DEFAULT_FRAMES))
        w, h = int(ctx.options.get("frame_width", 800)), int(ctx.options.get("frame_height", 600))
        jobs = max(1, int(ctx.options.get("jobs", 4)))
    except (TypeError, ValueError) as e:
        return Result(False, f"bad turntable option: {e}")
    if not 2 <= n <= MAX_FRAMES:
        return Result(False, f"frames must be between 2 and {MAX_FRAMES}")
    scene, _fmt, err = scad_scene(ctx)
    if scene is None:
        return Result(False, err)
    bbox = scene.bbox()
    frames = ctx.workdir / "frames"
    shutil.rmtree(frames, ignore_errors=True)
    frames.mkdir(parents=True)
    timeout = float(ctx.options.get("frame_timeout", OPENSCAD_TIMEOUT))
    with ThreadPoolExecutor(max_workers=min(jobs, n)) as ex:
        errs = list(ex.map(lambda i: _frame(ctx, bbox, i, n, frames / f"f{i:03d}.png", w, h, timeout), range(n)))
    errs = [e for e in errs if e]
    if errs:
        return Result(False, errs[0] + (f" (+{len(errs) - 1} more frames failed)" if len(errs) > 1 else ""))
    shutil.copyfile(frames / "f000.png", ctx.out_path)
    return Result(meta={"render_info": {"frames": n, "frame_size": [w, h], "elevation": ELEVATION,
                                        "bbox": bbox, "format": "png"}})


TURNTABLE_PY = {"scad_turntable": scad_turntable}
