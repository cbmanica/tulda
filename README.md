# tulda

LAN-only gallery for renders of CAD/print files, plus a small tool-agnostic
adapter runner that produces those renders. Python >= 3.11, stdlib only (no
third-party runtime dependencies). Design: [docs/PLAN.md](docs/PLAN.md).

Status: phases 1-2 plus the adapter core, request groups and named views. Built-in adapters: `openscad`
(.scad) and `bambu-export-png` (.3mf, Bambu Studio headless). Not yet: the
`bambu-plate` fallback, chaining examples, live refresh, STL
viewer (see the plan's phases).

## Install / run

    uv run tulda doctor              # or: python3 -m tulda doctor
    uv run tulda render part.scad    # writes a PNG + sidecar, prints the URL
    uv run tulda serve               # read-only server on the LAN IPv4, port 8765
    uv run tulda adapters
    python3 -m unittest discover -s tests -t .

`tulda render a.scad b.scad c.3mf` accepts several files; everything one invocation
produces is ONE request ("group"): id `<UTC stamp>-<slug>` (slug from `--title`/`--group`,
else the first file's name), optional human title, source list and the command, all stored
in each sidecar (`group_id`, `group_title`, `group_sources`, `request_cmd`, `seq`, `view`).
Sidecars without a `group_id` (older renders) are shown as ungrouped, one group per UTC day (`legacy-YYYYMMDD`).

Renders live under `$TULDA_RENDERS`, `--renders-root`, or `[defaults] renders_root`
in config, default `~/.local/share/tulda/renders`, laid out as
`<project>/<UTC-stamp>-<adapter>[-<view>]-<name>.png` with a `.json` sidecar (adapter,
stage, tool version, command, source sha256, git sha, timestamp). The project
defaults to the input's parent directory name. `render` prints
`http://<LocalHostName>.local:8765/<project>/`.

Pages: `/` is "Recent requests" (groups newest first, across all projects, so a request
that spans `nfc_teardrop/` and `build/` stays together); `/<project>/` is the same view
limited to one project; `/_g/<group id>/` is one request (shows the full command).
Each group is a card with title, time, source files and thumbnails (with a view badge);
chips filter by adapter, stage and view; "Compare" lets you tap two images to see them
side by side. (A project directory named `_g` is not listed.)

Views: an adapter declares named camera presets (`[adapter.X.views]`, each an `options`
table) and `default_views`. `--view bottom` (repeatable) or `--views iso,bottom` picks
them; with neither, the adapter's defaults are used. Each view is its own PNG, labelled
in the sidecar and UI. Built-in views:

- `openscad` (default `iso`, `bottom`): `iso top bottom front back left right`, via
  `--camera=0,0,0,rx,ry,rz,0 --autocenter --viewall`.
- `bambu-export-png` (default `iso`, `bottom`): `--camera-view N`. `--help` documents
  0 iso, 1 top-front, 2 left, 3 right (10-12 iso variants) and NO bottom. Probing Bambu
  Studio 02.09.01.70 showed undocumented 4 = top, 5 = bottom, 6 = front, 7 = rear; 5
  renders the underside in real filament colours, so a bed-face inlay (the pizza on the
  NFC tag) is visible. If a future Studio drops those, point the `bottom` view at an
  OpenSCAD-based fallback; there is no 3mf-to-mesh converter yet.

`--views slice` (`.3mf` only) slices a COPY of the model with Bambu Studio (`slice-bambu`, ~2 s,
the source is never modified) and stores a summary page (`.htm`) plus the same data in the
sidecar `info`: print time, layers, filament g/m per filament, extrusion per `; FEATURE:` type,
and explicit yes/no for supports, brim, skirt and prime tower ("No supports needed" when none).
The group page shows it as a text card. It slices with the settings embedded in the 3mf.

Provenance: for every `.3mf` source, whatever views are rendered, the sidecar records the
file's self-reported `<metadata>` from its root model (`Title`, `Designer`, `License`,
`Copyright`/`CopyRight`, `Origin`, `DesignModelId`, `DesignProfileId`, `DesignRegion`,
`Application`) under `provenance`. Item cards and the group page show Licence, Designer and
Title; an empty licence shows as "unknown (not recorded in file)", which does not mean
unlicensed. A `DesignModelId` adds "Source: MakerWorld model" (`FMI_...` ids: "Parametric Model
Maker"). No MakerWorld URL is built or fetched; the metadata is self-reported and the model's
MakerWorld page is authoritative. Parsing is stdlib-only and refuses DTDs/entities and oversized
members. To print it without rendering:

    tulda info model.3mf            # Licence: <licence>, Designer: <designer>, Title: <title>, ...
    tulda info --json model.3mf

### Catalog, navigation and management

A SQLite catalog at `<renders root>/.tulda/catalog.db` (never served over HTTP) indexes the
renders so the pages stay fast with thousands of groups. PNGs and sidecars remain the source
of truth; the DB is rebuilt automatically if missing or corrupt, and also holds your stars,
tags, title edits, notes and trash flags. Design notes: [docs/PLAN.md](docs/PLAN.md).

Web (read-only): group cards fold/unfold (newest open, state remembered per browser, "Expand
all"/"Collapse all"); "Load more requests"; `/_d/2026-11/` and `/_d/2026-11-24/` (UTC dates,
picker under "Browse by date"); `/search?q=`; chips filter by adapter, stage, view, tag,
starred, "show trash". JSON: `/api/groups`, `/api/dates`, `/api/latest`. All changes happen in the CLI:

    tulda ls [--project P] [--since 7d|DATE] [--until DATE] [--tag T] [--starred] [--trashed]
    tulda group show|star|unstar|trash|restore <id or unique prefix>
    tulda group rename <id> "title"; tulda group tag|untag <id> tag...; tulda group note <id> "text"
    tulda group rm <id> [--yes]           # permanently deletes files + rows (asks first)
    tulda prune --older-than 30d --keep-last 20 [--project P] [--no-keep-starred]   # lists only
    tulda prune ... --yes                 # really deletes; starred groups are kept by default
    tulda trash empty [--yes]
    tulda db reindex|stats|vacuum|check   # reindex keeps all metadata

Tags: every group automatically gets one tag per distinct source file of its renders, the
file stem lower-cased (`nfc_holder_body_pizza.scad` -> `nfc_holder_body_pizza`, `x.3mf` -> `x`). These
"auto" tags are stored with `kind='auto'`, regenerated on every index and by `tulda db reindex`, shown
prominently on group headers and under each image, and clickable (`/?tag=<name>`; the tag chips show
group counts). User tags (`tulda group tag`) are kept separately and survive reindex. Auto tags cannot be
removed with `untag` (they follow the render sources; trash or rename the group instead). The project name
is deliberately not a tag (it is already a nav entry and filter). Tag filter, `/search?q=` and
`tulda ls --tag` all match both kinds. Pagination is exact: the server reports `has_more` and the page
only shows "Load more requests" when it is true.

`tulda serve --sync-interval SEC` (default 30) re-syncs the index with the disk so renders
copied in from elsewhere show up.

Useful flags: `--adapter ID` (or `a,b` chain or a named chain), `--stage
source|built|sliced`, `--all`, `--opt w=800`, `--project`, `--keep`, `--config`.
Exit codes: 0 ok, 2 no adapter, 3 missing requirement, 4 step failed, 5 timeout.

## Running as a service (launchd)

    uv run tulda service install              # default port 8765; writes the plist, starts it
    tulda service status | start | stop | restart | logs [-n N] [-f] [--err] | uninstall
    tulda service print-plist                 # show what install would write

`install` options: `--port`, `--host` (repeatable or comma-separated), `--tailscale`, `--renders-root`, `--at-login`. Re-running `install` replaces the loaded job cleanly. The
label is `com.tulda.server`; the plist goes to `~/Library/LaunchAgents/`.

- Launch line: the absolute path of the Python that ran `install` (the project's
  `.venv/bin/python`) with `-u -m tulda serve`, plus `WorkingDirectory` and
  `PYTHONPATH` pointing at the project, so cwd does not matter. `uv run` is
  deliberately not used at service time (it needs the project dir and uv on
  PATH). Run `install` via `.venv/bin/tulda` or `uv run tulda`; if you move the
  project or recreate the venv, run `install` again. PATH is
  `/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin`; Bambu Studio is
  referenced by absolute path in the adapter, so it needs nothing extra.
- Lifecycle: `KeepAlive {SuccessfulExit: false}` with `ThrottleInterval` 10 s, so a
  crash restarts and a clean stop does not. launchd starts a KeepAlive job as soon
  as it is bootstrapped, so `start` = enable + `launchctl bootstrap`, `stop` =
  `launchctl bootout`, `restart` = `kickstart -k`. Without `--at-login` the
  service is `launchctl disable`d after bootstrap, so it is not loaded at the next
  login; `start` runs it again. With `--at-login` it also starts at every login.
- Logs: `~/Library/Logs/tulda/server.out.log` (startup lines) and `server.err.log`
  (one line per request). At each server start, a log over 5 MB is rotated, keeping
  `.1` and `.2`; the server reopens the files itself so the rotated file is not
  written to. Output is unbuffered/line-buffered.
- DHCP: auto-detected addresses (the LAN IPv4 when no `--host`, the tailnet IPv4
  with `--tailscale`) are re-derived every 30 s (`--watch-interval`) and the server
  exits with status 75 if one differs from what was bound (including Tailscale
  appearing after start), so launchd restarts it. Explicit `--host` values are
  never watched, and loopback (127.0.0.1) is never watched either.
- Loopback: 127.0.0.1 is always bound as well, in addition to the LAN and tailnet
  addresses (or the explicit `--host` values), so `curl http://127.0.0.1:<port>/`
  works on the Mac itself. It is added automatically; you do not pass it.
- Multiple addresses / Tailscale: one listening socket per address in one process.
  `tulda service install --host 192.168.1.50 --tailscale` (or `serve`) listens on <!-- guard:ok -->
  the LAN IP and the tailnet IPv4 (`tailscale ip -4` from PATH or
  `/Applications/Tailscale.app/...`, else any interface address in 100.64.0.0/10).
  If Tailscale is not up, a warning is logged and the others are served; once it
  appears the watcher exits 75 and launchd restarts onto it. A bind that fails
  (address not yet assigned) is logged and retried every 15 s in-process while the
  other addresses keep serving; if nothing binds, the process exits 1 for launchd.
  Not 0.0.0.0: only the listed interfaces.
- macOS notes: the application firewall is currently disabled on this machine, so
  no allow-prompt is needed. If it is enabled, allow the venv Python. Recent macOS
  may also gate LAN access by the "Local Network" privacy permission; this applies
  to processes that connect to other LAN hosts, and a server that only accepts
  inbound connections is normally unaffected, but a launchd service has no
  terminal to attribute a prompt to. If other devices cannot connect, check
  System Settings > Privacy & Security > Local Network and the firewall.

## Adapter config

Config is merged in order: built-ins, `~/.config/tulda/config.toml`, the
nearest `tulda.toml` above the input file, then `--config FILE`. Later wins per
adapter id.

    [defaults]
    width = 1600
    height = 1200

    [adapter.f3d]
    accepts  = [".stl", ".3mf", ".obj"]
    produces = "png"            # or an intermediate extension, chained automatically (max 3 steps)
    stage    = "built"          # source | built | sliced
    requires = ["f3d"]          # binaries on PATH or absolute paths
    version_cmd = ["f3d", "--version"]
    cmd = ["f3d", "--output={out}", "--resolution={w},{h}", "{in}"]
    timeout = 120
    priority = 0

    [chain.my-pipeline]
    steps = ["builder-adapter", "f3d"]

Placeholders: `{in} {out} {name} {workdir} {w} {h}` plus any key in the
adapter's `options`. Adapters whose tool picks its own filename set
`out_glob`, matched inside `{workdir}`. `cmd` is an argv list, never run
through a shell. A python adapter (`python = "path/to/file.py:func"`) is
supported; `func(ctx) -> Result`.

## Security notes

- The server is read-only (GET/HEAD; everything else is 405), never runs
  adapters, and only serves the renders root.
- Binds to the default interface's LAN IPv4, else 127.0.0.1; `--host` (repeatable)
  replaces that and `--tailscale` adds the tailnet IPv4. Loopback 127.0.0.1 is always
  bound too. Never 0.0.0.0: only the listed addresses; `--host` rejects wildcard/empty values (`0.0.0.0`, `::`, `0`, blank) with an error. There is no authentication:
  anyone on the LAN can view the renders, and with `--tailscale` so can every device
  on your whole tailnet (not just the home LAN). Still read-only.
- Host header must be one of: any bound address, 127.0.0.1, localhost, the
  Bonjour name, this machine's bare short names (`LocalHostName`, `gethostname()`, and the MagicDNS short name with `--tailscale`) and their `.local` forms, the full MagicDNS name (with `--tailscale`), or `--allow-host`, with a matching port (DNS-rebinding guard).
- Every path is percent-decoded once, rejected if any segment starts with `.`,
  resolved, and required to stay under the root; symlinks that escape are
  refused. Only png, jpg, svg, json, stl files are served.
- Config files execute commands as you; treat `tulda.toml` like a Makefile.

## Bambu Studio and licensing

tulda is independent; it is not affiliated with or endorsed by Bambu Lab. It does not
bundle or redistribute Bambu Studio: install it yourself and accept its terms. tulda only runs
your local Studio CLI (`--export-png`) and reads your own .3mf files; it never touches Bambu's
cloud, networking plugin, login or printers. `--camera-view` values 4-7 are undocumented and
may change. Read [Bambu Lab's Terms of Use](https://bambulab.com/en-us/policies/terms)
before use (version reviewed: last updated 24 April 2024), and especially before sharing or
hosting tulda for others. Use tulda only with your own models and your own licensed install;
do not expose it publicly or run it as a service for others without Bambu Lab's written
consent. Details: [docs/BAMBU-TERMS.md](docs/BAMBU-TERMS.md). Not legal advice.

## License

Copyright 2026 Benson Manica

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
