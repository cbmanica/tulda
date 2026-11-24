"""tulda command line: render, doctor, adapters, serve."""

from __future__ import annotations

import argparse
import os
import shlex
import sys
import urllib.parse
from pathlib import Path

from . import __version__
from .adapters import load_registry, parse_views, plan_jobs
from .errors import TuldaError
from .netinfo import bonjour_name, lan_ipv4, local_host_names, parse_hosts, tailscale_dnsname, tailscale_ipv4
from .runner import make_group, run_chain, tool_version

DEFAULT_PORT = 8765


def renders_root(args, reg=None) -> Path:
    v = getattr(args, "renders_root", None) or os.environ.get("TULDA_RENDERS") \
        or (reg.defaults.get("renders_root") if reg else None)
    return Path(v).expanduser() if v else Path.home() / ".local" / "share" / "tulda" / "renders"


def parse_opts(items) -> dict:
    out = {}
    for it in items or []:
        k, sep, v = it.partition("=")
        if not sep or not k:
            raise TuldaError(f"--opt expects key=value, got {it!r}")
        out[k] = int(v) if v.isdigit() else v
    return out


def request_command(args, names: list[str]) -> str:
    """The request as typed, with file arguments reduced to base names (no local paths)."""
    parts = ["tulda", "render", *names]
    if args.title:
        parts += ["--title", args.title]
    if args.adapter:
        parts += ["--adapter", args.adapter]
    if args.stage:
        parts += ["--stage", args.stage]
    if args.all:
        parts.append("--all")
    views = parse_views(args.view, args.views)
    if views:
        parts += ["--views", ",".join(views)]
    for o in args.opt or []:
        parts += ["--opt", o]
    if args.project:
        parts += ["--project", args.project]
    return shlex.join(parts)


def cmd_render(args) -> int:
    srcs = [Path(f).expanduser().resolve() for f in args.files]
    for src in srcs:
        if not src.is_file():
            raise TuldaError(f"no such file: {src}")
    views = parse_views(args.view, args.views)
    group = make_group(args.title, srcs, request_command(args, [f.name for f in srcs]))
    code = 0
    ok_any = False
    seq = 0
    projects: list[str] = []
    root = None
    port = None
    for src in srcs:
        reg = load_registry(src, Path(args.config) if args.config else None)
        root = renders_root(args, reg)
        port = args.port or int(reg.defaults.get("port", DEFAULT_PORT))
        project = args.project or src.parent.name
        opts = parse_opts(args.opt)
        try:
            jobs = plan_jobs(reg, src, args.adapter, args.stage, args.all, views)
        except TuldaError as e:
            print(f"error [{src.name}]: {e}", file=sys.stderr)
            code = code or e.code
            continue
        for job in jobs:
            try:
                png = run_chain(reg, job.chain, src, root, project, opts, args.keep,
                                group=group, view=job.view, view_opts=job.options,
                                seq=seq)
            except TuldaError as e:
                print(f"error [{job.label}] {src.name}: {e}", file=sys.stderr)
                code = code or e.code
                continue
            finally:
                seq += 1
            ok_any = True
            if project not in projects:
                projects.append(project)
            print(f"rendered {png}  ({job.label})")
    if ok_any:
        hosts = [f"http://{bonjour_name()}:{port}"]
        from .service import configured_tailscale_host
        ts = configured_tailscale_host()
        if ts:
            hosts.append(f"http://{ts}:{port}")
        gq = urllib.parse.quote(group["id"])
        print(f"group: {group['id']}")
        for h, label in zip(hosts, ("view", "tailscale")):
            print(f"{label}: {h}/_g/{gq}/")
            for p in projects:
                print(f"  project: {h}/{urllib.parse.quote(p)}/")
    return code


