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

### Project folders for MakerWorld downloads

For `.3mf`/`.zip` files you have **already downloaded yourself** (browser or Bambu Studio).
tulda never downloads, never contacts MakerWorld and never builds a MakerWorld URL.

    tulda project create ~/Downloads/<model>.3mf --destination ~/makerworld-projects
    tulda project create <thing>_stls.zip -d ~/makerworld-projects [--force] [--render]
    tulda watch ~/MakerWorld-inbox --destination ~/makerworld-projects [--once] [--interval 5] [--settle 5]
    tulda watch --from-config [--config PATH] [--once]      # every [[watch.dir]] of the config

Each project is `<destination>/<name>/` with `source/<original file>` (a copy; the original is
never moved or deleted), `files/` (zip contents: model, image, PDF and text files only),
`tulda-project.json` (source file name, sha256, time, self-reported licence/designer/title/
DesignModelId per 3mf, skipped members) and `LICENCE-NOTE.txt`. The name comes from the 3mf
`Title` or the file stem: brackets, quotes, slashes and other punctuation become `-`; Unicode
letters (for example CJK titles) are kept; clashes get `-2`, `-3`. Importing the same file
(same sha256) again reports `exists` and creates nothing.

What counts as a MakerWorld download: tulda decides from what a file contains, not from its
name or its MakerWorld metadata (which is often empty in real downloads).

- A `.3mf` qualifies when it is a readable 3mf, with or without `DesignModelId`/`Designer`.
- A `.zip` qualifies when, after the safety checks below, its file listing has at least one model
  file (`.3mf`, `.stl`, `.step`, `.stp`, `.obj`) and every other member is a harmless companion:
  readme/licence text (`.txt`, `.md`), PDF, images (`.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`) or
  `.mtl`. Anything else in the zip (programs, `.app` bundles, `.dylib`, scripts, nested zips,
  unknown types) means it is skipped, so app downloads in the same folder are left alone.
- Bare `.stl`/`.step` files are never picked up by `tulda watch` (you probably export your own
  STLs into the same folder).

Skipped files are logged as `skip <file>: <reason>`, for example `skip App.zip: zip contains 2
file(s) that are neither models nor readme/image/PDF companions: App.app/Contents/MacOS/App, ...`.
`project create --force` (or `force = true` for a watched folder) imports them anyway. The 3mf
metadata (title, designer, licence, DesignModelId) is still recorded in `tulda-project.json` and
`LICENCE-NOTE.txt` and names the project when it has a title; otherwise the file stem is used
(`Some+Thing+-+v2.zip` becomes `Some-Thing-v2`). Zips are checked before anything is
written: absolute or `..` paths, symlinks and encrypted members are refused, as are more than
2000 members, more than 2 GiB uncompressed, or suspicious compression ratios. With `--force`,
nested zips are unpacked one level deep.

Post-processing scripts: a 3mf can carry `post_process`, shell commands that the Bambu Studio /
OrcaSlicer / PrusaSlicer GUI runs after exporting G-code. If any 3mf in an import (bare, in the
zip or in a nested zip) has a non-empty one in any `*.config` member (JSON, XML key/value or
`key = value` format), the import stops and nothing is created. `project create` exits 3 and
prints the script plus the command to approve it; the watcher logs `held <file>:
post-processing script found in <member>: <command>` once per file content and shows the full
script and the approval command on the activity page. Approve only after reading the script:

    tulda project create <file> -d <destination> --allow-post-process

`--force` does not imply it. An approved import still copies the source byte for byte and
records the script and the approval under `post_process` in `tulda-project.json`. tulda itself
never runs these scripts: the `slice` view blanks `post_process` in the temporary copy it
slices (the Bambu Studio CLI refuses such files anyway). `tulda info file.3mf` prints any
script it finds.

