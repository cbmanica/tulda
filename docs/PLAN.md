# Render server plan

## Changes from previous version

- Tool-agnostic: OpenSCAD and Bambu Studio are now built-in adapters behind a generic adapter layer, not assumptions in the core.
- New "Adapter layer" section: contract, TOML config schema with worked example, built-in adapters, selection rules, chaining, errors, doctor.
- `kind` (scad|bambu) is replaced by `adapter` (id of the tool that produced the image) plus `stage` (source | built | sliced), plus a free `source_type` tag (file extension).
- CLI: `render scad` / `render bambu` become `render <file> [--adapter ID] [--stage S]`; added `render doctor` and `render adapters`.
- Sidecar JSON schema now records adapter id, tool version, command, source hash.
- UI filter chips are generated from the adapters/stages present in the data, not hardcoded.
- bambusa `make-3mf` is an optional "builder" adapter, not a special case.
- Project name `scadview` is dropped; the project is named `tulda` (a bambusa species).
- Phases reordered: core + adapter layer + OpenSCAD adapter first.
- Open questions updated; Prior art kept with notes on what genericization changes.

## Goal

A LAN-only web server to view renders of CAD/print files being worked on, from any tool (OpenSCAD, CadQuery, build123d, slicers, mesh tools). The user SSHes into the Mac (host `myhost.local`, LAN IP `192.168.1.50`), generates renders from the CLI, and views them in a browser on a remote device. The first-party use case is OpenSCAD plus Bambu Studio, shipped as built-in adapters.

## Findings

- The author's project is a flat repo of .scad/.stl/.3mf files, `prints.toml`, `overlays/`, and a git-ignored `build/` with 14 .3mf files.
- There are no PNG renders, Makefile, render script or server.
- The fswatch scad-to-3mf watcher was planned (`.handoff/20261115T200621Z`, `tools/watch-scad.sh`) but never built.
- `fswatch` and `openscad` are in `/opt/homebrew/bin`.
- bambusa (github.com/cbmanica/bambusa) is a separate, stdlib-only Python project run via uv. It provides `make-3mf` (scad to 3mf with profiles, TOML overlays, manifests), `make profile`, and `diff-profiles`. It has no PNG/thumbnail/serving code, and its README says it makes no network calls.

## Recommendation

Make this a separate project, NOT a bambusa feature. Genericization strengthens this: the viewer is now a tool-agnostic render gallery with bambusa as one optional builder plugin, so a dependency in either direction would be backwards.

- bambusa has one job (scad to Studio-ready 3mf) and a deliberate no-network stance.
- The viewer only needs a directory of images and should work for any folder and any toolchain.
- A daemon has a different lifecycle than a published OSS tool.
- Both can stay stdlib-only, so there is no reason to merge.

Integration stays loose: bambusa is invoked as an external command by a builder adapter (a config entry, no import). bambusa never imports the viewer.

Location: new repo, `tulda` (decided; a bambusa species), or a `tools/` directory if no new repo.

## Architecture

- Single-file-core Python stdlib `ThreadingHTTPServer` (uv run or system python, no pip). Core and built-in adapters stay stdlib-only; third-party adapters may bring their own deps in their own environment.
- Serves root `~/dev/my-project/renders/` read-only (GET/HEAD only).
- Layout: `renders/<project>/<UTC-stamp>-<adapter>-<name>.png`, with a sidecar `<same-name>.json`.
- Index generated per request by scanning: project list, newest-first thumbnail grid per project, filter chips, lightbox, and tap two images for side-by-side compare (plain JS/CSS).
- Filter chips are built from the `adapter` and `stage` values found in sidecars (images without a sidecar get chip "unknown"). Nothing is hardcoded to a tool.
- Live refresh: the page polls `/api/latest` (max mtime) every 2s and reloads on change. Use SSE only if that is laggy.

### Naming concepts

