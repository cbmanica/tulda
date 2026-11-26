"""`tulda watch --from-config`: poll every configured [[watch.dir]] in one loop.

One process, no threads: each poll lists one directory non-recursively, so a slow import only
delays the other entries. The server never imports this module (see tests/test_invariants.py)."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from . import watchconfig as wc
from .projects import Watcher, _warn_dest


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def overrides_from_args(args) -> dict:
    return {"interval": getattr(args, "interval", None), "settle": getattr(args, "settle", None),
            "render": getattr(args, "render", None), "force": getattr(args, "force", None)}


def make_watcher(e: wc.WatchEntry, log) -> Watcher:
    return Watcher(e.path, e.destination, settle=e.settle, render=e.render, force=e.force,
                   include=e.include, log=log)


class WatchLoop:
    """Polls each entry when due. `tick()` is one pass; `run()` loops until stopped."""

    def __init__(self, cfg: wc.WatchConfig, log=print, clock=time.time):
        self.log, self.clock = log, clock
        self.entries: dict = {}
        self.apply(cfg)

    def apply(self, cfg: wc.WatchConfig) -> None:
        old = self.entries
        self.entries = {}
        for e in cfg.dirs:
            w = old[e.key][0] if e.key in old else make_watcher(e, self.log)
            w.settle, w.render, w.force = e.settle, e.render, e.force
            w.include = list(e.include) if e.include else None
            self.entries[e.key] = [w, e, old[e.key][2] if e.key in old else 0.0]

    def tick(self, should_stop=lambda: False, once: bool = False) -> dict:
        total = {"created": 0, "exists": 0, "skipped": 0, "errors": 0, "waiting": 0}
        now = self.clock()
        for item in self.entries.values():
            w, e, due = item
            if should_stop():
                break
            if not once and now < due:
                continue
            item[2] = now + e.interval
            for k, v in w.poll(once=once, should_stop=should_stop).items():
                total[k] += v
        return total

    def period(self) -> float:
        ivs = [e.interval for _, e, _ in self.entries.values()]
        return max(0.2, min(ivs)) if ivs else 5.0


def cmd_watch_config(args) -> int:
    path = wc.config_path(getattr(args, "config", None))
    log = lambda m: print(m, flush=True)  # noqa: E731
    cfg = wc.apply_overrides(wc.load(path), overrides_from_args(args))
    if not cfg.dirs:
        raise wc.WatchConfigError(f"{path}: no [[watch.dir]] entries; add one, e.g.:\n{wc.SAMPLE}")
    for e in cfg.dirs:
        _warn_dest(e.destination)
    loop = WatchLoop(cfg, log=log)
    if args.once:
        c = loop.tick(once=True)
        print("summary: " + ", ".join(f"{k} {v}" for k, v in c.items()), flush=True)
        return 1 if c["errors"] else 0
    for e in cfg.dirs:
        log(f"watching {e.path} every {e.interval:g}s -> {e.destination}")
    try:
        while True:
            loop.tick()
            time.sleep(loop.period())
    except KeyboardInterrupt:
        log("stopped")
    return 0
