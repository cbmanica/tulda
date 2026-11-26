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
  without parsing logs. It is never served over HTTP."""

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
        self.state = {"pid": os.getpid(), "started": now_iso(), "config": str(self.cfg_path),
                      "mode": None, "since": None, "reason": None, "last_poll": None,
                      "last_created": None, "last_error": None, "dirs": []}

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

    def start(self) -> None:
        """Strict load: raises WatchConfigError on a bad config."""
        _, cfg = wc.read_config(self.cfg_path)
        self.use(cfg)
        self.set_mode("config")
        if not cfg.dirs:
            self.log(f"no [[watch.dir]] entries in {self.cfg_path}; idle")

    # -- polling -------------------------------------------------------------------------------
    def tick(self, should_stop=lambda: False, once: bool = False) -> dict:
        counts, listed = self.loop.tick(should_stop, once)
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


def cmd_watch_config(args) -> int:
    if getattr(args, "log_dir", None):
        from .logs import WATCH_LOG_NAMES, redirect_stdio
        redirect_stdio(Path(args.log_dir).expanduser(), names=WATCH_LOG_NAMES)
    path = wc.config_path(getattr(args, "config", None))
    sdir = wc.state_dir(getattr(args, "state_dir", None))
    log = timestamped if not args.once else (lambda m: print(m, flush=True))
    d = WatchDaemon(path, sdir, overrides_from_args(args), log=log, persist=not args.once)
    if args.once:
        d.start()
        if not d.cfg.dirs:
            raise wc.WatchConfigError(f"{path}: no [[watch.dir]] entries; add one, e.g.:\n{wc.SAMPLE}")
        c = d.tick(once=True)
        print("summary: " + ", ".join(f"{k} {v}" for k, v in c.items()), flush=True)
        return 1 if c["errors"] else 0
    stop = Stopper(log)
    stop.install()
    log(f"tulda watch pid={os.getpid()} config={path} state={sdir}")
    try:
        d.start()
    except wc.WatchConfigError as e:
        # Exit 0: launchd's KeepAlive {SuccessfulExit: false} does not restart a clean exit.
        log(f"error: {e}; not starting (fix the config, then `tulda service start watch`)")
        d.set_mode("idle", str(e))
        d.write_state()
        return 0
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
