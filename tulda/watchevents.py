"""Watcher activity: a small structured event log the watcher appends to and the server reads.

`<state dir>/watch-events.jsonl`, one JSON object per line, mode 600:

    {"time": "2026-01-01T12:00:00Z", "dir": "<watched folder name>", "file": "<file name>",
     "outcome": "created" | "exists" | "skipped" | "held" | "error" | "backlog",
     "project": "<project folder name>" | null, "reason": "<text>" | null,
     "script": "<full post-processing script text>", "approve": "<CLI command>"}   (held only)

"held" means a 3mf in the file carries a post-processing script: nothing was imported, and
`time` is UTC with a `Z` suffix; files written before that hold local-offset times
(`...-08:00`), which readers parse too (timefmt.parse_ts). `approve` is the `tulda project create ... --allow-post-process` command (home-relative paths)
for the user to run after reviewing `script`.

Capped: when the file passes MAX_BYTES it is renamed to watch-events.1.jsonl (one older
generation is kept) and a new file is started. Every value comes from file names in watched
folders and is therefore attacker-controlled: readers must escape it. Only folder and file
NAMES are stored, plus reasons, in which absolute paths are reduced to their last component
by `display_events`.

This module is imported by both the watcher and the read-only server; it must not import the
watcher, project or service modules (tests/test_invariants.py)."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .timefmt import utc_iso

EVENTS_NAME = "watch-events.jsonl"
OLD_EVENTS_NAME = "watch-events.1.jsonl"
STATE_NAME = "watch.json"
MAX_BYTES = 512 * 1024
MAX_FIELD = 500
OUTCOMES = ("created", "exists", "skipped", "held", "error", "backlog")
MAX_SCRIPT = 20000
_ABS = re.compile(r"(?<![\w.~])(?:/[^/\s'\"():,;]+)+/?")


def default_state_dir(explicit=None) -> Path:
    """`--state-dir`, else $TULDA_STATE_DIR, else $XDG_STATE_HOME/tulda or ~/.local/state/tulda."""
    v = explicit or os.environ.get("TULDA_STATE_DIR")
    if v:
        return Path(v).expanduser()
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "tulda"


def _clip(v) -> str | None:
    if v is None:
        return None
    return str(v)[:MAX_FIELD]


class EventLog:
    """Appends events; never raises (a full disk must not stop the watcher)."""

    def __init__(self, state_dir: Path, log=None, max_bytes: int = MAX_BYTES):
        self.path = Path(state_dir) / EVENTS_NAME
        self.old = Path(state_dir) / OLD_EVENTS_NAME
        self.log, self.max_bytes = log, max_bytes
        self.warned = False

    def append(self, folder: str, file: str, outcome: str, project: str | None = None,
               reason: str | None = None, script: str | None = None,
               approve: str | None = None) -> None:
        rec = {"time": utc_iso(),
               "dir": _clip(folder), "file": _clip(file), "outcome": outcome,
               "project": _clip(project), "reason": _clip(reason)}
        if script is not None:
            rec["script"] = str(script)[:MAX_SCRIPT]
        if approve is not None:
            rec["approve"] = str(approve)[:MAX_FIELD * 4]
        line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if self.path.stat().st_size + len(line) > self.max_bytes:
                    os.replace(self.path, self.old)
            except FileNotFoundError:
                pass
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        except OSError as e:
            if self.log and not self.warned:
                self.log(f"warning: cannot write watch events {self.path}: {e}")
            self.warned = True


def _read_lines(p: Path) -> list[dict]:
    out = []
    try:
        with open(p, "rb") as f:
            data = f.read(MAX_BYTES * 2)
    except OSError:
        return out
    for raw in data.splitlines():
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("outcome") in OUTCOMES:
            out.append(rec)
    return out


def collapse_repeats(recs: list[dict]) -> list[dict]:
    """Oldest-first records with repeated "exists" events folded away: an "exists" whose
    previous event for the same folder and file was "created" or "exists" of the same project
    adds one to that event's "repeats" count instead of being listed. Watchers without the
    handled-files record (projects.Handled) reported an unchanged file again on every restart.
    Only the returned list changes; the event file is never rewritten."""
    out, last = [], {}
    for r in recs:
        k = (r.get("dir"), r.get("file"))
        prev = last.get(k)
        if (r.get("outcome") == "exists" and prev is not None
                and prev.get("outcome") in ("created", "exists")
                and prev.get("project") == r.get("project")):
            prev["repeats"] = prev.get("repeats", 0) + 1
            continue
        r = dict(r)
        last[k] = r
        out.append(r)
    return out


def read_events(state_dir: Path, limit: int = 200) -> list[dict]:
    """Newest first, at most `limit`, repeats collapsed (collapse_repeats)."""
    recs = _read_lines(Path(state_dir) / OLD_EVENTS_NAME) + _read_lines(Path(state_dir) / EVENTS_NAME)
    return list(reversed(collapse_repeats(recs)))[:max(0, limit)]


def redact(text) -> str | None:
    """Absolute paths in free text reduced to their last component ('/a/b/c.zip' -> 'c.zip')."""
    if text is None:
        return None
    return _ABS.sub(lambda m: m.group(0).rstrip("/").rsplit("/", 1)[-1] or "/", str(text))


def _s(v) -> str | None:
    return None if v is None else str(v)[:MAX_FIELD]


def display_events(state_dir: Path, limit: int = 200) -> list[dict]:
    """Events for the server: plain strings only, reasons with absolute paths reduced."""
    out = []
    for r in read_events(state_dir, limit):
        held = r["outcome"] == "held"         # script text is shown verbatim for review
        ev = {"time": _s(r.get("time")), "dir": _s(r.get("dir")), "file": _s(r.get("file")),
              "outcome": r["outcome"], "project": _s(r.get("project")),
              "reason": _s(r.get("reason")) if held else redact(_s(r.get("reason")))}
        if isinstance(r.get("repeats"), int) and r["repeats"] > 0:
            ev["repeats"] = r["repeats"]
        if held:
            ev["script"] = None if r.get("script") is None else str(r["script"])[:MAX_SCRIPT]
            ev["approve"] = None if r.get("approve") is None else str(r["approve"])[:MAX_FIELD * 4]
        out.append(ev)
    return out


def _alive(pid) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def watcher_status(state_dir: Path) -> dict | None:
    """Summary of watch.json for display: no absolute paths (folder names only)."""
    try:
        st = json.loads((Path(state_dir) / STATE_NAME).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(st, dict):
        return None
    lc = st.get("last_created") if isinstance(st.get("last_created"), dict) else None
    le = st.get("last_error") if isinstance(st.get("last_error"), dict) else None
    dirs = []
    for d in st.get("dirs") or []:
        if not isinstance(d, dict):
            continue
        t = d.get("totals") if isinstance(d.get("totals"), dict) else {}
        dirs.append({"dir": _s(Path(str(d.get("path") or "")).name),
                     "destination": _s(Path(str(d.get("destination") or "")).name),
                     "last_poll": _s(d.get("last_poll")), "problem": redact(_s(d.get("error"))),
                     "totals": {k: t.get(k, 0) if isinstance(t.get(k, 0), int) else 0
                                for k in ("created", "exists", "skipped", "held", "errors")}})
    return {"running": _alive(st.get("pid")), "mode": _s(st.get("mode")),
            "reason": redact(_s(st.get("reason"))), "started": _s(st.get("started")),
            "last_poll": _s(st.get("last_poll")),
            "last_created": {"time": _s(lc.get("time")), "project": _s(lc.get("project")),
                             "file": _s(lc.get("source"))} if lc else None,
            "last_error": {"time": _s(le.get("time")), "message": redact(_s(le.get("message")))}
            if le else None,
            "dirs": dirs}
