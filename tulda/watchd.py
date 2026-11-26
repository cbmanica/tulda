"""`tulda watch --from-config`: poll every configured [[watch.dir]] in one loop.

One process, no threads: each poll lists one directory non-recursively, so a slow import only
delays the other entries. The server never imports this module (see tests/test_invariants.py).

Service behaviour (com.tulda.watch):
- SIGTERM/SIGINT set a stop flag: the file being imported is finished (create_project builds in a
  staging dir and renames, so it is all-or-nothing), no further file or render is started, and
  the process exits 0. A second signal aborts the current import (its staging dir is removed).
- `--log-dir DIR` rotates and redirects stdout/stderr to DIR/watch.log and watch.err.log.
- A small state file (`<state dir>/watch.json`: pid, config, last poll, last created, last
  error, per-directory status) lets `tulda service status watch` and `doctor` show health
  without parsing logs. It is never served over HTTP.
- Reload without signals: the config file's (mtime, size, inode) is checked every tick; a change
  that is stable for one tick is re-read and validated.
- Last-known-good fallback: once a config has been loaded and at least one of its directories
  listed, its exact bytes are saved as `<state dir>/watch-config.last-good.toml` (mode 600,
  atomic). When the config fails to read/parse/validate, or none of its directories is usable,
  the watcher logs why and runs on the last-known-good copy (mode "fallback"); without a usable
  copy it stays alive and idle (mode "idle"), so launchd never restart-loops. Not-config modes
  re-try the real config on every change and every RETRY seconds. The user's config file is only
  ever read."""

from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from . import watchconfig as wc
from .projects import Watcher, git_publish_warning

COUNT_KEYS = ("created", "exists", "skipped", "errors", "waiting")
RETRY = 60.0               # seconds between re-tries of the real config while not on it


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def overrides_from_args(args) -> dict:
    return {"interval": getattr(args, "interval", None), "settle": getattr(args, "settle", None),
            "render": getattr(args, "render", None), "force": getattr(args, "force", None)}


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def state_path(sdir: Path) -> Path:
    return Path(sdir) / wc.STATE_NAME