- `adapter`: id of the tool pipeline that produced the image (`openscad`, `bambu-plate`, `trimesh`). Goes in the filename and chips. Answers "which tool made this".
- `stage`: where the image sits in the pipeline: `source` (rendered from source/model geometry), `built` (rendered from a built artifact such as a 3mf), `sliced` (slicer preview or plate thumbnail). Answers "what am I looking at". Chips can filter on either.
- `source_type`: free tag, the input extension (`scad`, `py`, `stl`, `3mf`). Informational only.
- `kind` is retired because it conflated tool and stage.

## Adapter layer

### Shape and justification

Chosen: declarative TOML command-template adapters as the primary mechanism, with optional Python adapters (a single `.py` file path or an entry point) as an escape hatch.

- Most tools (OpenSCAD, f3d, Blender `-b -P`, `cadquery` scripts, PrusaSlicer/Orca CLI) are "run a command, get an image or mesh", which a command template covers with zero code and no plugin API to maintain.
- Some steps are logic, not commands (unzip a PNG from a 3mf, pick the largest `plate_N.png`, post-process). Those need Python. Allowing a Python adapter keeps the config path simple and the core small.
- Entry points / a plugin framework are heavier than needed for a LAN tool used by one person. Python adapters load from a path in config; entry-point (`tulda.adapters` group) loading is supported but deferred (see open questions).
- Core is stdlib only: `tomllib` (Python >=3.11), `subprocess`, `zipfile`, `importlib`, `shutil.which`.

### Adapter contract

An adapter is a named step: `inputs (file, options) -> output file + metadata`.

Fields (TOML) or attributes (Python):

- `id`: unique name.
- `accepts`: list of input extensions, e.g. `[".scad"]`. Matching is case-insensitive.
- `produces`: `"png"` (terminal, goes to gallery), or a mesh/other extension (`"stl"`, `"3mf"`) for intermediates.
- `stage`: `source` | `built` | `sliced` (only meaningful when `produces = "png"`).
- `requires`: list of binaries (resolved with `shutil.which`) or `{ python = "module" }` entries. Used by doctor and by selection.
- `version_cmd`: command whose first output line is recorded as `tool_version`.
- `cmd`: argv list with placeholders (below). Never run through a shell; each element is one argument.
- `timeout`: seconds, default 120.
- `env`, `cwd`: optional.
- `options`: defaults for `{w}`, `{h}`, `{camera}`, etc., overridable on the CLI with `--opt key=value`.
- `priority`: integer for tie-breaking, default 0.
- `python`: instead of `cmd`, `"path/to/file.py:func"`; the function is `run(ctx) -> Result`, where `ctx` carries `in_path`, `out_path`, `options`, `workdir`, and `Result` carries `ok`, `message`, extra metadata. A Python adapter still declares `accepts`, `produces`, `requires`.

Placeholders in `cmd`: `{in}`, `{out}` (adapter-chosen temp path in a scratch dir; the core moves it into `renders/`), `{w}`, `{h}`, `{name}`, `{workdir}`, plus any key under `options`. Unknown placeholders are a config error caught by `render doctor`. `{out}` must appear in `cmd` unless the adapter declares `out_glob` (for tools that choose their own filename, e.g. Bambu `--export-png`); the core then picks the single match from `workdir`.

### Config schema and worked example

