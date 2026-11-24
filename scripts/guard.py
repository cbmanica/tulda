#!/usr/bin/env python3
"""tulda guardrails: one stdlib script for Claude Code hooks, git hooks and CI.

Modes (first argument):
  bash          Claude PreToolUse (Bash): push/commit/add/launchctl guards   (stdin JSON)
  edit          Claude PreToolUse (Edit|Write|MultiEdit): protected files, Bambu reminder
  post-edit     Claude PostToolUse (Edit|Write|MultiEdit): privacy scan of the edited file
  stop          Claude Stop: run the test suite once if code changed
  session-start Claude SessionStart: print state warnings (denylist missing, hooksPath unset)
  git-precommit git pre-commit: privacy scan of staged added lines + protected files
  git-commitmsg git commit-msg: privacy scan of the message file (arg: path)
  git-prepush   git pre-push: ref guard + privacy scan of outgoing commits (stdin refs)
  ci-generic    CI: generic privacy scan of the tracked tree, vendor sums, LICENSE/NOTICE
  scan [paths]  local: full scan (generic + denylist) of tracked files (or given paths)
  scan-history  local: scan every added line and message in `git log -p --all`
  vendor-sums   regenerate tulda/static/vendor/SHA256SUMS (after a deliberate upgrade)

PRIVACY: this file ships generic patterns only. Specific private values (real host
name, tailnet, user name, ...) belong in a user-local denylist OUTSIDE the repo:
$TULDA_DENYLIST, else ~/.config/tulda-guard/denylist.txt (one regex per line, '#'
comments). Reports show file:line and a pattern label, never the matched text.
A line containing the marker  guard:ok  is skipped.

Hook exit codes: 2 blocks (stderr goes to Claude / the human); a JSON
permissionDecision "ask" asks. Internal errors fail open (exit 0) for Claude
hooks; git hooks and CI fail closed on findings only.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parent.parent)
VENDOR_DIR = "tulda/static/vendor"
SUMS_FILE = f"{VENDOR_DIR}/SHA256SUMS"
PROTECTED = ("LICENSE", "NOTICE")  # repo-root files
ZERO_SHA = "0" * 40

# ---- privacy patterns (generic only; label -> regex) ------------------------------------
GENERIC: dict[str, str] = {
    "private-ipv4": r"(?<![\d.])(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}(?![\d])",
    "cgnat-ipv4": r"(?<![\d.])100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}(?![\d])",
    "home-path": r"/Users/(?!(?:you|user|username|name|example|Shared|me)\b)[A-Za-z0-9._-]+",
    "tailnet-name": r"\b[\w-]+\.ts\.net\b",
    "mdns-name": r"\b[A-Za-z0-9-]+\.local(?![\w-]|\.\w)",
    "email": r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b",
}
# Matches removed from the line before scanning (documentation-safe values).
ALLOW_TOKENS = [
    r"127\.0\.0\.1",
    r"(?:192\.0\.2|198\.51\.100|203\.0\.113)\.\d{1,3}",  # RFC 5737
    r"[\w.+-]+@(?:[\w-]+\.)*(?:example\.(?:com|org|net)|users\.noreply\.github\.com)",
    r"noreply@(?:anthropic\.com|github\.com)",
    r"[\w-]+\.example(?:\.(?:com|org|net))?",
    r"[\w-]+\.local\.json",
    r"100\.64\.0\.0/10",  # the CGNAT range itself, as documentation
    r"(?:myhost|other)[\w-]*(?:\.local|(?:\.[\w-]+)*\.ts\.net)",  # placeholder names used in docs/tests
    r"(?:[\w-]+\.)?tail1234\.ts\.net",
]
# Fixture-heavy paths where RFC1918/CGNAT *test data* is expected (labels exempted per glob).
EXEMPT_LABELS = {"tests/*": {"private-ipv4", "cgnat-ipv4"}}
SKIP_MARK = "guard:ok"
# Files never scanned for content (vendored / generated / binary-ish).
SKIP_GLOBS = [f"{VENDOR_DIR}/*", "uv.lock", "*.png", "*.jpg", "*.glb", "*.3mf", "*.stl", "*.gz", "*.zip"]

_COMPILED: dict[str, re.Pattern] = {}


def _rx(p: str, flags=re.I) -> re.Pattern:
    key = (p, flags)
    if key not in _COMPILED:
        _COMPILED[key] = re.compile(p, flags)  # type: ignore[index]
    return _COMPILED[key]  # type: ignore[index]


def denylist_path() -> Path:
    return Path(os.environ.get("TULDA_DENYLIST") or Path.home() / ".config/tulda-guard/denylist.txt")


def load_denylist() -> list[re.Pattern]:
    out = []
    try:
        lines = denylist_path().read_text(errors="replace").splitlines()
    except OSError:
        return out
    for i, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            out.append(re.compile(line, re.I))
        except re.error:
            print(f"guard: denylist line {i} is not a valid regex (ignored)", file=sys.stderr)
    return out


def scan_text(text: str, use_denylist: bool = True) -> list[tuple[int, str]]:
    """-> [(line_number, label)]; never returns matched text."""
    deny = load_denylist() if use_denylist else []
    allow = [_rx(a) for a in ALLOW_TOKENS]
    generic = [(k, _rx(v)) for k, v in GENERIC.items()]
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        if SKIP_MARK in line:
            continue
        for a in allow:
            line = a.sub(" ", line)
        for label, rx in generic:
            if rx.search(line):
                hits.append((n, label))
        for rx in deny:
            if rx.search(line):
                hits.append((n, "denylist"))
    return hits


def skip_path(path: str) -> bool:
    return any(fnmatch.fnmatch(path, g) for g in SKIP_GLOBS)


def parse_added(diff: str) -> list[tuple[str, int, str]]:
    """Added lines of a unified diff -> (path, new_line_number, text)."""
    out, path, ln = [], "", 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            path = raw[4:].removeprefix("b/") if raw[4:] != "/dev/null" else ""
        elif raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            ln = int(m.group(1)) if m else 0
        elif raw.startswith("+") and not raw.startswith("+++"):
            out.append((path, ln, raw[1:]))
            ln += 1
        elif raw.startswith("-") or raw.startswith("\\"):
            pass
        elif raw.startswith(" "):
            ln += 1
    return out


def exempt(path: str) -> set[str]:
    return set().union(*[v for g, v in EXEMPT_LABELS.items() if fnmatch.fnmatch(path, g)], set())


def scan_added(diff: str, use_denylist: bool = True) -> list[str]:
    findings = []
    for path, ln, text in parse_added(diff):
        if skip_path(path):
            continue
        for _, label in scan_text(text, use_denylist):
            if label in exempt(path):
                continue
            findings.append(f"{path}:{ln}: {label}")
    return findings


def scan_paths(paths: list[str], use_denylist: bool = True) -> list[str]:
    findings = []
    for p in paths:
        if skip_path(p):
            continue
        fp = ROOT / p
        try:
            if fp.stat().st_size > 2_000_000 or not fp.is_file():
                continue
            data = fp.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:4096]:
            continue
        for n, label in scan_text(data.decode("utf-8", "replace"), use_denylist):
            if label in exempt(p):
                continue
            findings.append(f"{p}:{n}: {label}")
        for n, label in scan_text(p, use_denylist):  # the path itself
            findings.append(f"{p}: path: {label}")
    return findings


def git(*args: str, check=False, stdin=None) -> str:
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, input=stdin)
    if check and r.returncode:
        raise RuntimeError(f"git {args[0]} failed")
    return r.stdout


def tracked_files() -> list[str]:
    return [p for p in git("ls-files", "-z").split("\0") if p]


def report(findings: list[str], what: str) -> str:
    shown = findings[:30]
    more = f"\n  ... and {len(findings) - 30} more" if len(findings) > 30 else ""
    return (f"guard: privacy scan hit in {what} (locations only, matched text is never printed; "
            "inspect those lines yourself):\n  " + "\n  ".join(shown) + more
            + "\n  Replace with placeholders (192.0.2.x, host.example, /Users/you), or mark a "
            "deliberate line with 'guard:ok'.")


# ---- shell command analysis --------------------------------------------------------------
def split_segments(cmd: str) -> list[list[str]]:
    """Tokenise a shell command into simple-command token lists. Best effort."""
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=";&|()\n")
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        toks = cmd.split()
    segs: list[list[str]] = [[]]
    for t in toks:
        if t and set(t) <= set(";&|()\n"):
            segs.append([])
        else:
            segs[-1].append(t)
    out = []
    for s in segs:
        if s:
            out.append(s)
            # recurse into sh -c "..." / eval "..."
            if s[0] in ("sh", "bash", "zsh") and "-c" in s and s.index("-c") + 1 < len(s):
                out.extend(split_segments(s[s.index("-c") + 1]))
            elif s[0] == "eval" and len(s) > 1:
                out.extend(split_segments(" ".join(s[1:])))
    return out


WRAPPERS = {"env", "command", "sudo", "exec", "time", "nice", "builtin"}
GIT_OPT_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
COMMIT_OPT_ARG = {"-m", "-F", "-C", "-c", "-t", "--message", "--file", "--author", "--date",
                  "--reuse-message", "--reedit-message", "--template", "--cleanup", "-S",
                  "--gpg-sign", "--fixup", "--squash", "--trailer", "-u"}


def git_invocation(seg: list[str]):
    """-> (subcommand, args) if seg is a git command, else None."""
    i = 0
    while i < len(seg) and (seg[i] in WRAPPERS or re.match(r"^[A-Za-z_]\w*=", seg[i]) or seg[i].startswith("-")):
        i += 1
    if i >= len(seg) or Path(seg[i]).name != "git":
        return None
    i += 1
    while i < len(seg) and seg[i].startswith("-"):
        i += 2 if seg[i] in GIT_OPT_ARG else 1
    if i >= len(seg):
        return None
    return seg[i], seg[i + 1:]


def check_git_push(args: list[str]) -> list[str]:
    msgs = []
    refs = []
    for a in args:
        if a.startswith("--"):
            if a in ("--force", "--force-with-lease", "--force-if-includes") or a.startswith("--force-with-lease="):
                msgs.append("force-push (including --force-with-lease) is blocked")
            elif a == "--no-verify":
                msgs.append("--no-verify is blocked (it skips the guard hooks)")
            elif a in ("--delete", "--mirror", "--prune"):
                msgs.append(f"git push {a} is blocked (deletes/overwrites remote refs)")
        elif a.startswith("-") and len(a) > 1:
            if "f" in a[1:]:
                msgs.append("force-push (-f) is blocked")
        else:
            refs.append(a)
    for r in refs[1:]:  # refs[0] is the remote
        if r.startswith("+"):
            msgs.append("force refspec (+ref) is blocked")
        if r.startswith(":"):
            msgs.append("deleting a remote ref is blocked")
    if any(re.search(r"(^|[/:])(backup|pre-release)", r) for r in refs):
        msgs.append("pushing backup/pre-release refs is blocked")
    return msgs


def check_git_commit(args: list[str]) -> tuple[list[str], list[str], list[str]]:
    """-> (blocking messages, commit message texts, explicit paths)."""
    msgs, texts, paths = [], [], []
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            paths += args[i:]
            break
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            if name == "--all":
                msgs.append("git commit -a/--all is blocked: commit by explicit path")
            elif name == "--no-verify":
                msgs.append("--no-verify is blocked (it skips the guard hooks)")
            elif name == "--message":
                if eq:
                    texts.append(val)
                elif i < len(args):
                    texts.append(args[i])
                    i += 1
            elif name in COMMIT_OPT_ARG and not eq:
                i += 1
        elif a.startswith("-") and len(a) > 1:
            letters = a[1:]
            if "a" in letters.split("m")[0]:
                msgs.append("git commit -a is blocked: commit by explicit path")
            if "n" in letters.split("m")[0]:
                msgs.append("git commit -n (--no-verify) is blocked")
            if "m" in letters:
                tail = letters.split("m", 1)[1]
                if tail:
                    texts.append(tail)
                elif i < len(args):
                    texts.append(args[i])
                    i += 1
            elif a in COMMIT_OPT_ARG:
                i += 1
        else:
            paths.append(a)
    return msgs, texts, paths


def check_git_add(args: list[str]) -> list[str]:
    for a in args:
        if a in ("-A", "--all", ".", "-u", "--update", ":/", "*", "--no-ignore-removal") or \
           (a.startswith("-") and not a.startswith("--") and set(a[1:]) & set("Au") and len(a) > 1):
            return ["broad `git add` is blocked: stage explicit paths"]
    if not [a for a in args if not a.startswith("-")] and not any(a in ("-p", "--patch", "-e", "-i") for a in args):
        return ["`git add` without paths is blocked: stage explicit paths"]
    return []


LAUNCHCTL_MUTATE = {"bootstrap", "bootout", "load", "unload", "enable", "disable", "kickstart",
                    "remove", "submit", "stop", "start", "kill", "attach", "reboot", "config"}
READONLY_CMDS = {"ls", "cat", "stat", "head", "tail", "grep", "rg", "file", "plutil", "wc", "diff", "less", "echo"}


def check_launchctl(seg: list[str]) -> bool:
    if not seg:
        return False
    if seg[0] == "tulda":
        return False
    words = [Path(t).name for t in seg]
    if "launchctl" in words:
        rest = seg[words.index("launchctl") + 1:]
        return any(t in LAUNCHCTL_MUTATE for t in rest[:2])
    if any("Library/LaunchAgents" in t or "Library/LaunchDaemons" in t for t in seg) and seg[0] not in READONLY_CMDS:
        return True
    return False


def is_protected_rel(rel: str) -> bool:
    rel = rel.lstrip("./")
    return rel in PROTECTED or rel == VENDOR_DIR or rel.startswith(VENDOR_DIR + "/")


def rel_to_root(p: str) -> str | None:
    try:
        return os.path.relpath(os.path.realpath(p if os.path.isabs(p) else ROOT / p), os.path.realpath(ROOT))
    except ValueError:
        return None


WRITERS = {"rm", "mv", "cp", "sed", "tee", "truncate", "perl", "install", "dd", "chmod", "ln", "rsync", "patch"}


def check_bash_protected(cmd: str, seg: list[str]) -> bool:
    head = Path(seg[0]).name if seg else ""
    redirects = bool(re.search(r">\s*\S", cmd))
    if head not in WRITERS and not redirects:
        return False
    for t in seg:
        t = t.lstrip(">")
        if t and not t.startswith("-") and ("/" in t or t in PROTECTED or "." in t):
            rel = rel_to_root(t)
            if rel and not rel.startswith("..") and is_protected_rel(rel):
                return True
    return False


def analyze_bash(cmd: str) -> dict:
    """-> {"deny": [...], "ask": [...], "scan_text": [...], "commit_paths": [...], "push": bool, "commit": bool}"""
    res = {"deny": [], "ask": [], "texts": [], "paths": [], "push": False, "commit": False}
    for seg in split_segments(cmd):
        g = git_invocation(seg)
        if g:
            sub, args = g
            if sub == "push":
                res["push"] = True
                res["deny"] += check_git_push(args)
            elif sub == "commit":
                res["commit"] = True
                m, t, p = check_git_commit(args)
                res["deny"] += m
                res["texts"] += t
                res["paths"] += p
            elif sub == "add":
                res["deny"] += check_git_add(args)
            elif sub == "config" and any("core.hookspath" == a.lower() for a in args):
                res["ask"].append("changing core.hooksPath disables the repo guard hooks")
        if check_launchctl(seg):
            res["ask"].append("service change: use `tulda service ...` (tests must keep mocking run_launchctl)")
        if check_bash_protected(cmd, seg):
            res["deny"].append("LICENSE, NOTICE and tulda/static/vendor are protected: ask the user")
    res["deny"] = list(dict.fromkeys(res["deny"]))
    return res


# ---- scans that use git ------------------------------------------------------------------
def scan_staged() -> list[str]:
    return scan_added(git("diff", "--cached", "-U0", "--no-color", "--no-ext-diff"))


def scan_commit_paths(paths: list[str]) -> list[str]:
    """For `git commit -- paths`: scan their uncommitted changes (tracked diff + untracked files)."""
    if not paths:
        return []
    f = scan_added(git("diff", "HEAD", "-U0", "--no-color", "--no-ext-diff", "--", *paths))
    untracked = [p for p in git("ls-files", "-o", "--exclude-standard", "-z", "--", *paths).split("\0") if p]
    return f + scan_paths(untracked)


def scan_outgoing(range_args: list[str]) -> list[str]:
    f = scan_added(git("log", "-p", "-U0", "--no-color", "--no-ext-diff", "--format=", *range_args))
    for block in git("log", "--format=%h%x00%B%x01", *range_args).split("\x01"):
        if "\0" in block:
            sha, msg = block.strip("\n").split("\0", 1)
            f += [f"commit {sha} message line {n}: {label}" for n, label in scan_text(msg)]
    return f


# ---- Bambu reminder / vendor sums / quotes ----------------------------------------------
BAMBU_NOTE = ("Bambu terms reminder (warn only): quote Bambu documents only as short attributed "
              "excerpts; do not add Bambu Studio binaries, profiles or assets, or host renders of "
              "third-party models; see docs/BAMBU-TERMS.md.")


def bambu_relevant(path: str, text: str) -> bool:
    p = path.lower()
    return "bambu" in p or p.startswith(str(ROOT / "docs").lower() + os.sep) or bool(re.search(r"bambu", text, re.I))


def long_quote_findings(text: str, max_words: int = 60) -> list[int]:
    """Line numbers where a markdown blockquote run exceeds max_words words."""
    out, run, start = [], 0, 0
    for n, line in enumerate(text.splitlines() + [""], 1):
        if line.lstrip().startswith(">"):
            if run == 0:
                start = n
            run += len(line.lstrip("> ").split())
        else:
            if run > max_words:
                out.append(start)
            run = 0
    return out


def vendor_sums_compute() -> dict[str, str]:
    base = ROOT / VENDOR_DIR
    out = {}
    for p in sorted(base.rglob("*")):
        if p.is_file() and p.name != "SHA256SUMS":
            out[p.relative_to(base).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def vendor_sums_read() -> dict[str, str]:
    out = {}
    for line in (ROOT / SUMS_FILE).read_text().splitlines():
        if line.strip():
            h, _, name = line.partition("  ")
            out[name.strip()] = h.strip()
    return out


def vendor_sums_problems() -> list[str]:
    try:
        want = vendor_sums_read()
    except OSError:
        return [f"{SUMS_FILE} is missing"]
    have = vendor_sums_compute()
    probs = [f"{SUMS_FILE}: changed {k}" for k in sorted(have) if k in want and have[k] != want[k]]
    probs += [f"{SUMS_FILE}: unlisted file {k}" for k in sorted(set(have) - set(want))]
    probs += [f"{SUMS_FILE}: missing file {k}" for k in sorted(set(want) - set(have))]
    return probs


# ---- mode handlers -----------------------------------------------------------------------
def die(msg: str, code: int = 2):
    print(msg, file=sys.stderr)
    sys.exit(code)


def read_event() -> dict:
    try:
        if sys.stdin.isatty():
            return {}
        return json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        return {}


def mode_bash(ev: dict):
    cmd = (ev.get("tool_input") or {}).get("command", "")
    if not cmd:
        return
    a = analyze_bash(cmd)
    problems = list(a["deny"])
    if a["commit"]:
        for n, label in scan_text("\n".join(a["texts"])):
            problems.append(f"commit message text: {label}")
        findings = scan_staged() + scan_commit_paths(a["paths"])
        if findings:
            problems.append(report(findings, "the commit's changes"))
    if a["push"]:
        try:
            findings = scan_outgoing(["HEAD", "--not", "--remotes"])
        except Exception:
            findings = []
        if findings:
            problems.append(report(findings, "commits that would be pushed"))
    if problems:
        die("guard blocked this command:\n- " + "\n- ".join(problems))
    if a["ask"]:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "permissionDecision": "ask",
                                                 "permissionDecisionReason": "; ".join(a["ask"])}}))


def edit_texts(ti: dict) -> str:
    parts = [ti.get("content", ""), ti.get("new_string", "")]
    parts += [e.get("new_string", "") for e in ti.get("edits", []) if isinstance(e, dict)]
    return "\n".join(p for p in parts if isinstance(p, str))


def mode_edit(ev: dict):
    ti = ev.get("tool_input") or {}
    path = ti.get("file_path", "")
    if not path:
        return
    rel = rel_to_root(path)
    if rel and not rel.startswith("..") and is_protected_rel(rel) and os.environ.get("TULDA_ALLOW_PROTECTED") != "1":
        die(f"guard: {rel} is protected (LICENSE, NOTICE, vendored three.js). Ask the user; for a "
            "deliberate three.js upgrade they can run with TULDA_ALLOW_PROTECTED=1 and bump SHA256SUMS.")
    if bambu_relevant(path, edit_texts(ti)):
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "additionalContext": BAMBU_NOTE}}))


def mode_post_edit(ev: dict):
    path = (ev.get("tool_input") or {}).get("file_path", "")
    rel = rel_to_root(path) if path else None
    if not rel or rel.startswith("..") or skip_path(rel):
        return
    findings = scan_paths([rel])
    if findings:
        die(report(findings, "the file you just edited") + " (already written; fix it now)")


def changed_code() -> bool:
    out = git("status", "--porcelain", "--", "tulda", "tests", "scripts")
    return bool(out.strip())


def run_tests() -> tuple[int, str]:
    venv = ROOT / ".venv/bin/python"
    py = str(venv) if venv.exists() else sys.executable
    env = dict(os.environ, TULDA_GUARD_RUNNING="1")
    try:
        r = subprocess.run([py, "-m", "unittest", "discover", "-s", "tests", "-t", "."], cwd=ROOT,
                           capture_output=True, text=True, timeout=100, env=env)
    except subprocess.TimeoutExpired:
        return 0, "timeout"  # do not block on a slow suite
    return r.returncode, (r.stderr or r.stdout)[-1500:]


def mode_stop(ev: dict):
    if ev.get("stop_hook_active") or os.environ.get("TULDA_GUARD_RUNNING") or os.environ.get("TULDA_GUARD_NO_STOP"):
        return
    if not changed_code():
        return
    code, tail = run_tests()
    if code:
        die("guard: the test suite is failing (shown once; fix or tell the user if the failure is "
            "from someone else's in-progress work):\n" + tail)


def mode_session_start(_ev: dict):
    notes = ["tulda rules: public repo, placeholders only; read-only server, never bind 0.0.0.0/::; "
             "commit by explicit path; never force-push; no push without user approval (see CLAUDE.md)."]
    dl = denylist_path()
    if not dl.exists():
        notes.append(f"WARNING: privacy denylist not found at {dl}: scans are generic-only. "
                     "Create it (one regex per line) with your real host/tailnet/user names; keep it outside the repo.")
    elif ROOT in dl.resolve().parents:
        notes.append("WARNING: the denylist is inside the repo; move it outside.")
    hp = git("config", "--get", "core.hooksPath").strip()
    if hp != ".githooks":
        notes.append("git hooks are not enabled here (optional): git config core.hooksPath .githooks")
    print("\n".join(notes))


def protected_staged() -> list[str]:
    if os.environ.get("TULDA_ALLOW_PROTECTED") == "1":
        return []
    names = git("diff", "--cached", "--name-only", "-z").split("\0")
    return [n for n in names if n and is_protected_rel(n)]


def mode_git_precommit():
    probs = []
    prot = protected_staged()
    if prot:
        probs.append("protected files staged (set TULDA_ALLOW_PROTECTED=1 for a deliberate change):\n  " + "\n  ".join(prot))
    findings = scan_staged()
    if findings:
        probs.append(report(findings, "staged changes"))
    if probs:
        die("pre-commit blocked:\n" + "\n".join(probs), 1)


def mode_git_commitmsg(path: str):
    text = Path(path).read_text(errors="replace")
    text = "\n".join(l for l in text.splitlines() if not l.startswith("#"))
    hits = scan_text(text)
    if hits:
        die("commit-msg blocked: privacy scan hit in message at line(s) "
            + ", ".join(sorted({str(n) for n, _ in hits}, key=int)) + " (text not shown)", 1)


def mode_git_prepush(stdin_text: str):
    probs = []
    for line in stdin_text.splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        lref, lsha, rref, rsha = parts
        if re.match(r"^refs/(heads|tags)/.*(backup|pre-release)", rref) or re.match(r"^refs/(heads|tags)/.*(backup|pre-release)", lref):
            probs.append(f"pushing {rref} is blocked (backup/pre-release refs stay local)")
        if lsha == ZERO_SHA:
            probs.append(f"deleting remote ref {rref} is blocked")
            continue
        if rsha != ZERO_SHA:
            ok = subprocess.run(["git", "merge-base", "--is-ancestor", rsha, lsha], cwd=ROOT).returncode == 0
            if not ok:
                probs.append(f"{rref}: not a fast-forward (force-push blocked; run git fetch if the remote moved)")
                continue
            rng = [f"{rsha}..{lsha}"]
        else:
            rng = [lsha, "--not", "--remotes"]
        findings = scan_outgoing(rng)
        if findings:
            probs.append(report(findings, f"commits being pushed to {rref}"))
    if probs:
        die("pre-push blocked:\n- " + "\n- ".join(probs), 1)


def mode_ci_generic():
    probs = []
    files = tracked_files()
    findings = scan_paths(files, use_denylist=False)
    if findings:
        probs.append(report(findings, "the tracked tree (generic patterns only)"))
    for f in PROTECTED:
        if f not in files:
            probs.append(f"{f} is missing")
    probs += vendor_sums_problems()
    bt = ROOT / "docs/BAMBU-TERMS.md"
    if bt.exists():
        for n in long_quote_findings(bt.read_text()):
            probs.append(f"docs/BAMBU-TERMS.md:{n}: long verbatim quote block")
    if probs:
        die("ci-generic failed:\n- " + "\n- ".join(probs), 1)
    print("ci-generic ok")


def mode_scan(paths: list[str]):
    findings = scan_paths(paths or tracked_files())
    if not load_denylist():
        print("guard: note: no denylist loaded (generic patterns only)", file=sys.stderr)
    if findings:
        die(report(findings, "the tree"), 1)
    print("scan ok")


def mode_scan_history():
    findings = scan_outgoing(["--all"])
    if findings:
        die(report(findings, "history"), 1)
    print("history scan ok")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 0
    mode = argv[1]
    claude_modes = {"bash": mode_bash, "edit": mode_edit, "post-edit": mode_post_edit,
                    "stop": mode_stop, "session-start": mode_session_start}
    try:
        if mode == "session-start":
            mode_session_start({})
        elif mode in claude_modes:
            claude_modes[mode](read_event())
        elif mode == "git-precommit":
            mode_git_precommit()
        elif mode == "git-commitmsg":
            mode_git_commitmsg(argv[2])
        elif mode == "git-prepush":
            mode_git_prepush(sys.stdin.read())
        elif mode == "ci-generic":
            mode_ci_generic()
        elif mode == "scan":
            mode_scan(argv[2:])
        elif mode == "scan-history":
            mode_scan_history()
        elif mode == "vendor-sums":
            sums = vendor_sums_compute()
            (ROOT / SUMS_FILE).write_text("".join(f"{h}  {k}\n" for k, h in sums.items()))
            print(f"wrote {SUMS_FILE} ({len(sums)} files)")
        else:
            print(f"unknown mode {mode}", file=sys.stderr)
            return 64
    except SystemExit:
        raise
    except Exception as e:  # fail open for Claude hooks; fail closed for git/CI
        print(f"guard: internal error in {mode}: {type(e).__name__}", file=sys.stderr)
        return 0 if mode in claude_modes else 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
