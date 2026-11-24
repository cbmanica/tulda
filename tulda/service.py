"""Run `tulda serve` as a macOS launchd user service (LaunchAgent).

Design notes
- Launch line: the absolute path of the Python that runs the install command
  (normally <project>/.venv/bin/python, i.e. sys.executable, kept unresolved so the
  venv is honoured) with `-u -m tulda serve`, WorkingDirectory and PYTHONPATH set to
  the project. No `uv run` at service time: it needs uv on PATH, may re-sync/resolve
  and is a wrapper process; the venv python is one process and cwd independent.
- KeepAlive = {SuccessfulExit: false}: restart after a crash or the deliberate exit 75
  from the LAN-IP watcher, but not after a clean exit (SIGTERM is handled as exit 0).
  ThrottleInterval limits restart storms. KeepAlive makes launchd start the job as soon
  as it is bootstrapped, so "loaded" == "running or being kept running":
    start = enable + bootstrap, stop = bootout.
- Login behaviour: without --at-login the service is `launchctl disable`d after
  bootstrap, so it is not loaded at the next login (the plist stays installed);
  `start` re-enables it for this session. With --at-login it stays enabled and
  RunAtLoad is true.
- All launchctl calls go through `run_launchctl` so tests can mock it.
"""

from __future__ import annotations

import os
import plistlib
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import logs
from .errors import TuldaError

LABEL = "com.tulda.server"
DEFAULT_PORT = 8765
THROTTLE = 10
SERVICE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
LAUNCHCTL = "/bin/launchctl"
PROJECT_DIR = Path(__file__).resolve().parent.parent


def uid() -> int:
    return os.getuid()


def domain() -> str:
    return f"gui/{uid()}"


def target() -> str:
    return f"{domain()}/{LABEL}"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


# ---- launchctl wrapper and argv builders -----------------------------------

def run_launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([LAUNCHCTL, *args], capture_output=True, text=True, timeout=30)


def argv_bootstrap(path) -> list[str]:
    return ["bootstrap", domain(), str(path)]


def argv_bootout() -> list[str]:
    return ["bootout", target()]


def argv_kickstart(kill: bool = False) -> list[str]:
    return ["kickstart", *(["-k"] if kill else []), target()]


def argv_print() -> list[str]:
    return ["print", target()]


def argv_enable(on: bool = True) -> list[str]:
    return ["enable" if on else "disable", target()]


# ---- plist ------------------------------------------------------------------

def build_plist(*, python: str, project: Path, host, port: int,
                renders_root: str | None, log_dir: Path, at_login: bool = False,
                tailscale: bool = False, extra_args: list[str] | None = None) -> dict:
    args = [python, "-u", "-m", "tulda", "serve", "--port", str(port), "--verbose",
            "--log-dir", str(log_dir)]
    from .netinfo import parse_hosts
    for h in parse_hosts([host] if isinstance(host, str) else host):
        args += ["--host", h]
    if tailscale:
        args.append("--tailscale")
    if renders_root:
        args += ["--renders-root", str(renders_root)]
    args += list(extra_args or [])
    return {
        "Label": LABEL,
        "ProgramArguments": args,
        "WorkingDirectory": str(project),
        "EnvironmentVariables": {
            "PATH": SERVICE_PATH,
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": str(project),
            "HOME": str(Path.home()),
        },
        "RunAtLoad": bool(at_login),
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": THROTTLE,
        "ProcessType": "Background",
        "StandardOutPath": str(logs.log_path(log_dir)),
        "StandardErrorPath": str(logs.log_path(log_dir, err=True)),
    }


def plist_bytes(d: dict) -> bytes:
    return plistlib.dumps(d, sort_keys=False)


def read_installed() -> dict | None:
    try:
        with open(plist_path(), "rb") as f:
            return plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None


def args_options(argv: list[str], flag: str) -> list[str]:
    """Every value of a repeatable option (comma lists flattened)."""
    from .netinfo import parse_hosts
    return parse_hosts([argv[i + 1] for i, a in enumerate(argv[:-1]) if a == flag])


