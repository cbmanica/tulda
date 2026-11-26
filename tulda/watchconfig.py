"""`[watch]` configuration for `tulda watch --from-config` (the com.tulda.watch service).

Watch entries are read from exactly one file: the explicit `--config PATH`, else the user config
(`$XDG_CONFIG_HOME/tulda/config.toml`, default `~/.config/tulda/config.toml`). They are never
read from a `tulda.toml` found by walking up from a file, so a downloaded or cloned folder cannot
add a watch target. The file is only ever read here, never written.

    [watch]                       # defaults for every [[watch.dir]]
    interval = 5                  # seconds between polls
    settle = 5                    # minimum file age before import
    render = false                # renders become visible to everyone who can reach `serve`
    force = false                 # import files that detection would skip too
    backlog = "ignore"            # "ignore": files already there when a folder is first
                                  # watched are never imported; "import": they are
    # include = ["*.3mf", "*_stls.zip"]   # optional file-name globs on top of detection

    [[watch.dir]]
    path = "~/MakerWorld-inbox"
    destination = "~/makerworld-projects"
    # settle / interval / render / force / include / backlog override [watch] for this entry

Precedence: built-in defaults < [watch] < [[watch.dir]] entry < command-line flags.
Unknown keys are errors (typos are caught, not ignored).

backlog: the first time a folder can be listed, the watcher records its current files (name,
size, inode) in <state dir>/watch-baselines/ and, with "ignore" (the default), never imports
them; only files that appear afterwards are. Adding ~/Downloads therefore does not import years
of old downloads. A folder the watcher already watched before this existed keeps its old
behaviour (see projects.Baseline)."""

from __future__ import annotations

import errno
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

DEFAULTS = {"interval": 5.0, "settle": 5.0, "render": False, "force": False, "include": None,
            "backlog": "ignore"}
BACKLOG_VALUES = ("ignore", "import")
SETTING_KEYS = set(DEFAULTS)
WATCH_KEYS = SETTING_KEYS | {"dir"}
ENTRY_KEYS = SETTING_KEYS | {"path", "destination"}
LAST_GOOD_NAME = "watch-config.last-good.toml"
STATE_NAME = "watch.json"
# macOS privacy (TCC) protected folders under the home directory.
PROTECTED = ("Downloads", "Desktop", "Documents", "Library/Mobile Documents")

SAMPLE = """\
[watch]
interval = 5
settle = 5
render = false

[[watch.dir]]
path = "~/MakerWorld-inbox"
destination = "~/makerworld-projects"
"""


class WatchConfigError(ConfigError):
    code = 2


@dataclass
class WatchEntry:
    path: Path
    destination: Path
    interval: float = 5.0
    settle: float = 5.0
    render: bool = False
    force: bool = False
    include: list | None = None
    backlog: str = "ignore"

    @property
    def key(self) -> tuple:
        return (str(self.path), str(self.destination))


@dataclass
class WatchConfig:
    source: str
    dirs: list = field(default_factory=list)
    settings: dict = field(default_factory=dict)


# ---- locations ------------------------------------------------------------------------------

def user_config_path() -> Path:
    from .adapters.registry import user_config_path as ucp
    return ucp()


def config_path(explicit=None) -> Path:
    return Path(explicit).expanduser() if explicit else user_config_path()


def state_dir(explicit=None) -> Path:
    from .watchevents import default_state_dir
    return default_state_dir(explicit)


def last_good_path(sdir: Path) -> Path:
    return Path(sdir) / LAST_GOOD_NAME


def real_python(exe: str | None = None) -> str:
    """The resolved interpreter binary; macOS attributes privacy (TCC) access to this file,
    not to the venv symlink."""
    return os.path.realpath(exe or sys.executable)


# ---- parsing ---------------------------------------------------------------------------------

def _num(v, key: str, where: str, minimum: float, strict: bool) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise WatchConfigError(f"{where}: {key} must be a number, got {v!r}")
    if v < minimum or (strict and v == minimum):
        raise WatchConfigError(f"{where}: {key} must be {'>' if strict else '>='} {minimum:g}, got {v!r}")
    return float(v)


