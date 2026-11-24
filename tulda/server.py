"""Read-only gallery server over a renders root (GET/HEAD only)."""

from __future__ import annotations

import json
import os
import re
import shutil
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from pathlib import Path

from .gallery import render_index

ALLOWED_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".svg": "image/svg+xml", ".json": "application/json",
               ".stl": "model/stl"}
MAX_SIDECAR = 256 * 1024
GROUP_ID = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
GROUP_PREFIX = "_g"   # /_g/<group id>/ is the group page; a project dir of that name is not listed


def _strs(v, limit=50) -> list[str]:
    return [x[:200] for x in v[:limit] if isinstance(x, str)] if isinstance(v, list) else []


def _text(v, limit=300):
    return v[:limit] if isinstance(v, str) else None


def group_fields(meta: dict, mtime: float) -> dict:
    """Group info for one render. Sidecars without a valid group_id (legacy, or no sidecar)
    are grouped by UTC day of their creation time."""
    gid = meta.get("group_id")
    if isinstance(gid, str) and GROUP_ID.match(gid):
        return {"group_id": gid, "group_title": _text(meta.get("group_title")),
                "group_sources": _strs(meta.get("group_sources")),
                "request_cmd": _text(meta.get("request_cmd"), 1000),
                "group_created_utc": _text(meta.get("group_created_utc"), 40),
                "seq": meta["seq"] if isinstance(meta.get("seq"), int) else 0, "legacy": False}
    day = datetime.fromtimestamp(mtime, timezone.utc).strftime("%Y-%m-%d")
    return {"group_id": f"legacy-{day.replace('-', '')}", "group_title": f"Earlier renders, {day}",
            "group_sources": [], "request_cmd": None, "group_created_utc": None,
            "seq": 0, "legacy": True}


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
    items = []
    for pdir in sorted(root.iterdir()):
        if pdir.name.startswith(".") or pdir.name == GROUP_PREFIX or not pdir.is_dir():
            continue
        try:
            if not pdir.resolve().is_relative_to(root):
                continue
        except OSError:
            continue
        for png in pdir.iterdir():
            if png.name.startswith(".") or png.suffix.lower() != ".png":
                continue
            try:
                rp = png.resolve(strict=True)
                if not rp.is_relative_to(root):
                    continue
                mtime = rp.stat().st_mtime
            except OSError:
                continue
            meta = {}
            side = png.with_suffix(".json")
            try:
                if side.is_file() and side.resolve().is_relative_to(root) \
                        and side.stat().st_size <= MAX_SIDECAR:
                    meta = json.loads(side.read_text())
                    if not isinstance(meta, dict):
                        meta = {}
            except (OSError, ValueError):
                meta = {}
            q = urllib.parse.quote
            items.append({
                "project": pdir.name, "file": png.name,
                "url": f"/{q(pdir.name)}/{q(png.name)}",
                "json_url": f"/{q(pdir.name)}/{q(side.name)}" if meta else None,
                "mtime": mtime, "adapter": meta.get("adapter") or "unknown",
                "stage": meta.get("stage") or "unknown",
                "source_type": meta.get("source_type"), "source": meta.get("source"),
                "created_utc": meta.get("created_utc"), "git_sha": meta.get("git_sha"),
                "git_dirty": meta.get("git_dirty"),
                "tool_version": ((meta.get("steps") or [{}])[-1] or {}).get("tool_version")
                if isinstance(meta.get("steps"), list) and meta.get("steps") else None,
                "source_sha256": meta.get("source_sha256"),
                "view": _text(meta.get("view"), 40), **group_fields(meta, mtime),
            })
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
            "Cache-Control": "no-store",
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

    def _serve(self, head: bool):
        if not self._host_ok():
            return self._send(403, b"bad host\n", head=head)
        path = self.path
        if not path.startswith("/"):
            return self._send(400, b"bad request\n", head=head)
        route = urllib.parse.unquote(path.split("?", 1)[0].split("#", 1)[0])
        root: Path = self.server.root
        if route == "/api/index":
            body = json.dumps(scan(root)).encode()
            return self._send(200, body, "application/json", head,
                              {"Cache-Control": "no-store"})
        gparts = [p for p in route.split("/") if p]
        if gparts and gparts[0] == GROUP_PREFIX:
            if len(gparts) != 2 or not GROUP_ID.match(gparts[1]):
                return self._send(404, b"not found\n", head=head)
            items = scan(root)
            if not any(i["group_id"] == gparts[1] for i in items):
                return self._send(404, b"no such group\n", head=head)
            return self._send_page(render_index(items, None, gparts[1]), head)
        try:
            target = safe_resolve(root, path)
        except PathError as e:
            return self._send(e.status, (str(e) + "\n").encode(), head=head)
        if target.is_dir():
            if target != root and target.parent != root:
                return self._send(404, b"not found\n", head=head)
            project = None if target == root else target.name
            return self._send_page(render_index(scan(root), project), head)
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


def make_server(root: Path, host: str, port: int, allowed_hosts=(), verbose=False) -> Server:
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    srv = Server((host, port), Handler)
    srv.root = root
    srv.verbose = verbose
    srv.allowed_hosts = {h.lower() for h in allowed_hosts} | {"127.0.0.1", "localhost", host.lower()}
    return srv


class MultiServer:
    """One listening socket per address, all sharing one root, Host allow-list and
    handler. Addresses that fail to bind are logged and retried every retry_interval
    seconds while the others keep serving."""

    def __init__(self, root, hosts, port, allowed_hosts=(), verbose=False,
                 retry_interval=15.0, log=print):
        import threading
        self.root = Path(root).resolve()
        self.port = port
        self.verbose = verbose
        self.retry_interval = retry_interval
        self.log = log
        self.allowed = {h.lower() for h in allowed_hosts} | {"localhost", "127.0.0.1"}
        self.servers: dict[str, Server] = {}
        self.pending: list[str] = list(dict.fromkeys(hosts))
        self.errors: dict[str, str] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list = []
        self.try_bind()

    def try_bind(self) -> list[str]:
        import threading
        got = []
        with self._lock:
            for h in list(self.pending):
                try:
                    srv = make_server(self.root, h, self.port, (), self.verbose)
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
        self._stop.wait()

    def shutdown(self):
        self._stop.set()

    def server_close(self):
        self._stop.set()
        for srv in list(self.servers.values()):
            srv.shutdown()
            srv.server_close()