`tulda watch` polls (no fswatch), waits until a file's size and mtime are stable and it is at
least `--settle` seconds old, ignores `.crdownload`/`.part`/`.download`/`.tmp` files (and files
with a `NAME.part` sibling), logs `created`/`exists`/`skip`/`held`/`error` per file and keeps
going on errors. With `--from-config` it also appends each outcome to
`~/.local/state/tulda/watch-events.jsonl` (capped at 512 KiB, one older generation kept), which
`tulda serve` shows in its "Watcher" tab (`/_w/`; JSON at `/api/watch`): newest
first, with the watcher's running status and last poll. Folder and file names only; absolute
paths in error text are reduced to their last component. `--render` (off by default) also renders `.3mf`/`.scad`/`.stl` into the renders root; it is
off because rendering is slow and because the gallery shows renders to everyone who can reach
`tulda serve`.

The Watcher tab also lists every imported project folder in the `[[watch.dir]] destination`s of
the user config, with its model files (`.3mf`/`.stl`/`.scad` in `files/`, or the bare 3mf in
`source/`). A file already rendered (matched by sha256 against the catalog, so it does not
matter which project or group name it was rendered under) links to its gallery group (and
gets a "Render all views" button when some of the views below are missing); any other file
has a **Render** button. It queues one background render (one at a time, through
the same `tulda render` CLI path, views `iso,bottom,interactive`; `.stl` gets `interactive`
only; the group and the renders-root project are named after the project folder) and the
page shows queued / rendering / done / failed. Files whose 3mf carries a post-processing script
that was not approved, or projects containing a `tulda.toml`, show "held" and cannot be
rendered from the page.

Licences: MakerWorld's default Standard Digital File License forbids sharing, hosting or
distributing the files or derivatives. Keep the destination user-local. tulda warns (and changes
nothing) when the destination is inside a git work tree and not ignored; add the suggested
`.gitignore` line before committing. The licence metadata is self-reported; the model's
MakerWorld page is authoritative. Not legal advice.

### New original designs

    tulda project new "Cable Clip" [-d ~/designs] [--render]

creates `<destination>/Cable-Clip/` (same name sanitising and `-2`, `-3` collision rules as
`project create`) with:

- `cable_clip.scad`: a valid OpenSCAD starter (header comment for axes, print orientation and
  material/profile, `$fn`, a `part` selector, a `// ---- parameters` block, `module main()`
  with a placeholder cube, and the render call), so it renders as soon as it is created;
- `README.md`: title, licence and print-orientation TODOs, a file table, "Parameters and why"
  and notes;
- `tulda-project.json`: `origin = "own"`, empty `licence` and `designer` to fill in, no
  MakerWorld provenance.

The destination defaults to `default_destination` in the user config:

    [projects]
    default_destination = "~/designs"

`--render` renders the starter into the renders root (needs OpenSCAD).

### Catalog, navigation and management

A SQLite catalog at `<renders root>/.tulda/catalog.db` (never served over HTTP) indexes the
renders so the pages stay fast with thousands of groups. PNGs and sidecars remain the source
of truth; the DB is rebuilt automatically if missing or corrupt, and also holds your stars,
tags, title edits, notes and trash flags. Design notes: [docs/PLAN.md](docs/PLAN.md).

Web: two tabs at the top of every page, **Gallery** (everything below; the search box sits in
its header, since search is a filter of the gallery) and **Watcher** (above). Group cards fold/unfold (newest open, state remembered per browser, "Expand
all"/"Collapse all"); "Load more requests"; `/_d/2026-11/` and `/_d/2026-11-24/` (UTC dates,
picker under "Browse by date"); `/search?q=`; chips filter by adapter, stage, view, tag,
starred, "show trash". JSON: `/api/groups`, `/api/dates`, `/api/latest`. Apart from the
Watcher tab's Render buttons, all changes happen in the CLI:

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

## Watching an inbox as a service (com.tulda.watch)

A second, independent LaunchAgent runs `tulda watch --from-config`. It is not part of the
server: the server never imports the import code (its only write, the Render button, runs
`tulda render` in a subprocess) and keeps running if the watcher is stopped (and vice versa).

