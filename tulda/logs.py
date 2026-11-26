"""Log helpers: size-capped rotation, tailing, and stdio redirection for the service."""

from __future__ import annotations

import os
import sys
from collections import deque
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024
KEEP = 2
LOG_NAMES = {"out": "server.out.log", "err": "server.err.log"}
WATCH_LOG_NAMES = {"out": "watch.log", "err": "watch.err.log"}


def default_log_dir() -> Path:
    return Path.home() / "Library" / "Logs" / "tulda"


def log_path(log_dir: Path, err: bool = False, names: dict | None = None) -> Path:
    return Path(log_dir) / (names or LOG_NAMES)["err" if err else "out"]


def rotate(path: Path, max_bytes: int = MAX_BYTES, keep: int = KEEP) -> bool:
    """If path is larger than max_bytes, shift path -> path.1 -> path.2 ... (keep
    old files in total) and drop the oldest. Returns True if rotated."""
    path = Path(path)
    try:
        if path.stat().st_size <= max_bytes:
            return False
    except OSError:
        return False
    for i in range(keep, 0, -1):
        src = path if i == 1 else path.with_name(f"{path.name}.{i - 1}")
        dst = path.with_name(f"{path.name}.{i}")
        try:
            os.replace(src, dst)
        except OSError:
            pass
    return True


def tail_lines(path: Path, n: int) -> list[str]:
    """Last n lines of a file (reads it in chunks from the end)."""
    if n <= 0:
        return []
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        data = b""
        while pos > 0 and data.count(b"\n") <= n:
            step = min(8192, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
    lines = data.decode("utf-8", "replace").splitlines()
    return list(deque(lines, maxlen=n))


def redirect_stdio(log_dir: Path, max_bytes: int = MAX_BYTES, keep: int = KEEP,
                   names: dict | None = None) -> None:
    """Rotate the service log files, then point fd 1 and 2 at fresh append-mode
    handles. launchd opened the files before we started; doing it ourselves means
    the rotated-away file is never the one we keep writing to."""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    for err in (False, True):
        p = log_path(log_dir, err, names)
        rotate(p, max_bytes, keep)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(fd, 2 if err else 1)
        os.close(fd)
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(line_buffering=True)
        except (AttributeError, ValueError):
            pass