def cmd_adapters(args) -> int:
    reg = load_registry(None, Path(args.config) if args.config else None)
    for a in reg.adapters.values():
        miss = a.missing()
        print(f"{a.id:20} {','.join(a.accepts):14} -> {a.produces:4} {a.stage or '-':7} "
              f"prio={a.priority:<3} {'OK' if not miss else 'MISSING ' + ','.join(miss)}")
    for cid, steps in reg.chains.items():
        print(f"chain {cid}: {' -> '.join(steps)}")
    return 0


def cmd_doctor(args) -> int:
    reg = load_registry(None, Path(args.config) if args.config else None)
    print(f"tulda {__version__}; config files: {', '.join(reg.files)}")
    bad = 0
    seen: dict = {}
    for a in reg.adapters.values():
        errs = a.config_errors(reg.defaults)
        miss = a.missing()
        status = "ok" if not errs and not miss else "BROKEN"
        if a.enabled and status != "ok":
            bad += 1
        print(f"[{status}] {a.id} ({a.source}){'' if a.enabled else ' [disabled]'}")
        for b, path in a.resolved_binaries().items():
            print(f"    {b}: {path or 'NOT FOUND'}")
        if not miss and a.version_cmd:
            print(f"    version: {tool_version(a)}")
        for m in miss:
            print(f"    missing requirement: {m}")
        for e in errs:
            print(f"    config error: {e}")
        if a.produces == "png":
            for ext in a.accepts:
                seen.setdefault((ext, a.priority), []).append(a.id)
    for (ext, prio), ids in seen.items():
        if len(ids) > 1:
            print(f"warning: {ext} priority {prio} shared by {', '.join(ids)} (config order breaks the tie)")
    root = renders_root(args, reg)
    print(f"renders root: {root}")
    print(f"bind address would be {lan_ipv4()}, host name {bonjour_name()}, "
          f"tailnet {tailscale_ipv4() or 'not found'}")
    return 3 if bad else 0


def resolve_hosts(explicit, tailscale: bool):
    """-> (hosts, auto) where auto maps 'lan'/'tailscale' to the auto-detected IP
    (None when not found). Explicit hosts replace the LAN default; --tailscale adds."""
    hosts = parse_hosts(explicit)
    auto: dict = {}
    if not hosts:
        auto["lan"] = lan_ipv4()
        hosts.append(auto["lan"])
    if tailscale:
        auto["tailscale"] = tailscale_ipv4()
        if auto["tailscale"]:
            if auto["tailscale"] not in hosts:
                hosts.append(auto["tailscale"])
        else:
            print("warning: --tailscale: no tailnet IPv4 found (is Tailscale up?); "
                  "continuing without it", file=sys.stderr, flush=True)
    return hosts, auto