1. Make an inbox folder **outside** `~/Downloads`, `~/Desktop` and `~/Documents`, for example
   `~/MakerWorld-inbox`, and point your browser's "ask where to save" (or Bambu Studio's
   download folder) at it. Those three folders are protected by macOS privacy controls (TCC);
   a background service cannot list them without a grant, while a plain folder in your home
   directory needs none.
2. Add the watch section to `~/.config/tulda/config.toml` (or keep it in a separate file and
   pass `--config PATH` to `install`):

       [watch]                      # defaults for every [[watch.dir]]
       interval = 5                 # seconds between polls
       settle = 5                   # minimum file age before import
       render = false               # renders are visible to everyone who can reach `serve`
       # force = false              # also import files detection would skip
       # backlog = "ignore"         # files already in a folder when it is first watched are
                                    # never imported ("import" to take them too)
       # include = ["*.3mf", "*_stls.zip"]   # optional file-name globs

       [[watch.dir]]
       path = "~/MakerWorld-inbox"
       destination = "~/makerworld-projects"
       # interval / settle / render / force / include / backlog may be overridden per entry

   Paths must be absolute or start with `~`. Unknown keys, duplicate paths and a destination
   inside its watched path (or the reverse) are errors. Precedence: built-in defaults <
   `[watch]` < `[[watch.dir]]` < command-line flags. Watch entries are read only from this
   file (or `--config`), never from a `tulda.toml` found next to a model or in a repository.
3. Check and install:

       tulda watch --check-config           # validate without running (exit 2 if invalid)
       tulda doctor                         # listability, TCC, git, renders-root checks
       tulda service install watch          # refuses a config without [[watch.dir]]
       tulda service status watch           # pid, config mode, last poll/import/error
       tulda service logs watch [-f]        # ~/Library/Logs/tulda/watch.log

`tulda service install|status|start|stop|restart|uninstall [server|watch|all]`,
`logs [server|watch]` and `print-plist [server|watch]` all default to `server`.

Behaviour:
- Polling, settle and partial-download rules are those of `tulda watch` above. Each project is
  built in a hidden staging folder and renamed into place under a lock file
  (`<destination>/.tulda-project.lock`), so a manual `tulda project create` and the service
  never import the same file twice.
- SIGTERM (`tulda service stop watch`) finishes the current file and exits 0; launchd does not
  restart a clean exit. A crash is restarted after 10 s.
- Config changes are picked up automatically (the file's mtime is checked on every poll;
  no restart needed). After a config has been loaded and used for one successful poll, a copy
  is kept as the last-known-good config (`~/.local/state/tulda/watch-config.last-good.toml`,
  mode 600; your config file is never written). If the config later fails to parse or
  validate, or none of its folders can be listed, the watcher logs why and keeps running on
  the last-known-good copy; `service status watch` and `doctor` then show
  `running on fallback config since <time>: <reason>`. Fix the file and it switches back by
  itself. Without a last-known-good copy it stays idle (no restart loop) until the file
  changes. `tulda config show-last-good [--diff]` shows that copy.
- State for `status` lives in `~/.local/state/tulda/watch.json` (never served over HTTP).
- Backlog (`backlog = "ignore"`, the default): the first time a folder can be listed, the
  watcher records the files already in it (name, size and inode, not mtime) in
  `~/.local/state/tulda/watch-baselines/` and logs `backlog <folder>: ignoring N file(s) already
  there`. Those files are never imported; files that appear later are, including ones that
  arrive with an old mtime (Taildrop, unzip) or by renaming a partial download, and a file that
  replaces an old one under the same name. Adding `~/Downloads` thus does not import old
  downloads. Set `backlog = "import"` for a folder to import what is already there. A folder
  the service already watched before this feature existed keeps importing as before. Deleting
  a folder's baseline file makes the next run record a fresh one. Ad-hoc `tulda watch DIR`
  (without `--from-config`) keeps no baseline and handles every file.

