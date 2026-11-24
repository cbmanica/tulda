"""Unit tests for scripts/guard.py (all sample 'private' values are placeholders)."""
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("tulda_guard", ROOT / "scripts" / "guard.py")
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


def deny(cmd):
    return guard.analyze_bash(cmd)["deny"]


class Privacy(unittest.TestCase):
    def labels(self, text, deny_file=None):
        env = {"TULDA_DENYLIST": deny_file or "/nonexistent/denylist"}
        with mock.patch.dict(os.environ, env):
            return [(n, lab) for n, lab in guard.scan_text(text)]

    def test_generic_hits(self):
        self.assertEqual(self.labels("a\nhost 10.1.2.3 up"), [(2, "private-ipv4")])  # guard:ok
        self.assertEqual(self.labels("ip 192.168.0.9"), [(1, "private-ipv4")])  # guard:ok
        self.assertEqual(self.labels("ts 100.100.1.2"), [(1, "cgnat-ipv4")])  # guard:ok
        self.assertEqual(self.labels("x.tailabc.ts.net"), [(1, "tailnet-name")])  # guard:ok
        self.assertEqual(self.labels("cd /Users/someone/x"), [(1, "home-path")])  # guard:ok
        self.assertEqual(self.labels("mail a.b@corp.org"), [(1, "email")])  # guard:ok
        self.assertEqual(self.labels("box.local"), [(1, "mdns-name")])  # guard:ok

    def test_allowlist(self):
        for ok in ("127.0.0.1", "192.0.2.5", "198.51.100.7", "203.0.113.9", "/Users/you/x",
                   "Co-Authored-By: X <noreply@anthropic.com>", "me@example.com", "settings.local.json",
                   "100.64.0.0/10", "172.32.0.1", "11.1.2.3", "script x guard:ok 10.0.0.1"):
            self.assertEqual(self.labels(ok), [], ok)

    def test_allowed_token_does_not_mask_real_one_on_same_line(self):
        self.assertEqual(self.labels("127.0.0.1 and 10.9.9.9"), [(1, "private-ipv4")])  # guard:ok

    def test_denylist_and_no_text_leak(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d, "deny.txt")
            f.write_text("# comment\n\nsecretbox\n(unclosed\n")
            with redirect_stderr(io.StringIO()) as err:
                hits = self.labels("my SecretBox here", str(f))
            self.assertEqual(hits, [(1, "denylist")])
            self.assertIn("line 4", err.getvalue())
            report = guard.report(["a.py:1: denylist"], "x")
            self.assertNotIn("secretbox", report.lower())

    def test_missing_denylist_is_generic_only(self):
        self.assertEqual(self.labels("secretbox"), [])

    def test_parse_added_line_numbers(self):
        diff = ("diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1,2 +10,3 @@\n ctx\n+one 10.0.0.1\n-gone\n+two\n")  # guard:ok
        self.assertEqual(guard.parse_added(diff), [("f", 11, "one 10.0.0.1"), ("f", 12, "two")])  # guard:ok
        with mock.patch.dict(os.environ, {"TULDA_DENYLIST": "/nonexistent"}):
            self.assertEqual(guard.scan_added(diff), ["f:11: private-ipv4"])

    def test_vendor_and_test_fixture_exemptions(self):
        diff = "+++ b/tulda/static/vendor/x.js\n@@ -0,0 +1 @@\n+10.0.0.1\n"  # guard:ok
        self.assertEqual(guard.scan_added(diff), [])
        diff = "+++ b/tests/t.py\n@@ -0,0 +1 @@\n+10.0.0.1\n"  # guard:ok
        self.assertEqual(guard.scan_added(diff), [])
        diff = "+++ b/tests/t.py\n@@ -0,0 +1 @@\n+a@corp.org\n"  # guard:ok
        self.assertEqual(len(guard.scan_added(diff)), 1)