```toml
# tulda.toml (project-local) -- merged over built-ins

[defaults]
width = 1600
height = 1200

[adapter.openscad]
accepts  = [".scad"]
produces = "png"
stage    = "source"
requires = ["openscad"]
version_cmd = ["openscad", "--version"]
cmd = ["openscad", "--backend=manifold", "-o", "{out}",
       "--imgsize={w},{h}", "--autocenter", "--viewall", "{in}"]
timeout = 180

[adapter.cadquery-stl]            # step 1 of a chain: py -> stl
accepts  = [".py"]
produces = "stl"
requires = [{ python = "cadquery" }]
cmd = ["python", "tools/cq_export.py", "{in}", "{out}"]

[adapter.f3d]                      # step 2: mesh -> png (generic fallback)
accepts  = [".stl", ".3mf", ".obj", ".step"]
produces = "png"
stage    = "built"
requires = ["f3d"]
cmd = ["f3d", "--output={out}", "--resolution={w},{h}", "{in}"]

[adapter.bambusa-3mf]              # builder: scad -> 3mf
accepts  = [".scad"]
produces = "3mf"
requires = ["uv"]
cwd = "../bambusa"
cmd = ["uv", "run", "make-3mf", "{in}", "-o", "{out}"]   # flags unverified, check bambusa --help

[adapter.bambu-plate]              # Python: embedded plate PNG from a sliced 3mf
accepts  = [".3mf"]
produces = "png"
stage    = "sliced"
python   = "builtin:bambu_plate"   # unzip Metadata/plate_N.png via zipfile

[adapter.bambu-export-png]         # slicer render, verify flag first
accepts  = [".3mf"]
produces = "png"
stage    = "sliced"
requires = ["bambu-studio"]
cmd = ["bambu-studio", "--export-png", "{in}"]
out_glob = "*.png"
priority = -1

[chain.scad-sliced]                # named multi-step pipeline
steps = ["bambusa-3mf", "bambu-plate"]
```

### Built-in default adapters

Shipped inside the project (stdlib only, overridable by config of the same id):

| id | accepts | produces | stage | Notes |
|---|---|---|---|---|
| `openscad` | .scad | png | source | Command template as above. |
| `bambu-plate` | .3mf | png | sliced | Python. Extracts `Metadata/plate_N.png` with `zipfile`; one image per plate. No Bambu Studio needed. Fails clearly if no plate PNG or it is blank (see spike). |
| `bambu-export-png` | .3mf | png | sliced | Command. Needs Bambu Studio 2.1.0+, a display on macOS. Flag VERIFIED 2026-11-24 (Bambu Studio 02.09.01.70, headless, ~0.3 s): `BambuStudio --export-png 1 --outputdir <existing dir> <file.3mf>` writes `plate_1_0.png` (512x512 RGBA) plus `result.json`; a missing outputdir exits 243. Camera and all-plates variants untested. Now the default Bambu adapter; `bambu-plate` is a low-priority fallback only (none of `build/*.3mf` embed a PNG). |
| `openscad-mesh` | .stl | png | built | Generic fallback: wraps the STL in a tiny generated `.scad` `import()` and renders with OpenSCAD. Chosen so the fallback needs no extra tool on this Mac. |

For a plain (unsliced) 3mf with no plate PNG, the fallback is the mesh route: an optional Python adapter extracts the model mesh to STL, then `openscad-mesh`. Only included if the spike shows it is cheap. Other tools (f3d, Blender, trimesh/matplotlib, CadQuery, build123d, PrusaSlicer/Orca/Cura CLIs) are NOT built in; they are documented config snippets in `examples/`.

### Discovery and selection rules

1. Config load order, later wins per adapter id: built-ins, `~/.config/tulda/config.toml`, `./tulda.toml` (found by walking up from the input file), then `--config FILE`.
2. Candidate adapters: those whose `accepts` contains the input extension and whose `requires` are all satisfied (`which`/import check, cached per run).
3. Terminal rule: `render <file>` wants a PNG. If a candidate produces `png`, pick the highest `priority`, then config order. If none do, build a chain by BFS over `produces -> accepts` (max depth 3, no repeated adapter) to the shortest path ending in png; ties broken by summed priority. Chains can also be named under `[chain.*]` and selected explicitly.
4. Overrides: `--adapter ID` forces one adapter (or `--adapter a,b` an explicit chain); `--stage S` restricts to adapters of that stage; `--all` renders with every satisfiable terminal adapter (e.g. both source and sliced views).
5. If nothing matches: exit non-zero and print which extensions are supported and which adapters were skipped for missing binaries.

### Chaining

