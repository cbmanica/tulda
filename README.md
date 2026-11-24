# tulda

LAN-only gallery for renders of CAD/print files, plus a small tool-agnostic
adapter runner that produces those renders. Python >= 3.11, stdlib only (no
third-party runtime dependencies). Design: [docs/PLAN.md](docs/PLAN.md).

Status: phases 1-2 plus the adapter core. Built-in adapters: `openscad`
(.scad) and `bambu-export-png` (.3mf, Bambu Studio headless). Not yet: the
`bambu-plate` fallback, chaining examples, live refresh, compare view, STL
viewer (see the plan's phases).

## Install / run

    uv run tulda doctor              # or: python3 -m tulda doctor
    uv run tulda render part.scad    # writes a PNG + sidecar, prints the URL
    uv run tulda serve               # read-only server on the LAN IPv4, port 8765
    uv run tulda adapters
    python3 -m unittest discover -s tests -t .

Renders live under `$TULDA_RENDERS`, `--renders-root`, or `[defaults] renders_root`
in config, default `~/.local/share/tulda/renders`, laid out as
`<project>/<UTC-stamp>-<adapter>-<name>.png` with a `.json` sidecar (adapter,
stage, tool version, command, source sha256, git sha, timestamp). The project
defaults to the input's parent directory name. `render` prints
`http://<LocalHostName>.local:8765/<project>/`.

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
  never watched.
- Multiple addresses / Tailscale: one listening socket per address in one process.
  `tulda service install --host 192.168.1.50 --tailscale` (or `serve`) listens on
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
  replaces that and `--tailscale` adds the tailnet IPv4. There is no authentication:
  anyone on the LAN can view the renders, and with `--tailscale` so can every device
  on your whole tailnet (not just the home LAN). Still read-only.
- Host header must be one of: any bound address, 127.0.0.1, localhost, the
  Bonjour name, this machine's bare short names (`LocalHostName`, `gethostname()`, and the MagicDNS short name with `--tailscale`) and their `.local` forms, the full MagicDNS name (with `--tailscale`), or `--allow-host`, with a matching port (DNS-rebinding guard).
- Every path is percent-decoded once, rejected if any segment starts with `.`,
  resolved, and required to stay under the root; symlinks that escape are
  refused. Only png, jpg, svg, json, stl files are served.
- Config files execute commands as you; treat `tulda.toml` like a Makefile.

## License

Copyright 2026 Benson Manica

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
