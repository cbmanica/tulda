"""Gallery server over a renders root: GET/HEAD only, with ONE exception.

The exception is `POST /api/render` (the watcher page's Render buttons): it renders one model
file of an imported project folder in the configured watch destinations, identified by an
opaque id, after Host, Origin and per-process token checks. All validation, the queue and the
render subprocess live in webrender.py; see its docstring. Every other method or path is 405."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from pathlib import Path

from .catalog import Catalog, CatalogError, Filters, valid_day
from .gallery import GALLERY_JS, render_index
from .timefmt import LOCAL_HASH, script_hash
from .viewer import THREE_VERSION, render_viewer
from .items import (DATE_PREFIX, STATIC_PREFIX, VIEWER_PREFIX, GROUP_ID, GROUP_PREFIX, RESERVED_DIRS, group_fields,  # noqa: F401
                    TRIGGERS, WATCH_PREFIX, is_project_dir, read_item)
from .watchevents import default_state_dir, display_events, watcher_status
from .watchpage import RESULT_CODES, render_watch_page
from . import webrender

WATCH_EVENTS_SHOWN = 300
GALLERY_HASH = script_hash(GALLERY_JS)


ALLOWED_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".svg": "image/svg+xml", ".json": "application/json",
               ".stl": "model/stl", ".glb": "model/gltf-binary", ".htm": "text/html; charset=utf-8"}

# /_s/<path>: files shipped inside the package (never the renders root), by explicit allowlist.
# The vendored three.js lives in a version-named directory, so its URLs are immutable.
STATIC_ROOT = Path(__file__).resolve().parent / "static"
_JS = "application/javascript"
THREE_DIR = f"vendor/three-{THREE_VERSION}/"
STATIC_FILES = {
    "viewer.js": _JS,
    "turntable.js": _JS,
    THREE_DIR + "build/three.module.js": _JS,
    THREE_DIR + "build/three.core.js": _JS,
    THREE_DIR + "examples/jsm/controls/OrbitControls.js": _JS,
    THREE_DIR + "examples/jsm/loaders/GLTFLoader.js": _JS,
    THREE_DIR + "examples/jsm/utils/BufferGeometryUtils.js": _JS,
    THREE_DIR + "examples/jsm/utils/SkeletonUtils.js": _JS,
    THREE_DIR + "LICENSE": "text/plain",
}

PAGE_SIZE = 20

# Gallery and watcher pages: inline scripts are allowed only by sha256 hash (no 'unsafe-inline',
# no same-origin script files): the gallery script and the local-time script. Both are fixed
# strings; page data travels in a non-executed application/json block.
PAGE_SCRIPT_SRC = " ".join((GALLERY_HASH, LOCAL_HASH))
PAGE_CSP = ("default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; "
            f"script-src {PAGE_SCRIPT_SRC}; connect-src 'self'; form-action 'self'; frame-ancestors 'none'")


def _vals(q: dict, key: str, pat=re.compile(r"^[^\x00-\x1f]{1,100}$")) -> list[str]:
    return [v for v in q.get(key, []) if pat.match(v)][:10]


def parse_filters(q: dict, project: str | None = None, day: str | None = None) -> Filters:
    """Validated Filters from a parsed query string (parse_qs). Unknown/invalid values are dropped."""
    qv = (q.get("q") or [None])[0]
    return Filters(project=project, adapters=_vals(q, "adapter"), stages=_vals(q, "stage"),
                   views=_vals(q, "view"), tags=[t.lower() for t in _vals(q, "tag")],
                   triggers=[t for t in _vals(q, "trigger") if t in (*TRIGGERS, "unknown")],
                   starred=(q.get("starred") or [""])[0] not in ("", "0"),
                   trash=(q.get("trash") or [""])[0] not in ("", "0"),
                   q=qv[:100] if qv else None, day=day)


def filters_qs(f: Filters) -> str:
    p: list[tuple[str, str]] = []
    if f.project:
        p.append(("project", f.project))
    if f.day:
        p.append(("day", f.day))
    p += [("adapter", v) for v in f.adapters] + [("stage", v) for v in f.stages] + \
         [("view", v) for v in f.views] + [("tag", v) for v in f.tags] + \
         [("trigger", v) for v in f.triggers]
    if f.starred:
        p.append(("starred", "1"))
    if f.trash:
        p.append(("trash", "1"))
    if f.q is not None:
        p.append(("q", f.q))
    return urllib.parse.urlencode(p)


class PathError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def safe_resolve(root: Path, urlpath: str) -> Path:
    """Map a request path to a path inside root or raise PathError.
    root must already be resolved."""
    raw = urlpath.split("?", 1)[0].split("#", 1)[0]
    path = urllib.parse.unquote(raw)
    if "\x00" in path or "\\" in path:
        raise PathError(400, "bad path")
    parts = [p for p in path.split("/") if p]
    if any(p.startswith(".") for p in parts):
        raise PathError(403, "dotfiles are not served")
    try:
        resolved = root.joinpath(*parts).resolve(strict=True)
    except (OSError, RuntimeError):
        raise PathError(404, "not found") from None
    if not resolved.is_relative_to(root):
        raise PathError(403, "outside root")
    if any(p.startswith(".") for p in resolved.relative_to(root).parts):
        raise PathError(403, "dotfiles are not served")
    return resolved


def scan(root: Path) -> list[dict]:
    """Full filesystem scan (no catalog). Kept for tests and as the reference for the indexer."""
    items = []
    for pdir in sorted(root.iterdir()):
        if not is_project_dir(root, pdir):
            continue
        for png in pdir.iterdir():
            it = read_item(root, pdir, png)
            if it:
                items.append(it)
    items.sort(key=lambda i: (i["mtime"], i["file"]), reverse=True)
    return items


class Handler(BaseHTTPRequestHandler):
    server_version = "tulda"
    sys_version = ""

    def log_message(self, fmt, *args):
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def log_date_time_string(self):
        """Request-log timestamps in UTC (the stdlib default is the server's local zone)."""
        from .timefmt import utc_iso
        return utc_iso()

    # -- helpers
    def _send(self, status: int, body: bytes = b"", ctype="text/plain; charset=utf-8",
              head=False, extra=None, referrer="no-referrer"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", referrer)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _send_page(self, html: str, head: bool, viewer: bool = False, referrer: str = "no-referrer"):
        # The viewer page also loads same-origin ES modules (/_s/) and fetches the GLB.
        csp = ("default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; "
               "script-src 'self' 'unsafe-inline'; connect-src 'self'" if viewer else PAGE_CSP)
        return self._send(200, html.encode(), "text/html; charset=utf-8", head, {
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Content-Security-Policy": csp, "X-Frame-Options": "DENY"}, referrer=referrer)

    def _static(self, rel: str, head: bool):
        ctype = STATIC_FILES.get(rel)
        if ctype is None:
            return self._send(404, b"not found\n", head=head)
        try:
            data = (STATIC_ROOT / rel).read_bytes()
        except OSError:
            return self._send(404, b"not found\n", head=head)
        # vendored files sit in a version-named directory: safe to cache forever. Our own
        # scripts change with tulda, so they are revalidated on every load.
        cache = "public, max-age=31536000, immutable" if rel.startswith("vendor/") else "no-cache"
        return self._send(200, data, ctype, head, {"Cache-Control": cache})

    def _host_ok(self) -> bool:
        h = self.headers.get("Host")
        if not h:
            return False
        h = h.strip().lower()
        if h.startswith("["):
            name, _, rest = h[1:].partition("]")
            port = rest[1:] if rest.startswith(":") else ""
        else:
            name, _, port = h.partition(":")
        if port and (not port.isdigit() or int(port) != self.server.server_address[1]):
            return False
        return name in self.server.allowed_hosts

    def _json(self, obj, head: bool):
        return self._send(200, json.dumps(obj).encode(), "application/json", head,
                          {"Cache-Control": "no-store"})

    def _page(self, ctx: dict, f: Filters, head: bool, groups=None, nxt=None):
        cat: Catalog = self.server.catalog
        rev = cat.rev()     # read BEFORE the data: a write in between makes the page look stale, never fresh
        if groups is None:
            groups, nxt = cat.list_groups(f, PAGE_SIZE)
        data = {"ctx": ctx, "groups": groups, "next": nxt, "has_more": nxt is not None,
                "page_size": PAGE_SIZE, "facets": cat.facets(),
                "dates": cat.dates(f.trash), "rev": rev, "qs": filters_qs(f),
                "filters": {"adapter": f.adapters, "stage": f.stages, "view": f.views, "tag": f.tags,
                            "trigger": f.triggers,
                            "starred": f.starred, "trash": f.trash}}
        return self._send_page(render_index(data), head)

    def _serve(self, head: bool):
        if not self._host_ok():
            return self._send(403, b"bad host\n", head=head)
        path = self.path
        if not path.startswith("/"):
            return self._send(400, b"bad request\n", head=head)
        raw, _, query = path.partition("#")[0].partition("?")
        route = urllib.parse.unquote(raw)
        q = urllib.parse.parse_qs(query, max_num_fields=40)
        gparts = [p for p in route.split("/") if p]
        try:
            return self._route(route, gparts, q, path, self.server.root, self.server.catalog, head)
        except CatalogError as e:
            return self._send(400, (str(e) + "\n").encode(), head=head)
        except sqlite3.Error as e:
            self.server.log(f"catalog error on {raw}: {e}")
            return self._send(503, b"catalog unavailable; retry\n", head=head)

    def _route(self, route, gparts, q, path, root, cat, head):
        if route == "/favicon.ico":      # no icon; 204 keeps browser consoles free of 404 noise
            return self._send(204, head=head, extra={"Cache-Control": "public, max-age=86400"})
        cat.maybe_sync(self.server.sync_min_interval)
        if route == "/api/latest":
            return self._json(cat.latest(), head)
        if route == "/api/dates":
            return self._json({"dates": cat.dates((q.get("trash") or [""])[0] not in ("", "0"))}, head)
        if route == "/api/index":
            try:
                lim = int((q.get("limit") or ["1000"])[0])
            except ValueError:
                lim = 1000
            return self._json(cat.all_items(max(1, min(lim, 5000))), head)
        if route == "/api/groups":
            day = (q.get("day") or [None])[0]
            if day and not valid_day(day):
                return self._send(400, b"bad day\n", head=head)
            proj = (q.get("project") or [None])[0]
            f = parse_filters(q, proj, day)
            try:
                lim = int((q.get("limit") or [str(PAGE_SIZE)])[0])
            except ValueError:
                lim = PAGE_SIZE
            groups, nxt = cat.list_groups(f, lim, (q.get("cursor") or [None])[0])
            return self._json({"groups": groups, "next": nxt, "has_more": nxt is not None}, head)
        if route == "/api/watch":
            sdir = self.server.watch_state_dir
            return self._json({"status": watcher_status(sdir),
                               "events": display_events(sdir, WATCH_EVENTS_SHOWN),
                               "projects": self._projects()}, head)
        if gparts and gparts[0] == WATCH_PREFIX:
            if len(gparts) != 1:
                return self._send(404, b"not found\n", head=head)
            sdir = self.server.watch_state_dir
            code = (q.get("r") or [""])[0]
            html = render_watch_page(watcher_status(sdir), display_events(sdir, WATCH_EVENTS_SHOWN),
                                     self._projects(), self.server.render_token,
                                     code if code in RESULT_CODES else None,
                                     self.server.render_queue.active())
            # same-origin (not no-referrer) so the browser sends a real Origin with the form POST
            return self._send_page(html, head, referrer="same-origin")
        if route == "/search":
            f = parse_filters(q)
            return self._page({"kind": "search", "q": f.q or ""}, f, head)
        if gparts and gparts[0] == DATE_PREFIX:
            if len(gparts) != 2 or not valid_day(gparts[1]):
                return self._send(404, b"not found\n", head=head)
            f = parse_filters(q, None, gparts[1])
            return self._page({"kind": "day", "day": gparts[1]}, f, head)
        if gparts and gparts[0] == GROUP_PREFIX:
            if len(gparts) != 2 or not GROUP_ID.match(gparts[1]):
                return self._send(404, b"not found\n", head=head)
            g = cat.get_group(gparts[1])
            if not g or not g["n_renders"]:
                return self._send(404, b"no such group\n", head=head)
            return self._page({"kind": "group", "group": gparts[1]}, Filters(trash=True), head, [g], None)
        if gparts and gparts[0] == STATIC_PREFIX:
            return self._static("/".join(gparts[1:]), head)
        if gparts and gparts[0] == VIEWER_PREFIX:
            if len(gparts) != 3 or any(p.startswith(".") for p in gparts[1:]):
                return self._send(404, b"not found\n", head=head)
            r = cat.get_render(gparts[1], gparts[2])
            if not r:
                return self._send(404, b"no such render\n", head=head)
            return self._send_page(render_viewer(r), head, viewer=True)
        try:
            target = safe_resolve(root, path)
        except PathError as e:
            return self._send(e.status, (str(e) + "\n").encode(), head=head)
        if target.is_dir():
            if target != root and target.parent != root:
                return self._send(404, b"not found\n", head=head)
            if target == root:
                return self._page({"kind": "recent"}, parse_filters(q), head)
            return self._page({"kind": "project", "project": target.name},
                              parse_filters(q, target.name), head)
        ctype = ALLOWED_EXT.get(target.suffix.lower())
        if ctype is None or not target.is_file():
            return self._send(403, b"file type not served\n", head=head)
        try:
            f = open(target, "rb")
        except OSError:
            return self._send(404, b"not found\n", head=head)
        with f:
            size = os.fstat(f.fileno()).st_size
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            # meshes and turntable frames are named by timestamp and never rewritten
            stable = target.suffix.lower() == ".glb" or target.parent.name.endswith(".frames")
            self.send_header("Cache-Control", "public, max-age=3600" if stable else "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; sandbox")
            self.end_headers()
            if not head:
                shutil.copyfileobj(f, self.wfile)

    def do_GET(self):
        self._serve(False)

    def do_HEAD(self):
        self._serve(True)

    def _deny(self):
        self._send(405, b"read-only server\n", extra={"Allow": "GET, HEAD"})

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = _deny

    # -- the single write exception: POST /api/render (see webrender.py)
    def _projects(self) -> list[dict]:
        srv = self.server
        cands = webrender.candidates(webrender.watch_destinations(srv.watch_config), srv.render_secret)
        return webrender.describe(cands, srv.catalog, srv.render_queue.snapshot())

    def _origin_ok(self) -> bool:
        """Origin must be present and equal to this request's own (already allow-listed) Host
        origin. A browser POST from any other page carries that page's origin (or "null")."""
        origin = (self.headers.get("Origin") or "").strip().lower()
        host = (self.headers.get("Host") or "").strip().lower()
        if not origin or not host or origin != f"http://{host}":
            return False
        site = self.headers.get("Sec-Fetch-Site")
        return site is None or site.strip().lower() == "same-origin"

    def _refuse(self, status: int, msg: str):
        self.server.log(f"web render refused ({status}): {msg}")
        return self._send(status, (msg + "\n").encode(), extra={"Cache-Control": "no-store"})

    def do_POST(self):
        raw = self.path.partition("?")[0]
        if raw != "/api/render":
            return self._deny()
        if not self._host_ok():
            return self._refuse(403, "bad host")
        if not self._origin_ok():
            return self._refuse(403, "bad or missing Origin")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/x-www-form-urlencoded":
            return self._refuse(415, "form data expected")
        try:
            n = int(self.headers.get("Content-Length") or "-1")
        except ValueError:
            n = -1
        if not 0 < n <= 4096:
            return self._refuse(400, "bad length")
        try:
            body = self.rfile.read(n).decode("ascii")
            form = urllib.parse.parse_qs(body, strict_parsing=True, max_num_fields=4)
        except (UnicodeDecodeError, ValueError):
            return self._refuse(400, "bad form")
        if not webrender.token_ok(self.server.render_token, (form.get("token") or [None])[0]):
            return self._refuse(403, "bad or missing token")
        srv = self.server
        projects = webrender.candidates(webrender.watch_destinations(srv.watch_config), srv.render_secret)
        hit = webrender.find(projects, (form.get("id") or [None])[0])
        if hit is None:
            return self._refuse(404, "not an eligible file")
        project, f = hit
        facts = webrender.file_facts(f["path"], fresh=True)
        hold = webrender.hold_reason(project, f, facts)
        if hold:
            return self._result("held", project, None, hold)
        try:
            done = srv.catalog.groups_by_source_sha([facts["sha256"]]).get(facts["sha256"] or "")
        except sqlite3.Error:
            done = None
        if done and not webrender.missing_views(f["ext"], done):
            return self._result("rendered", project, None, "already rendered")
        code, job = srv.render_queue.submit(project, f)
        return self._result(code, project, job, None)

    def _result(self, code: str, project: dict, job, reason):
        accept = (self.headers.get("Accept") or "").lower()
        if "application/json" in accept:
            status = {"queued": 202, "duplicate": 202, "full": 503, "held": 409, "rendered": 409}[code]
            return self._send(status, json.dumps({"result": code, "reason": reason, "job": job}).encode(),
                              "application/json", extra={"Cache-Control": "no-store"})
        loc = f"/{WATCH_PREFIX}/?r={urllib.parse.quote(code)}#{urllib.parse.quote(project['anchor'])}"
        return self._send(303, b"", extra={"Location": loc, "Cache-Control": "no-store"})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        # HTTPServer.server_bind calls getfqdn(), a slow reverse-DNS lookup; skip it.
        import socketserver
        socketserver.TCPServer.server_bind(self)
        self.server_name = str(self.server_address[0])
        self.server_port = self.server_address[1]


def make_server(root: Path, host: str, port: int, allowed_hosts=(), verbose=False,
                catalog: Catalog | None = None, log=print, watch_state_dir: Path | None = None,
                watch_config: Path | None = None, render: dict | None = None) -> Server:
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    srv = Server((host, port), Handler)
    srv.root = root
    srv.verbose = verbose
    srv.log = log
    srv.sync_min_interval = 2.0
    # The watcher's state dir (watch.json, watch-events.jsonl): read only, for /_w/.
    srv.watch_state_dir = Path(watch_state_dir) if watch_state_dir else default_state_dir()
    # Render buttons: the config whose [[watch.dir]] destinations hold eligible projects (None =
    # no buttons), and the per-process secret, CSRF token and queue (shared by MultiServer).
    srv.watch_config = Path(watch_config) if watch_config else None
    render = render or new_render_state(root, log)
    srv.render_secret, srv.render_token, srv.render_queue = render["secret"], render["token"], render["queue"]
    srv.catalog = catalog or Catalog(root, log=log)
    if catalog is None:
        srv.catalog.sync()
    srv.allowed_hosts = {h.lower() for h in allowed_hosts} | {"127.0.0.1", "localhost", host.lower()}
    return srv


def new_render_state(root: Path, log=print, run=None) -> dict:
    root = Path(root).resolve()
    return {"secret": webrender.new_secret(), "token": webrender.new_token(),
            "queue": webrender.RenderQueue(run or (lambda p, f: webrender.cli_render(p, f, root)), log=log)}


class MultiServer:
    """One listening socket per address, all sharing one root, Host allow-list and
    handler. Addresses that fail to bind are logged and retried every retry_interval
    seconds while the others keep serving."""

    def __init__(self, root, hosts, port, allowed_hosts=(), verbose=False,
                 retry_interval=15.0, log=print, sync_interval=30.0, watch_config=None):
        import threading
        self.root = Path(root).resolve()
        self.port = port
        self.verbose = verbose
        self.retry_interval = retry_interval
        self.log = log
        self.sync_interval = sync_interval
        self.catalog = Catalog(self.root, log=log)
        self.render = new_render_state(self.root, log)
        self.watch_config = watch_config
        self._initial_sync()
        self.allowed = {h.lower() for h in allowed_hosts} | {"localhost", "127.0.0.1"}
        self.servers: dict[str, Server] = {}
        self.pending: list[str] = list(dict.fromkeys(hosts))
        self.errors: dict[str, str] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list = []
        self.try_bind()

    def _initial_sync(self) -> None:
        t0 = time.monotonic()
        try:
            st = self.catalog.sync()
            self.log(f"catalog: {self.catalog.path} synced in {time.monotonic() - t0:.2f}s "
                     f"({st['indexed']} indexed, {st['missing']} missing, {st['dirs']} dirs scanned)")
        except (sqlite3.Error, OSError) as e:
            self.log(f"catalog: initial sync failed: {e}")

    def _sync_loop(self):
        while not self._stop.wait(self.sync_interval):
            try:
                self.catalog.sync()
            except (sqlite3.Error, OSError) as e:
                self.log(f"catalog: sync failed: {e}")

    def try_bind(self) -> list[str]:
        import threading
        got = []
        with self._lock:
            for h in list(self.pending):
                try:
                    srv = make_server(self.root, h, self.port, (), self.verbose, self.catalog, self.log,
                                      render=self.render, watch_config=self.watch_config)
                except OSError as e:
                    if self.errors.get(h) != str(e):
                        self.log(f"cannot bind {h}:{self.port}: {e}; will retry")
                    self.errors[h] = str(e)
                    continue
                if not self.port:
                    self.port = srv.server_address[1]
                srv.allowed_hosts = self.allowed
                self.allowed.add(h.lower())
                self.servers[h] = srv
                self.pending.remove(h)
                self.errors.pop(h, None)
                got.append(h)
                t = threading.Thread(target=srv.serve_forever, daemon=True, name=f"serve-{h}")
                t.start()
                self._threads.append(t)
                self.log(f"listening on http://{h}:{self.port}/")
        return got

    def _retry_loop(self):
        while not self._stop.wait(self.retry_interval):
            if self.pending:
                self.try_bind()

    def serve_forever(self):
        import threading
        threading.Thread(target=self._retry_loop, daemon=True, name="bind-retry").start()
        if self.sync_interval > 0:
            threading.Thread(target=self._sync_loop, daemon=True, name="catalog-sync").start()
        self._stop.wait()

    def shutdown(self):
        self._stop.set()

    def server_close(self):
        self._stop.set()
        for srv in list(self.servers.values()):
            srv.shutdown()
            srv.server_close()
