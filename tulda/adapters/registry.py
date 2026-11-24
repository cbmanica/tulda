"""Adapter model, TOML loading/merging, requirement checks, command templating."""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import ConfigError
from . import probes

STAGES = ("source", "built", "sliced", "mesh")
KINDS = ("image", "mesh", "turntable", "slice")
BUILTIN_TOML = Path(__file__).with_name("builtin.toml")
BASE_KEYS = {"in", "out", "name", "workdir", "w", "h"}
_TOKEN = re.compile(r"\{\{|\}\}|\{(\w+)\}")
VIEW_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_WHOLE = re.compile(r"^\{(\w+)\}$")


def template_names(s: str) -> list[str]:
    return [m.group(1) for m in _TOKEN.finditer(s) if m.group(1)]


def render_argv(cmd: list[str], values: dict) -> list[str]:
    """Expand placeholders. An element that is exactly "{key}" whose value is
    a list expands to zero or more arguments. No shell is ever involved."""
    out: list[str] = []
    for el in cmd:
        m = _WHOLE.match(el)
        if m:
            if m.group(1) not in values:
                raise ConfigError(f"unknown placeholder {{{m.group(1)}}} in {el!r}")
            v = values[m.group(1)]
            if isinstance(v, list):
                out.extend(str(x) for x in v)
                continue

        def sub(mm):
            if mm.group(0) == "{{":
                return "{"
            if mm.group(0) == "}}":
                return "}"
            k = mm.group(1)
            if k not in values:
                raise ConfigError(f"unknown placeholder {{{k}}} in {el!r}")
            v = values[k]
            if isinstance(v, list):
                raise ConfigError(f"list option {{{k}}} must be a whole argument, got {el!r}")
            return str(v)

        out.append(_TOKEN.sub(sub, el))
    return out


@dataclass
class Adapter:
    id: str
    accepts: list[str]
    produces: str
    stage: str | None = None
    requires: list = field(default_factory=list)
    version_cmd: list[str] | None = None
    cmd: list[str] | None = None
    python: str | None = None
    timeout: float = 120
    env: dict = field(default_factory=dict)
    cwd: str | None = None
    options: dict = field(default_factory=dict)
    priority: int = 0
    out_glob: str | None = None
    enabled: bool = True
    kind: str = "image"       # image: one PNG per view; mesh: .glb (interactive); turntable: PNG frames
    views: dict = field(default_factory=dict)
    default_views: list = field(default_factory=list)
    base_dir: Path = field(default_factory=Path.cwd)
    source: str = "builtin"

    @classmethod
    def from_dict(cls, id: str, d: dict, base_dir: Path, source: str) -> "Adapter":
        def err(m):
            return ConfigError(f"adapter {id!r} ({source}): {m}")

        if "accepts" not in d or "produces" not in d:
            raise err("'accepts' and 'produces' are required")
        accepts = [("." + a.lstrip(".")).lower() for a in d["accepts"]]
        produces = str(d["produces"]).lstrip(".").lower()
        stage = d.get("stage")
        kind = d.get("kind", "image")
        if kind not in KINDS:
            raise err(f"bad kind {kind!r} (one of {', '.join(KINDS)})")
        if kind == "mesh" and produces != "glb":
            raise err("kind 'mesh' adapters must produce glb")
        if kind == "turntable" and produces != "png":
            raise err("kind 'turntable' adapters must produce png (frame 0 is the poster)")
        if kind == "slice" and produces != "htm":
            raise err("kind 'slice' adapters must produce htm (a summary page)")
        if produces == "png" and stage not in STAGES:
            raise err(f"png adapters need stage in {STAGES}")
        if stage is not None and stage not in STAGES:
            raise err(f"bad stage {stage!r}")
        if bool(d.get("cmd")) == bool(d.get("python")):
            raise err("exactly one of 'cmd' or 'python' is required")
        for k in ("cmd", "version_cmd"):
            if k in d and not (isinstance(d[k], list) and all(isinstance(x, str) for x in d[k])):
                raise err(f"'{k}' must be a list of strings")
        views = d.get("views", {})
        if not isinstance(views, dict) or not all(isinstance(v, dict) for v in views.values()):
            raise err("'views' must be a table of tables")
        for vn, v in views.items():
            if not VIEW_NAME.match(vn):
                raise err(f"bad view name {vn!r} (lowercase letters, digits, - and _)")
            if not isinstance(v.get("options"), dict):
                raise err(f"view {vn!r} needs an 'options' table")
        dv = list(d.get("default_views", []))
        bad = [x for x in dv if x not in views]
        if bad:
            raise err(f"default_views not declared in views: {', '.join(bad)}")
        return cls(
            id=id, kind=kind, views=views, default_views=dv, accepts=accepts, produces=produces, stage=stage,
            requires=list(d.get("requires", [])), version_cmd=d.get("version_cmd"),
            cmd=d.get("cmd"), python=d.get("python"), timeout=float(d.get("timeout", 120)),
            env={str(k): str(v) for k, v in d.get("env", {}).items()}, cwd=d.get("cwd"),
            options=dict(d.get("options", {})), priority=int(d.get("priority", 0)),
            out_glob=d.get("out_glob"), enabled=bool(d.get("enabled", True)),
            base_dir=base_dir, source=source,
        )

    def missing(self) -> list[str]:
        miss = []
        for r in self.requires:
            if isinstance(r, str):
                if not shutil.which(r):
                    miss.append(r)
            elif isinstance(r, dict) and "python" in r:
                try:
                    found = importlib.util.find_spec(r["python"]) is not None
                except (ImportError, ValueError):
                    found = False
                if not found:
                    miss.append(f"python:{r['python']}")
            else:
                miss.append(f"<bad requirement {r!r}>")
        return miss

    def resolved_binaries(self) -> dict:
        return {r: shutil.which(r) for r in self.requires if isinstance(r, str)}

    def config_errors(self, defaults: dict) -> list[str]:
        errs = []
        if self.cmd:
            known = BASE_KEYS | set(self.options) | {"width", "height"}
            for el in self.cmd + (self.version_cmd or []):
                for n in template_names(el):
                    if n not in known:
                        errs.append(f"unknown placeholder {{{n}}}")
            joined = " ".join(self.cmd)
            if "{out}" not in joined and not self.out_glob:
                errs.append("cmd lacks {out} and no out_glob declared")
        if self.python and not (self.python.startswith("builtin:") or ":" in self.python):
            errs.append("python must be 'builtin:name' or 'path.py:func'")
        return errs


