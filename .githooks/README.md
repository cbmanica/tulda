# Git hooks

Opt in once per clone (not done automatically; `--no-verify` bypasses these, and the
Claude Code hook blocks `--no-verify`):

    git config core.hooksPath .githooks

- `pre-commit`: privacy scan of staged added lines; refuses staged changes to LICENSE,
  NOTICE and `tulda/static/vendor` unless `TULDA_ALLOW_PROTECTED=1`.
- `commit-msg`: privacy scan of the message.
- `pre-push`: refuses non-fast-forward pushes, ref deletions and `backup*`/`pre-release*`
  refs; scans outgoing commits and messages.

Private values (your real host name, tailnet, user name) go in a denylist outside the
repo: `~/.config/tulda-guard/denylist.txt` (or `$TULDA_DENYLIST`), one regex per line.
Never commit it. Findings print file:line only, never the matched text.
