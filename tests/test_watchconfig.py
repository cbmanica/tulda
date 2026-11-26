"""Watch config ([watch] / [[watch.dir]]), `tulda watch --from-config`, doctor checks."""

import errno
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tests.test_projects import MW, threemf_bytes, write_3mf, write_zip
from tulda import cli, projects, watchconfig as wc
from tulda.watchconfig import WatchConfigError


def tmpdir() -> Path:
    d = Path(tempfile.mkdtemp()).resolve()
    return d


class Parse(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir()
        self.addCleanup(shutil.rmtree, self.d)

    def parse(self, text):
        return wc.parse_bytes(text.encode(), "test.toml")

    def test_no_watch_section(self):
        cfg = self.parse("[defaults]\nport = 8765\n")
        self.assertEqual(cfg.dirs, [])
        self.assertEqual(cfg.settings["interval"], 5.0)

    def test_precedence(self):
        cfg = self.parse(f"""
[watch]
settle = 7
render = true
[[watch.dir]]
path = "{self.d}/in"
destination = "{self.d}/out"
[[watch.dir]]
path = "{self.d}/in2"
destination = "{self.d}/out"
settle = 1
interval = 2.5
force = true
include = ["*.3mf"]
""")
        a, b = cfg.dirs
        self.assertEqual((a.interval, a.settle, a.render, a.force, a.include), (5.0, 7.0, True, False, None))
        self.assertEqual((b.interval, b.settle, b.render, b.force, b.include), (2.5, 1.0, True, True, ["*.3mf"]))
        wc.apply_overrides(cfg, {"settle": 0.5, "render": False, "interval": None, "force": None})
        self.assertEqual([(e.settle, e.render, e.interval) for e in cfg.dirs], [(0.5, False, 5.0), (0.5, False, 2.5)])

    def test_tilde_expanded(self):
        cfg = self.parse('[[watch.dir]]\npath = "~/inbox-example"\ndestination = "~/projects-example"\n')
        self.assertEqual(cfg.dirs[0].path, Path.home() / "inbox-example")

    def test_errors(self):
        d = self.d
        cases = {
            "[watch]\nintervall = 5\n": "unknown key",
            f'[[watch.dir]]\npath = "{d}/a"\ndestination = "{d}/b"\ntypo = 1\n': "unknown key",
            f'[[watch.dir]]\npath = "{d}/a"\n': "missing required key 'destination'",
            f'[[watch.dir]]\ndestination = "{d}/a"\n': "missing required key 'path'",
            f'[[watch.dir]]\npath = "{d}/a"\ndestination = "{d}/a/sub"\n': "inside each other",
            f'[[watch.dir]]\npath = "{d}/a"\ndestination = "{d}/a"\n': "inside each other",
            f'[[watch.dir]]\npath = "{d}/a/sub"\ndestination = "{d}/a"\n': "inside each other",
            f'[[watch.dir]]\npath = "{d}/a"\ndestination = "{d}/b"\n'
            f'[[watch.dir]]\npath = "{d}/a/"\ndestination = "{d}/c"\n': "duplicates entry #1",
            '[[watch.dir]]\npath = "rel"\ndestination = "/x"\n': "absolute",
            f'[watch.dir]\npath = "{d}/a"\ndestination = "{d}/b"\n': "array of tables",
            "[watch]\ninterval = 0\n": "interval must be > 0",
            "[watch]\nsettle = -1\n": "settle must be >= 0",
            '[watch]\nsettle = "5"\n': "must be a number",
            "[watch]\nrender = 1\n": "true or false",
            '[watch]\nbacklog = "skip"\n': 'backlog must be "ignore" or "import"',
            "[watch]\ninclude = []\n": "include",
            '[watch]\ninclude = ["a/b.zip"]\n': "file names only",
            "watch = 3\n": "must be a table",
            "[watch\n": "TOML syntax error",
        }
        for text, msg in cases.items():
            with self.subTest(text=text):
                with self.assertRaises(WatchConfigError) as cm:
                    self.parse(text)
                self.assertIn(msg, str(cm.exception))

    def test_read_config_missing_and_not_utf8(self):
        with self.assertRaisesRegex(WatchConfigError, "not found"):
            wc.read_config(self.d / "nope.toml")
        (self.d / "bin.toml").write_bytes(b"\xff\xfe")
        with self.assertRaisesRegex(WatchConfigError, "UTF-8"):
            wc.read_config(self.d / "bin.toml")

    def test_project_tulda_toml_never_supplies_watch_entries(self):
        # A tulda.toml in a folder (e.g. a cloned repo) is not a watch config source.
        (self.d / "tulda.toml").write_text(f'[[watch.dir]]\npath = "{self.d}/x"\ndestination = "{self.d}/y"\n')
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.d / "cfg")}), \
                mock.patch("os.getcwd", lambda: str(self.d)):
            self.assertEqual(wc.config_path(None), self.d / "cfg" / "tulda" / "config.toml")