def _settings(d: dict, where: str) -> dict:
    out = {}
    for k, v in d.items():
        if k == "interval":
            out[k] = _num(v, k, where, 0.0, strict=True)
        elif k == "settle":
            out[k] = _num(v, k, where, 0.0, strict=False)
        elif k in ("render", "force"):
            if not isinstance(v, bool):
                raise WatchConfigError(f"{where}: {k} must be true or false, got {v!r}")
            out[k] = v
        elif k == "include":
            if isinstance(v, str):
                v = [v]
            if not isinstance(v, list) or not v or not all(isinstance(x, str) and x for x in v):
                raise WatchConfigError(f"{where}: include must be a non-empty list of file-name globs")
            if any("/" in x for x in v):
                raise WatchConfigError(f"{where}: include globs match file names only (no '/')")
            out[k] = list(v)
        elif k == "backlog":
            if v not in BACKLOG_VALUES:
                raise WatchConfigError(f"{where}: backlog must be \"ignore\" or \"import\", got {v!r}")
            out[k] = v
    return out


def _path(v, key: str, where: str) -> Path:
    if not isinstance(v, str) or not v.strip():
        raise WatchConfigError(f"{where}: {key} must be a non-empty string")
    p = Path(v).expanduser()
    if not p.is_absolute():
        raise WatchConfigError(f"{where}: {key} must be absolute or start with ~ (got {v!r})")
    return Path(os.path.normpath(p))


def _inside(a: Path, b: Path) -> bool:
    """a == b or a is inside b (after resolving symlinks where they exist)."""
    a, b = a.resolve(), b.resolve()
    return a == b or b in a.parents


def parse_watch(data: dict, source: str = "<config>") -> WatchConfig:
    """Validate the `watch` part of a parsed TOML document. Raises WatchConfigError."""
    w = data.get("watch")
    cfg = WatchConfig(source=source)
    if w is None:
        cfg.settings = dict(DEFAULTS)
        return cfg
    if not isinstance(w, dict):
        raise WatchConfigError("watch must be a table: use [watch] and [[watch.dir]]")
    unknown = sorted(set(w) - WATCH_KEYS)
    if unknown:
        raise WatchConfigError(f"[watch]: unknown key(s) {', '.join(unknown)} "
                               f"(allowed: {', '.join(sorted(WATCH_KEYS))})")
    base = {**DEFAULTS, **_settings({k: v for k, v in w.items() if k != "dir"}, "[watch]")}
    cfg.settings = base
    dirs = w.get("dir", [])
    if isinstance(dirs, dict):
        raise WatchConfigError("[watch.dir] must be an array of tables: write [[watch.dir]]")
    if not isinstance(dirs, list):
        raise WatchConfigError("watch.dir must be an array of tables ([[watch.dir]])")
    seen: dict = {}
    for i, e in enumerate(dirs, 1):
        where = f"[[watch.dir]] #{i}"
        if not isinstance(e, dict):
            raise WatchConfigError(f"{where}: must be a table")
        unknown = sorted(set(e) - ENTRY_KEYS)
        if unknown:
            raise WatchConfigError(f"{where}: unknown key(s) {', '.join(unknown)} "
                                   f"(allowed: {', '.join(sorted(ENTRY_KEYS))})")
        for req in ("path", "destination"):
            if req not in e:
                raise WatchConfigError(f"{where}: missing required key {req!r}")
        src, dest = _path(e["path"], "path", where), _path(e["destination"], "destination", where)
        if _inside(dest, src) or _inside(src, dest):
            raise WatchConfigError(f"{where}: destination and path must not be the same or "
                                   f"inside each other")
        rp = str(src.resolve())
        if rp in seen:
            raise WatchConfigError(f"{where}: path duplicates entry #{seen[rp]}")
        seen[rp] = i
        s = {**base, **_settings({k: v for k, v in e.items() if k in SETTING_KEYS}, where)}
        cfg.dirs.append(WatchEntry(path=src, destination=dest, **s))
    return cfg


def parse_bytes(raw: bytes, source: str) -> WatchConfig:
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as e:
        raise WatchConfigError(f"{source}: not UTF-8 ({e})") from e
    except tomllib.TOMLDecodeError as e:
        raise WatchConfigError(f"{source}: TOML syntax error: {e}") from e
    return parse_watch(data, source)


def read_config(path: Path) -> tuple[bytes, WatchConfig]:
    """-> (raw bytes, parsed config) from one consistent read. Raises WatchConfigError."""
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError as e:
        raise WatchConfigError(f"config file not found: {path}") from e
    except OSError as e:
        raise WatchConfigError(f"cannot read config {path}: {e.strerror or e}") from e
    return raw, parse_bytes(raw, str(path))


def load(path: Path) -> WatchConfig:
    return read_config(path)[1]


def apply_overrides(cfg: WatchConfig, overrides: dict) -> WatchConfig:
    """Command-line flags (None = not given) override every entry."""
    ov = {k: v for k, v in (overrides or {}).items() if v is not None}
    if not ov:
        return cfg
    for e in cfg.dirs:
        for k, v in ov.items():
            setattr(e, k, v)
    return cfg


