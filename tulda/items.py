"""Reading one render (PNG + optional sidecar) into a plain dict. Shared by the
catalog indexer and the legacy full scan; knows nothing about HTTP or SQLite."""

from __future__ import annotations

import json
import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from .threemf import PROVENANCE_KEYS

MAX_SIDECAR = 256 * 1024
GROUP_ID = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
# /_g/<group id>/ and /_d/<date>/ are routes; project directories of these names are not listed.
GROUP_PREFIX = "_g"
DATE_PREFIX = "_d"
VIEWER_PREFIX = "_v"       # /_v/<project>/<name>/ is the interactive viewer page
STATIC_PREFIX = "_s"       # /_s/<path> is vendored viewer assets from the package
WATCH_PREFIX = "_w"         # /_w/ is the read-only watcher activity page
RESERVED_DIRS = {GROUP_PREFIX, DATE_PREFIX, VIEWER_PREFIX, STATIC_PREFIX, WATCH_PREFIX}
# What a catalog row's primary file may be: a PNG (kind image / turntable poster) or a GLB (kind mesh).
RENDER_EXTS = (".png", ".glb", ".htm")
KINDS = ("image", "mesh", "turntable", "slice")
MAX_INFO = 24 * 1024
PROVENANCE_KINDS = ("makerworld", "parametric")
PROVENANCE_CAVEAT = ("Metadata is self-reported by the file and may be missing or wrong; "
                     "the model's MakerWorld page is authoritative.")
PROVENANCE_CAVEAT_SHORT = "self-reported; the MakerWorld page is authoritative"
UNKNOWN_LICENCE = "unknown (not recorded in file)"
# How a render request was started (sidecar `trigger`): a direct CLI run, the Watcher tab's
# Render button, or the watcher's render=true. Renders made before this was recorded have none
# (shown as unknown, never guessed). `trigger_note` is optional free text from `--note`.
TRIGGERS = ("manual", "web", "watch")
MAX_NOTE = 200
_CTRL = re.compile(r"[\x00-\x1f\x7f]+")


def clean_trigger(v) -> str | None:
    return v if v in TRIGGERS else None


def clean_note(v) -> str | None:
    """A trigger note as one short plain-text line (control characters become spaces)."""
    if not isinstance(v, str):
        return None
    v = " ".join(_CTRL.sub(" ", v).split())[:MAX_NOTE]
    return v or None


def _strs(v, limit=50) -> list[str]:
    return [x[:200] for x in v[:limit] if isinstance(x, str)] if isinstance(v, list) else []


def _text(v, limit=300):
    return v[:limit] if isinstance(v, str) else None


def clean_provenance(v) -> dict | None:
    """A sidecar's `provenance` object reduced to known string fields (or None)."""
    if not isinstance(v, dict) or v.get("error"):
        return None
    out = {k: _text(v.get(k), 500) or None for k in PROVENANCE_KEYS}
    out["kind"] = v.get("kind") if v.get("kind") in PROVENANCE_KINDS else None
    return out


def provenance_lines(p: dict | None) -> list[list[str]]:
    """[[label, value]] shown on cards and the group page. Plain text: the page renders it with
    textContent. An empty licence is "unknown", never "unlicensed". No MakerWorld URL is built."""
    if not p:
        return []
    lines = [["Licence", p.get("license") or UNKNOWN_LICENCE]]
    for key, label in (("designer", "Designer"), ("title", "Title"), ("copyright", "Copyright")):
        if p.get(key):
            lines.append([label, p[key]])
    if p.get("kind") == "parametric":
        lines.append(["Source", "MakerWorld model (Parametric Model Maker)"])
    elif p.get("kind") == "makerworld":
        lines.append(["Source", "MakerWorld model"])
    return lines


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def group_fields(meta: dict, mtime: float) -> dict:
    """Group info for one render. Sidecars without a valid group_id (legacy, or no sidecar)
    are grouped by UTC day of their creation time."""
    gid = meta.get("group_id")
    if isinstance(gid, str) and GROUP_ID.match(gid):
        return {"group_id": gid, "group_title": _text(meta.get("group_title")),
                "group_sources": _strs(meta.get("group_sources")),
                "request_cmd": _text(meta.get("request_cmd"), 1000),
                "group_created_utc": _text(meta.get("group_created_utc"), 40),
                "seq": meta["seq"] if isinstance(meta.get("seq"), int) else 0, "legacy": False,
                "trigger": clean_trigger(meta.get("trigger")),
                "trigger_note": clean_note(meta.get("trigger_note"))}
    day = utc_day(mtime)
    return {"group_id": f"legacy-{day.replace('-', '')}", "group_title": f"Earlier renders, {day}",
            "group_sources": [], "request_cmd": None, "group_created_utc": None,
            "seq": 0, "legacy": True, "trigger": None, "trigger_note": None}