class FromConfig(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir()
        self.addCleanup(shutil.rmtree, self.d)
        env = mock.patch.dict(os.environ, {"TULDA_STATE_DIR": str(self.d / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.inbox, self.dest = self.d / "inbox", self.d / "projects"
        self.inbox.mkdir()
        self.cfg = self.d / "config.toml"
        self.cfg.write_text(f'[watch]\nsettle = 0\nbacklog = "import"\n[[watch.dir]]\n'
                            f'path = "{self.inbox}"\ndestination = "{self.dest}"\n')

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_once(self):
        write_3mf(self.inbox / "a.3mf")
        old = time.time() - 60
        os.utime(self.inbox / "a.3mf", (old, old))
        code, out, _ = self.run_cli("watch", "--from-config", "--config", str(self.cfg), "--once")
        self.assertEqual(code, 0, out)
        self.assertIn("created Example-Box", out)
        self.assertTrue((self.dest / "Example-Box-v2-final" / "tulda-project.json").is_file())

    def test_include_and_force(self):
        write_zip(self.inbox / "plain.zip", {"a.stl": b"solid", "setup.sh": b"echo"})
        write_3mf(self.inbox / "a.3mf")
        self.cfg.write_text(f'[watch]\nsettle = 0\n[[watch.dir]]\npath = "{self.inbox}"\n'
                            f'destination = "{self.dest}"\nforce = true\ninclude = ["*.ZIP"]\n'
                            f'backlog = "import"\n')
        code, out, _ = self.run_cli("watch", "--from-config", "--config", str(self.cfg), "--once")
        self.assertEqual(code, 0, out)
        self.assertIn("created plain <- plain.zip", out)
        self.assertNotIn("a.3mf", out)

    def test_invalid_config_exits_2(self):
        self.cfg.write_text("[watch]\nbogus = 1\n")
        code, _, err = self.run_cli("watch", "--from-config", "--config", str(self.cfg), "--once")
        self.assertEqual(code, 2)
        self.assertIn("unknown key", err)

    def test_adhoc_requires_dir_and_dest(self):
        code, _, err = self.run_cli("watch", str(self.inbox))
        self.assertNotEqual(code, 0)
        self.assertIn("--destination", err)


class Backlog(unittest.TestCase):
    def setUp(self):
        from tulda import watchd
        self.watchd = watchd
        self.d = tmpdir()
        self.addCleanup(shutil.rmtree, self.d)
        self.inbox, self.dest, self.sdir = self.d / "inbox", self.d / "projects", self.d / "state"
        self.inbox.mkdir()
        self.cfg = self.d / "config.toml"
        self.logs = []

    def config(self, extra=""):
        self.cfg.write_text(f'[watch]\nsettle = 0\n[[watch.dir]]\npath = "{self.inbox}"\n'
                            f'destination = "{self.dest}"\n{extra}')

    def run_daemon(self, ticks=3):
        now = [time.time() + 10]
        dm = self.watchd.WatchDaemon(self.cfg, self.sdir, log=self.logs.append, clock=lambda: now[0])
        dm.start()
        for _ in range(ticks):
            now[0] += 6
            dm.tick()
        return dm

    def test_default_and_override(self):
        self.config()
        self.assertEqual(wc.load(self.cfg).dirs[0].backlog, "ignore")
        self.config('backlog = "import"\n')
        self.assertEqual(wc.load(self.cfg).dirs[0].backlog, "import")

    def test_new_folder_backlog_not_imported(self):
        write_3mf(self.inbox / "old.3mf")
        self.config()
        self.run_daemon()
        self.assertFalse(self.dest.exists())
        self.assertTrue(self.watchd.read_state(self.sdir)[self.watchd.BASELINE_MARK])

    def test_import_option_imports_backlog(self):
        write_3mf(self.inbox / "old.3mf")
        self.config('backlog = "import"\n')
        self.run_daemon()
        self.assertTrue(any(m.startswith("created Example-Box") for m in self.logs))

    def test_folder_from_pre_baseline_state_is_adopted(self):
        write_3mf(self.inbox / "a.3mf")
        self.config()
        self.sdir.mkdir()
        (self.sdir / "watch.json").write_text(json.dumps(
            {"pid": 1, "dirs": [{"path": str(self.inbox), "error": None}]}))
        self.run_daemon()
        self.assertTrue(any(m.startswith("created Example-Box") for m in self.logs))

    def test_state_from_baseline_version_or_error_is_not_adopted(self):
        write_3mf(self.inbox / "a.3mf")
        self.config()
        self.sdir.mkdir()
        for prior in ({"dirs": [{"path": str(self.inbox), "error": None}], "backlog_baselines": 1},
                      {"dirs": [{"path": str(self.inbox), "error": "cannot list"}]}):
            self.assertEqual(self.watchd.adoptable_dirs(prior), set())
        (self.sdir / "watch.json").write_text(json.dumps(
            {"dirs": [{"path": str(self.inbox), "error": "macOS privacy (TCC)"}]}))
        self.run_daemon()
        self.assertFalse(self.dest.exists())

    def test_once_records_baseline_and_imports_nothing(self):
        write_3mf(self.inbox / "old.3mf")
        self.config()
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = cli.main(["watch", "--from-config", "--config", str(self.cfg), "--once",
                             "--state-dir", str(self.sdir)])
        self.assertEqual(code, 0)
        self.assertIn("ignoring 1 file(s)", out.getvalue())
        self.assertFalse(self.dest.exists())

    def events(self):
        p = self.sdir / "watch-events.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def test_restart_does_not_rereport_unchanged_file(self):
        """Regression: every restart recorded another "exists" for the same imported zip."""
        self.config()
        self.run_daemon(1)                                    # empty folder: baseline recorded
        write_zip(self.inbox / "Some+Thing.zip", {"Thing.3mf": threemf_bytes(MW)})
        self.run_daemon()
        self.assertEqual([(e["file"], e["outcome"]) for e in self.events()],
                         [("", "backlog"), ("Some+Thing.zip", "created")])
        for _ in range(3):                                    # service restarts
            self.logs.clear()
            self.run_daemon()
            self.assertEqual(len(self.events()), 2, self.events())
            self.assertFalse([m for m in self.logs if "Some+Thing.zip" in m], self.logs)
        # a genuinely new file with the same content is still reported
        shutil.copy(self.inbox / "Some+Thing.zip", self.inbox / "copy.zip")
        self.run_daemon()
        self.assertEqual(self.events()[-1]["file"], "copy.zip")
        self.assertEqual(self.events()[-1]["outcome"], "exists")
        self.assertEqual(len(self.events()), 3)

    def test_restart_after_adoption_and_changed_file(self):
        """Adopted (pre-baseline) folder: imported once, silent on restart, re-handled on change."""
        f = write_zip(self.inbox / "a.zip", {"Thing.3mf": threemf_bytes(MW)})
        self.config()
        self.sdir.mkdir()
        (self.sdir / "watch.json").write_text(json.dumps(
            {"pid": 1, "dirs": [{"path": str(self.inbox), "error": None}]}))
        self.run_daemon()
        self.run_daemon()
        self.assertEqual([e["outcome"] for e in self.events()], ["created"])
        write_zip(f, {"Thing.3mf": threemf_bytes({**MW, "Title": "Changed"})})
        os.utime(f, (time.time() - 60, time.time() - 60))
        self.run_daemon()
        self.assertEqual([(e["outcome"], e["project"]) for e in self.events()],
                         [("created", "Example-Box-v2-final"), ("created", "Changed")])

    def test_skips_not_rereported_but_errors_retried(self):
        self.config()
        self.run_daemon(1)
        write_zip(self.inbox / "App.zip", {"App.app/x": b"x"})
        (self.inbox / "bad.zip").write_bytes(b"not a zip")
        with mock.patch.object(projects, "create_project", side_effect=OSError("disk full")):
            self.run_daemon()
        outcomes = sorted(e["outcome"] for e in self.events() if e["file"])
        self.assertEqual(outcomes, ["error", "error"])
        self.run_daemon()                                     # errors are retried
        self.run_daemon()                                     # skips are not re-reported
        outcomes = sorted(e["outcome"] for e in self.events() if e["file"])
        self.assertEqual(outcomes, ["error", "error", "skipped", "skipped"])
        # force turned on: an earlier skip is re-evaluated
        self.config("force = true\n")
        self.run_daemon()
        self.assertEqual([e["outcome"] for e in self.events() if e["file"] == "App.zip"][-1],
                         "created")

    def test_handled_record_is_private_and_pruned(self):
        self.config()
        self.run_daemon(1)
        f = write_zip(self.inbox / "a.zip", {"Thing.3mf": threemf_bytes(MW)})
        self.run_daemon()
        rec = projects.handled_file(self.sdir, self.inbox, self.dest)
        self.assertEqual(oct(rec.stat().st_mode & 0o777), "0o600")
        self.assertEqual(list(json.loads(rec.read_text())["files"]), ["a.zip"])
        f.unlink()
        self.run_daemon(1)
        self.assertEqual(json.loads(rec.read_text())["files"], {})
        rec.write_text("{broken")                             # unreadable: started afresh
        write_zip(self.inbox / "a.zip", {"Thing.3mf": threemf_bytes(MW)})
        self.run_daemon()
        self.assertTrue(any("unreadable" in m for m in self.logs))
        self.assertEqual(self.events()[-1]["outcome"], "exists")

    def test_check_config_shows_backlog(self):
        self.config()
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["watch", "--check-config", str(self.cfg)]), 0)
        self.assertIn("backlog ignore", out.getvalue())


class Doctor(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir()
        self.addCleanup(shutil.rmtree, self.d)
        self.inbox, self.dest = self.d / "inbox", self.d / "projects"
        self.inbox.mkdir()
        self.cfg = self.d / "config.toml"

    def report(self, extra="", renders=None, tailscale=False):
        self.cfg.write_text(f'[[watch.dir]]\npath = "{self.inbox}"\ndestination = "{self.dest}"\n{extra}')
        lines, errors = wc.doctor_report(self.cfg, renders or self.d / "renders", "/py/bin/python3.12",
                                         tailscale, explicit=True)
        return "\n".join(lines), errors

    def test_ok(self):
        text, errors = self.report()
        self.assertEqual(errors, 0, text)
        self.assertIn("[ok] watch config valid", text)
        self.assertIn("/py/bin/python3.12", text)

    def test_broken_config(self):
        self.cfg.write_text("[watch]\nnope = 1\n")
        lines, errors = wc.doctor_report(self.cfg, self.d, "/py", False, explicit=True)
        self.assertEqual(errors, 1)
        self.assertIn("unknown key", "\n".join(lines))

    def test_missing_config_is_fine(self):
        lines, errors = wc.doctor_report(self.d / "none.toml", self.d, "/py", False)
        self.assertEqual(errors, 0)
        self.assertIn("no config file", "\n".join(lines))

    def test_tcc_eperm_message(self):
        def listdir(p):
            raise PermissionError(errno.EPERM, "Operation not permitted")
        with mock.patch.object(wc.os, "listdir", listdir), mock.patch.object(wc.sys, "platform", "darwin"):
            text, errors = self.report()
        self.assertEqual(errors, 1)
        self.assertIn("macOS privacy (TCC)", text)
        self.assertIn("Full Disk Access", text)
        self.assertIn("/py/bin/python3.12", text)

    def test_eacces_is_not_tcc(self):
        def listdir(p):
            raise PermissionError(errno.EACCES, "Permission denied")
        with mock.patch.object(wc.os, "listdir", listdir):
            text, errors = self.report()
        self.assertEqual(errors, 1)
        self.assertNotIn("TCC", text)
        self.assertIn("Permission denied", text)

    def test_missing_watched_dir(self):
        shutil.rmtree(self.inbox)
        text, errors = self.report()
        self.assertIn("does not exist", text)
        self.assertEqual(errors, 1)

    def test_destination_inside_renders_root(self):
        self.dest = self.d / "renders" / "mw"
        text, errors = self.report(renders=self.d / "renders")
        self.assertIn("inside the renders root", text)
        self.assertEqual(errors, 1)

    def test_render_with_tailscale_warns(self):
        text, errors = self.report(extra="render = true\n", tailscale=True)
        self.assertIn("whole tailnet", text)
        self.assertEqual(errors, 0)
        text, _ = self.report(extra="render = true\n", tailscale=False)
        self.assertNotIn("tailnet", text)

    def test_git_warning(self):
        if not shutil.which("git"):
            self.skipTest("git not installed")
        subprocess.run(["git", "init", "-q", str(self.d)], check=True)
        text, errors = self.report()
        self.assertIn("Add this line to its .gitignore", text)
        self.assertIn("/projects/", text)
        (self.d / ".gitignore").write_text("/projects/\n")
        text, _ = self.report()
        self.assertNotIn(".gitignore", text)

    def test_protected_location(self):
        home = self.d
        self.assertEqual(wc.protected_location(home / "Downloads" / "x", home), "~/Downloads")
        self.assertIsNone(wc.protected_location(home / "MakerWorld-inbox", home))

    def test_cli_doctor_counts_watch_errors(self):
        self.cfg.write_text("[watch]\nnope = 1\n")
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()), \
                mock.patch.object(cli, "doctor_watch", wraps=cli.doctor_watch) as dw:
            code = cli.main(["doctor", "--config", str(self.cfg)])
        dw.assert_called_once()
        self.assertEqual(code, 3)
        self.assertIn("[BROKEN] watch config", out.getvalue())


class Robustness(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir()
        self.addCleanup(shutil.rmtree, self.d)
        self.dest = self.d / "projects"
        self.src = write_3mf(self.d / "a.3mf")

    def visible(self):
        return sorted(p.name for p in self.dest.iterdir() if not p.name.startswith("."))

    def test_second_sha_check_under_lock(self):
        projects.create_project(self.src, self.dest)
        real = projects.find_existing
        calls = []

        def racy(dest, sha):                      # the unlocked fast path misses the other import
            calls.append(1)
            return None if len(calls) == 1 else real(dest, sha)
        with mock.patch.object(projects, "find_existing", racy):
            state, proj = projects.create_project(self.src, self.dest)
        self.assertEqual(state, "exists")
        self.assertEqual(self.visible(), ["Example-Box-v2-final"])
        self.assertFalse(list(self.dest.glob(projects.STAGING_PREFIX + "*")))

    def test_concurrent_importers(self):
        import threading
        results, barrier = [], threading.Barrier(4)

        def go():
            barrier.wait()
            results.append(projects.create_project(self.src, self.dest)[0])
        ts = [threading.Thread(target=go) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(sorted(results), ["created", "exists", "exists", "exists"])
        self.assertEqual(self.visible(), ["Example-Box-v2-final"])

    def test_interrupt_leaves_nothing(self):
        with mock.patch.object(projects, "licence_note", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                projects.create_project(self.src, self.dest)
        self.assertEqual([p.name for p in self.dest.iterdir() if p.name != projects.LOCK_NAME], [])

    def test_staging_dirs_are_not_projects_and_stale_ones_are_removed(self):
        self.dest.mkdir()
        st = self.dest / (projects.STAGING_PREFIX + "old")
        st.mkdir()
        (st / projects.MANIFEST).write_text('{"source": {"sha256": "%s"}}' % projects.sha256_file(self.src))
        old = time.time() - projects.STALE_STAGING - 10
        os.utime(st, (old, old))
        fresh = self.dest / (projects.STAGING_PREFIX + "fresh")
        fresh.mkdir()
        state, _ = projects.create_project(self.src, self.dest)
        self.assertEqual(state, "created")            # the staging manifest did not count
        self.assertFalse(st.exists())
        self.assertTrue(fresh.exists())               # may belong to a running importer


class DaemonState(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir()
        self.addCleanup(shutil.rmtree, self.d)
        self.inbox, self.dest, self.sdir = self.d / "inbox", self.d / "projects", self.d / "state"
        self.inbox.mkdir()
        self.cfg = self.d / "config.toml"
        self.cfg.write_text(f'[watch]\nsettle = 0\n[[watch.dir]]\npath = "{self.inbox}"\n'
                            f'destination = "{self.dest}"\n')
        self.logs = []

    def test_state_file(self):
        from tulda import watchd
        now = [time.time() + 10]
        dm = watchd.WatchDaemon(self.cfg, self.sdir, log=self.logs.append, clock=lambda: now[0])
        dm.start()
        dm.tick()                                      # records the (empty) backlog baseline
        write_3mf(self.inbox / "a.3mf")                # arrives after watching began
        now[0] += 6
        dm.tick()
        now[0] += 6
        dm.tick()
        st = watchd.read_state(self.sdir)
        self.assertEqual(st["mode"], "config")
        self.assertEqual(st["pid"], os.getpid())
        self.assertTrue(st["last_poll"])
        self.assertEqual(st["last_created"]["project"], "Example-Box-v2-final")
        self.assertEqual(st["dirs"][0]["totals"]["created"], 1)
        self.assertEqual(oct((self.sdir / "watch.json").stat().st_mode & 0o777), "0o600")
        text = "\n".join(watchd.state_lines(st))
        self.assertIn("Example-Box-v2-final <- a.3mf", text)
        self.assertIn("running", text)

    def test_unlistable_dir_logged_once_and_in_state(self):
        from tulda import watchd
        dm = watchd.WatchDaemon(self.cfg, self.sdir, log=self.logs.append)
        dm.start()
        shutil.rmtree(self.inbox)
        for _ in range(3):
            dm.loop.entries[next(iter(dm.loop.entries))][2] = 0
            dm.tick()
        self.assertEqual(sum("cannot list" in m for m in self.logs), 1)
        st = watchd.read_state(self.sdir)
        self.assertIn("cannot list", st["last_error"]["message"])
        self.assertIn("cannot list", st["dirs"][0]["error"])

    def test_tcc_error_in_watcher(self):
        w = projects.Watcher(self.inbox, self.dest, log=self.logs.append)

        def boom(self_):
            raise PermissionError(errno.EPERM, "Operation not permitted")
        with mock.patch.object(Path, "iterdir", boom), mock.patch.object(wc.sys, "platform", "darwin"):
            w.poll()
            w.poll()
        self.assertTrue(w.tcc_blocked)
        self.assertEqual(len(self.logs), 1)
        self.assertIn("Full Disk Access", self.logs[0])

    def test_state_lines_fallback_and_missing(self):
        from tulda import watchd
        self.assertIn("no state file", watchd.state_lines(None)[0])
        st = {"pid": -1, "mode": "fallback", "since": "2026-01-01T00:00:00+00:00",
              "reason": "bad toml", "dirs": []}
        text = "\n".join(watchd.state_lines(st))
        self.assertIn("running on fallback config since 2026-01-01T00:00:00+00:00: bad toml", text)
        self.assertIn("not running", text)

    def test_sigterm_clean_exit_and_log_dir(self):
        import signal
        logdir = self.d / "logs"
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent))
        p = subprocess.Popen([sys.executable, "-m", "tulda", "watch", "--from-config", "--config",
                              str(self.cfg), "--state-dir", str(self.sdir), "--log-dir", str(logdir),
                              "--interval", "0.2"], env=env)
        try:
            end = time.time() + 15
            while time.time() < end and not (self.sdir / "watch.json").exists():
                time.sleep(0.1)
            self.assertTrue((self.sdir / "watch.json").exists())
            p.send_signal(signal.SIGTERM)
            self.assertEqual(p.wait(15), 0)
        finally:
            if p.poll() is None:
                p.kill()
        log = (logdir / "watch.log").read_text()
        self.assertIn("received SIGTERM", log)
        self.assertIn("stopped", log)
        self.assertTrue((logdir / "watch.err.log").exists())


class Fallback(unittest.TestCase):
    """Reload on change and last-known-good fallback (driven tick by tick with a fake clock)."""

    def setUp(self):
        from tulda import watchd
        self.watchd = watchd
        self.d = tmpdir()
        self.addCleanup(shutil.rmtree, self.d)
        self.inbox, self.inbox2 = self.d / "inbox", self.d / "inbox2"
        self.inbox.mkdir()
        self.inbox2.mkdir()
        self.dest, self.sdir = self.d / "projects", self.d / "state"
        self.cfg = self.d / "config.toml"
        self.good = (f'[watch]\nsettle = 0\ninterval = 1\n[[watch.dir]]\npath = "{self.inbox}"\n'
                     f'destination = "{self.dest}"\n')
        self.mt = time.time() - 1000
        self.write(self.good)
        self.now = [time.time() + 100]
        self.logs = []

    def write(self, text):
        self.cfg.write_text(text)
        self.mt += 10                                  # distinct mtime even on coarse clocks
        os.utime(self.cfg, (self.mt, self.mt))

    def daemon(self):
        dm = self.watchd.WatchDaemon(self.cfg, self.sdir, log=self.logs.append, clock=lambda: self.now[0])
        dm.start()
        return dm

    def tick(self, dm, n=1):
        for _ in range(n):
            self.now[0] += 2
            dm.tick()

    def watched(self, dm):
        return sorted(e.path.name for _, e, _ in dm.loop.entries.values())

    def state(self):
        return self.watchd.read_state(self.sdir)

    def test_bad_edit_falls_back_then_fix_recovers_and_updates_last_good(self):
        dm = self.daemon()
        self.tick(dm)
        lg = self.sdir / "watch-config.last-good.toml"
        self.assertEqual(lg.read_text(), self.good)
        self.assertEqual(oct(lg.stat().st_mode & 0o777), "0o600")
        before = self.cfg.read_bytes()

        self.write("[watch\nbroken")
        self.tick(dm)                                  # first sight of the change: debounce
        self.assertEqual(dm.mode, "config")
        self.tick(dm)
        self.assertEqual(dm.mode, "fallback")
        self.assertEqual(self.watched(dm), ["inbox"])  # still watching via last-known-good
        st = self.state()
        self.assertEqual(st["mode"], "fallback")
        self.assertIn("TOML syntax error", st["reason"])
        text = "\n".join(self.watchd.state_lines(st))
        self.assertRegex(text, r"running on fallback config since \S+: .*TOML syntax error")
        self.assertTrue(any("running on last-known-good config" in m for m in self.logs))
        write_3mf(self.inbox / "a.3mf")               # imports keep working on fallback
        self.tick(dm, 3)
        self.assertTrue((self.dest / "Example-Box-v2-final").is_dir())
        self.assertEqual(self.cfg.read_bytes(), b"[watch\nbroken")   # user file never rewritten
        self.assertEqual(lg.read_text(), self.good)    # fallback mode never overwrites last-good

        n = len(self.logs)
        self.now[0] += self.watchd.RETRY + 1           # periodic retry, same error: no new noise
        dm.tick()
        self.assertEqual(dm.mode, "fallback")
        self.assertFalse([m for m in self.logs[n:] if "error" in m])

        fixed = self.good + f'[[watch.dir]]\npath = "{self.inbox2}"\ndestination = "{self.dest}"\n'
        self.write(fixed)
        self.tick(dm, 2)
        self.assertEqual(dm.mode, "config")
        self.assertEqual(self.watched(dm), ["inbox", "inbox2"])
        self.assertEqual(lg.read_text(), fixed)        # updated after a successful poll
        self.assertEqual(self.state()["mode"], "config")
        self.assertIsNotNone(before)

    def test_invalid_schema_and_unusable_dirs_fall_back(self):
        dm = self.daemon()
        self.tick(dm)
        self.write(self.good.replace("settle = 0", "setle = 0"))
        self.tick(dm, 2)
        self.assertEqual(dm.mode, "fallback")
        self.assertIn("unknown key", dm.state["reason"])
        self.write(f'[[watch.dir]]\npath = "{self.d}/missing"\ndestination = "{self.dest}"\n')
        self.tick(dm, 2)
        self.assertEqual(dm.mode, "fallback")
        self.assertIn("no watched directory is usable", dm.state["reason"])
        self.assertEqual(self.watched(dm), ["inbox"])
        (self.d / "missing").mkdir()                   # the folder appears: periodic retry
        self.now[0] += self.watchd.RETRY + 1
        dm.tick()
        self.assertEqual(dm.mode, "config")
        self.assertEqual(self.watched(dm), ["missing"])

    def test_no_last_good_stays_idle_then_recovers(self):
        self.write("[watch]\nbogus = true\n")
        dm = self.daemon()
        self.assertEqual(dm.mode, "idle")
        self.assertIn("no last-known-good config saved yet", dm.state["reason"])
        self.assertEqual(dm.loop.entries, {})
        self.tick(dm, 3)                               # alive, idle, nothing raised
        self.assertEqual(self.state()["mode"], "idle")
        self.assertIn("idle since", "\n".join(self.watchd.state_lines(self.state())))
        self.assertFalse((self.sdir / "watch-config.last-good.toml").exists())
        self.write(self.good)
        self.tick(dm, 2)
        self.assertEqual(dm.mode, "config")
        self.assertTrue((self.sdir / "watch-config.last-good.toml").exists())

    def test_missing_config_without_last_good_is_idle(self):
        self.cfg.unlink()
        dm = self.daemon()
        self.assertEqual(dm.mode, "idle")
        self.assertIn("config file not found", dm.state["reason"])

    def test_corrupt_last_good(self):
        self.sdir.mkdir(mode=0o700)
        (self.sdir / "watch-config.last-good.toml").write_bytes(b"\x00garbage[[[")
        self.write("[watch\n")
        dm = self.daemon()
        self.assertEqual(dm.mode, "idle")
        self.assertIn("last-known-good config is unusable too", dm.state["reason"])
        self.write(self.good)
        self.tick(dm, 2)
        self.assertEqual(dm.mode, "config")
        self.assertEqual((self.sdir / "watch-config.last-good.toml").read_text(), self.good)

    def test_valid_reload_keeps_memory_of_handled_files(self):
        dm = self.daemon()
        self.tick(dm)
        write_3mf(self.inbox / "a.3mf")
        self.tick(dm, 3)
        created = [m for m in self.logs if m.startswith("created")]
        self.assertEqual(len(created), 1)
        self.write(self.good.replace("interval = 1", "interval = 1.5"))
        self.tick(dm, 4)
        self.assertTrue(any("changed; reloaded" in m for m in self.logs))
        self.assertFalse([m for m in self.logs if m.startswith("exists")])
        self.assertEqual(next(iter(dm.loop.entries.values()))[1].interval, 1.5)

    def test_check_config_cli(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(cli.main(["watch", "--check-config", str(self.cfg)]), 0)
            self.assertEqual(cli.main(["watch", "--config", str(self.cfg), "--check-config"]), 0)
            self.assertEqual(cli.main(["config", "check", str(self.cfg)]), 0)
        self.assertIn("valid:", out.getvalue())
        self.assertIn(str(self.inbox), out.getvalue())
        self.write("[watch]\nnope = 1\n")
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(cli.main(["watch", "--check-config", str(self.cfg)]), 2)
        self.assertIn("unknown key", err.getvalue())
        self.assertFalse(self.sdir.exists())          # validation writes nothing

    def test_show_last_good(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(cli.main(["config", "show-last-good", "--state-dir", str(self.sdir)]), 1)
        self.assertIn("no last-known-good", err.getvalue())
        dm = self.daemon()
        self.tick(dm)
        self.write(self.good.replace("interval = 1", "interval = 2"))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["config", "show-last-good", "--state-dir", str(self.sdir)]), 0)
        self.assertIn("interval = 1", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(["config", "show-last-good", "--diff", "--state-dir", str(self.sdir),
                      "--config", str(self.cfg)])
        self.assertIn("-interval = 1\n+interval = 2", out.getvalue())

    def test_cli_loop_survives_broken_config(self):
        """The real process with a broken config and no last-good stays up (no launchd loop)."""
        import signal
        self.write("[watch\n")
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent))
        p = subprocess.Popen([sys.executable, "-m", "tulda", "watch", "--from-config", "--config",
                              str(self.cfg), "--state-dir", str(self.sdir)], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            end = time.time() + 15
            while time.time() < end and not (self.sdir / "watch.json").exists():
                time.sleep(0.1)
            time.sleep(0.5)
            self.assertIsNone(p.poll())
            self.assertEqual(self.watchd.read_state(self.sdir)["mode"], "idle")
            p.send_signal(signal.SIGTERM)
            out, _ = p.communicate(timeout=15)
            self.assertEqual(p.returncode, 0)
        finally:
            if p.poll() is None:
                p.kill()
        self.assertIn("idle until", out)


class ZipConfigNeverExtracted(unittest.TestCase):
    """A config inside a downloaded zip could inject adapter commands via the walk-up project
    config lookup used by `render`; it must never be extracted."""

    def test_toml_not_kept(self):
        self.assertNotIn(".toml", projects.KEEP_EXT)
        for ext in (".toml", ".cfg", ".ini", ".py", ".sh", ".command", ".yaml", ".yml"):
            self.assertNotIn(ext, projects.KEEP_EXT)

    def test_zip_with_tulda_toml(self):
        d = tmpdir()
        self.addCleanup(shutil.rmtree, d)
        src = write_zip(d / "thing_stls.zip", {"obj_1_a.stl": b"solid", "tulda.toml": b"[adapter.x]\n",
                                               "sub/tulda.toml": b"x", "config.toml": b"x"})
        (d / "dest").mkdir()
        state, proj = projects.create_project(src, d / "dest", force=True)
        self.assertEqual(state, "created")
        names = [p.name for p in proj.rglob("*")]
        self.assertFalse([n for n in names if n.endswith(".toml")], names)
        from tulda.adapters.registry import find_project_config
        self.assertIsNone(find_project_config(proj / "files" / "obj_1_a.stl"))
        import json
        skipped = json.loads((proj / "tulda-project.json").read_text())["skipped"]
        self.assertEqual(sorted(s["path"] for s in skipped), ["config.toml", "sub/tulda.toml", "tulda.toml"])


class ServerImportIsolation(unittest.TestCase):
    def test_server_never_imports_projects_or_runner(self):
        code = ("import sys, tulda.server; bad = [m for m in ('tulda.projects', 'tulda.runner', "
                "'tulda.watchd', 'tulda.watchconfig', 'tulda.service', 'tulda.manage') if m in sys.modules]; "
                "print(bad); sys.exit(1 if bad else 0)")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           cwd=str(Path(__file__).resolve().parent.parent))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
