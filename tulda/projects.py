"""Local project folders for MakerWorld downloads the user already saved by hand.

`tulda project create FILE --destination DIR` and `tulda watch DIR --destination DIR`.
Local files only: this module never opens a network connection, never builds a MakerWorld URL
and never downloads anything. Originals are copied, never moved or deleted. Third-party model
files must not be redistributed (MakerWorld's default Standard Digital File License forbids
sharing or hosting them), so projects belong in a user-local directory, and a warning is printed
when the destination sits, not git-ignored, inside a git work tree. See docs/BAMBU-TERMS.md.

Layout of DIR/<name>/:
  source/<original file name>   byte copy of the download
  files/...                     extracted zip contents (zips only; kept extensions only)
  tulda-project.json            manifest: source name, sha256, time, per-3mf provenance
  LICENCE-NOTE.txt              licence summary + "MakerWorld page is authoritative" reminder

Naming: the 3mf Title (first qualifying 3mf for a zip), else the file stem. Unicode letters and
digits are kept (NFKC-normalised, so APFS/ext4 handle them and a Chinese title stays readable);
everything else becomes a separator; runs of separators collapse to "-". If nothing is left,
the stem is tried, then "project". Names compare case-insensitively (APFS default) and collide
to "-2", "-3", ... The same source (by sha256) is never imported twice into one destination.

MakerWorld detection (conservative; `--force` overrides for `project create`):
  * .3mf: root-model metadata has DesignModelId, DesignProfileId, DesignRegion or Designer.
    Application=BambuStudio alone does not count (that is every Studio project).
  * .zip: contains such a .3mf (not looked for inside nested zips), or looks like MakerWorld's
    STL/STEP download: at least one mesh/CAD member (.stl/.step/.stp/.3mf/.obj), every other
    member a harmless companion (readme/licence text, PDF, images, .mtl), AND either the zip
    stem ends in _stls/_steps (a browser duplicate suffix such as " (1)" or "-2" is allowed
    after it) or every mesh/CAD member is named obj_<n>_<name>.<ext>.
    This was inferred from observed downloads and may change; it is deliberately narrow so
    that app bundles and other zips in a Downloads folder are skipped."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .errors import TuldaError

MANIFEST = "tulda-project.json"
LOCK_NAME = ".tulda-project.lock"
STAGING_PREFIX = ".tulda-staging-"
STALE_STAGING = 24 * 3600            # staging dirs older than this are leftovers of a kill
LICENCE_NOTE = "LICENCE-NOTE.txt"
KEEP_EXT = {".3mf", ".scad", ".stl", ".step", ".stp", ".obj", ".amf", ".ply", ".f3d", ".iges",
            ".igs", ".dxf", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf", ".txt",
            ".md", ".json", ".csv", ".mtl"}
MODEL_EXT = {".stl", ".step", ".stp", ".3mf", ".obj"}
COMPANION_EXT = {".txt", ".md", ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".mtl"}
RENDER_EXT = {".3mf", ".scad", ".stl"}
PARTIAL_EXT = {".crdownload", ".part", ".partial", ".download", ".tmp", ".opdownload"}
MAX_FILES = 2000                     # members per archive (nested members included)
MAX_TOTAL = 2 * 1024 ** 3            # uncompressed bytes per archive (nested included)
MAX_RATIO = 200                      # compression ratio refused for members above RATIO_MIN
RATIO_MIN = 16 * 1024 ** 2
MAX_NAME = 80                        # characters in a project name
MW_ZIP_STEM = re.compile(r"_(stls?|steps?)(\s*\(\d+\)|-\d+)?$", re.I)  # + browser dup suffix
MW_MEMBER = re.compile(r"obj_\d+_.+\.(stl|step|stp|3mf|obj)$", re.I)
_SEP = re.compile(r"[\s\-]+")


class ProjectError(TuldaError):
    code = 1


class UnsafeArchive(ProjectError):
    code = 2


class NotMakerWorld(ProjectError):
    code = 2


# ---- naming ------------------------------------------------------------------------------

def sanitize_name(text: str | None) -> str:
    """Filesystem-safe, readable folder name: Unicode letters/digits/marks, '_' and '.' kept;
    everything else (brackets, quotes, slashes, punctuation, symbols, emoji, control chars) is a
    separator; whitespace/dash runs collapse to one '-'. May return ''."""
    s = unicodedata.normalize("NFKC", text or "")
    out = []
    for ch in s:
        cat = unicodedata.category(ch)
        if cat[0] in "LNM" or ch in "_.":
            out.append(ch)
        else:
            out.append(" ")
    s = _SEP.sub("-", "".join(out)).strip("-._ ")
    s = re.sub(r"\.{2,}", ".", s)
    if len(s) > MAX_NAME:
        s = s[:MAX_NAME].rstrip("-._ ")
    while len(s.encode("utf-8")) > 200:          # filesystem byte limit (255) with headroom
        s = s[:-1].rstrip("-._ ")
    return s


def base_name(title: str | None, stem: str) -> str:
    stem = MW_ZIP_STEM.sub("", stem)
    return sanitize_name(title) or sanitize_name(stem) or "project"


def unique_dir(dest: Path, name: str) -> Path:
    taken = {p.name.casefold() for p in dest.iterdir()} if dest.is_dir() else set()
    cand, n = name, 1
    while cand.casefold() in taken:
        n += 1
        cand = f"{name}-{n}"
    return dest / cand


# ---- detection ----------------------------------------------------------------------------

MW_KEYS = ("design_model_id", "design_profile_id", "design_region", "designer")


def provenance_of(path) -> dict | None:
    from .mesh import MeshError
    from .threemf import read_provenance
    try:
        return read_provenance(path)
    except MeshError:
        return None


def is_mw_provenance(prov: dict | None) -> bool:
    return bool(prov) and any(prov.get(k) for k in MW_KEYS)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _member_path(info: zipfile.ZipInfo) -> PurePosixPath | None:
    """Safe relative path for a member, None for directories / macOS junk. Raises
    UnsafeArchive for absolute paths, '..', drive letters, symlinks or encrypted members."""
    raw = info.filename.replace("\\", "/")
    mode = (info.external_attr >> 16) & 0o170000
    if mode == 0o120000:
        raise UnsafeArchive(f"symlink member refused: {raw!r}")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise UnsafeArchive(f"absolute member path refused: {raw!r}")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise UnsafeArchive(f"'..' member path refused: {raw!r}")
    if info.flag_bits & 0x1:
        raise UnsafeArchive(f"encrypted member refused: {raw!r}")
    if info.is_dir() or not parts:
        return None
    if parts[0] == "__MACOSX" or parts[-1].startswith("._") or parts[-1] == ".DS_Store":
        return None
    clean = []
    for p in parts:
        p = re.sub(r"[\x00-\x1f\x7f:]", "_", p).strip()
        if not p or p in (".", ".."):
            raise UnsafeArchive(f"bad member path: {raw!r}")
        clean.append(p)
    return PurePosixPath(*clean)


class Budget:
    def __init__(self, files=MAX_FILES, total=MAX_TOTAL):
        self.files, self.total = files, total

    def take(self, info: zipfile.ZipInfo, label: str) -> None:
        self.files -= 1
        if self.files < 0:
            raise UnsafeArchive(f"more than {MAX_FILES} members")
        self.total -= info.file_size
        if self.total < 0:
            raise UnsafeArchive(f"more than {MAX_TOTAL:,} bytes uncompressed")
        if info.file_size > RATIO_MIN and info.file_size > MAX_RATIO * max(info.compress_size, 1):
            raise UnsafeArchive(f"{label}: compression ratio above {MAX_RATIO} (zip bomb?)")


def _scan(z: zipfile.ZipFile, budget: Budget, label: str):
    """[(info, safe_path)] for file members; validates every member before anything is written."""
    out = []
    infos = z.infolist()
    if len(infos) > MAX_FILES:
        raise UnsafeArchive(f"{label}: more than {MAX_FILES} members")
    for info in infos:
        p = _member_path(info)
        if p is None:
            continue
        budget.take(info, label)
        out.append((info, p))
    return out


def _copy_member(z: zipfile.ZipFile, info: zipfile.ZipInfo, target: Path) -> int:
    """Stream one member, refusing to write more than its declared size."""
    n = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with z.open(info) as src, open(target, "xb") as dst:
            for chunk in iter(lambda: src.read(1 << 20), b""):
                n += len(chunk)
                if n > info.file_size:
                    raise UnsafeArchive(f"{info.filename!r} inflates past its declared size")
                dst.write(chunk)
    except (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, NotImplementedError) as e:
        raise ProjectError(f"{info.filename!r}: cannot extract ({e})") from e
    return n


def zip_looks_mw(stem: str, paths: list[PurePosixPath]) -> bool:
    models = [p for p in paths if p.suffix.lower() in MODEL_EXT]
    if not models or any(p.suffix.lower() not in MODEL_EXT | COMPANION_EXT for p in paths):
        return False
    return bool(MW_ZIP_STEM.search(stem)) or all(MW_MEMBER.fullmatch(p.name) for p in models)


def detect(src: Path) -> tuple[bool, str]:
    """-> (qualifies, reason). Reads only; nothing is written outside a private temp dir."""
    ext = src.suffix.lower()
    if ext == ".3mf":
        prov = provenance_of(src)
        if prov is None:
            return False, "not a readable 3mf"
        return (True, "3mf with MakerWorld metadata") if is_mw_provenance(prov) \
            else (False, "3mf without MakerWorld metadata (DesignModelId/Designer...)")
    if ext != ".zip":
        return False, "not a .3mf or .zip"
    try:
        z = zipfile.ZipFile(src)
    except (zipfile.BadZipFile, OSError) as e:
        return False, f"not a readable zip ({e})"
    with z, tempfile.TemporaryDirectory(prefix="tulda-detect-") as td:
        try:
            members = _scan(z, Budget(), src.name)
        except UnsafeArchive as e:
            return False, f"unsafe archive: {e}"
        for info, p in members:
            if p.suffix.lower() == ".3mf":
                tmp = Path(td) / "probe.3mf"
                try:
                    _copy_member(z, info, tmp)
                except (ProjectError, OSError):
                    continue
                ok = is_mw_provenance(provenance_of(tmp))
                tmp.unlink()
                if ok:
                    return True, "zip containing a 3mf with MakerWorld metadata"
        if zip_looks_mw(src.stem, [p for _, p in members]):
            return True, "zip of STL/STEP files in MakerWorld download layout"
    return False, "zip without MakerWorld markers"


# ---- create -------------------------------------------------------------------------------

@contextlib.contextmanager
def dest_lock(dest: Path):
    """Exclusive advisory lock on DEST/.tulda-project.lock, so a manual `project create` and the
    watch service never pick the same name or import the same source twice."""
    fd = os.open(Path(dest) / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def clean_stale_staging(dest: Path, now: float | None = None) -> int:
    """Remove staging dirs left by a killed import (older than STALE_STAGING). Call under lock."""
    now = now or time.time()
    n = 0
    for p in Path(dest).glob(STAGING_PREFIX + "*"):
        try:
            if p.is_dir() and now - p.stat().st_mtime > STALE_STAGING:
                shutil.rmtree(p, ignore_errors=True)
                n += 1
        except OSError:
            continue
    return n


def find_existing(dest: Path, sha: str) -> Path | None:
    if not dest.is_dir():
        return None
    for m in dest.glob(f"*/{MANIFEST}"):
        if m.parent.name.startswith("."):
            continue                    # staging dirs and other hidden folders are not projects
        try:
            if json.loads(m.read_text("utf-8")).get("source", {}).get("sha256") == sha:
                return m.parent
        except (OSError, ValueError, AttributeError):
            continue
    return None


def _extract(src: Path, out: Path, budget: Budget, files: list, skipped: list, depth: int = 0,
             prefix: PurePosixPath = PurePosixPath()) -> None:
    try:
        z = zipfile.ZipFile(src)
    except (zipfile.BadZipFile, OSError) as e:
        raise ProjectError(f"{src.name}: not a readable zip ({e})") from e
    with z:
        members = _scan(z, budget, src.name)
        for info, p in members:
            rel = prefix / p
            ext = p.suffix.lower()
            if ext == ".zip":
                if depth >= 1:
                    skipped.append({"path": str(rel), "reason": "nested zip deeper than one level"})
                    continue
                with tempfile.TemporaryDirectory(prefix="tulda-nested-") as td:
                    tmp = Path(td) / "nested.zip"
                    _copy_member(z, info, tmp)
                    sub = PurePosixPath(*rel.parent.parts, sanitize_name(p.stem) or "nested")
                    _extract(tmp, out, budget, files, skipped, depth + 1, sub)
                continue
            if ext not in KEEP_EXT:
                skipped.append({"path": str(rel), "reason": f"extension {ext or '(none)'} not kept"})
                continue
            target = out / rel
            if target.exists():
                skipped.append({"path": str(rel), "reason": "duplicate path"})
                continue
            _copy_member(z, info, target)
            files.append(str(PurePosixPath("files") / rel))


def licence_note(name: str, models: list[dict]) -> str:
    lines = [f"Project: {name}", ""]
    if models:
        for m in models:
            pv = m.get("provenance") or {}
            lines.append(f"{m['path']}:")
            lines.append(f"  Licence:  {pv.get('license') or 'unknown (not recorded in file)'}")
            for key, label in (("designer", "Designer"), ("title", "Title"),
                               ("copyright", "Copyright"), ("design_model_id", "DesignModelId")):
                if pv.get(key):
                    lines.append(f"  {label + ':':9} {pv[key]}")
    else:
        lines.append("Licence: unknown (no 3mf metadata; STL/STEP files carry none)")
    lines += ["", "This metadata is self-reported by the file and may be missing or wrong; the",
              "model's MakerWorld page is authoritative. Check it before doing anything beyond",
              "printing for yourself.",
              "",
              "Do not redistribute these files. MakerWorld's default Standard Digital File License",
              "forbids sharing, hosting or distributing the files and derivative works; CC licences",
              "require attribution and may forbid commercial use or sharing modified versions.",
              "Keep this folder out of public git repositories and shared folders.", ""]
    return "\n".join(lines)


def create_project(src: Path, dest: Path, force: bool = False,
                   now: datetime | None = None) -> tuple[str, Path]:
    """-> ("created" | "exists", project dir). Raises NotMakerWorld / UnsafeArchive / ProjectError.
    Builds in a hidden staging dir inside dest and renames it into place, so a failure or kill
    never leaves a half project that looks complete. The name choice and rename happen under
    DEST/.tulda-project.lock with a second sha256 check, so concurrent importers (the watch
    service and a manual `project create`) cannot create duplicates."""
    src = Path(src)
    if not src.is_file():
        raise ProjectError(f"{src.name}: no such file")
    ext = src.suffix.lower()
    if ext not in (".3mf", ".zip"):
        raise ProjectError(f"{src.name}: not a .3mf or .zip")
    ok, reason = detect(src)
    if not ok and not force:
        raise NotMakerWorld(f"{src.name}: {reason} (use --force to import anyway)")
    dest.mkdir(parents=True, exist_ok=True)
    sha = sha256_file(src)
    existing = find_existing(dest, sha)
    if existing:
        return "exists", existing
    staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=dest))
    try:
        (staging / "source").mkdir()
        shutil.copy2(src, staging / "source" / src.name)
        files = [f"source/{src.name}"]
        skipped: list = []
        if ext == ".zip":
            _extract(src, staging / "files", Budget(), files, skipped)
        models = []
        for rel in files:
            if rel.lower().endswith(".3mf"):
                models.append({"path": rel, "provenance": provenance_of(staging / rel)})
        title = next((m["provenance"].get("title") for m in models
                      if m["provenance"] and is_mw_provenance(m["provenance"])
                      and m["provenance"].get("title")), None) \
            or next((m["provenance"].get("title") for m in models
                     if m["provenance"] and m["provenance"].get("title")), None)
        name = base_name(title, src.stem)
        now = now or datetime.now(timezone.utc)
        manifest = {
            "tulda_project": 1, "name": name,
            "created_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": {"filename": src.name, "sha256": sha, "size": src.stat().st_size, "type": ext[1:]},
            "detection": {"makerworld": ok, "reason": reason, "forced": bool(force and not ok)},
            "files": files, "skipped": skipped, "models": models,
            "note": "Self-reported metadata; the MakerWorld page is authoritative. Do not redistribute.",
        }
        with dest_lock(dest):
            existing = find_existing(dest, sha)
            if existing:
                shutil.rmtree(staging, ignore_errors=True)
                return "exists", existing
            target = unique_dir(dest, name)
            manifest["name"] = target.name
            (staging / MANIFEST).write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", "utf-8")
            (staging / LICENCE_NOTE).write_text(licence_note(target.name, models), "utf-8")
            os.rename(staging, target)
            clean_stale_staging(dest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return "created", target


# ---- new original project -----------------------------------------------------------------

README_NAME = "README.md"


def scad_stem(folder: str) -> str:
    """snake_case file stem from a project folder name: 'Cable-Clip.v2' -> 'cable_clip_v2'."""
    return re.sub(r"_+", "_", re.sub(r"[-.\s]+", "_", folder.casefold())).strip("_") or "model"


def scad_template(stem: str, title: str) -> str:
    return f"""// {stem}.scad - {title} (original design; untested, not yet printed)
