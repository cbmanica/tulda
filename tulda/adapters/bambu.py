"""Bambu adapters.

`bambu-export-png` is a plain command adapter defined in builtin.toml
(verified working, see the comment there).

TODO(bambu-plate): NOT implemented. Python adapter that unzips
Metadata/plate_N.png from a .3mf with zipfile; low-priority fallback for
sliced 3mf files that embed plate PNGs (the files in bambu/build/ do not).
Register it in BUILTIN_PY as "bambu_plate" and add an [adapter.bambu-plate]
entry (python = "builtin:bambu_plate", priority = -1) to builtin.toml.
"""

from .meshes import MESH_PY
from .turntable import TURNTABLE_PY

BUILTIN_PY: dict = {**MESH_PY, **TURNTABLE_PY}  # name -> callable(ctx) -> Result