# ---- environment checks ---------------------------------------------------------------------

def is_tcc_error(e: BaseException) -> bool:
    """macOS privacy (TCC) refusals surface as EPERM ('Operation not permitted'), unlike plain
    Unix permission problems (EACCES)."""
    return isinstance(e, OSError) and e.errno == errno.EPERM and sys.platform == "darwin"


def protected_location(p: Path, home: Path | None = None) -> str | None:
    home = (home or Path.home()).resolve()
    rp = Path(p).expanduser().resolve()
    for name in PROTECTED:
        root = home / name
        if rp == root or root in rp.parents:
            return f"~/{name}"
    if str(rp).startswith("/Volumes/"):
        return "/Volumes (removable or network volume)"
    return None


def tcc_help(python: str) -> str:
    return ("macOS privacy (TCC) is blocking access (Operation not permitted). Either watch an "
            "inbox folder outside ~/Downloads, ~/Desktop and ~/Documents, or grant Full Disk "
            "Access to the interpreter the service runs: System Settings > Privacy & Security > "
            f"Full Disk Access > + > Cmd-Shift-G > {python} ; then `tulda service restart watch`")


def probe_dir(p: Path) -> str | None:
    """None if p is a listable directory, else a reason."""
    try:
        if not p.exists():
            return "does not exist"
        if not p.is_dir():
            return "is not a directory"
        os.listdir(p)
    except OSError as e:
        if is_tcc_error(e):
            return "TCC"
        return f"cannot list ({e.strerror or e})"
    return None


def _writable(p: Path) -> bool:
    probe = p
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return probe.is_dir() and os.access(probe, os.W_OK | os.X_OK)


def doctor_report(cfg_path: Path, renders_root: Path, python: str,
                  server_tailscale: bool, explicit: bool = False) -> tuple[list[str], int]:
    """-> (lines, error count). Read-only: lists directories, runs read-only git queries."""
    from .projects import git_publish_warning
    lines = [f"watch config: {cfg_path}" + ("" if explicit else " (user config)")]
    if not cfg_path.exists():
        lines.append("    no config file; no directories watched (sample:)")
        lines += [("      " + ln) if ln else "" for ln in SAMPLE.splitlines()]
        return lines, 0
    try:
        cfg = load(cfg_path)
    except WatchConfigError as e:
        return lines + [f"[BROKEN] watch config: {e}"], 1
    if not cfg.dirs:
        lines.append("    no [[watch.dir]] entries; nothing watched")
        return lines, 0
    errors = 0
    lines.append(f"[ok] watch config valid: {len(cfg.dirs)} director{'y' if len(cfg.dirs) == 1 else 'ies'}")
    lines.append(f"    service interpreter (grant Full Disk Access to this if needed): {python}")
    for e in cfg.dirs:
        lines.append(f"  {e.path} -> {e.destination}  (interval {e.interval:g}s, settle "
                     f"{e.settle:g}s, render {str(e.render).lower()}, force {str(e.force).lower()}, "
                     f"backlog {e.backlog}"
                     + (f", include {','.join(e.include)}" if e.include else "") + ")")
        why = probe_dir(e.path)
        if why == "TCC":
            errors += 1
            lines.append(f"    [BROKEN] cannot list {e.path}: " + tcc_help(python))
        elif why:
            errors += 1
            lines.append(f"    [BROKEN] watched path {why}")
        else:
            lines.append("    [ok] watched path listable from this process")
        prot = protected_location(e.path)
        if prot:
            lines.append(f"    note: {prot} is privacy-protected; this check ran with this "
                         f"terminal's access, the service needs its own (Full Disk Access for "
                         f"{python})")
        if not _writable(e.destination):
            errors += 1
            lines.append("    [BROKEN] destination is not writable")
        prot = protected_location(e.destination)
        if prot:
            lines.append(f"    note: destination is under {prot} (privacy-protected); prefer a "
                         f"folder outside it")
        try:
            if _inside(e.destination, renders_root):
                errors += 1
                lines.append(f"    [BROKEN] destination is inside the renders root "
                             f"({renders_root}); `tulda serve` would publish the files")
        except OSError:
            pass
        w = git_publish_warning(e.destination)
        if w:
            lines.append("    " + w)
        if e.render and server_tailscale:
            lines.append("    warning: render = true and the server service binds Tailscale: "
                         "renders of these models are visible to your whole tailnet")
    return lines, errors
