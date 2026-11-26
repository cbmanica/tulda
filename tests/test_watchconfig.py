"""Watch config ([watch] / [[watch.dir]]), `tulda watch --from-config`, doctor checks."""

import errno
import io
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

from tests.test_projects import MW, write_3mf, write_zip
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
        self.inbox, self.dest = self.d / "inbox", self.d / "projects"
        self.inbox.mkdir()
        self.cfg = self.d / "config.toml"
        self.cfg.write_text(f'[watch]\nsettle = 0\n[[watch.dir]]\npath = "{self.inbox}"\n'
                            f'destination = "{self.dest}"\n')

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
        write_zip(self.inbox / "plain.zip", {"a.stl": b"solid"})
        write_3mf(self.inbox / "a.3mf")
        self.cfg.write_text(f'[watch]\nsettle = 0\n[[watch.dir]]\npath = "{self.inbox}"\n'
                            f'destination = "{self.dest}"\nforce = true\ninclude = ["*.ZIP"]\n')
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
