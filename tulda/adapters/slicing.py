"""Built-in `slice` adapter: .3mf -> slice summary page (+ the summary in the sidecar `info`).

Bambu Studio slices a COPY of the model in a scratch directory (the source file and its
repository are never touched):

    BambuStudio --slice N --export-3mf sliced.3mf --outputdir <abs workdir/out> <copy>.3mf

N is the `plate` option (default 1; `tulda render --plates` sets it per plate). `--export-3mf`
is relative to --outputdir. The result contains Metadata/plate_N.gcode and
Metadata/slice_info.config, which tulda.slicesummary parses; the summary records the plate.
The slicer logs a benign "Invalid T command (T65535)" error and may exit non-zero; success is
judged by the output file, retrying once with --no-check (skips the printable-area / validity pre-checks).

Embedded post-processing scripts: a 3mf's Metadata/project_settings.config may carry
`post_process`, a list of shell commands that the Bambu Studio / OrcaSlicer GUI runs after
exporting G-code. Their CLIs do not run them (the call is commented out upstream, and with the
default --normative_check a non-empty post_process makes the CLI exit with "post_process is not
supported under CLI"). The copy is neutralised anyway, defence in depth against a slicer build
or `bambu_bin` that behaves differently, and so such files still slice: `post_process` is
emptied in every JSON *.config member; a *.config that mentions post_process but is not JSON
is refused rather than sliced."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

from .. import slicesummary
from ..threemf import SCRIPT_KEYS     # config keys holding shell commands run by the slicer GUI
from .api import Context, Result

DEFAULT_BIN = "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio"
GCODE = "Metadata/plate_{}.gcode"
INFO = "Metadata/slice_info.config"

class UnsafeProject(Exception):
    pass


def _neutralise(data: bytes, name: str) -> bytes | None:
    """New bytes for a *.config member with SCRIPT_KEYS emptied, or None if nothing to change."""
    if not any(k.encode() in data for k in SCRIPT_KEYS):
        return None
    try:
        cfg = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        cfg = None
    if not isinstance(cfg, dict):
        raise UnsafeProject(f"{name} mentions post_process but is not a JSON object; not slicing")
    hits = [k for k in SCRIPT_KEYS if k in cfg]
    if not hits:
        return None                       # only mentioned inside some other value: harmless
    for k in hits:
        cfg[k] = []
    return json.dumps(cfg, indent=4, ensure_ascii=False).encode("utf-8")


def safe_copy(src: Path, dst: Path) -> list[str]:
    """Copy a 3mf for slicing with embedded post-processing commands removed.
    -> names of the members that were changed. Raises UnsafeProject, zipfile.BadZipFile."""
    changed = []
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for info in zin.infolist():
            if info.filename.lower().endswith(".config") and not info.is_dir():
                new = _neutralise(zin.read(info), info.filename)
                if new is not None:
                    changed.append(info.filename)
                    zout.writestr(info, new, compress_type=info.compress_type)
                    continue
            with zin.open(info) as r, zout.open(info, "w") as w:
                shutil.copyfileobj(r, w, 1 << 20)
    return changed


def _slice(binary: str, copy: Path, out_dir: Path, timeout: float, extra: list[str],
           plate: int = 1) -> tuple[Path | None, str]:
    argv = [binary, "--slice", str(plate), *extra, "--export-3mf", "sliced.3mf",
            "--outputdir", str(out_dir), str(copy)]
    try:
        # own session and no stdin: the Studio CLI can crash when attached to a terminal
        p = subprocess.run(argv, capture_output=True, text=True, errors="replace",
                           stdin=subprocess.DEVNULL, timeout=timeout, start_new_session=True)
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
    from ..dims import plate_option
    plate = plate_option(ctx.options) or 1
    gcode = GCODE.format(plate)
    src = ctx.in_path
    if not zipfile.is_zipfile(src):
        return Result(False, "not a 3MF (zip) file")
    # Work on a copy so the slicer can never write next to / into the source.
    indir, outdir = ctx.workdir / "in", ctx.workdir / "out"
    indir.mkdir(parents=True, exist_ok=True)
    outdir.mkdir(parents=True, exist_ok=True)
    copy = indir / "model.3mf"
    try:
        safe_copy(src, copy)
    except UnsafeProject as e:
        return Result(False, str(e))
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError) as e:
        return Result(False, f"cannot copy the 3MF for slicing: {e}")
    sliced, err = _slice(binary, copy, outdir, timeout, [], plate)
    if sliced is None and "timed out" not in err and "cannot run" not in err:
        sliced, err2 = _slice(binary, copy, outdir, timeout, ["--no-check"], plate)
        err = err2 or err
    if sliced is None:
        return Result(False, "slicing failed: " + err)
    try:
        with zipfile.ZipFile(sliced) as z:
            with z.open(gcode) as g:
                parsed = slicesummary.parse_gcode(io.TextIOWrapper(g, encoding="utf-8", errors="replace"))
            try:
                info = slicesummary.parse_slice_info(z.read(INFO).decode("utf-8", "replace"), plate)
            except KeyError:
                info = {}
    except (KeyError, zipfile.BadZipFile) as e:
        return Result(False, f"slicer output has no {gcode}: {e}")
    summary = slicesummary.summarize(parsed, info)
    summary["plate"] = plate
    ctx.out_path.write_text(slicesummary.render_html(summary, src.stem), encoding="utf-8")
    return Result(meta={"render_info": {"format": "slice-summary", **summary}})


SLICE_PY = {"bambu_slice": bambu_slice}