def read_state(sdir: Path) -> dict | None:
    try:
        d = json.loads(state_path(sdir).read_text("utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def make_watcher(e: wc.WatchEntry, log, clock=time.time) -> Watcher:
    return Watcher(e.path, e.destination, settle=e.settle, render=e.render, force=e.force,
                   include=e.include, log=log, clock=clock)


class WatchLoop:
    """Polls each entry when due. `tick()` is one pass over the entries."""

    def __init__(self, cfg: wc.WatchConfig | None, log=print, clock=time.time):
        self.log, self.clock = log, clock
        self.entries: dict = {}
        self.totals: dict = {}
        if cfg is not None:
            self.apply(cfg)

    def apply(self, cfg: wc.WatchConfig | None) -> None:
        """Swap in a new entry list; watchers for an unchanged (path, destination) keep their
        memory of already-handled files."""
        old = self.entries
        self.entries = {}
        for e in (cfg.dirs if cfg else []):
            w = old[e.key][0] if e.key in old else make_watcher(e, self.log, self.clock)
            w.settle, w.render, w.force = e.settle, e.render, e.force
            w.include = list(e.include) if e.include else None
            self.entries[e.key] = [w, e, old[e.key][2] if e.key in old else 0.0]
            self.totals.setdefault(e.key, dict.fromkeys(COUNT_KEYS, 0))

    def tick(self, should_stop=lambda: False, once: bool = False) -> tuple[dict, bool]:
        """-> (counts, listed_any): listed_any is True if at least one directory was listed."""
        total = dict.fromkeys(COUNT_KEYS, 0)
        listed = False
        now = self.clock()
        for key, item in self.entries.items():
            w, e, due = item
            if should_stop():
                break
            if not once and now < due:
                continue
            item[2] = now + e.interval
            c = w.poll(once=once, should_stop=should_stop)
            w.last_poll = now_iso()
            if w.list_error is None:
                listed = True
            for k, v in c.items():
                total[k] += v
                if k != "waiting":
                    self.totals[key][k] += v
        return total, listed

    def period(self) -> float:
        ivs = [e.interval for _, e, _ in self.entries.values()]
        return max(0.2, min(ivs)) if ivs else 5.0

    def dir_states(self) -> list:
        out = []
        for key, (w, e, _) in self.entries.items():
            out.append({"path": str(e.path), "destination": str(e.destination),
                        "last_poll": getattr(w, "last_poll", None), "error": w.list_error,
                        "tcc_blocked": w.tcc_blocked, "last_error": w.last_error,
                        "last_created": w.last_created, "totals": self.totals.get(key)})
        return out


class WatchDaemon:
    """Config-driven loop with a state file. `start()` loads the config; `tick()` polls."""

    def __init__(self, cfg_path: Path, sdir: Path, overrides: dict | None = None, log=print,
                 clock=time.time, persist: bool = True):
        self.cfg_path, self.sdir, self.persist = Path(cfg_path), Path(sdir), persist
        self.overrides = overrides or {}
        self.log, self.clock = log, clock
        self.loop = WatchLoop(None, log=log, clock=clock)
        self.cfg: wc.WatchConfig | None = None
        self.raw: bytes | None = None          # bytes of the config in use
        self.good_saved = False
        self.sig_seen = self.sig_pending = None
        self.last_retry = 0.0
        self.state = {"pid": os.getpid(), "started": now_iso(), "config": str(self.cfg_path),
                      "mode": None, "since": None, "reason": None, "last_poll": None,
                      "last_created": None, "last_error": None, "last_good": None, "dirs": []}

    @property
    def mode(self) -> str | None:
        return self.state["mode"]

    @property
    def last_good(self) -> Path:
        return wc.last_good_path(self.sdir)

    # -- config --------------------------------------------------------------------------------
    def set_mode(self, mode: str, reason: str | None = None) -> None:
        if (mode, reason) != (self.state["mode"], self.state["reason"]):
            self.state.update(mode=mode, reason=reason, since=now_iso())

    def use(self, cfg: wc.WatchConfig | None) -> None:
        if cfg is not None:
            wc.apply_overrides(cfg, self.overrides)
            for e in cfg.dirs:
                w = git_publish_warning(e.destination)
                if w:
                    self.log(w)
        self.cfg = cfg
        self.loop.apply(cfg)
        for e in (cfg.dirs if cfg else []):
            self.log(f"watching {e.path} every {e.interval:g}s -> {e.destination}"
                     + (" (render)" if e.render else "") + (" (force)" if e.force else ""))

    def start_strict(self) -> None:
        """For --once: raises WatchConfigError on a bad config, no fallback."""
        self.raw, cfg = wc.read_config(self.cfg_path)
        self.use(cfg)
        self.set_mode("config")

    def start(self) -> None:
        """Load the config, falling back to last-known-good or idle; never raises on config."""
        self.sig_seen = self.signature()
        try:
            st = self.last_good.stat()
            self.state["last_good"] = {"path": str(self.last_good), "saved": datetime.fromtimestamp(
                st.st_mtime).astimezone().isoformat(timespec="seconds")}
        except OSError:
            pass
        self.load()

    def signature(self):
        try:
            st = os.stat(self.cfg_path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    @staticmethod
    def unusable(cfg: wc.WatchConfig) -> str | None:
        """A reason when the config has directories but none of them can be listed."""
        if not cfg.dirs:
            return None
        why = []
        for e in cfg.dirs:
            r = wc.probe_dir(e.path)
            if r is None:
                return None
            why.append(f"{e.path} " + ("is blocked by macOS privacy (TCC); grant Full Disk Access "
                                       f"to {wc.real_python()} or use an unprotected inbox"
                                       if r == "TCC" else r))
        return "no watched directory is usable: " + "; ".join(why)

    def load(self) -> None:
        self.last_retry = self.clock()
        try:
            raw, cfg = wc.read_config(self.cfg_path)
            why = self.unusable(cfg)
            if why:
                raise wc.WatchConfigError(why)
        except wc.WatchConfigError as e:
            self.fallback(str(e))
            return
        if self.mode != "config" or raw != self.raw:
            if self.mode in ("fallback", "idle"):
                self.log(f"config {self.cfg_path} is valid again; leaving {self.mode} mode")
            elif self.mode == "config":
                self.log(f"config {self.cfg_path} changed; reloaded")
            self.use(cfg)
            self.raw = raw
            self.good_saved = self._last_good_bytes() == raw
            if not cfg.dirs:
                self.log(f"no [[watch.dir]] entries in {self.cfg_path}; nothing to watch "
                         f"(`tulda service stop watch` to stop the service)")
        self.set_mode("config")

    def _last_good_bytes(self) -> bytes | None:
        try:
            return self.last_good.read_bytes()
        except OSError:
            return None

    def fallback(self, reason: str) -> None:
        if self.mode in ("fallback", "idle") and self.state["reason"] is not None \
                and self.state["reason"].startswith(reason):
            return                                   # same problem: already handled and logged
        self.log(f"error: config {self.cfg_path}: {reason}")
        if self.last_good.exists():
            try:
                raw, cfg = wc.read_config(self.last_good)
                why = self.unusable(cfg)
                if why:
                    raise wc.WatchConfigError(why)
                if not cfg.dirs:
                    raise wc.WatchConfigError("it has no [[watch.dir]] entries")
            except wc.WatchConfigError as e:
                msg = f"last-known-good config is unusable too ({e})"
            else:
                if self.mode != "fallback" or raw != self.raw:
                    self.log(f"running on last-known-good config {self.last_good} "
                             f"(your config file is not modified)")
                    self.use(cfg)
                    self.raw = raw
                self.set_mode("fallback", reason)
                return
        else:
            msg = "no last-known-good config saved yet"
        self.log(f"{msg}; idle until {self.cfg_path} changes (no restart needed)")
        self.use(None)
        self.raw = None
        self.set_mode("idle", f"{reason}; {msg}")

    def check_reload(self) -> None:
        sig = self.signature()
        if sig != self.sig_seen:
            if sig == self.sig_pending:              # unchanged for one tick: not mid-save
                self.sig_seen, self.sig_pending = sig, None
                self.load()
            else:
                self.sig_pending = sig
        elif self.mode != "config" and self.clock() - self.last_retry >= RETRY:
            self.load()                              # e.g. a watched folder appeared again

    def save_last_good(self) -> None:
        if self.good_saved or self.raw is None or self.mode != "config":
            return
        try:
            atomic_write(self.last_good, self.raw, 0o600)
        except OSError as e:
            self.log(f"warning: cannot save last-known-good config {self.last_good}: {e}")
            return
        self.good_saved = True
        self.state["last_good"] = {"path": str(self.last_good), "saved": now_iso()}
        self.log(f"saved last-known-good config {self.last_good}")

    # -- polling -------------------------------------------------------------------------------
    def tick(self, should_stop=lambda: False, once: bool = False) -> dict:
        if not once:
            self.check_reload()
        counts, listed = self.loop.tick(should_stop, once)
        if listed and self.persist:
            self.save_last_good()
        self.after_poll(listed)
        return counts

    def after_poll(self, listed: bool) -> None:
        if self.loop.entries:
            self.state["last_poll"] = now_iso()
        for w, _, _ in self.loop.entries.values():
            if w.last_created and w.last_created != self.state["last_created"] and (
                    not self.state["last_created"]
                    or w.last_created["time"] >= self.state["last_created"]["time"]):
                self.state["last_created"] = dict(w.last_created, dir=str(w.src_dir))
            err = w.list_error or w.last_error
            if err and err != (self.state["last_error"] or {}).get("message"):
                self.state["last_error"] = {"time": now_iso(), "message": err}
        self.write_state()

    def write_state(self) -> None:
        self.state["dirs"] = self.loop.dir_states()
        if not self.persist:
            return
        try:
            atomic_write(state_path(self.sdir),
                         (json.dumps(self.state, indent=2, ensure_ascii=False) + "\n").encode())
        except OSError as e:
            self.log(f"warning: cannot write state file {state_path(self.sdir)}: {e}")

    def period(self) -> float:
        return self.loop.period()


def state_lines(st: dict | None) -> list[str]:
    """Human summary of a watch.json for `service status watch` and `doctor`."""
    if not st:
        return ["state     : no state file yet (watcher never ran)"]
    alive = pid_alive(st.get("pid"))
    out = [f"process   : pid {st.get('pid')} {'running' if alive else 'not running'}, "
           f"started {st.get('started')}"]
    mode = st.get("mode")
    if mode == "fallback":
        out.append(f"config    : running on fallback config since {st.get('since')}: {st.get('reason')}")
    elif mode == "idle":
        out.append(f"config    : idle since {st.get('since')}: {st.get('reason')}")
    else:
        out.append(f"config    : {st.get('config')}")
    out.append(f"last poll : {st.get('last_poll') or '-'}")
    lc = st.get("last_created")
    out.append(f"last new  : {lc['time']} {lc['project']} <- {lc['source']}" if lc else "last new  : -")
    le = st.get("last_error")
    out.append(f"last error: {le['time']} {le['message']}" if le else "last error: -")
    for d in st.get("dirs") or []:
        t = d.get("totals") or {}
        out.append(f"watching  : {d['path']} -> {d['destination']}  ("
                   + ", ".join(f"{k} {t.get(k, 0)}" for k in ("created", "exists", "skipped", "errors"))
                   + ")")
        if d.get("error"):
            out.append(f"  problem : {d['error']}")
    return out


class Stopper:
    """SIGTERM/SIGINT: first signal requests a clean stop, a second aborts the current file."""

    def __init__(self, log=print):
        self.event = threading.Event()
        self.log = log

    def __call__(self) -> bool:
        return self.event.is_set()

    def install(self) -> None:
        def handler(signum, frame):
            if self.event.is_set():
                raise KeyboardInterrupt
            self.log(f"received {signal.Signals(signum).name}; finishing the current file, then exiting")
            self.event.set()
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)

    def sleep(self, secs: float) -> None:
        self.event.wait(secs)


def timestamped(m: str) -> None:
    print(f"{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')} {m}", flush=True)


def check_config(path: Path, overrides: dict | None = None) -> int:
    """`tulda watch --check-config [PATH]`: validate and summarise; never runs, never writes."""
    try:
        cfg = wc.apply_overrides(wc.load(path), overrides or {})
    except wc.WatchConfigError as e:
        print(f"invalid: {e}", file=sys.stderr)
        return 2
    print(f"valid: {path}")
    if not cfg.dirs:
        print("no [[watch.dir]] entries; nothing would be watched")
    for e in cfg.dirs:
        print(f"  {e.path} -> {e.destination}  interval {e.interval:g}s, settle {e.settle:g}s, "
              f"render {str(e.render).lower()}, force {str(e.force).lower()}"
              + (f", include {', '.join(e.include)}" if e.include else ""))
        why = wc.probe_dir(e.path)
        if why:
            print(f"    warning: watched path "
                  + ("is blocked by macOS privacy (TCC) for this process" if why == "TCC" else why))
    return 0


def cmd_watch_config(args) -> int:
    if getattr(args, "check_config", None) is not None:
        return check_config(Path(args.check_config).expanduser() if args.check_config
                            else wc.config_path(getattr(args, "config", None)),
                            overrides_from_args(args))
    if getattr(args, "log_dir", None):
        from .logs import WATCH_LOG_NAMES, redirect_stdio
        redirect_stdio(Path(args.log_dir).expanduser(), names=WATCH_LOG_NAMES)
    path = wc.config_path(getattr(args, "config", None))
    sdir = wc.state_dir(getattr(args, "state_dir", None))
    log = timestamped if not args.once else (lambda m: print(m, flush=True))
    d = WatchDaemon(path, sdir, overrides_from_args(args), log=log, persist=not args.once)
    if args.once:
        d.start_strict()
        if not d.cfg.dirs:
            raise wc.WatchConfigError(f"{path}: no [[watch.dir]] entries; add one, e.g.:\n{wc.SAMPLE}")
        c = d.tick(once=True)
        print("summary: " + ", ".join(f"{k} {v}" for k, v in c.items()), flush=True)
        return 1 if c["errors"] else 0
    stop = Stopper(log)
    stop.install()
    log(f"tulda watch pid={os.getpid()} config={path} state={sdir}")
    d.start()
    d.write_state()
    try:
        while not stop():
            d.tick(should_stop=stop)
            stop.sleep(d.period())
    except KeyboardInterrupt:
        log("aborted the current file")
    d.write_state()
    log("stopped")
    sys.stdout.flush()
    return 0


def cmd_show_last_good(args) -> int:
    lg = wc.last_good_path(wc.state_dir(args.state_dir))
    try:
        raw = lg.read_bytes()
    except FileNotFoundError:
        print(f"no last-known-good config yet ({lg}); it is saved after the watcher has used a "
              f"config successfully", file=sys.stderr)
        return 1
    if not args.diff:
        print(f"# {lg}")
        sys.stdout.write(raw.decode("utf-8", "replace"))
        return 0
    import difflib
    cur = wc.config_path(args.config)
    try:
        now = cur.read_bytes().decode("utf-8", "replace")
    except OSError:
        now = ""
    diff = list(difflib.unified_diff(raw.decode("utf-8", "replace").splitlines(True),
                                     now.splitlines(True), str(lg), str(cur)))
    if not diff:
        print("identical")
        return 0
    sys.stdout.writelines(d if d.endswith("\n") else d + "\n" for d in diff)
    return 0


def add_config_parser(sub) -> None:
    c = sub.add_parser("config", help="watch config helpers (read-only)")
    cs = c.add_subparsers(dest="config_cmd", required=True)
    g = cs.add_parser("show-last-good", help="print the last-known-good watch config the watcher "
                      "falls back to (or --diff it against the current config)")
    g.add_argument("--diff", action="store_true", help="unified diff: last-good -> current config")
    g.add_argument("--config", metavar="PATH", help="current config to diff against (default: user config)")
    g.add_argument("--state-dir", metavar="DIR", help="state directory (default as `tulda watch`)")
    g.set_defaults(fn=cmd_show_last_good)
    k = cs.add_parser("check", help="same as `tulda watch --check-config [PATH]`")
    k.add_argument("path", nargs="?", default="")
    k.add_argument("--config", metavar="PATH")
    k.set_defaults(fn=lambda a: check_config(Path(a.path).expanduser() if a.path
                                             else wc.config_path(a.config)))