def configured_tailscale_host() -> str | None:
    """MagicDNS name (else tailnet IP) if the installed service was set up with --tailscale."""
    from .netinfo import tailscale_dnsname, tailscale_ipv4
    cfg = read_installed()
    if not cfg or "--tailscale" not in cfg.get("ProgramArguments", []):
        return None
    return tailscale_dnsname() or tailscale_ipv4()


def args_option(argv: list[str], flag: str, default=None):
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


# ---- `launchctl print` parsing ---------------------------------------------

def parse_print(text: str) -> dict:
    """Top-level fields of `launchctl print gui/UID/LABEL` (one tab of indent)."""
    out: dict = {}
    for line in text.splitlines():
        m = re.match(r"^\t(?!\t)([a-z][a-z ]*?) = (.*)$", line)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if k in ("state", "path", "type", "program", "immediate reason"):
            out[k] = v
        elif k == "pid" and v.isdigit():
            out["pid"] = int(v)
        elif k == "runs" and v.isdigit():
            out["runs"] = int(v)
        elif k == "last exit code":
            out["last exit code"] = int(v) if re.fullmatch(r"-?\d+", v) else None
            out["last exit raw"] = v
        elif k == "last terminating signal":
            out["last signal"] = v
    return out


# ---- operations -------------------------------------------------------------

def is_loaded() -> tuple[bool, dict]:
    p = run_launchctl(*argv_print())
    if p.returncode != 0:
        return False, {}
    return True, parse_print(p.stdout)


def _check(p: subprocess.CompletedProcess, what: str) -> None:
    if p.returncode != 0:
        msg = (p.stderr or p.stdout).strip()
        raise TuldaError(f"launchctl {what} failed ({p.returncode}): {msg}", 4)


def install(*, host, port, renders_root, at_login, python=None, project=PROJECT_DIR,
            log_dir=None, tailscale=False) -> Path:
    log_dir = Path(log_dir) if log_dir else logs.default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    d = build_plist(python=python or sys.executable, project=Path(project), host=host,
                    port=port, renders_root=renders_root, log_dir=log_dir, at_login=at_login,
                    tailscale=tailscale)
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".plist.tmp")
    tmp.write_bytes(plist_bytes(d))
    os.replace(tmp, path)
    if is_loaded()[0]:
        run_launchctl(*argv_bootout())
    run_launchctl(*argv_enable(True))
    _check(run_launchctl(*argv_bootstrap(path)), "bootstrap")
    if not at_login:
        run_launchctl(*argv_enable(False))
    return path


def uninstall() -> bool:
    was = False
    if is_loaded()[0]:
        was = True
        run_launchctl(*argv_bootout())
    run_launchctl(*argv_enable(True))  # clear our override entry
    path = plist_path()
    if path.exists():
        path.unlink()
        was = True
    return was


def start() -> None:
    cfg = read_installed()
    if cfg is None:
        raise TuldaError("not installed; run `tulda service install` first", 3)
    if is_loaded()[0]:
        _check(run_launchctl(*argv_kickstart()), "kickstart")
        return
    at_login = bool(cfg.get("RunAtLoad"))
    run_launchctl(*argv_enable(True))
    try:
        _check(run_launchctl(*argv_bootstrap(plist_path())), "bootstrap")
    finally:
        if not at_login:
            run_launchctl(*argv_enable(False))


def stop() -> bool:
    if not is_loaded()[0]:
        return False
    _check(run_launchctl(*argv_bootout()), "bootout")
    return True


def restart() -> None:
    if read_installed() is None:
        raise TuldaError("not installed; run `tulda service install` first", 3)
    if is_loaded()[0]:
        _check(run_launchctl(*argv_kickstart(kill=True)), "kickstart -k")
    else:
        start()