Watching `~/Downloads` instead (opt-in): the service needs Full Disk Access for the real
interpreter binary (not the venv symlink). `tulda doctor` prints its path. System Settings >
Privacy & Security > Full Disk Access > `+` > Cmd-Shift-G > paste that path > enable, then
`tulda service restart watch`. Without the grant the log shows a `macOS privacy (TCC)` error
naming the same path. Caveats: the grant covers every script that interpreter runs (a
uv-managed Python is shared by all venvs using that version), and it is lost silently when
that Python is upgraded or reinstalled; re-check `tulda service status watch` after upgrades.
Running `tulda doctor` from Terminal tests Terminal's access, not the service's.

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

- The server is read-only (GET/HEAD; everything else is 405) with one exception,
  `POST /api/render` (the Watcher tab's Render button). It never runs adapters itself and
  only serves the renders root. The exception, precisely:
  - It renders one model file of a project folder (a directory holding `tulda-project.json`)
    directly inside a `[[watch.dir]] destination` of the user config, nothing else. No upload,
    no path: the form sends an opaque id (HMAC of destination, folder and relative path under
    a per-process secret) that must match the eligible list the server recomputes on every
    request. Eligible: `.3mf`/`.stl`/`.scad` named by the manifest or under `files/`; the
    project folder is a real directory (not a symlink), the file's resolved path equals its
    lexical path (no symlink anywhere below the project), it is a regular file, and no path
    part starts with `.`.
  - CSRF: the Host must pass the allow-list below, `Origin` must be present and equal to
    `http://<that Host>` (so one of the server's own host:port origins; `null`, missing or any
    other origin is refused), `Sec-Fetch-Site`, when sent, must be `same-origin`, and the form
    must carry the per-process random token embedded in the Watcher page. The page is sent
    with `Referrer-Policy: same-origin` (browsers send `Origin: null` under `no-referrer`),
    `frame-ancestors 'none'` and `X-Frame-Options: DENY`. Body: form-encoded, at most 4 KiB.
  - Refused with the reason shown: a post-processing script the manifest records as not
    approved, or that a fresh scan of the 3mf finds and the manifest did not approve (an
    unreadable 3mf counts as unscannable and is refused); a `tulda.toml` in the project folder
    (it would add adapters); a file already rendered in every view the button makes.
  - One background worker renders one file at a time (at most 8 waiting; a file already
    queued or rendering is not queued again) by running `python -P -m tulda render -- FILE`
    (argv list, cwd = renders root). The request thread only queues and redirects (303) back
    to the Watcher tab; it works without JavaScript.
  - Anyone who can reach the server (LAN, and the tailnet with `--tailscale`) can load the
    page and press Render. It can only render files that are already in your project folders.
- Binds to the default interface's LAN IPv4, else 127.0.0.1; `--host` (repeatable)
  replaces that and `--tailscale` adds the tailnet IPv4. Loopback 127.0.0.1 is always
  bound too. Never 0.0.0.0: only the listed addresses; `--host` rejects wildcard/empty values (`0.0.0.0`, `::`, `0`, blank) with an error. There is no authentication:
  anyone on the LAN can view the renders, and with `--tailscale` so can every device
  on your whole tailnet (not just the home LAN). Still read-only apart from the Render
  button.
- Host header must be one of: any bound address, 127.0.0.1, localhost, the
  Bonjour name, this machine's bare short names (`LocalHostName`, `gethostname()`, and the MagicDNS short name with `--tailscale`) and their `.local` forms, the full MagicDNS name (with `--tailscale`), or `--allow-host`, with a matching port (DNS-rebinding guard).
- Every path is percent-decoded once, rejected if any segment starts with `.`,
  resolved, and required to stay under the root; symlinks that escape are
  refused. Only png, jpg, svg, json, stl files are served.
- `/_w/` and `/api/watch` read the watcher's `watch.json` and `watch-events.jsonl` from the
  state dir and list the project folders of the watch destinations. File names there come
  from downloads and are untrusted: every value is HTML-escaped, the page has no script, and
  anyone who can reach the server can see the names of files dropped into watched folders and
  of imported projects.
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
