"""Run an adapter chain, publish the PNG atomically, write the sidecar."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .adapters.bambu import BUILTIN_PY
from .adapters.registry import Adapter, Registry, option_values, render_argv
from .errors import ConfigError, MissingRequirement, StepFailed, StepTimeout, TuldaError

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@dataclass
class Context:
    in_path: Path
    out_path: Path
    options: dict
    workdir: Path


@dataclass
class Result:
    ok: bool = True
    message: str = ""
    meta: dict = field(default_factory=dict)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def tail(text: str, n: int = 20) -> str:
    return "\n".join(text.strip().splitlines()[-n:])


_ver_cache: dict = {}


def tool_version(a: Adapter) -> str | None:
    if not a.version_cmd:
        return None
    key = tuple(a.version_cmd)
    if key not in _ver_cache:
        try:
            p = subprocess.run(a.version_cmd, capture_output=True, text=True, timeout=20)
            lines = (p.stdout + p.stderr).strip().splitlines()
            _ver_cache[key] = lines[0] if lines else None
        except (OSError, subprocess.SubprocessError):
            _ver_cache[key] = None
    return _ver_cache[key]


def git_info(path: Path) -> dict:
    d = str(path.parent)
    try:
        sha = subprocess.run(["git", "-C", d, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        if sha.returncode != 0:
            return {}
        st = subprocess.run(["git", "-C", d, "status", "--porcelain", "--", str(path)],
                            capture_output=True, text=True, timeout=10)
        return {"git_sha": sha.stdout.strip(), "git_dirty": bool(st.stdout.strip())}
    except (OSError, subprocess.SubprocessError):
        return {}


def _load_python(a: Adapter):
    spec = a.python
    if spec.startswith("builtin:"):
        fn = BUILTIN_PY.get(spec[len("builtin:"):])
        if fn is None:
            raise ConfigError(f"adapter {a.id!r}: builtin python adapter {spec!r} is not implemented")
        return fn
    file, _, func = spec.rpartition(":")
    path = (a.base_dir / file).resolve()
    s = importlib.util.spec_from_file_location(f"tulda_adapter_{a.id}", path)
    if s is None or s.loader is None:
        raise ConfigError(f"adapter {a.id!r}: cannot load {path}")
    mod = importlib.util.module_from_spec(s)
    s.loader.exec_module(mod)
    return getattr(mod, func or "run")


def run_step(reg: Registry, a: Adapter, in_path: Path, scratch: Path, idx: int,
             cli_opts: dict) -> tuple[Path, dict]:
    workdir = scratch / f"step{idx}-work"
    workdir.mkdir(parents=True, exist_ok=True)
    out = scratch / f"step{idx}-out.{a.produces}"
    opts = option_values(reg, a, cli_opts)
    info = {"adapter": a.id, "input_sha256": sha256(in_path), "tool_version": tool_version(a)}
    start = time.monotonic()
    if a.python:
        fn = _load_python(a)
        res = fn(Context(in_path, out, opts, workdir))
        info.update(cmd=None, exit=0 if res.ok else 1, **res.meta)
        if not res.ok:
            raise StepFailed(f"{a.id}: {res.message}")
    else:
        values = dict(opts, **{"in": str(in_path), "out": str(out), "name": in_path.stem,
                               "workdir": str(workdir)})
        argv = render_argv(a.cmd, values)
        info["cmd"] = argv
        cwd = (a.base_dir / a.cwd) if a.cwd else None
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, errors="replace", cwd=cwd,
                                    env={**os.environ, **a.env}, start_new_session=True)
        except FileNotFoundError as e:
            raise MissingRequirement(f"{a.id}: cannot execute {argv[0]!r}: {e}") from e
        try:
            stdout, stderr = proc.communicate(timeout=a.timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            info.update(exit=None, error="timeout")
            raise StepTimeout(f"{a.id}: timed out after {a.timeout:g}s") from None
        info["exit"] = proc.returncode
        if proc.returncode != 0:
            info["stderr_tail"] = tail(stderr or stdout)
            raise StepFailed(f"{a.id}: exit {proc.returncode}\n{info['stderr_tail']}")
        if a.out_glob:
            matches = sorted(workdir.glob(a.out_glob))
            if len(matches) != 1:
                raise StepFailed(f"{a.id}: out_glob {a.out_glob!r} matched {len(matches)} files in workdir")
            shutil.move(str(matches[0]), out)
    info["duration_s"] = round(time.monotonic() - start, 3)
    if not out.is_file() or out.stat().st_size == 0:
        raise StepFailed(f"{a.id}: no output produced")
    if a.produces == "png" and out.read_bytes()[:8] != PNG_MAGIC:
        raise StepFailed(f"{a.id}: output is not a PNG")
    return out, info


def _atomic_write(dest: Path, data: bytes | None = None, src: Path | None = None) -> None:
    tmp = dest.with_name(f".{dest.name}.tmp{os.getpid()}")
    if src is not None:
        shutil.copyfile(src, tmp)
    else:
        tmp.write_bytes(data)
    os.replace(tmp, dest)


def slugify(text: str, default: str = "render") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-")
    return s or default


def make_group(title: str | None, sources: list[Path], command: str,
               now: datetime | None = None) -> dict:
    """A render request: id = UTC stamp + short slug (from the title, else the first
    source's stem). Shared by every PNG the request produces."""
    now = now or datetime.now(timezone.utc)
    names = [p.name for p in sources]
    slug = slugify(title or (sources[0].stem if sources else ""))
    return {"id": f"{now.strftime('%Y%m%dT%H%M%SZ')}-{slug}", "title": title or None,
            "sources": names, "command": command,
            "created_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ")}


