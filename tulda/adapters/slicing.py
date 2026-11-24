"""Built-in `slice` adapter: .3mf -> slice summary page (+ the summary in the sidecar `info`).

Bambu Studio slices a COPY of the model in a scratch directory (the source file and its
repository are never touched):

    BambuStudio --slice 1 --export-3mf sliced.3mf --outputdir <abs workdir/out> <copy>.3mf

`--export-3mf` is relative to --outputdir. The result contains Metadata/plate_1.gcode and
Metadata/slice_info.config, which tulda.slicesummary parses. The slicer logs a benign
"Invalid T command (T65535)" error and may exit non-zero; success is judged by the output
file, retrying once with --no-check (skips the printable-area / validity pre-checks)."""

from __future__ import annotations

import io
import shutil
import subprocess
import zipfile
from pathlib import Path

from .. import slicesummary
from .api import Context, Result

DEFAULT_BIN = "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio"
GCODE = "Metadata/plate_1.gcode"
INFO = "Metadata/slice_info.config"


def _slice(binary: str, copy: Path, out_dir: Path, timeout: float, extra: list[str]) -> tuple[Path | None, str]:
    argv = [binary, "--slice", "1", *extra, "--export-3mf", "sliced.3mf",
            "--outputdir", str(out_dir), str(copy)]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, errors="replace",
                           timeout=timeout, start_new_session=True)
    except subprocess.TimeoutExpired:
        return None, f"slicer timed out after {timeout:g}s"
    except OSError as e:
        return None, f"cannot run slicer: {e}"
    out = out_dir / "sliced.3mf"
    if out.is_file() and out.stat().st_size > 0:
        return out, ""
    return None, "\n".join((p.stderr or p.stdout).strip().splitlines()[-8:]) or f"slicer exit {p.returncode}"


def bambu_slice(ctx: Context) -> Result:
    binary = str(ctx.options.get("bambu_bin") or DEFAULT_BIN)
    timeout = float(ctx.options.get("slice_timeout", 180))
    src = ctx.in_path
    if not zipfile.is_zipfile(src):
        return Result(False, "not a 3MF (zip) file")
    # Work on a copy so the slicer can never write next to / into the source.
    indir, outdir = ctx.workdir / "in", ctx.workdir / "out"
    indir.mkdir(parents=True, exist_ok=True)
    outdir.mkdir(parents=True, exist_ok=True)
    copy = indir / "model.3mf"
    shutil.copyfile(src, copy)
    sliced, err = _slice(binary, copy, outdir, timeout, [])
    if sliced is None and "timed out" not in err and "cannot run" not in err:
        sliced, err2 = _slice(binary, copy, outdir, timeout, ["--no-check"])
        err = err2 or err
    if sliced is None:
        return Result(False, "slicing failed: " + err)
    try:
        with zipfile.ZipFile(sliced) as z:
            with z.open(GCODE) as g:
                parsed = slicesummary.parse_gcode(io.TextIOWrapper(g, encoding="utf-8", errors="replace"))
            try:
                info = slicesummary.parse_slice_info(z.read(INFO).decode("utf-8", "replace"))
            except KeyError:
                info = {}
    except (KeyError, zipfile.BadZipFile) as e:
        return Result(False, f"slicer output has no {GCODE}: {e}")
    summary = slicesummary.summarize(parsed, info)
    ctx.out_path.write_text(slicesummary.render_html(summary, src.stem), encoding="utf-8")
    return Result(meta={"render_info": {"format": "slice-summary", **summary}})


SLICE_PY = {"bambu_slice": bambu_slice}