Each step writes into a per-run scratch dir; step N's `{out}` becomes step N+1's `{in}`. Only the final PNG(s) are moved into `renders/`; intermediates are kept in `build/` only with `--keep`. The sidecar records every step. Builder steps (like bambusa) are just non-terminal adapters in the chain, so "scad -> 3mf -> plate PNG" needs no special code.

### Errors, timeouts, safety

- Each step runs with `subprocess.run(argv, timeout=..., capture_output=True)`, no shell. Timeout kills the process group; the step is reported as `timeout`.
- Non-zero exit, missing output, empty or non-PNG output (checked by magic bytes) is a failure. stderr tail (last 20 lines) is printed and stored in the sidecar of a `.fail.json` next to nothing else in `renders/` (no partial PNG is published).
- Writes are atomic: temp file in the same directory, then `os.replace`.
- Config command templates are executed as the local user, so configs are trusted like a Makefile; the server never reads or runs adapters, only the CLI does. The server only serves files already in `renders/`.
- Exit codes: 0 ok, 2 no adapter, 3 missing requirement, 4 step failed, 5 timeout.

### Doctor / check

- `render doctor` lists every adapter with: satisfied or missing requirements, resolved binary path, `version_cmd` output, and config errors (unknown placeholders, missing `{out}`, `accepts` collisions). Exit non-zero if any adapter you have configured as `enabled` is broken.
- `render adapters` lists adapters and the extensions each handles.
- `render doctor --smoke` renders a bundled tiny fixture per available adapter, to catch display/OpenGL problems (Bambu export on macOS).

## Producing renders (CLI the viewer ships)

- `render <file> [--adapter ID[,ID]] [--stage S] [--all] [--opt k=v] [--project NAME] [--config FILE]`. Project defaults to the input's parent directory name; auto-detect uses the extension.
- Example default for `foo.scad`: runs the `openscad` adapter, writes `renders/<project>/<stamp>-openscad-foo.png` and sidecar, and prints `http://myhost.local:8765/<project>/`.
- Example `render build/foo.3mf`: `bambu-plate` (embedded plate PNG).
- Example `render foo.scad --adapter bambusa-3mf,bambu-plate`: builder chain.
- Hard part: Bambu plate PNG reliability. Needs a spike (sliced via Studio vs headless; one source claimed `plate_1.png` is blank if sliced headlessly; unverified).
- Later, `render watch` reuses the fswatch idea; it just re-runs `render <file>` on change.

## Request groups and views (added 2026-11-24)

- A render request (one `tulda render a b c` invocation) is a group: id `<UTC stamp>-<slug>`, optional title (`--title`/`--group`), sources, command. Stored in every sidecar (`group_id`, `group_title`, `group_sources`, `request_cmd`, `group_created_utc`, `seq`). The on-disk layout stays `<project>/...` (a request can span projects); grouping happens at read time, so old sidecars work and appear as one `legacy-YYYYMMDD` group per UTC day.
- Landing page `/` lists groups newest first across all projects; `/<project>/` filters the same view; `/_g/<id>/` shows one group. Group ids are validated against `[A-Za-z0-9._-]{1,100}` and only matched against scanned data, never mapped to paths.
- Views are per-adapter named option sets (`views.<name>.options`) plus `default_views`; the view name goes in the filename and sidecar (`view`). Bambu Studio has no documented bottom view; `--camera-view 5` is an empirically verified undocumented bottom (4 top, 6 front, 7 rear) on 02.09.01.70. Fallback if that disappears: extract the 3mf meshes (zipfile/xml, apply build transforms) to STL and render with OpenSCAD `--camera=...,180,0,0,0`. Not implemented.

## Sidecar metadata schema

`<name>.json`, written atomically next to the PNG:

