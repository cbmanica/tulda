"""Types shared by Python adapters and the runner."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


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