class BashGuards(unittest.TestCase):
    def test_push_blocks(self):
        for c in ("git push --force", "git push -f origin main", "git push origin +main",
                  "git push --force-with-lease", "git push -fu origin x", "git push origin :old",
                  "git push --delete origin x", "git push origin backup-2026", "git push origin pre-release",
                  "git push --no-verify", "FOO=1 git push --force", "sh -c 'git push -f'",
                  "git status && git push --force", "git -C . push --force"):
            self.assertTrue(deny(c), c)

    def test_push_allows(self):
        for c in ("git push", "git push -u origin main", "git push origin main", "git push --dry-run -n"):
            self.assertEqual(deny(c), [], c)

    def test_commit_and_add(self):
        for c in ("git add -A", "git add .", "git add -u", "git add --all", "git add",
                  "git commit -a -m x", "git commit -am x", "git commit --all", "git commit --no-verify -m x",
                  "git commit -nm x", "git x; git add ."):
            self.assertTrue(deny(c), c)
        for c in ("git add a.py b.py", "git add -- a", "git commit -m 'fix -a flag' -- a.py",
                  "git commit -m x -- a b", "git add -p", "echo git add ."):
            self.assertEqual(deny(c), [], c)

    def test_commit_paths_and_messages(self):
        a = guard.analyze_bash("git commit -m 'hello' -- a.py b.py")
        self.assertEqual((a["texts"], a["paths"]), (["hello"], ["a.py", "b.py"]))
        self.assertEqual(guard.analyze_bash("git commit --message=hi x")["texts"], ["hi"])

    def test_launchctl_asks(self):
        for c in ("launchctl bootout gui/501/x", "/bin/launchctl unload foo.plist", "launchctl kickstart -k gui/1/x",
                  "cp x.plist ~/Library/LaunchAgents/"):
            a = guard.analyze_bash(c)
            self.assertTrue(a["ask"] and not a["deny"], c)
        for c in ("launchctl list", "launchctl print gui/501/x", "ls ~/Library/LaunchAgents", "tulda service install"):
            self.assertEqual(guard.analyze_bash(c)["ask"], [], c)

    def test_protected_via_bash(self):
        for c in ("rm LICENSE", "sed -i '' s/a/b/ NOTICE", "echo x > tulda/static/vendor/a.js", "mv NOTICE x"):
            self.assertTrue(deny(c), c)
        for c in ("cat LICENSE", "grep x NOTICE", "git diff -- LICENSE"):
            self.assertEqual(deny(c), [], c)

    def test_hooks_path_change_asks(self):
        self.assertTrue(guard.analyze_bash("git config core.hooksPath /dev/null")["ask"])


class Protected(unittest.TestCase):
    def test_paths(self):
        for p in ("LICENSE", "NOTICE", "tulda/static/vendor/three-0.187.0/build/three.core.js"):
            self.assertTrue(guard.is_protected_rel(p), p)
        for p in ("README.md", "tulda/static/viewer.js", "docs/LICENSE", "tulda/LICENSE"):
            self.assertFalse(guard.is_protected_rel(p), p)

    def run_edit(self, path, env=None):
        ev = {"tool_input": {"file_path": str(ROOT / path), "content": "x"}}
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.dict(os.environ, env or {}), redirect_stdout(out), redirect_stderr(err):
            try:
                guard.mode_edit(ev)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_edit_blocked_and_override(self):
        self.assertEqual(self.run_edit("LICENSE")[0], 2)
        self.assertEqual(self.run_edit("tulda/static/vendor/a.js")[0], 2)
        self.assertEqual(self.run_edit("LICENSE", {"TULDA_ALLOW_PROTECTED": "1"})[0], 0)
        self.assertEqual(self.run_edit("tulda/server.py")[0], 0)

    def test_bambu_warning_is_nonblocking(self):
        code, out, _ = self.run_edit("docs/BAMBU-TERMS.md")
        self.assertEqual(code, 0)
        self.assertIn("Bambu terms reminder", json.loads(out)["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.run_edit("tulda/catalog.py")[1], "")


class StopHook(unittest.TestCase):
    def test_skips(self):
        with mock.patch.object(guard, "run_tests", side_effect=AssertionError("must not run")):
            guard.mode_stop({"stop_hook_active": True})
            with mock.patch.dict(os.environ, {"TULDA_GUARD_RUNNING": "1"}):
                guard.mode_stop({})
            with mock.patch.dict(os.environ, {"TULDA_GUARD_NO_STOP": "1"}):
                guard.mode_stop({})
            with mock.patch.object(guard, "changed_code", return_value=False):
                guard.mode_stop({})

    def test_blocks_on_failure_once(self):
        with mock.patch.object(guard, "changed_code", return_value=True), \
                mock.patch.object(guard, "run_tests", return_value=(1, "FAILED")), \
                redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                guard.mode_stop({})
            self.assertEqual(cm.exception.code, 2)
            guard.mode_stop({"stop_hook_active": True})  # second pass lets Claude stop

    def test_passes_when_green(self):
        with mock.patch.object(guard, "changed_code", return_value=True), \
                mock.patch.object(guard, "run_tests", return_value=(0, "")):
            guard.mode_stop({})


class Misc(unittest.TestCase):
    def test_long_quote_detection(self):
        short = "> " + "word " * 10
        long_ = "\n".join("> " + "word " * 10 for _ in range(8))
        self.assertEqual(guard.long_quote_findings(short), [])
        self.assertEqual(guard.long_quote_findings("intro\n\n" + long_), [3])

    def test_bambu_terms_doc_has_no_long_quotes(self):
        doc = ROOT / "docs" / "BAMBU-TERMS.md"
        if doc.exists():
            self.assertEqual(guard.long_quote_findings(doc.read_text()), [])

    def test_prepush_rejects_denylisted_refs_and_deletes(self):
        z = "0" * 40
        a = "a" * 40
        for line in (f"refs/heads/x {a} refs/heads/backup-1 {z}", f"(delete) {z} refs/heads/x {a}"):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                guard.mode_git_prepush(line + "\n")

    def test_bad_stdin_fails_open(self):
        with mock.patch("sys.stdin", io.StringIO("not json")):
            self.assertEqual(guard.read_event(), {})


if __name__ == "__main__":
    unittest.main()