//
// Axes: X = width, Y = depth, Z = height. Bed face at z = 0.
// Print orientation: TODO (which face goes on the bed, and why; supports or none).
// Material / profile: TODO (material, slicer profile or settings this part assumes).
//
// part: "main" | add selectors here (e.g. "body", "lid") and thin wrapper files that only set `part`
//       after including this file.

$fn = 48;

part = "main";

// ---- parameters (mm) ---------------------------------------------------------------
width  = 20;     // outside width  (X)
depth  = 20;     // outside depth  (Y)
height = 10;     // outside height (Z)

// ---- modules -----------------------------------------------------------------------
module main() {{
    // Placeholder solid so the file renders; replace with the real model.
    cube([width, depth, height]);
}}

// ---- render ------------------------------------------------------------------------
if (part == "main") main();
"""


def readme_template(stem: str, title: str) -> str:
    return f"""# {title} (untested, not yet printed)

Original design. Licence: TODO (also set `licence` in `tulda-project.json`).

## Print orientation

TODO: which face goes on the bed, supports, material and layer height.

| File | Purpose |
|---|---|
| `{stem}.scad` | the model; `part = "main"` |

## Parameters and why

- `width`, `depth`, `height`: placeholders (20 x 20 x 10 mm); replace with the real dimensions and
  say where each number comes from (measured part, clearance, layer multiple).

