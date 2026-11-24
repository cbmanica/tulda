"""Capability probes for option values written as "@probe:<name>" in config."""

import functools
import shutil
import subprocess


@functools.lru_cache(maxsize=None)
def openscad_backend() -> list[str]:
    """['--backend=manifold'] if the installed openscad advertises --backend
    with Manifold support, else [] (older builds default sensibly)."""
    exe = shutil.which("openscad")
    if not exe:
        return []
    try:
        p = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    text = (p.stdout + p.stderr).lower()
    if "--backend" in text and "manifold" in text:
        return ["--backend=manifold"]
    return []


PROBES = {"openscad_backend": openscad_backend}


def resolve(value):
    if isinstance(value, str) and value.startswith("@probe:"):
        name = value[len("@probe:"):]
        fn = PROBES.get(name)
        if fn is None:
            from ..errors import ConfigError
            raise ConfigError(f"unknown probe {name!r}")
        return fn()
    return value