def render_url(project: str, file: str) -> str:
    q = urllib.parse.quote
    return f"/{q(project)}/{q(file)}"


def viewer_url(project: str, file: str) -> str:
    """Interactive viewer page for a mesh/turntable render: /_v/<project>/<file stem>/."""
    q = urllib.parse.quote
    return f"/{VIEWER_PREFIX}/{q(project)}/{q(file.rsplit('.', 1)[0])}/"


def is_project_dir(root: Path, pdir: Path) -> bool:
    if pdir.name.startswith(".") or pdir.name in RESERVED_DIRS:
        return False
    try:
        return pdir.is_dir() and pdir.resolve().is_relative_to(root)
    except OSError:
        return False


def read_item(root: Path, pdir: Path, png: Path) -> dict | None:
    """One render as a dict, or None if it must not be listed (dotfile, not a PNG/GLB, symlink
    escaping root, unreadable). root must be resolved."""
    if png.name.startswith(".") or png.suffix.lower() not in RENDER_EXTS:
        return None
    try:
        rp = png.resolve(strict=True)
        if not rp.is_relative_to(root):
            return None
        st = rp.stat()
    except OSError:
        return None
    meta: dict = {}
    side = png.with_suffix(".json")
    side_mtime = None
    try:
        if side.is_file() and side.resolve().is_relative_to(root):
            sst = side.stat()
            side_mtime = sst.st_mtime
            if sst.st_size <= MAX_SIDECAR:
                meta = json.loads(side.read_text())
                if not isinstance(meta, dict):
                    meta = {}
    except (OSError, ValueError):
        meta = {}
    steps = meta.get("steps")
    kind = meta.get("kind") if meta.get("kind") in KINDS else "image"
    if png.suffix.lower() == ".glb":
        kind = "mesh"
    elif png.suffix.lower() == ".htm":
        kind = "slice"
    info = meta.get("info")
    if not isinstance(info, dict) or len(json.dumps(info)) > MAX_INFO:
        info = None
    return {
        "kind": kind, "info": info,
        "project": pdir.name, "file": png.name,
        "url": render_url(pdir.name, png.name),
        "json_url": render_url(pdir.name, side.name) if meta else None,
        "mtime": st.st_mtime, "size": st.st_size, "sidecar_mtime": side_mtime,
        "adapter": meta.get("adapter") or "unknown",
        "stage": meta.get("stage") or "unknown",
        "source_type": _text(meta.get("source_type"), 20), "source": _text(meta.get("source")),
        "created_utc": _text(meta.get("created_utc"), 40), "git_sha": _text(meta.get("git_sha"), 40),
        "git_dirty": meta.get("git_dirty") if isinstance(meta.get("git_dirty"), bool) else None,
        "tool_version": _text(((steps[-1] or {}) if isinstance(steps, list) and steps
                               and isinstance(steps[-1], dict) else {}).get("tool_version")),
        "source_sha256": _text(meta.get("source_sha256"), 80),
        "view": _text(meta.get("view"), 40), **group_fields(meta, st.st_mtime),
        "provenance": clean_provenance(meta.get("provenance")),
    }