@dataclass
class Registry:
    adapters: dict[str, Adapter] = field(default_factory=dict)
    chains: dict[str, list[str]] = field(default_factory=dict)
    defaults: dict = field(default_factory=dict)
    files: list[str] = field(default_factory=list)

    def merge_file(self, path: Path, source: str | None = None) -> None:
        try:
            data = tomllib.loads(Path(path).read_text())
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise ConfigError(f"cannot load config {path}: {e}") from e
        src = source or str(path)
        self.files.append(src)
        self.defaults.update(data.get("defaults", {}))
        for aid, d in data.get("adapter", {}).items():
            self.adapters[aid] = Adapter.from_dict(aid, d, Path(path).resolve().parent, src)
        for cid, d in data.get("chain", {}).items():
            self.chains[cid] = list(d.get("steps", []))


def find_project_config(start: Path | None) -> Path | None:
    if start is None:
        return None
    d = start.resolve()
    d = d if d.is_dir() else d.parent
    for p in [d, *d.parents]:
        f = p / "tulda.toml"
        if f.is_file():
            return f
    return None


def user_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "tulda" / "config.toml"


def load_registry(input_path: Path | None = None, extra: Path | None = None,
                  user_config: Path | None = None) -> Registry:
    """built-ins, user config, project tulda.toml (walk up), then --config."""
    reg = Registry()
    reg.merge_file(BUILTIN_TOML, "builtin")
    uc = user_config if user_config is not None else user_config_path()
    if uc.is_file():
        reg.merge_file(uc)
    pc = find_project_config(input_path)
    if pc:
        reg.merge_file(pc)
    if extra:
        reg.merge_file(Path(extra))
    return reg


def option_values(reg: Registry, a: Adapter, cli_opts: dict) -> dict:
    vals = {"w": reg.defaults.get("width", 1600), "h": reg.defaults.get("height", 1200)}
    vals.update(a.options)
    vals.update(cli_opts)
    return {k: probes.resolve(v) for k, v in vals.items()}