## Notes

-
"""


def projects_default_destination(config: Path | None = None) -> Path | None:
    """[projects] default_destination from the user config (or an explicit --config)."""
    import tomllib
    from .watchconfig import config_path
    path = config_path(config)
    try:
        data = tomllib.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise ProjectError(f"cannot read {path}: {e}") from e
    sect = data.get("projects", {})
    if not isinstance(sect, dict):
        raise ProjectError(f"{path}: projects must be a table ([projects])")
    v = sect.get("default_destination")
    if v is None:
        return None
    if not isinstance(v, str) or not v.strip():
        raise ProjectError(f"{path}: [projects] default_destination must be a non-empty string")
    d = Path(v).expanduser()
    if not d.is_absolute():
        raise ProjectError(f"{path}: [projects] default_destination must be absolute or start with ~")
    return d


def new_project(name: str, dest: Path, now: datetime | None = None) -> Path:
    """Create DEST/<name>/ for an original design: <stem>.scad skeleton, README.md stub and a
    manifest with origin "own". Same naming and collision rules as create_project; built in a
    staging dir and renamed under the destination lock. Returns the project dir."""
    folder = sanitize_name(name)
    if not folder:
        raise ProjectError(f"{name!r}: no usable characters for a folder name")
    title = " ".join(name.split()) or folder
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=dest))
    try:
        with dest_lock(dest):
            target = unique_dir(dest, folder)
            stem = scad_stem(target.name)
            (staging / f"{stem}.scad").write_text(scad_template(stem, title), "utf-8")
            (staging / README_NAME).write_text(readme_template(stem, title), "utf-8")
            now = now or datetime.now(timezone.utc)
            manifest = {
                "tulda_project": 1, "name": target.name, "title": title,
                "created_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "origin": "own", "licence": "", "designer": "",
                "files": [f"{stem}.scad", README_NAME],
                "note": "Original design. Fill in licence and designer; no third-party provenance.",
            }
            (staging / MANIFEST).write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", "utf-8")
            os.rename(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target


# ---- git publication warning ---------------------------------------------------------------

def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None


def git_publish_warning(dest: Path) -> str | None:
    """A warning when dest is inside a git work tree and is not git-ignored (with or without a
    remote: a remote can be added later and a push would publish the files).
    Read-only git queries only; never edits .gitignore or anything else in that repo."""
    probe = Path(dest).resolve()
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    r = _git(probe, "rev-parse", "--show-toplevel")
    if not r or r.returncode != 0 or not r.stdout.strip():
        return None
    top = Path(r.stdout.strip()).resolve()
    rm = _git(top, "remote")
    remote = bool(rm and rm.returncode == 0 and rm.stdout.strip())
    child = Path(dest).resolve() / ".tulda-ignore-probe"
    ci = _git(top, "check-ignore", "-q", "--no-index", str(child))
    if ci and ci.returncode == 0:
        return None
    try:
        rel = Path(dest).resolve().relative_to(top).as_posix()
    except ValueError:
        rel = Path(dest).name
    where = "with a remote" if remote else "(no remote yet; one may be added later)"
    return (f"warning: destination is inside a git repository {where} ({top.name}) and is not "
            f"git-ignored; MakerWorld files (Standard Digital File License) must not be "
            f"published. Add this line to its .gitignore before committing: /{rel}/")


# ---- render (optional) ----------------------------------------------------------------------

def render_project(proj: Path, log=print) -> int:
    files = [proj / f for f in json.loads((proj / MANIFEST).read_text("utf-8"))["files"]
             if Path(f).suffix.lower() in RENDER_EXT]
    if not files:
        log(f"render {proj.name}: no .3mf/.scad/.stl to render")
        return 0
    from .cli import main as cli_main
    log(f"render {proj.name}: {len(files)} file(s); note: renders are shown by `tulda serve` to "
        f"anyone who can reach it; keep third-party models off shared servers")
    return cli_main(["render", *map(str, files), "--project", proj.name, "--title", proj.name])


# ---- watch ---------------------------------------------------------------------------------

def is_partial(p: Path) -> bool:
    if p.name.startswith(".") or p.suffix.lower() in PARTIAL_EXT:
        return True
    return any(p.with_name(p.name + s).exists() for s in PARTIAL_EXT)


class Watcher:
    """Polls one directory (not recursive). A candidate (.3mf/.zip, not hidden, no partial
    sibling such as Firefox's NAME.part) is processed once its size and mtime are unchanged
    between two polls and it is at least `settle` seconds old (with --once, only the age test
    applies). Each (path, size, mtime) is handled once per run; sha256 idempotency covers runs."""

    def __init__(self, src_dir: Path, dest: Path, settle: float = 5.0, render: bool = False,
                 log=print, clock=time.time, force: bool = False, include=None):
        self.src_dir, self.dest, self.settle, self.render = Path(src_dir), Path(dest), settle, render
        self.force, self.include = force, list(include) if include else None
        self.log, self.clock = log, clock
        self.last: dict = {}
        self.done: set = set()
        self.list_error: str | None = None     # set while the directory cannot be listed
        self.tcc_blocked = False
        self.last_created: dict | None = None
        self.last_error: str | None = None

    def wanted(self, p: Path) -> bool:
        if p.suffix.lower() not in (".3mf", ".zip"):
            return False
        return not self.include or any(fnmatch.fnmatch(p.name.casefold(), g.casefold())
                                       for g in self.include)

    def candidates(self):
        try:
            entries = sorted(self.src_dir.iterdir())
        except OSError as e:
            from .watchconfig import is_tcc_error, real_python, tcc_help
            msg = f"cannot list {self.src_dir}: {e.strerror or e}"
            tcc = is_tcc_error(e)
            if tcc:
                msg += "; " + tcc_help(real_python())
            if msg != self.list_error:           # log once per distinct problem, not every poll
                self.log(f"error: {msg}")
            self.list_error, self.tcc_blocked = msg, tcc
            return None
        if self.list_error:
            self.log(f"ok: {self.src_dir} is listable again")
        self.list_error, self.tcc_blocked = None, False
        return [p for p in entries if self.wanted(p) and p.is_file()]

    def poll(self, once: bool = False, should_stop=lambda: False) -> dict:
        counts = {"created": 0, "exists": 0, "skipped": 0, "errors": 0, "waiting": 0}
        seen = set()
        found = self.candidates()
        if found is None:
            counts["errors"] += 1
            return counts
        for p in found:
            if should_stop():
                break
            try:
                st = p.stat()
            except OSError:
                continue
            key = (str(p), st.st_size, st.st_mtime_ns)
            seen.add(str(p))
            if key in self.done:
                continue
            prev = self.last.get(str(p))
            self.last[str(p)] = key
            old = self.clock() - st.st_mtime >= self.settle
            stable = once or prev == key
            if is_partial(p) or st.st_size == 0 or not (old and stable):
                counts["waiting"] += 1
                continue
            self.done.add(key)
            try:
                state, proj = create_project(p, self.dest, force=self.force)
            except NotMakerWorld as e:
                counts["skipped"] += 1
                self.log(f"skip {p.name}: {str(e).split(': ', 1)[-1].replace(' (use --force to import anyway)', '')}")
                continue
            except Exception as e:     # one bad file must not stop the watcher
                counts["errors"] += 1
                self.last_error = f"{p.name}: {e}"
                self.log(f"error {p.name}: {e}")
                continue
            counts[state] += 1
            self.log(f"{state} {proj.name} <- {p.name}")
            if state == "created":
                self.last_created = {"project": proj.name, "source": p.name,
                                     "time": datetime.now().astimezone().isoformat(timespec="seconds")}
            if state == "created" and self.render and not should_stop():
                try:
                    render_project(proj, self.log)
                except Exception as e:
                    self.log(f"error rendering {proj.name}: {e}")
        if not should_stop():
            for k in list(self.last):
                if k not in seen:
                    del self.last[k]
        return counts

    def run(self, interval: float = 5.0, once: bool = False, sleep=time.sleep,
            should_stop=lambda: False) -> dict:
        if once:
            return self.poll(once=True, should_stop=should_stop)
        while not should_stop():
            self.poll(should_stop=should_stop)
            if should_stop():
                break
            sleep(interval)
        return {}


# ---- CLI ------------------------------------------------------------------------------------

def _warn_dest(dest: Path) -> None:
    w = git_publish_warning(dest)
    if w:
        print(w, file=sys.stderr, flush=True)


def cmd_project_create(args) -> int:
    dest = Path(args.destination).expanduser()
    _warn_dest(dest)
    code = 0
    for f in args.files:
        src = Path(f).expanduser()
        try:
            state, proj = create_project(src, dest, force=args.force)
        except ProjectError as e:
            print(f"error: {e}", file=sys.stderr)
            code = code or e.code
            continue
        print(f"{state} {proj}" + (" (same source already imported)" if state == "exists" else ""))
        if state == "created" and args.render:
            code = render_project(proj) or code
    return code


def cmd_project_new(args) -> int:
    dest = Path(args.destination).expanduser() if args.destination \
        else projects_default_destination(args.config)
    if dest is None:
        raise ProjectError("no destination: pass --destination DIR or set [projects] "
                           "default_destination in the user config")
    proj = new_project(args.name, dest)
    print(f"created {proj}")
    for f in json.loads((proj / MANIFEST).read_text("utf-8"))["files"]:
        print(f"  {f}")
    if args.render:
        return render_project(proj)
    return 0


def cmd_watch(args) -> int:
    if getattr(args, "check_config", None) is not None or getattr(args, "from_config", False):
        from .watchd import cmd_watch_config
        return cmd_watch_config(args)
    if not args.directory or not args.destination:
        raise ProjectError("usage: tulda watch DIR --destination DEST  (or --from-config)")
    if args.interval is None:
        args.interval = 5.0
    if args.settle is None:
        args.settle = 5.0
    dest = Path(args.destination).expanduser()
    src_dir = Path(args.directory).expanduser()
    if not src_dir.is_dir():
        raise ProjectError(f"not a directory: {src_dir}")
    if dest.resolve() == src_dir.resolve():
        raise ProjectError("destination must differ from the watched directory")
    _warn_dest(dest)
    w = Watcher(src_dir, dest, settle=args.settle, render=bool(args.render),
                force=bool(args.force), log=lambda m: print(m, flush=True))
    if args.once:
        c = w.run(once=True)
        print("summary: " + ", ".join(f"{k} {v}" for k, v in c.items()), flush=True)
        return 1 if c["errors"] else 0
    print(f"watching {src_dir} every {args.interval:g}s -> {dest} (Ctrl-C to stop)", flush=True)
    from .watchd import Stopper
    stop = Stopper(lambda m: print(m, flush=True))
    stop.install()
    try:
        w.run(interval=args.interval, sleep=stop.sleep, should_stop=stop)
    except KeyboardInterrupt:
        print("aborted the current file", flush=True)
    print("stopped", flush=True)
    return 0


def add_project_parsers(sub) -> None:
    p = sub.add_parser("project", help="local project folders for MakerWorld files you downloaded yourself")
    ps = p.add_subparsers(dest="project_cmd", required=True)
    c = ps.add_parser("create", help="make DEST/<name>/ from a downloaded .3mf or .zip (copied, never moved)")
    c.add_argument("files", nargs="+", metavar="file")
    c.add_argument("--destination", "-d", required=True, metavar="DIR",
                   help="user-local projects directory (keep it out of public repos)")
    c.add_argument("--force", action="store_true", help="import even without MakerWorld markers")
    c.add_argument("--render", action="store_true",
                   help="also render .3mf/.scad/.stl into the renders root (off by default)")
    c.set_defaults(fn=cmd_project_create)
    n = ps.add_parser("new", help="start an original design: DEST/<name>/ with a starter .scad, "
                      "README.md and a manifest marked origin = own")
    n.add_argument("name", help="project name (sanitised like `project create`; clashes get -2, -3)")
    n.add_argument("--destination", "-d", metavar="DIR",
                   help="parent directory (default: [projects] default_destination in the user config)")
    n.add_argument("--config", metavar="PATH", help="config to read default_destination from")
    n.add_argument("--render", action="store_true", help="also render the starter model (needs OpenSCAD)")
    n.set_defaults(fn=cmd_project_new)
    w = sub.add_parser("watch", help="poll a folder (e.g. Downloads) and make projects for finished "
                       "MakerWorld .3mf/.zip downloads; originals are never moved")
    w.add_argument("directory", nargs="?", help="folder to poll (ad-hoc mode; ignores the config)")
    w.add_argument("--destination", "-d", metavar="DIR")
    w.add_argument("--from-config", action="store_true",
                   help="watch every [[watch.dir]] of the user config (or --config); what the "
                   "com.tulda.watch service runs")
    w.add_argument("--config", metavar="PATH",
                   help="watch config to use instead of the user config (with --from-config)")
    w.add_argument("--check-config", nargs="?", const="", default=None, metavar="PATH",
                   help="validate the watch config (PATH, else --config, else the user config) "
                   "and exit: 0 valid, 2 invalid")
    w.add_argument("--log-dir", metavar="DIR",
                   help="rotate and redirect output to DIR/watch.log and watch.err.log (with --from-config)")
    w.add_argument("--state-dir", metavar="DIR",
                   help="where watch.json and the last-known-good config live (default "
                   "$TULDA_STATE_DIR, else $XDG_STATE_HOME/tulda or ~/.local/state/tulda)")
    w.add_argument("--once", action="store_true", help="scan once and exit")
    w.add_argument("--interval", type=float, default=None, metavar="SEC",
                   help="poll period (default 5; overrides the config)")
    w.add_argument("--settle", type=float, default=None, metavar="SEC",
                   help="minimum age of a file before it is processed (default 5; overrides the config)")
    w.add_argument("--render", action=argparse.BooleanOptionalAction, default=None,
                   help="render new projects (off by default; overrides the config)")
    w.add_argument("--force", action=argparse.BooleanOptionalAction, default=None,
                   help="import files without MakerWorld markers too (off by default)")
    w.set_defaults(fn=cmd_watch)
