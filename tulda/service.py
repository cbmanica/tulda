"""Run `tulda serve` (com.tulda.server) and `tulda watch --from-config` (com.tulda.watch) as
macOS launchd user services (LaunchAgents). Every subcommand takes an optional target
`server` (default, unchanged behaviour), `watch` or `all`.

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
- The watcher is a separate job, not a thread in `serve`: the server exits 75 on every address
  change, must stay read-only, and must not share fate with archive extraction or Bambu Studio
  renders. Only the watcher may need a macOS privacy grant (Full Disk Access) for its inbox.
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
WATCH_LABEL = "com.tulda.watch"
LABELS = {"server": LABEL, "watch": WATCH_LABEL}
EXIT_TIMEOUT = 30          # seconds launchd waits after SIGTERM (the watcher finishes a file)
DEFAULT_PORT = 8765
THROTTLE = 10
SERVICE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
LAUNCHCTL = "/bin/launchctl"
PROJECT_DIR = Path(__file__).resolve().parent.parent


def uid() -> int:
    return os.getuid()


def domain() -> str:
    return f"gui/{uid()}"


def target(label: str = LABEL) -> str:
    return f"{domain()}/{label}"


def plist_path(label: str = LABEL) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


# ---- launchctl wrapper and argv builders -----------------------------------

def run_launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([LAUNCHCTL, *args], capture_output=True, text=True, timeout=30)


def argv_bootstrap(path) -> list[str]:
    return ["bootstrap", domain(), str(path)]


def argv_bootout(label: str = LABEL) -> list[str]:
    return ["bootout", target(label)]


def argv_kickstart(kill: bool = False, label: str = LABEL) -> list[str]:
    return ["kickstart", *(["-k"] if kill else []), target(label)]


def argv_print(label: str = LABEL) -> list[str]:
    return ["print", target(label)]


def argv_enable(on: bool = True, label: str = LABEL) -> list[str]:
    return ["enable" if on else "disable", target(label)]


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


def build_watch_plist(*, python: str, project: Path, log_dir: Path, config: str | None = None,
                      state_dir: str | None = None, at_login: bool = False) -> dict:
    """com.tulda.watch: `tulda watch --from-config`. Same interpreter/env as the server; low
    priority; a clean exit (SIGTERM, or a config it cannot use) is not restarted."""
    args = [python, "-u", "-m", "tulda", "watch", "--from-config", "--log-dir", str(log_dir)]
    if config:
        args += ["--config", str(config)]
    if state_dir:
        args += ["--state-dir", str(state_dir)]
    return {
        "Label": WATCH_LABEL,
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
        "ExitTimeOut": EXIT_TIMEOUT,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 5,
        "StandardOutPath": str(logs.log_path(log_dir, names=logs.WATCH_LOG_NAMES)),
        "StandardErrorPath": str(logs.log_path(log_dir, err=True, names=logs.WATCH_LOG_NAMES)),
    }


def plist_bytes(d: dict) -> bytes:
    return plistlib.dumps(d, sort_keys=False)


def read_installed(label: str = LABEL) -> dict | None:
    try:
        with open(plist_path(label), "rb") as f:
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


def server_binds_tailscale() -> bool:
    """True if the installed server service was set up with --tailscale."""
    return "--tailscale" in (read_installed() or {}).get("ProgramArguments", [])


def configured_watch_python() -> str | None:
    """Interpreter of the installed watch service, if any (else None: use this one)."""
    argv = (read_installed(WATCH_LABEL) or {}).get("ProgramArguments") or []
    return argv[0] if argv else None


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

def is_loaded(label: str = LABEL) -> tuple[bool, dict]:
    p = run_launchctl(*argv_print(label))
    if p.returncode != 0:
        return False, {}
    return True, parse_print(p.stdout)


def _check(p: subprocess.CompletedProcess, what: str) -> None:
    if p.returncode != 0:
        msg = (p.stderr or p.stdout).strip()
        raise TuldaError(f"launchctl {what} failed ({p.returncode}): {msg}", 4)


def _install_plist(d: dict, label: str, at_login: bool) -> Path:
    path = plist_path(label)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".plist.tmp")
    tmp.write_bytes(plist_bytes(d))
    os.replace(tmp, path)
    if is_loaded(label)[0]:
        run_launchctl(*argv_bootout(label))
    run_launchctl(*argv_enable(True, label))
    _check(run_launchctl(*argv_bootstrap(path)), "bootstrap")
    if not at_login:
        run_launchctl(*argv_enable(False, label))
    return path


def install(*, host, port, renders_root, at_login, python=None, project=PROJECT_DIR,
            log_dir=None, tailscale=False) -> Path:
    log_dir = Path(log_dir) if log_dir else logs.default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    d = build_plist(python=python or sys.executable, project=Path(project), host=host,
                    port=port, renders_root=renders_root, log_dir=log_dir, at_login=at_login,
                    tailscale=tailscale)
    return _install_plist(d, LABEL, at_login)


def install_watch(*, at_login, config=None, state_dir=None, python=None, project=PROJECT_DIR,
                  log_dir=None) -> Path:
    log_dir = Path(log_dir) if log_dir else logs.default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    d = build_watch_plist(python=python or sys.executable, project=Path(project), log_dir=log_dir,
                          config=config, state_dir=state_dir, at_login=at_login)
    return _install_plist(d, WATCH_LABEL, at_login)


def uninstall(label: str = LABEL) -> bool:
    was = False
    if is_loaded(label)[0]:
        was = True
        run_launchctl(*argv_bootout(label))
    run_launchctl(*argv_enable(True, label))  # clear our override entry
    path = plist_path(label)
    if path.exists():
        path.unlink()
        was = True
    return was


def _which_name(label: str) -> str:
    return "" if label == LABEL else " watch"


def start(label: str = LABEL) -> None:
    cfg = read_installed(label)
    if cfg is None:
        raise TuldaError(f"not installed; run `tulda service install{_which_name(label)}` first", 3)
    if is_loaded(label)[0]:
        _check(run_launchctl(*argv_kickstart(label=label)), "kickstart")
        return
    at_login = bool(cfg.get("RunAtLoad"))
    run_launchctl(*argv_enable(True, label))
    try:
        _check(run_launchctl(*argv_bootstrap(plist_path(label))), "bootstrap")
    finally:
        if not at_login:
            run_launchctl(*argv_enable(False, label))


def stop(label: str = LABEL) -> bool:
    if not is_loaded(label)[0]:
        return False
    _check(run_launchctl(*argv_bootout(label)), "bootout")
    return True


def restart(label: str = LABEL) -> None:
    if read_installed(label) is None:
        raise TuldaError(f"not installed; run `tulda service install{_which_name(label)}` first", 3)
    if is_loaded(label)[0]:
        _check(run_launchctl(*argv_kickstart(kill=True, label=label)), "kickstart -k")
    else:
        start(label)



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


def server_status(args) -> int:
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


def server_install(args) -> int:
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


def server_uninstall(args) -> int:
    print("uninstalled" if uninstall() else "nothing to uninstall")
    return 0


def server_start(args) -> int:
    start()
    cfg = read_installed() or {}
    argv = cfg.get("ProgramArguments", [])
    ok = _wait_all(_cfg_probe_hosts(cfg), int(args_option(argv, "--port", DEFAULT_PORT)))
    print("started" if ok else "started, but not listening yet; see `tulda service logs --err`")
    return 0 if ok else 1


def server_stop(args) -> int:
    print("stopped" if stop() else "not loaded")
    return 0


def server_restart(args) -> int:
    restart()
    cfg = read_installed() or {}
    argv = cfg.get("ProgramArguments", [])
    time.sleep(0.5)
    ok = _wait_all(_cfg_probe_hosts(cfg), int(args_option(argv, "--port", DEFAULT_PORT)))
    print("restarted" if ok else "restarted, but not listening yet; see `tulda service logs --err`")
    return 0 if ok else 1


def server_logs(args) -> int:
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


def server_print_plist(args) -> int:
    root = None
    if args.renders_root or os.environ.get("TULDA_RENDERS"):
        root = str(Path(args.renders_root or os.environ["TULDA_RENDERS"]).expanduser().resolve())
    d = build_plist(python=sys.executable, project=PROJECT_DIR, host=args.host, port=args.port,
                    renders_root=root, log_dir=logs.default_log_dir(), at_login=args.at_login,
                    tailscale=args.tailscale)
    sys.stdout.write(plist_bytes(d).decode())
    return 0


# ---- watch service ----------------------------------------------------------

def _watch_state_dir(cfg) -> Path:
    from . import watchconfig as wc
    argv = (cfg or {}).get("ProgramArguments", [])
    return wc.state_dir(args_option(argv, "--state-dir"))


def _watch_running(cfg, secs: float = 5.0) -> bool:
    """True once launchd reports a pid and the watcher wrote its state file."""
    from .watchd import read_state
    end = time.time() + secs
    while True:
        loaded, info = is_loaded(WATCH_LABEL)
        st = read_state(_watch_state_dir(cfg))
        if loaded and info.get("pid") and st and st.get("pid") == info.get("pid"):
            return True
        if time.time() >= end:
            return False
        time.sleep(0.25)


def watch_preflight(config: str | None) -> tuple[bool, list[str]]:
    """-> (ok, lines): the config must parse and have at least one [[watch.dir]]."""
    from . import watchconfig as wc
    path = wc.config_path(config)
    try:
        cfg = wc.load(path)
    except wc.WatchConfigError as e:
        return False, [f"error: {e}", "fix it (`tulda watch --check-config`) and run install again"]
    if not cfg.dirs:
        return False, [f"error: {path} has no [[watch.dir]] entries; add for example:", "",
                       *wc.SAMPLE.splitlines()]
    py = wc.real_python()
    lines = [f"config: {path} ({len(cfg.dirs)} director{'y' if len(cfg.dirs) == 1 else 'ies'})"]
    for e in cfg.dirs:
        lines.append(f"  {e.path} -> {e.destination}")
        prot = wc.protected_location(e.path)
        if prot:
            lines.append(f"  note: {prot} is privacy-protected (TCC); the service can only list it "
                         f"with Full Disk Access for {py} (System Settings > Privacy & Security > "
                         f"Full Disk Access > + > Cmd-Shift-G > paste the path), then "
                         f"`tulda service restart watch`. A dedicated inbox outside ~/Downloads, "
                         f"~/Desktop and ~/Documents needs no grant.")
    return True, lines


def watch_install(args) -> int:
    config = getattr(args, "config", None)
    config = str(Path(config).expanduser().resolve()) if config else None
    ok, lines = watch_preflight(config)
    for ln in lines:
        print(ln, file=sys.stdout if ok else sys.stderr)
    if not ok:
        return 2
    path = install_watch(at_login=args.at_login, config=config)
    print(f"installed {path}")
    up = _watch_running(read_installed(WATCH_LABEL))
    print("started: " + ("running" if up else "NOT running yet; see `tulda service logs watch`"))
    print("at login: " + ("yes" if args.at_login else "no (disabled; `tulda service start watch` to run)"))
    return 0 if up else 1


def watch_status(args) -> int:
    from .watchd import read_state, state_lines
    cfg = read_installed(WATCH_LABEL)
    loaded, info = is_loaded(WATCH_LABEL)
    running = info.get("state") == "running" and info.get("pid") is not None
    print(f"installed : {'yes' if cfg else 'no'}  ({plist_path(WATCH_LABEL)})")
    print(f"loaded    : {'yes' if loaded else 'no'}")
    print(f"running   : {'yes' if running else 'no'}" + (f"  pid {info['pid']}" if running else ""))
    print(f"last exit : {info.get('last exit raw') or '-'}   runs: {info.get('runs') or '-'}")
    print(f"at login  : {'yes' if (cfg or {}).get('RunAtLoad') else 'no'}")
    for ln in state_lines(read_state(_watch_state_dir(cfg))):
        print(ln)
    if cfg:
        print(f"logs      : {Path(cfg.get('StandardOutPath', '')).parent}")
    return 0 if running else 1


def watch_uninstall(args) -> int:
    print("uninstalled watch" if uninstall(WATCH_LABEL) else "watch: nothing to uninstall")
    return 0


def watch_start(args) -> int:
    start(WATCH_LABEL)
    up = _watch_running(read_installed(WATCH_LABEL))
    print("started watch" if up else "started watch, but not running yet; see `tulda service logs watch`")
    return 0 if up else 1


def watch_stop(args) -> int:
    print("stopped watch" if stop(WATCH_LABEL) else "watch: not loaded")
    return 0


def watch_restart(args) -> int:
    restart(WATCH_LABEL)
    time.sleep(0.5)
    up = _watch_running(read_installed(WATCH_LABEL))
    print("restarted watch" if up else "restarted watch, but not running yet; see `tulda service logs watch`")
    return 0 if up else 1


def watch_logs(args) -> int:
    cfg = read_installed(WATCH_LABEL)
    log_dir = Path(cfg["StandardOutPath"]).parent if cfg else logs.default_log_dir()
    path = logs.log_path(log_dir, args.err, logs.WATCH_LOG_NAMES)
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


def watch_print_plist(args) -> int:
    config = getattr(args, "config", None)
    config = str(Path(config).expanduser().resolve()) if config else None
    d = build_watch_plist(python=sys.executable, project=PROJECT_DIR, log_dir=logs.default_log_dir(),
                          config=config, at_login=args.at_login)
    sys.stdout.write(plist_bytes(d).decode())
    return 0


HANDLERS = {
    "status": (server_status, watch_status), "install": (server_install, watch_install),
    "uninstall": (server_uninstall, watch_uninstall), "start": (server_start, watch_start),
    "stop": (server_stop, watch_stop), "restart": (server_restart, watch_restart),
    "logs": (server_logs, watch_logs), "print-plist": (server_print_plist, watch_print_plist),
}


def dispatch(name: str):
    def run(args) -> int:
        which = getattr(args, "which", None) or "server"
        srv, wat = HANDLERS[name]
        if which == "server":
            return srv(args)
        if which == "watch":
            return wat(args)
        if name in ("logs", "print-plist"):
            raise TuldaError(f"`service {name}` takes one target: server or watch", 2)
        codes = []
        for title, fn in (("server", srv), ("watch", wat)):
            print(f"== {title} ==")
            codes.append(fn(args))
            sys.stdout.flush()
        return max(codes)
    return run


cmd_status, cmd_install, cmd_uninstall, cmd_start = (dispatch(n) for n in ("status", "install", "uninstall", "start"))
cmd_stop, cmd_restart, cmd_logs, cmd_print_plist = (dispatch(n) for n in ("stop", "restart", "logs", "print-plist"))


def add_service_parser(sub) -> None:
    sp = sub.add_parser("service", help="manage the launchd user services (com.tulda.server, com.tulda.watch)")
    ssub = sp.add_subparsers(dest="service_cmd", required=True)

    def which(p, choices=("server", "watch", "all")):
        p.add_argument("which", nargs="?", default="server", choices=choices,
                       help="server (default), watch" + (" or all" if "all" in choices else ""))

    def opts(p):
        p.add_argument("--port", type=int, default=DEFAULT_PORT)
        p.add_argument("--host", action="append", default=[], metavar="IP",
                       help="bind address; repeatable or comma-separated (default: auto LAN IPv4, restarted if it changes; 127.0.0.1 is always added)")
        p.add_argument("--tailscale", action="store_true",
                       help="also bind this Mac's tailnet IPv4 (reaches your whole tailnet)")
        p.add_argument("--renders-root", help="renders directory (default: as `serve`)")
        p.add_argument("--at-login", action="store_true",
                       help="also start at every login (default: only when started)")
        p.add_argument("--config", metavar="PATH",
                       help="watch only: watch config file baked into the plist (default: the user config)")

    p = ssub.add_parser("install", help="write the plist, bootstrap it, and start the service")
    which(p)
    opts(p)
    p.set_defaults(fn=cmd_install)
    p = ssub.add_parser("print-plist", help="print the plist that install would write")
    which(p, ("server", "watch"))
    opts(p)
    p.set_defaults(fn=cmd_print_plist)
    for name, fn, h in (("uninstall", cmd_uninstall, "stop and remove the plist"),
                        ("start", cmd_start, "bootstrap/kickstart the service"),
                        ("stop", cmd_stop, "bootout the service (plist stays installed)"),
                        ("restart", cmd_restart, "kickstart -k (kill and restart)"),
                        ("status", cmd_status, "loaded/running/pid/exit/URL/port check; watch: "
                                               "config mode, last poll, last import, last error")):
        q = ssub.add_parser(name, help=h)
        which(q)
        q.set_defaults(fn=fn)
    p = ssub.add_parser("logs", help="tail the service log")
    which(p, ("server", "watch"))
    p.add_argument("-n", type=int, default=50, help="lines (default 50)")
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("--err", action="store_true", help="stderr (server: request lines; watch: tracebacks) instead of stdout")
    p.set_defaults(fn=cmd_logs)