```json
{
  "schema": 1,
  "adapter": "openscad",
  "stage": "source",
  "source_type": "scad",
  "source": "foo.scad",
  "source_sha256": "...",
  "git_sha": "74fe7c9",
  "git_dirty": true,
  "steps": [
    {"adapter": "openscad", "cmd": ["openscad", "..."],
     "tool_version": "OpenSCAD 20xx.xx", "duration_s": 3.2, "exit": 0}
  ],
  "options": {"w": 1600, "h": 1200},
  "created_utc": "2026-11-24T12:00:00Z"
}
```

`adapter` and `stage` are those of the terminal step; `steps` lists the whole chain, including intermediates and their input hashes. Unknown fields are ignored by the UI, so adapters may add their own (e.g. `plate: 2`). Compare view can warn when two images have different `source_sha256` or `tool_version`.

## What stays specific on purpose

- Bambu plate-thumbnail extraction (`Metadata/plate_N.png`) is inherently slicer-specific file-format knowledge. It lives as a built-in Python adapter, not core. Other slicers (Orca shares the layout, PrusaSlicer/Cura embed thumbnails in G-code or 3mf differently) would be separate adapters.
- The `stage` vocabulary (source/built/sliced) is fixed in the core so chips and compare stay consistent. Adapters pick one; they cannot invent stages.
- PNG is the only gallery format (server allowlist). Adapters must produce PNG at the terminal step.
- Python >=3.11 and stdlib-only for core and built-ins.
- The server, path safety and network rules are tool-independent and unchanged.

## Security / network

- Bind to the en0 address, not 0.0.0.0; fall back to 127.0.0.1. Re-resolve at start, and restart (launchd) if the DHCP address changes.
- Read-only. Resolve every path and require it stays under root; reject dotfiles and symlink escapes; allowlist extensions (png, jpg, svg, json, stl).
- Check the Host header against DNS rebinding.
- Multiple interfaces: `--host` is repeatable and `--tailscale` adds the tailnet IPv4 (one socket per address, same process; failed binds retried; auto-detected addresses watched, exit 75 on change). Still never 0.0.0.0. Security: Tailscale exposure reaches the user's entire tailnet, not only the home LAN; no auth, read-only still applies. Host allow-list: bound IPs, Bonjour name, MagicDNS name, `--allow-host`.
- Auth: none by default, or an optional `?t=<token>` stored in a cookie.
- Bonjour: macOS already advertises `myhost.local`. May need a macOS firewall allow for python.
- Adapter execution is CLI-only and never reachable over HTTP.

## Running

- Manual `render serve` while iterating.
- launchd LaunchAgent `com.tulda.server` via `tulda service install|start|stop|restart|status|logs|uninstall|print-plist` (implemented; see README). Venv python launch, KeepAlive on non-zero exit, exit 75 on LAN IP change, logs in `~/Library/Logs/tulda/`.
- Firewall: the macOS application firewall is disabled on this machine, so no allow rule was needed. If enabled, allow the venv Python. Local Network privacy permission is mainly for outbound LAN access; unverified for a launchd-run inbound server, so check it if remote devices cannot connect.
- Default port 8765.

## Optional later

A three.js STL/3MF viewer needs vendored JS (not stdlib-only; CDN unless vendored). Separate phase.

## Phases

1. Server with path safety, dir-scan index, LAN binding (images from any source; sidecar-driven chips).
2. Adapter core: config loading, selection, step runner, timeout/error handling, sidecar writer, `render <file>`, `render adapters`, `render doctor`. Built-in `openscad` adapter first, with URL printing.
3. Polling live refresh, compare, metadata display.
4. Spike then built-in `bambu-plate` (zipfile) and `bambu-export-png` (verify `--help` first); `openscad-mesh` fallback.
5. Chaining: builder adapters (bambusa `make-3mf` as config entry), named chains, `--all`.
6. launchd agent (DONE, see Running) and token; `examples/` configs for f3d, Blender, CadQuery, trimesh, PrusaSlicer/Orca.
7. Optional STL viewer, watcher, entry-point third-party adapters.

## Open questions for the user