def port_listening(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def status_info() -> dict:
    cfg = read_installed()
    loaded, info = is_loaded()
    argv = (cfg or {}).get("ProgramArguments", [])
    port = int(args_option(argv, "--port", DEFAULT_PORT))
    out = {"installed": cfg is not None, "plist": str(plist_path()), "loaded": loaded,
           "state": info.get("state"), "pid": info.get("pid"),
           "last_exit": info.get("last exit raw"), "runs": info.get("runs"),
           "at_login": bool((cfg or {}).get("RunAtLoad")), "port": port,
           "log_dir": str(Path((cfg or {}).get("StandardOutPath", "")).parent) if cfg else None}
    return out


def wait_listening(host: str, port: int, secs: float = 8.0) -> bool:
    end = time.time() + secs
    while time.time() < end:
        if port_listening(host, port):
            return True
        time.sleep(0.25)
    return False


# ---- CLI --------------------------------------------------------------------

def _probe_hosts(hosts, tailscale: bool = False) -> list[str]:
    """Addresses the service would bind: explicit hosts, else LAN IPv4; plus tailnet IPv4,
    plus loopback (always bound; mirrors cli.resolve_hosts)."""
    from .netinfo import LOOPBACK, lan_ipv4, parse_hosts, tailscale_ipv4
    out = parse_hosts(hosts if not isinstance(hosts, str) else [hosts]) or [lan_ipv4()]
    if tailscale:
        ts = tailscale_ipv4()
        if ts and ts not in out:
            out.append(ts)
    if LOOPBACK not in out:
        out.append(LOOPBACK)
    return out


def _cfg_probe_hosts(cfg) -> list[str]:
    argv = (cfg or {}).get("ProgramArguments", [])
    return _probe_hosts(args_options(argv, "--host"), "--tailscale" in argv)


def _wait_all(hosts, port) -> bool:
    return all(wait_listening(h, port) for h in hosts)


def cmd_status(args) -> int:
    from .netinfo import bonjour_name
    s = status_info()
    print(f"installed : {'yes' if s['installed'] else 'no'}  ({s['plist']})")
    print(f"loaded    : {'yes' if s['loaded'] else 'no'}")
    running = s["state"] == "running" and s["pid"] is not None
    print(f"running   : {'yes' if running else 'no'}" + (f"  pid {s['pid']}" if running else ""))
    print(f"last exit : {s['last_exit'] if s['last_exit'] is not None else '-'}   runs: {s['runs'] if s['runs'] is not None else '-'}")
    print(f"at login  : {'yes' if s['at_login'] else 'no'}")
    cfg = read_installed()
    hosts = _cfg_probe_hosts(cfg)
    states = {h: bool(s["installed"] and port_listening(h, s["port"])) for h in hosts}
    for h, up in states.items():
        print(f"listening : {'yes' if up else 'no'}  ({h}:{s['port']})")
    listening = bool(states) and all(states.values())
    print(f"url       : http://{bonjour_name()}:{s['port']}/")
    for h in hosts:
        print(f"url       : http://{h}:{s['port']}/")
    ts = configured_tailscale_host()
    if ts:
        print(f"tailscale : http://{ts}:{s['port']}/")
    if s["log_dir"]:
        print(f"logs      : {s['log_dir']}")
    return 0 if running and listening else 1


def cmd_install(args) -> int:
    root = None
    if args.renders_root or os.environ.get("TULDA_RENDERS"):
        root = str(Path(args.renders_root or os.environ["TULDA_RENDERS"]).expanduser().resolve())
    path = install(host=args.host, port=args.port, renders_root=root, at_login=args.at_login,
                   tailscale=args.tailscale)
    print(f"installed {path}")
    ok = _wait_all(_probe_hosts(args.host, args.tailscale), args.port)
    print("started: " + ("listening" if ok else "NOT listening yet; see `tulda service logs --err`"))
    print("at login: " + ("yes" if args.at_login else "no (disabled; `tulda service start` to run)"))
    return 0 if ok else 1


def cmd_uninstall(args) -> int:
    print("uninstalled" if uninstall() else "nothing to uninstall")
    return 0


def cmd_start(args) -> int:
    start()
    cfg = read_installed() or {}
    argv = cfg.get("ProgramArguments", [])
    ok = _wait_all(_cfg_probe_hosts(cfg), int(args_option(argv, "--port", DEFAULT_PORT)))
    print("started" if ok else "started, but not listening yet; see `tulda service logs --err`")
    return 0 if ok else 1


def cmd_stop(args) -> int:
    print("stopped" if stop() else "not loaded")
    return 0


def cmd_restart(args) -> int:
    restart()
    cfg = read_installed() or {}
    argv = cfg.get("ProgramArguments", [])
    time.sleep(0.5)
    ok = _wait_all(_cfg_probe_hosts(cfg), int(args_option(argv, "--port", DEFAULT_PORT)))
    print("restarted" if ok else "restarted, but not listening yet; see `tulda service logs --err`")
    return 0 if ok else 1


def cmd_logs(args) -> int:
    cfg = read_installed()
    log_dir = Path(cfg["StandardOutPath"]).parent if cfg else logs.default_log_dir()
    path = logs.log_path(log_dir, args.err)
    if not path.exists():
        print(f"no log file yet: {path}", file=sys.stderr)
        return 1
    for line in logs.tail_lines(path, args.n):
        print(line)
    if args.follow:
        sys.stdout.flush()
        try:
            follow(path)
        except KeyboardInterrupt:
            pass
    return 0


def follow(path: Path, sleep=time.sleep, should_stop=lambda: False) -> None:
    """Like tail -f; reopens when the file is rotated or truncated."""
    f = open(path, "rb")
    f.seek(0, os.SEEK_END)
    ino = os.fstat(f.fileno()).st_ino
    try:
        while not should_stop():
            chunk = f.read()
            if chunk:
                sys.stdout.write(chunk.decode("utf-8", "replace"))
                sys.stdout.flush()
                continue
            try:
                st = os.stat(path)
            except OSError:
                st = None
            if st and (st.st_ino != ino or st.st_size < f.tell()):
                f.close()
                f = open(path, "rb")
                ino = os.fstat(f.fileno()).st_ino
                continue
            sleep(0.5)
    finally:
        f.close()


def cmd_print_plist(args) -> int:
    root = None
    if args.renders_root or os.environ.get("TULDA_RENDERS"):
        root = str(Path(args.renders_root or os.environ["TULDA_RENDERS"]).expanduser().resolve())
    d = build_plist(python=sys.executable, project=PROJECT_DIR, host=args.host, port=args.port,
                    renders_root=root, log_dir=logs.default_log_dir(), at_login=args.at_login,
                    tailscale=args.tailscale)
    sys.stdout.write(plist_bytes(d).decode())
    return 0


def add_service_parser(sub) -> None:
    sp = sub.add_parser("service", help="manage the launchd user service (com.tulda.server)")
    ssub = sp.add_subparsers(dest="service_cmd", required=True)

    def opts(p):
        p.add_argument("--port", type=int, default=DEFAULT_PORT)
        p.add_argument("--host", action="append", default=[], metavar="IP",
                       help="bind address; repeatable or comma-separated (default: auto LAN IPv4, restarted if it changes; 127.0.0.1 is always added)")
        p.add_argument("--tailscale", action="store_true",
                       help="also bind this Mac's tailnet IPv4 (reaches your whole tailnet)")
        p.add_argument("--renders-root", help="renders directory (default: as `serve`)")
        p.add_argument("--at-login", action="store_true",
                       help="also start at every login (default: only when started)")

    p = ssub.add_parser("install", help="write the plist, bootstrap it, and start the server")
    opts(p)
    p.set_defaults(fn=cmd_install)
    p = ssub.add_parser("print-plist", help="print the plist that install would write")
    opts(p)
    p.set_defaults(fn=cmd_print_plist)
    for name, fn, h in (("uninstall", cmd_uninstall, "stop and remove the plist"),
                        ("start", cmd_start, "bootstrap/kickstart the service"),
                        ("stop", cmd_stop, "bootout the service (plist stays installed)"),
                        ("restart", cmd_restart, "kickstart -k (kill and restart)"),
                        ("status", cmd_status, "loaded/running/pid/exit/URL/port check")):
        q = ssub.add_parser(name, help=h)
        q.set_defaults(fn=fn)
    p = ssub.add_parser("logs", help="tail the service log")
    p.add_argument("-n", type=int, default=50, help="lines (default 50)")
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("--err", action="store_true", help="stderr (request lines) instead of stdout")
    p.set_defaults(fn=cmd_logs)
