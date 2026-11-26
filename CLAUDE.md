# tulda: rules for Claude Code

This is a public Apache-2.0 repo. Stdlib-only Python; no new runtime dependencies.

- Never write real hostnames, LAN/Tailscale IPs, tailnet names, usernames, home paths or
  emails into any file, test, commit message or branch name. Use placeholders
  (`host.example`, `192.0.2.1`, `/Users/you`). Deliberate exceptions carry a `guard:ok` marker.
- The server is read-only: GET/HEAD only, everything else 405. It must never bind
  `0.0.0.0` or `::`. Mutations happen only through the CLI, with ONE exception:
  `POST /api/render` (tulda/webrender.py, the Watcher tab's Render button) queues a
  `tulda render` subprocess for one model file of a project folder in a configured watch
  destination, chosen by opaque server-side id (no client paths, symlinks refused), after
  Host + Origin == Host origin + per-process token checks, refusing unapproved
  post-processing scripts. Do not add other write routes or widen this one without the
  user's explicit request; keep README "Security notes" and tests/test_invariants.py in sync.
- Commit by explicit path: `git commit -m "..." -- path1 path2`. Never `git add -A`,
  `git add .`, `git add -u` or `git commit -a`. Another agent may be editing the same tree.
- Do not push unless the user asks. Never force-push (including `--force-with-lease`),
  never push `backup*` or `pre-release*` refs, never use `--no-verify`.
- Do not edit `LICENSE`, `NOTICE` or `tulda/static/vendor/*`. A deliberate three.js
  upgrade is the user's call; bump `tulda/static/vendor/SHA256SUMS`
  (`python3 scripts/guard.py vendor-sums`) in the same commit.
- Do not run `launchctl` or touch LaunchAgents directly; use `tulda service`. Tests must
  keep mocking `run_launchctl`.
- Bambu content: short attributed excerpts only; no Studio binaries, profiles or assets;
  no hosting of third-party model renders. See `docs/BAMBU-TERMS.md`.
- Before finishing run `python3 -m unittest discover -s tests -t .`.

## Guard layers (all share `scripts/guard.py`)

- Claude Code hooks: `.claude/settings.json` (committed, active in Claude Code sessions here).
- Git hooks: opt in per clone with `git config core.hooksPath .githooks`.
- CI: `.github/workflows/ci.yml` (generic patterns only; the private denylist never runs there).
- Private values to catch (your real host name, tailnet, user name) go in a denylist
  outside the repo: `~/.config/tulda-guard/denylist.txt` or `$TULDA_DENYLIST`, one regex per
  line. Never commit it. Guard output shows file:line only, never the matched text.
- The Stop hook runs the test suite once when `tulda/`, `tests/` or `scripts/` have
  uncommitted changes (skip with `TULDA_GUARD_NO_STOP=1`).