Project name is decided: `tulda`. The questions below are still unanswered; implementation proceeds with the defaults in parentheses, which are **provisional defaults** and may change once answered.

1. Separate repo vs `tools/` in bambu (new repo).
2. Is "Bambu Studio renders" the sliced-3mf plate thumbnail or a plain model view (plate preview)? Decides which Bambu adapter is the default (`bambu-plate`).
3. Config location: user-level `~/.config/...`, project-local `tulda.toml`, or both (both, local wins).
4. Third-party adapters in v1: config-only command adapters yes; Python-file adapters yes; installable entry-point packages no (defer).
5. Token or open on LAN (no token).
6. Always-on launchd or manual (manual).
7. Git-ignore and prune renders, keep last N per project (git-ignore).
8. STL viewer in v1 (no).
9. Keep `openscad-mesh` as the generic mesh fallback, or require f3d for meshes (openscad-mesh, since already installed).

Removed as answered by the design: tool-specific CLI shape (now `render <file>`), and the `kind` naming question.

## Prior art

Researched 2026-11-24. "Last push" is `pushed_at` from the GitHub API (or PyPI release date) as fetched. "Unverified" means the page did not show it.

| Name | URL | What it does | License | Language / deps | Last activity | Fit |
|---|---|---|---|---|---|---|
| openscad-playground | https://github.com/openscad/openscad-playground | OpenSCAD compiled to WASM with Monaco editor, 3D viewer, live preview, Customizer; self-hostable; iframe embed | GPL-3.0 deployed (GPL v2+ source; per-component licenses, LICENSE.md, LICENSE.monaco, LICENSE.viewstl) | TS/React/WASM; Node >=22 to build | Push 2026-11-18 | Not useful for the gallery. Interactive editor, no PNG gallery, needs Node. Possible later STL-viewer source. |
| OpenSCAD Studio | https://github.com/zacharyfmarion/openscad-studio | Editor with live 3D preview, Customizer; Tauri desktop plus WASM web; localhost MCP server in desktop app | GPL-2.0 | TypeScript, Rust (Tauri), React, three.js | Push 2026-11-13 | Not useful. Editor-centric, no CLI/headless mode described. |
| SCADPipeline | https://github.com/numberformat/SCADPipeline | CI toolchain: builds STLs in Docker, makes PNG previews, publishes static viewer on GitHub Pages | MIT | Bash/PowerShell, Docker, GitHub Actions | Push 2026-02-22 | Borrow ideas only (PNG naming/size conventions). Needs Docker and CI, not LAN/live. |
| cadquery-server | https://github.com/roipoussiere/cadquery-server | Web server with file watcher and live reload that renders CadQuery models (three-cad-viewer); can build static site | MIT | Python, CadQuery, Poetry | ARCHIVED, last push 2023-07-21; page says moved to Codeberg | Borrow the watch-and-reload idea. Not for OpenSCAD, dead upstream. |
| stldev | https://github.com/snowbldr/stldev | Watches source, runs any build command, reloads tiled f3d preview windows (OpenSCAD listed as usable) | MIT | Go; needs f3d | Push 2026-04-21; 1 star | Not useful. Local windows, not a web server. Minor idea reuse. |
| bdbox | https://pypi.org/project/bdbox/ | build123d workshop: `bdbox model.py view` gives web UI with live reload, parameters panel; STEP/STL export | LGPL-3.0-or-later | Python >=3.11, build123d, OCP Viewer (other deps unverified) | Release 0.31.1, 2026-11-06 | Not useful (build123d only). Shows the live-view UX. |
| Online 3D Viewer | https://github.com/kovacsv/Online3DViewer | Browser viewer for many formats incl. STL (import/export) and 3MF (import only); npm package `online-3d-viewer`; has a self-hosted viewer manual section | MIT | JavaScript, three.js | Push 2026-06-24 | Best candidate for the optional STL/3MF viewer phase. Vendor the built JS to stay LAN-only. Self-hosting steps not verified here. |
| 3mfthumb | https://github.com/themanyone/3mfthumb | File-manager thumbnailer for 3MF/STL/GLB; uses embedded thumbnail else renders mesh | GPL-2.0 | C++; lib3mf, libzip, cairo, zlib | Push 2026-11-08 | Not useful (Linux/Windows file managers). Confirms the embedded-thumbnail approach. |
| bambuuzle | https://pypi.org/project/bambuuzle/ | Extract/re-insert G-code in Bambu `.gcode.3mf` files | MIT | Stdlib only; Python >=3.11 | Release 0.1.0, 2026-04-01 | Borrow ideas (stdlib zipfile handling of Bambu 3mf). Does not export thumbnails per the page. |
| Printernizer | https://github.com/schmacka/printernizer | Printer management app; extracts PNG thumbnails from G-code comments, serves them via API with cache headers | Not stated on page; GitHub reports NOASSERTION | Python backend (inferred), JS frontend, DB | Push 2026-10-30 | Not useful as-is (printer fleet app). G-code-comment thumbnails are a possible alternative source for sliced files. |
| Bambu Studio `--export-png` (Printago docs) | https://printago.io/slicer-cli/export-png | Per Printago docs (third party): Bambu Studio 2.1.0+, 512x512 orthographic PNG per plate, `--camera-view` 0-12; must be the only CLI action; needs a display on macOS | N/A (feature of Bambu Studio) | Bambu Studio CLI | n/a | Directly relevant to the `bambu-export-png` adapter spike. Official wiki (https://github.com/bambulab/BambuStudio/wiki/Command-Line-Usage) does NOT list it, so verify with the installed version's `--help`. No custom filenames (hence `out_glob`); silent failure if OpenGL init fails. |
| Embedded `Metadata/plate_N.png` | https://dev.to/joyfunc/whats-actually-inside-a-bambuorca-3mf and https://gist.github.com/steveneaston/24f20e67429b72cc3aef1565f908a1f4 | Plate thumbnails inside the 3mf zip, extractable with stdlib zipfile | n/a | Python stdlib | n/a | Confirms the `bambu-plate` adapter approach. One search source claimed `plate_1.png` is blank if sliced headlessly; unverified, check in the spike. |

