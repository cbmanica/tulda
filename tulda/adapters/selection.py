"""Adapter selection and chaining (BFS, max depth 3)."""

from __future__ import annotations

from pathlib import Path

from ..errors import MissingRequirement, NoAdapter
from .registry import Adapter, Registry

MAX_DEPTH = 3


def _order(reg: Registry) -> dict:
    return {aid: i for i, aid in enumerate(reg.adapters)}


def explicit_chain(reg: Registry, ext: str, spec: str, stage: str | None) -> list[Adapter]:
    ids: list[str] = []
    for part in [p.strip() for p in spec.split(",") if p.strip()]:
        ids.extend(reg.chains[part] if part in reg.chains and part not in reg.adapters else [part])
    if not ids:
        raise NoAdapter("empty --adapter")
    chain = []
    for i in ids:
        if i not in reg.adapters:
            raise NoAdapter(f"unknown adapter {i!r}; known: {', '.join(reg.adapters)}")
        chain.append(reg.adapters[i])
    cur = ext
    for a in chain:
        if cur not in a.accepts:
            raise NoAdapter(f"adapter {a.id!r} does not accept {cur!r} (accepts {a.accepts})")
        cur = "." + a.produces
    if chain[-1].produces != "png":
        raise NoAdapter(f"chain must end in png, ends in {chain[-1].produces!r}")
    if stage and chain[-1].stage != stage:
        raise NoAdapter(f"terminal adapter stage is {chain[-1].stage!r}, not {stage!r}")
    for a in chain:
        m = a.missing()
        if m:
            raise MissingRequirement(f"adapter {a.id!r} is missing: {', '.join(m)}")
    return chain


def _find_chains(ok: list[Adapter], ext: str, stage, order) -> list[list[Adapter]]:
    paths: list[list[Adapter]] = [[]]
    for _ in range(MAX_DEPTH):
        nxt = []
        for p in paths:
            cur = ("." + p[-1].produces) if p else ext
            for a in ok:
                if cur in a.accepts and a not in p:
                    nxt.append(p + [a])
        done = [p for p in nxt if p[-1].produces == "png" and (not stage or p[-1].stage == stage)]
        if done:
            done.sort(key=lambda p: (-sum(a.priority for a in p), [order[a.id] for a in p]))
            return done
        paths = [p for p in nxt if p[-1].produces != "png"]
    return []


def plan(reg: Registry, path: Path, adapter: str | None = None, stage: str | None = None,
         all_: bool = False) -> list[list[Adapter]]:
    ext = Path(path).suffix.lower()
    if adapter:
        return [explicit_chain(reg, ext, adapter, stage)]
    order = _order(reg)
    enabled = [a for a in reg.adapters.values() if a.enabled]
    ok = [a for a in enabled if not a.missing()]
    direct = sorted(
        [a for a in ok if ext in a.accepts and a.produces == "png" and (not stage or a.stage == stage)],
        key=lambda a: (-a.priority, order[a.id]))
    if direct:
        return [[a] for a in direct] if all_ else [[direct[0]]]
    chains = _find_chains(ok, ext, stage, order)
    if chains:
        return chains if all_ else [chains[0]]
    skipped = [a for a in enabled if a.missing() and (ext in a.accepts or a.produces != "png")]
    if skipped and any(ext in a.accepts for a in skipped):
        det = "; ".join(f"{a.id} needs {', '.join(a.missing())}" for a in skipped)
        raise MissingRequirement(f"no usable adapter for {ext!r}; skipped: {det}")
    supported = sorted({e for a in enabled for e in a.accepts})
    msg = f"no adapter for {ext or '(no extension)'!r}" + (f" with stage {stage!r}" if stage else "")
    msg += f"; supported extensions: {', '.join(supported)}"
    if skipped:
        msg += "; skipped (missing requirements): " + ", ".join(a.id for a in skipped)
    raise NoAdapter(msg)
