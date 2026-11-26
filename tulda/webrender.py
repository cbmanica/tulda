"""Render buttons on the watcher page: the server's single write exception.

`POST /api/render` is the only request the server accepts that is not GET/HEAD. It renders ONE
model file of an imported project folder, and nothing else:

* Eligible files are computed on the server on every request: model files (RENDER_EXT) of
  project folders (a directory holding tulda-project.json) directly inside the watch
  destinations of the user config (`[[watch.dir]] destination`). The candidates are the
  manifest's `files` plus a bounded walk of the project's `files/` folder. Each one is
  re-validated: the project folder is a real directory (no symlink) directly in the destination,
  the file's resolved path equals its lexical path (no symlink anywhere below the project
  folder), it is a regular file, no path part starts with ".", the extension is allowed.
* The client never sends a path, only an opaque id: an HMAC (per-process secret) of the
  destination, folder and relative path. An id that is not in the freshly computed list is
  rejected.
* Refused (with the reason shown): a project whose manifest records a post-processing script
  that was not approved, or a .3mf whose fresh scan finds a script the manifest did not approve
  (or that cannot be scanned); a project folder containing a `tulda.toml` (adapter config would
  be picked up by the render); a file that is already rendered (matched by sha256).
* CSRF and transport checks (Host allow-list, Origin equal to the Host origin, a per-process
  random token in the form) are done by the server before anything here is called.
* Renders run in ONE background worker thread, one at a time, through the same CLI path as
  `tulda project create --render` / `tulda render` (a `python -m tulda render` subprocess), with
  a bounded queue; a file already queued or rendering is not queued twice.

Folder and file names come from downloads and are attacker-controlled: the page escapes them;
this module never builds a shell command (argv lists only) and never puts a name before `--`."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path, PurePosixPath

from .items import RESERVED_DIRS
from .timefmt import utc_iso
from .watchevents import redact

# Same values as projects.MANIFEST / projects.RENDER_EXT (a test keeps them equal). Not imported:
# the server must not load the import code (tests/test_watchconfig.py, ServerImportIsolation).
MANIFEST = "tulda-project.json"
RENDER_EXT = {".3mf", ".scad", ".stl"}

MAX_MANIFEST = 1 << 20
MAX_WALK = 500                 # entries looked at in one project's files/ folder
MAX_DEPTH = 4
MAX_PROJECTS = 500
# Views: what the CLI renders for imported projects. Bambu Studio and OpenSCAD give iso and bottom
# PNGs; `interactive` adds the 3D viewer's GLB. There is no PNG adapter for .stl, so an STL gets
# only the viewer mesh. `slice` is not a default anywhere (it slices with Bambu Studio and is only
# rendered when asked for), so it is not added here either.
VIEWS = {".3mf": "iso,bottom,interactive", ".scad": "iso,bottom,interactive", ".stl": "interactive"}
RENDER_TIMEOUT = 1800.0


def new_secret() -> bytes:
    return secrets.token_bytes(32)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_ok(expected: str, got) -> bool:
    return isinstance(got, str) and bool(expected) and hmac.compare_digest(expected.encode(), got.encode())


def watch_destinations(config_path: Path | None) -> list[Path]:
    """Destinations of the user config's [[watch.dir]] entries (resolved, de-duplicated).
    An unreadable or invalid config gives none: nothing is eligible."""
    if config_path is None:
        return []
    try:
        from .watchconfig import load
        cfg = load(Path(config_path))
    except Exception:  # noqa: BLE001 - any config problem means "no destinations"
        return []
    out: list[Path] = []
    for d in cfg.dirs:
        try:
            r = Path(d.destination).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if r.is_dir() and r not in out:
            out.append(r)
    return out


def _plain(name: str) -> bool:
    return bool(name) and not name.startswith(".") and "/" not in name and "\\" not in name \
        and "\x00" not in name and name not in RESERVED_DIRS


def _read_manifest(proj: Path) -> dict | None:
    m = proj / MANIFEST
    try:
        st = os.lstat(m)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_MANIFEST:
            return None
        data = json.loads(m.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def safe_project(dest: Path, name: str) -> Path | None:
    """DEST/name when it is a real directory (not a symlink) directly in dest. dest resolved."""
    if not _plain(name):
        return None
    p = dest / name
    try:
        st = os.lstat(p)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        return None
    try:
        if p.resolve(strict=True) != p:
            return None
    except (OSError, RuntimeError):
        return None
    return p


def safe_file(proj: Path, rel) -> Path | None:
    """proj/rel when rel is a plain relative path to a regular file with an allowed extension and
    no symlink anywhere between proj and the file (resolved path == lexical path)."""
    if not isinstance(rel, str) or not rel or "\x00" in rel or "\\" in rel or len(rel) > 1000:
        return None
    pp = PurePosixPath(rel)
    if pp.is_absolute() or not pp.parts or any(x in ("", ".", "..") or x.startswith(".") for x in pp.parts):
        return None
    if pp.suffix.lower() not in RENDER_EXT:
        return None
    p = proj.joinpath(*pp.parts)
    try:
        if p.resolve(strict=True) != p:
            return None
        st = os.lstat(p)
    except (OSError, RuntimeError):
        return None
    return p if stat.S_ISREG(st.st_mode) else None


def _walk_files(proj: Path) -> list[str]:
    """Model files under proj/files/ (no symlinks followed, bounded)."""
    out: list[str] = []
    base = proj / "files"
    seen = 0
    stack = [(base, 0)]
    while stack:
        d, depth = stack.pop()
        try:
            if os.lstat(d).st_mode & 0o170000 != stat.S_IFDIR:
                continue
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            continue
        for e in entries:
            seen += 1
            if seen > MAX_WALK:
                return out
            if e.name.startswith("."):
                continue
            if e.is_dir(follow_symlinks=False):
                if depth + 1 < MAX_DEPTH:
                    stack.append((Path(e.path), depth + 1))
            elif e.is_file(follow_symlinks=False) and Path(e.name).suffix.lower() in RENDER_EXT:
                out.append(Path(e.path).relative_to(proj).as_posix())
    return out


def file_id(secret: bytes, dest: Path, folder: str, rel: str) -> str:
    msg = "\x00".join((str(dest), folder, rel)).encode("utf-8", "surrogateescape")
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()[:32]


def candidates(dests: list[Path], secret: bytes) -> list[dict]:
    """Projects with their eligible files (validated, not hashed). Paths stay server-side."""
    projects = []
    for dest in dests:
        try:
            names = sorted(e.name for e in os.scandir(dest))
        except OSError:
            continue
        for name in names:
            if len(projects) >= MAX_PROJECTS:
                break
            proj = safe_project(dest, name)
            if proj is None:
                continue
            man = _read_manifest(proj)
            if man is None:
                continue
            rels = [r for r in man.get("files") or [] if isinstance(r, str)]
            rels += [r for r in _walk_files(proj) if r not in rels]
            files = []
            for rel in rels:
                path = safe_file(proj, rel)
                if path is None:
                    continue
                files.append({"id": file_id(secret, dest, name, rel), "rel": rel, "path": path,
                              "ext": path.suffix.lower()})
            title = man.get("title") if isinstance(man.get("title"), str) else None
            projects.append({"folder": name, "title": (title or name)[:200], "dest": dest.name,
                             "dest_path": dest, "path": proj, "manifest": man,
                             "created": man.get("created_utc") if isinstance(man.get("created_utc"), str) else None,
                             "anchor": "p-" + file_id(secret, dest, name, "")[:12], "files": files})
    projects.sort(key=lambda p: (p["created"] or "", p["folder"]), reverse=True)
    return projects


def find(projects: list[dict], fid) -> tuple[dict, dict] | None:
    if not isinstance(fid, str) or len(fid) != 32:
        return None
    for p in projects:
        for f in p["files"]:
            if hmac.compare_digest(f["id"], fid):
                return p, f
    return None


# ---- content checks (cached by file identity) -----------------------------------------------

_cache: dict[str, tuple] = {}
_cache_lock = threading.Lock()


def _ident(path: Path):
    st = os.stat(path)
    return (st.st_size, st.st_mtime_ns, st.st_ino, st.st_dev)


def file_facts(path: Path, fresh: bool = False) -> dict:
    """{"sha256", "scripts"} for a file; scripts is a list of post-process findings for a .3mf
    ([] otherwise) or None when the 3mf could not be scanned. Cached on (size, mtime, inode)."""
    try:
        ident = _ident(path)
    except OSError:
        return {"sha256": None, "scripts": None}
    key = str(path)
    with _cache_lock:
        hit = _cache.get(key)
    if hit and hit[0] == ident and not fresh:
        return hit[1]
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        sha = h.hexdigest()
    except OSError:
        sha = None
    scripts: list | None = []
    if path.suffix.lower() == ".3mf":
        from .mesh import MeshError
        from .threemf import find_post_process
        try:
            scripts = find_post_process(path)
        except (MeshError, OSError):
            scripts = None
    facts = {"sha256": sha, "scripts": scripts}
    with _cache_lock:
        if len(_cache) > 5000:
            _cache.clear()
        _cache[key] = (ident, facts)
    return facts


def hold_reason(project: dict, f: dict, facts: dict) -> str | None:
    """Why this file must not be rendered from the web page (None = it may be)."""
    proj: Path = project["path"]
    d = f["path"].parent
    while True:
        if (d / "tulda.toml").exists():
            return "the project folder contains a tulda.toml (adapter config); review it and render from the CLI"
        if d == proj or proj not in d.parents:
            break
        d = d.parent
    pp = project["manifest"].get("post_process")
    pp = pp if isinstance(pp, dict) else {}
    approved = pp.get("approved") is True
    if pp.get("present") and not approved:
        return "post-processing script not approved (see the import's held entry; approve with the CLI)"
    approved_cmds = {x.get("command") for x in pp.get("findings") or [] if isinstance(x, dict)} if approved else set()
    if f["ext"] == ".3mf":
        if facts.get("scripts") is None:
            return "cannot check this 3mf for post-processing scripts (not a readable 3mf)"
        if any(s.get("command") not in approved_cmds for s in facts["scripts"]):
            return "contains a post-processing script that was not approved"
    return None


def missing_views(ext: str, groups: list[dict]) -> list[str]:
    """Views the web render would make that no present render of this content has yet."""
    have = {v for g in groups for v in g.get("views") or []}
    return [v for v in VIEWS[ext].split(",") if v not in have]


def describe(projects: list[dict], catalog, jobs: dict) -> list[dict]:
    """Display data (no absolute paths): per file its rendered groups, hold reason, job state."""
    facts = {f["id"]: file_facts(f["path"]) for p in projects for f in p["files"]}
    try:
        groups = catalog.groups_by_source_sha([x["sha256"] for x in facts.values()]) if catalog else {}
    except Exception:  # noqa: BLE001 - a busy or broken catalog only hides the rendered state
        groups = {}
    out = []
    for p in projects:
        files = []
        for f in p["files"]:
            fx = facts[f["id"]]
            done = groups.get(fx["sha256"] or "", [])
            files.append({"id": f["id"], "file": f["rel"], "type": f["ext"].lstrip("."),
                          "rendered": done, "missing_views": missing_views(f["ext"], done),
                          "hold": hold_reason(p, f, fx), "job": jobs.get(f["id"])})
        out.append({"folder": p["folder"], "title": p["title"], "destination": p["dest"],
                    "created": p["created"], "anchor": p["anchor"], "files": files})
    return out


# ---- the render worker ---------------------------------------------------------------------

def cli_render(project: dict, f: dict, renders_root: Path, timeout: float = RENDER_TIMEOUT) -> tuple[bool, str]:
    """`tulda render FILE --project FOLDER --title TITLE --views ...` in a subprocess: the exact
    CLI path. -P keeps the current directory off sys.path; cwd is the renders root."""
    argv = [sys.executable, "-P", "-m", "tulda", "render", f"--project={project['folder']}",
            f"--title={project['title']}", f"--views={VIEWS[f['ext']]}", "--trigger=web",
            f"--renders-root={renders_root}", "--", str(f["path"])]
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, errors="replace",
                                cwd=str(renders_root), start_new_session=True)
    except OSError as e:
        return False, f"cannot start the renderer: {e}"
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        return False, f"timed out after {timeout:g}s"
    n = sum(1 for ln in out.splitlines() if ln.startswith("rendered "))
    errs = [ln for ln in err.splitlines() if ln.strip()]
    if proc.returncode == 0:
        return True, f"{n} render(s)"
    msg = "; ".join(errs[-3:]) or f"exit {proc.returncode}"
    return False, redact((f"{n} render(s), then: " if n else "") + msg)[:400]


class RenderQueue:
    """One worker thread, at most `max_pending` jobs waiting. Job state is kept per file id
    (the newest `keep` jobs) so the page can show queued / rendering / done / failed."""

    def __init__(self, run, max_pending: int = 8, keep: int = 100, log=print):
        self._run = run
        self.max_pending, self.keep, self.log = max_pending, keep, log
        self._cv = threading.Condition()
        self._pending: deque = deque()
        self.jobs: dict[str, dict] = {}
        self._thread: threading.Thread | None = None

    def submit(self, project: dict, f: dict) -> tuple[str, dict]:
        """-> ("queued" | "duplicate" | "full", job)."""
        with self._cv:
            cur = self.jobs.get(f["id"])
            if cur and cur["state"] in ("queued", "rendering"):
                return "duplicate", dict(cur)
            if len(self._pending) >= self.max_pending:
                return "full", dict(cur or {})
            job = {"id": f["id"], "project": project["folder"], "file": f["rel"], "state": "queued",
                   "message": None, "queued": _now(), "started": None, "finished": None}
            self.jobs.pop(f["id"], None)
            self.jobs[f["id"]] = job
            while len(self.jobs) > self.keep:
                old = next((k for k, j in self.jobs.items() if j["state"] in ("done", "failed")), None)
                if old is None:
                    break
                del self.jobs[old]
            self._pending.append((job, project, f))
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, daemon=True, name="web-render")
                self._thread.start()
            self._cv.notify()
            return "queued", dict(job)

    def snapshot(self) -> dict[str, dict]:
        with self._cv:
            return {k: dict(v) for k, v in self.jobs.items()}

    def active(self) -> bool:
        with self._cv:
            return any(j["state"] in ("queued", "rendering") for j in self.jobs.values())

    def _loop(self):
        while True:
            with self._cv:
                while not self._pending:
                    if not self._cv.wait(timeout=60):
                        if not self._pending:
                            self._thread = None
                            return
                job, project, f = self._pending.popleft()
                job["state"], job["started"] = "rendering", _now()
            self.log(f"web render: {project['folder']}/{f['rel']}")
            try:
                ok, msg = self._run(project, f)
            except Exception as e:  # noqa: BLE001 - a crash in one job must not kill the worker
                ok, msg = False, f"{type(e).__name__}: {e}"
            with self._cv:
                job["state"], job["message"], job["finished"] = ("done" if ok else "failed"), msg, _now()
            self.log(f"web render {'done' if ok else 'failed'}: {project['folder']}/{f['rel']}: {msg}")


def _now() -> str:
    return utc_iso()


def wait_idle(q: RenderQueue, timeout: float = 5.0) -> bool:
    """Test helper: wait until nothing is queued or rendering."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not q.active():
            return True
        time.sleep(0.01)
    return False
