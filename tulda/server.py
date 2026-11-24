"""Read-only gallery server over a renders root (GET/HEAD only)."""

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
from .gallery import render_index
from .items import (DATE_PREFIX, GROUP_ID, GROUP_PREFIX, RESERVED_DIRS, group_fields,  # noqa: F401
                    is_project_dir, read_item)


ALLOWED_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".svg": "image/svg+xml", ".json": "application/json",
               ".stl": "model/stl"}

PAGE_SIZE = 20


def _vals(q: dict, key: str, pat=re.compile(r"^[^\x00-\x1f]{1,100}$")) -> list[str]:
    return [v for v in q.get(key, []) if pat.match(v)][:10]


def parse_filters(q: dict, project: str | None = None, day: str | None = None) -> Filters:
    """Validated Filters from a parsed query string (parse_qs). Unknown/invalid values are dropped."""
    qv = (q.get("q") or [None])[0]
    return Filters(project=project, adapters=_vals(q, "adapter"), stages=_vals(q, "stage"),
                   views=_vals(q, "view"), tags=[t.lower() for t in _vals(q, "tag")],
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
         [("view", v) for v in f.views] + [("tag", v) for v in f.tags]
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

    # -- helpers
    def _send(self, status: int, body: bytes = b"", ctype="text/plain; charset=utf-8",
              head=False, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _send_page(self, html: str, head: bool):
        return self._send(200, html.encode(), "text/html; charset=utf-8", head, {
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Content-Security-Policy": "default-src 'none'; img-src 'self'; "
            "style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'"})

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
            self.send_header("Cache-Control", "no-cache")
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

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = _deny


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
                catalog: Catalog | None = None, log=print) -> Server:
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    srv = Server((host, port), Handler)
    srv.root = root
    srv.verbose = verbose
    srv.log = log
    srv.sync_min_interval = 2.0
    srv.catalog = catalog or Catalog(root, log=log)
    if catalog is None:
        srv.catalog.sync()
    srv.allowed_hosts = {h.lower() for h in allowed_hosts} | {"127.0.0.1", "localhost", host.lower()}
    return srv


class MultiServer:
    """One listening socket per address, all sharing one root, Host allow-list and
    handler. Addresses that fail to bind are logged and retried every retry_interval
    seconds while the others keep serving."""

    def __init__(self, root, hosts, port, allowed_hosts=(), verbose=False,
                 retry_interval=15.0, log=print, sync_interval=30.0):
        import threading
        self.root = Path(root).resolve()
        self.port = port
        self.verbose = verbose
        self.retry_interval = retry_interval
        self.log = log
        self.sync_interval = sync_interval
        self.catalog = Catalog(self.root, log=log)
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
                    srv = make_server(self.root, h, self.port, (), self.verbose, self.catalog, self.log)
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