def run_chain(reg: Registry, chain: list[Adapter], src: Path, renders_root: Path,
              project: str, cli_opts: dict | None = None, keep: bool = False,
              group: dict | None = None, view: str | None = None,
              view_opts: dict | None = None, seq: int = 0) -> Path:
    cli_opts = {**(view_opts or {}), **(cli_opts or {})}
    src = src.resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    term = chain[-1]
    proj_dir = renders_root / project
    proj_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{stamp}-{term.id}" + (f"-{view}" if view else "")
    base = f"{prefix}-{src.stem}"
    n = 1
    while (proj_dir / f"{base}.png").exists() or (proj_dir / f"{base}.fail.json").exists():
        n += 1
        base = f"{prefix}-{src.stem}-{n}"
    scratch = Path(tempfile.mkdtemp(prefix="tulda-"))
    steps: list[dict] = []
    try:
        cur = src
        try:
            for i, a in enumerate(chain):
                cur, info = run_step(reg, a, cur, scratch, i, cli_opts)
                steps.append(info)
        except TuldaError as e:
            _atomic_write(proj_dir / f"{base}.fail.json", json.dumps(
                {"schema": 1, "error": str(e), "code": e.code, "source": str(src),
                 "steps": steps, "created_utc": _now(),
                 **({"group_id": group["id"], "view": view} if group else {})}, indent=2).encode())
            raise
        opts = option_values(reg, term, cli_opts)
        sidecar = {
            "schema": 1, "adapter": term.id, "stage": term.stage,
            "source_type": src.suffix.lstrip(".").lower(), "source": src.name,
            "source_sha256": steps[0]["input_sha256"], **git_info(src),
            "steps": steps,
            "options": {k: v for k, v in opts.items() if not isinstance(v, list)},
            "tulda_version": __version__, "created_utc": _now(),
        }
        if view:
            sidecar["view"] = view
        if group:
            sidecar.update(group_id=group["id"], group_title=group.get("title"),
                           group_sources=group.get("sources"), request_cmd=group.get("command"),
                           group_created_utc=group.get("created_utc"), seq=seq)
        png = proj_dir / f"{base}.png"
        _atomic_write(png, src=cur)
        _atomic_write(proj_dir / f"{base}.json", json.dumps(sidecar, indent=2).encode())
        if keep:
            keep_dir = renders_root.parent / "build" / base
            keep_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(scratch, keep_dir, dirs_exist_ok=True)
        return png
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