Not found: any existing project that is a LAN image-gallery server for OpenSCAD/Bambu renders with live refresh, or a headless OpenSCAD+Bambu thumbnail renderer. Generic image-gallery servers with live reload were not evaluated in depth (searches surfaced none worth listing). Also not searched: generic "run a command template per file type" render-adapter frameworks; the adapter layer here is designed from scratch and may overlap with existing thumbnailer or build-runner tools.

### Takeaway

- No reusable project covers the plan: nothing serves a read-only, auto-refreshing gallery of renders from arbitrary CAD toolchains. The closest are code-CAD live viewers (cadquery-server archived, bdbox build123d-only) and editors (playground, Studio), which do not fit stdlib-only or PNG-gallery needs.
- Build-vs-reuse recommendation is unchanged: build the small stdlib server.
- Two inputs refine it. Bambu Studio's `--export-png` (2.1.0+, unofficial docs only) is now one built-in adapter alongside `bambu-plate`, needing a display on macOS. Online 3D Viewer (MIT) is the likely vendored library for the optional viewer phase.
- Effect of genericization on "borrow ideas":
  - cadquery-server, bdbox and stldev were dismissed as single-tool; under the new design they are reference points for what a CadQuery/build123d/f3d adapter would invoke (stldev's "run any build command" is the same idea as our `cmd` templates). Still not reusable code.
  - 3mfthumb's "embedded thumbnail else render the mesh" is effectively our selection rule (`bambu-plate` first, mesh fallback second), which strengthens that design choice.
  - bambuuzle and Printernizer remain Bambu-specific; they inform only the built-in Bambu adapters, which is where that knowledge now belongs.
  - Nothing found changes the decision to keep the core stdlib-only.