def cmd_serve(args) -> int:
    import signal
    import threading
    import time
    from .server import MultiServer
    if args.log_dir:
        from .logs import redirect_stdio
        redirect_stdio(Path(args.log_dir).expanduser())
    reg = load_registry(None, Path(args.config) if args.config else None)
    hosts, auto = resolve_hosts(args.host, args.tailscale)
    port = args.port if args.port is not None else int(reg.defaults.get("port", DEFAULT_PORT))
    root = renders_root(args, reg)
    magic = tailscale_dnsname() if args.tailscale else None
    names = [bonjour_name(), *local_host_names(magic), *args.allow_host]
    srv = MultiServer(root, hosts, port, names, args.verbose,
                      log=lambda m: print(m, flush=True), sync_interval=args.sync_interval)
    if not srv.servers:
        print("error: could not bind any address; exiting for launchd retry", file=sys.stderr, flush=True)
        return 1
    bound = srv.port
    print(f"tulda {__version__} pid={os.getpid()} bind={','.join(hosts)} port={bound} root={srv.root}", flush=True)
    for h in srv.servers:
        print(f"serving {srv.root} (read-only) on http://{h}:{bound}/", flush=True)
    print(f"LAN URL: http://{bonjour_name()}:{bound}/", flush=True)
    if "tailscale" in auto and auto["tailscale"]:
        print(f"Tailscale URL: http://{(magic if args.tailscale and magic else auto['tailscale'])}:{bound}/", flush=True)

    def sigterm(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, sigterm)

    if auto and args.watch_interval > 0:
        derive = {"lan": lan_ipv4, "tailscale": tailscale_ipv4}

        def watch():
            # Auto-detected addresses (LAN IPv4, tailnet IPv4) are re-derived; if one differs
            # from what was bound (DHCP change, or Tailscale appeared/changed after start) exit
            # 75 so launchd's KeepAlive restarts us. Explicit --host values are never watched;
            # those that failed to bind are retried in-process by MultiServer.
            while True:
                time.sleep(args.watch_interval)
                for kind, was in auto.items():
                    now = derive[kind]()
                    if now != was:
                        print(f"{'LAN' if kind == 'lan' else 'Tailscale'} address changed {was} -> {now}; exiting for restart", flush=True)
                        os._exit(75)
        threading.Thread(target=watch, daemon=True, name="ip-watch").start()
    try:
        srv.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        print("stopped", flush=True)
    finally:
        srv.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tulda", description="Render gallery and adapter runner.")
    p.add_argument("--version", action="version", version=f"tulda {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--config", help="extra TOML config, merged last")
        sp.add_argument("--renders-root", help="renders directory (env TULDA_RENDERS)")

    r = sub.add_parser("render", help="render one or more files to PNG via adapters; "
                       "all outputs of one invocation form one group (request)")
    r.add_argument("files", nargs="+", metavar="file")
    r.add_argument("--title", "--group", dest="title", metavar="NAME",
                   help="human title for this request's group")
    r.add_argument("--view", action="append", default=[], metavar="NAME",
                   help="view to render (repeatable or comma separated), e.g. bottom")
    r.add_argument("--views", metavar="A,B", help="comma separated views, e.g. iso,bottom "
                   "(default: the adapter's default_views)")
    r.add_argument("--adapter", help="adapter id, a,b chain, or named chain")
    r.add_argument("--stage", choices=["source", "built", "sliced", "mesh"])
    r.add_argument("--all", action="store_true", help="render with every satisfiable terminal adapter")
    r.add_argument("--opt", action="append", metavar="K=V", help="override option, e.g. w=800")
    r.add_argument("--project")
    r.add_argument("--keep", action="store_true", help="keep intermediates in build/ beside the renders root")
    r.add_argument("--port", type=int, help="port used in the printed URL")
    common(r)
    r.set_defaults(fn=cmd_render)
    d = sub.add_parser("doctor", help="check adapters and environment")
    common(d)
    d.set_defaults(fn=cmd_doctor)
    a = sub.add_parser("adapters", help="list adapters")
    common(a)
    a.set_defaults(fn=cmd_adapters)
    s = sub.add_parser("serve", help="serve the renders root read-only")
    s.add_argument("--host", action="append", default=[], metavar="IP",
                   help="bind address; repeatable or comma-separated (default: LAN IPv4, else 127.0.0.1)")
    s.add_argument("--tailscale", action="store_true",
                   help="also bind this Mac's tailnet IPv4 (reaches your whole tailnet)")
    s.add_argument("--port", type=int)
    s.add_argument("--allow-host", action="append", default=[], help="extra accepted Host header name")
    s.add_argument("--verbose", action="store_true", help="log every request line (stderr)")
    s.add_argument("--log-dir", help="rotate and redirect stdout/stderr to server.{out,err}.log here")
    s.add_argument("--watch-interval", type=float, default=30.0, metavar="SEC",
                   help="exit 75 if an auto-detected address (LAN/tailnet) differs from the bound one (0 = off)")
    s.add_argument("--sync-interval", type=float, default=30.0, metavar="SEC",
                   help="incremental catalog sync period for renders created elsewhere (0 = off)")
    common(s)
    s.set_defaults(fn=cmd_serve)
    from .manage import add_manage_parsers
    add_manage_parsers(sub, common)
    from .service import add_service_parser
    add_service_parser(sub)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except TuldaError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
