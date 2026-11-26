"""Project folders and the download watcher. All fixtures are synthetic, built in temp dirs."""

import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import unittest.mock
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from tulda import cli, projects
from tulda.projects import (NotMakerWorld, UnsafeArchive, Watcher, base_name, create_project,
                            detect, git_publish_warning, sanitize_name)

NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
CUBE = ('<object id="1" type="model"><mesh><vertices>'
        '<vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/>'
        '</vertices><triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object>')


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp()).resolve()


def threemf_bytes(meta: dict | None = None) -> bytes:
    md = "".join(f'<metadata name="{k}">{v}</metadata>' for k, v in (meta or {}).items())
    xml = (f'<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" xmlns="{NS}">{md}'
           f'<resources>{CUBE}</resources><build><item objectid="1"/></build></model>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("3D/3dmodel.model", xml)
    return buf.getvalue()


MW = {"Title": "Example Box (v2) [final]", "Designer": "example-designer",
      "License": "Standard Digital File License", "DesignModelId": "123"}


def write_3mf(path: Path, meta=MW) -> Path:
    path.write_bytes(threemf_bytes(meta))
    return path


def write_zip(path: Path, members: dict) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


class Naming(unittest.TestCase):
    def test_sanitize(self):
        self.assertEqual(sanitize_name('Box (v2) [final] {x} "q" \'s\' a/b\\c'), "Box-v2-final-x-q-s-a-b-c")
        self.assertEqual(sanitize_name("  many   spaces -- and---dashes  "), "many-spaces-and-dashes")
        self.assertEqual(sanitize_name("../../etc"), "etc")
        self.assertEqual(sanitize_name("a\x00b\nc"), "a-b-c")
        self.assertEqual(sanitize_name("!!!"), "")

    def test_unicode_kept(self):
        self.assertEqual(sanitize_name("收纳盒 (大)"), "收纳盒-大")
        self.assertEqual(sanitize_name("Café ＢＯＸ"), "Café-BOX")    # NFKC folds fullwidth

    def test_length_cap(self):
        self.assertLessEqual(len(sanitize_name("x" * 500)), projects.MAX_NAME)
        self.assertLessEqual(len(sanitize_name("盒" * 500).encode()), 200)

    def test_base_name_fallbacks(self):
        self.assertEqual(base_name(None, "Kitty(Designer)_stls"), "Kitty-Designer")
        self.assertEqual(base_name("???", "stem"), "stem")
        self.assertEqual(base_name(None, "()"), "project")


class Detection(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir()
        self.addCleanup(shutil.rmtree, self.d)

    def test_3mf(self):
        self.assertTrue(detect(write_3mf(self.d / "a.3mf"))[0])
        self.assertFalse(detect(write_3mf(self.d / "b.3mf", {"Application": "BambuStudio-01"}))[0])
        (self.d / "c.3mf").write_bytes(b"junk")
        self.assertFalse(detect(self.d / "c.3mf")[0])

    def test_zip_with_mw_3mf(self):
        z = write_zip(self.d / "x.zip", {"m/a.3mf": threemf_bytes(MW), "readme.txt": "hi"})
        self.assertTrue(detect(z)[0])

    def test_zip_stl_layout(self):
        stl = b"solid x\nendsolid x\n"
        self.assertTrue(detect(write_zip(self.d / "Thing(maker)_stls.zip", {"a.stl": stl}))[0])
        self.assertTrue(detect(write_zip(self.d / "plain.zip", {"obj_1_A.stl": stl, "obj_2_B.stl": stl}))[0])
        # wrong layout or non-model members: not detected
        self.assertFalse(detect(write_zip(self.d / "plain2.zip", {"a.stl": stl}))[0])
        self.assertFalse(detect(write_zip(self.d / "App_stls.zip", {"a.stl": stl, "x.dylib": b"x"}))[0])
        self.assertFalse(detect(write_zip(self.d / "app.zip", {"App.app/Contents/x": b"x"}))[0])

    def test_force(self):
        src = write_zip(self.d / "misc.zip", {"a.stl": b"solid"})
        with self.assertRaises(NotMakerWorld):
            create_project(src, self.d / "out")
        state, proj = create_project(src, self.d / "out", force=True)
        self.assertEqual(state, "created")
        self.assertTrue(json.loads((proj / projects.MANIFEST).read_text())["detection"]["forced"])


class Create(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir()
        self.addCleanup(shutil.rmtree, self.d)
        self.dest = self.d / "projects"

    def test_3mf_project(self):
        src = write_3mf(self.d / "dl.3mf")
        before = src.read_bytes()
        now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        state, proj = create_project(src, self.dest, now=now)
        self.assertEqual((state, proj.name), ("created", "Example-Box-v2-final"))
        self.assertEqual(src.read_bytes(), before)               # original untouched
        self.assertEqual((proj / "source" / "dl.3mf").read_bytes(), before)
        m = json.loads((proj / projects.MANIFEST).read_text())
        self.assertEqual(m["source"]["filename"], "dl.3mf")
        self.assertEqual(len(m["source"]["sha256"]), 64)
        self.assertEqual(m["created_utc"], "2026-01-02T03:04:05Z")
        self.assertEqual(m["models"][0]["provenance"]["design_model_id"], "123")
        note = (proj / projects.LICENCE_NOTE).read_text()
        self.assertIn("Standard Digital File License", note)
        self.assertIn("authoritative", note)
        self.assertIn("Do not redistribute", note)
        self.assertEqual([p.name for p in self.dest.iterdir()], [proj.name])   # no staging left

    def test_unknown_licence_note(self):
        src = write_zip(self.d / "T_stls.zip", {"a.stl": b"solid"})
        _, proj = create_project(src, self.dest)
        self.assertIn("unknown", (proj / projects.LICENCE_NOTE).read_text())
        self.assertEqual(proj.name, "T")

    def test_idempotent_and_collisions(self):
        a = write_3mf(self.d / "a.3mf")
        s1, p1 = create_project(a, self.dest)
        s2, p2 = create_project(a, self.dest)
        self.assertEqual((s1, s2, p1), ("created", "exists", p2))
        b = write_3mf(self.d / "b.3mf", {**MW, "DesignProfileId": "9"})   # same title, other bytes
        c = write_3mf(self.d / "c.3mf", {**MW, "DesignProfileId": "10"})
        self.assertEqual(create_project(b, self.dest)[1].name, "Example-Box-v2-final-2")
        self.assertEqual(create_project(c, self.dest)[1].name, "Example-Box-v2-final-3")
        (self.dest / "other").mkdir()
        self.assertEqual(projects.unique_dir(self.dest, "OTHER").name, "OTHER-2")   # case-insensitive

    def test_zip_extract_filters_and_nested(self):
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as z:
            z.writestr("deep.stl", b"solid")
            z.writestr("deeper.zip", b"PK")
        src = write_zip(self.d / "pack.zip", {
            "models/a.3mf": threemf_bytes(MW), "models/b.stl": b"solid", "doc/read me.pdf": b"%PDF",
            "tool.exe": b"MZ", "__MACOSX/._a": b"", "extras.zip": inner.getvalue(), "dir/": b""})
        _, proj = create_project(src, self.dest)
        m = json.loads((proj / projects.MANIFEST).read_text())
        self.assertIn("files/models/a.3mf", m["files"])
        self.assertIn("files/extras/deep.stl", m["files"])
        self.assertTrue((proj / "files" / "doc" / "read me.pdf").is_file())
        self.assertFalse((proj / "files" / "tool.exe").exists())
        reasons = {s["path"]: s["reason"] for s in m["skipped"]}
        self.assertIn("tool.exe", reasons)
        self.assertIn("nested zip", reasons["extras/deeper.zip"])
        self.assertEqual(proj.name, "Example-Box-v2-final")
        self.assertEqual(m["models"][0]["path"], "files/models/a.3mf")


class UnsafeZips(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir()
        self.addCleanup(shutil.rmtree, self.d)
        self.dest = self.d / "projects"

    def _assert_refused(self, src):
        with self.assertRaises(UnsafeArchive):
            create_project(src, self.dest, force=True)
        self.assertFalse(any(self.dest.iterdir()))       # staging cleaned, nothing written
        self.assertFalse((self.d / "evil.stl").exists())

    def test_zip_slip(self):
        self._assert_refused(write_zip(self.d / "s1.zip", {"../evil.stl": b"x"}))
        self._assert_refused(write_zip(self.d / "s2.zip", {"/abs/evil.stl": b"x"}))
        self._assert_refused(write_zip(self.d / "s3.zip", {"a\\..\\..\\evil.stl": b"x"}))
        self._assert_refused(write_zip(self.d / "s4.zip", {"C:/evil.stl": b"x"}))

    def test_symlink(self):
        src = self.d / "link.zip"
        with zipfile.ZipFile(src, "w") as z:
            info = zipfile.ZipInfo("link.stl")
            info.external_attr = (0o120777 << 16)
            z.writestr(info, "/etc/passwd")
        self._assert_refused(src)

    def test_too_many_files(self):
        src = write_zip(self.d / "many.zip", {f"{i}.stl": b"" for i in range(5)})
        with unittest.mock.patch.object(projects, "MAX_FILES", 3):
            self._assert_refused(src)

    def test_bomb_ratio_and_total(self):
        src = write_zip(self.d / "bomb.zip", {"big.stl": b"\0" * (2 * 1024 * 1024)})
        with unittest.mock.patch.object(projects, "RATIO_MIN", 1024):
            self._assert_refused(src)
        with unittest.mock.patch.object(projects.Budget.__init__, "__defaults__", (projects.MAX_FILES, 1000)):
            self._assert_refused(src)

    def test_lying_size_header(self):
        src = write_zip(self.d / "lie.zip", {"a.stl": b"A" * 5000})
        data = bytearray(src.read_bytes())
        # shrink the declared uncompressed size in the central directory to 10 bytes
        cd = data.rfind(b"PK\x01\x02")
        data[cd + 24:cd + 28] = (10).to_bytes(4, "little")
        src.write_bytes(bytes(data))
        with self.assertRaises(projects.ProjectError):
            create_project(src, self.dest, force=True)
        self.assertFalse(any(self.dest.iterdir()))

    def test_detect_never_raises_on_unsafe(self):
        ok, reason = detect(write_zip(self.d / "e_stls.zip", {"../x.stl": b"x"}))
        self.assertFalse(ok)
        self.assertIn("unsafe", reason)


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir()
        self.addCleanup(shutil.rmtree, self.d)
        self.inbox = self.d / "Downloads"
        self.inbox.mkdir()
        self.dest = self.d / "projects"
        self.logs = []
        self.now = time.time() + 100
        self.w = Watcher(self.inbox, self.dest, settle=5, log=self.logs.append, clock=lambda: self.now)

    def test_stability_and_partials(self):
        a = write_3mf(self.inbox / "a.3mf")
        write_3mf(self.inbox / "b.3mf", {**MW, "Title": "Other"})
        (self.inbox / "b.3mf.part").write_bytes(b"x")            # Firefox still downloading
        write_3mf(self.inbox / "c.3mf.crdownload")
        (self.inbox / "empty.zip").write_bytes(b"")
        c = self.w.poll()                                         # first sight: not yet stable
        self.assertEqual(c["created"], 0)
        c = self.w.poll()
        self.assertEqual(c["created"], 1)
        self.assertTrue(any(line.startswith("created Example-Box") for line in self.logs))
        self.assertTrue(a.exists())                               # original kept
        c = self.w.poll()                                         # nothing new
        self.assertEqual((c["created"], c["exists"]), (0, 0))
        (self.inbox / "b.3mf.part").unlink()
        self.w.poll()
        self.w.poll()
        self.assertEqual(len(list(self.dest.iterdir())), 2)

    def test_growing_file_waits(self):
        f = self.inbox / "g.3mf"
        f.write_bytes(threemf_bytes(MW)[:100])
        self.w.poll()
        f.write_bytes(threemf_bytes(MW))
        c = self.w.poll()
        self.assertEqual(c["created"], 0)
        self.assertGreaterEqual(c["waiting"], 1)

    def test_young_file_waits(self):
        write_3mf(self.inbox / "a.3mf")
        self.w.clock = time.time
        self.assertEqual(self.w.run(once=True)["waiting"], 1)

    def test_once_skips_and_errors_per_file(self):
        write_3mf(self.inbox / "good.3mf")
        write_zip(self.inbox / "SomeApp.zip", {"App.app/x": b"x"})
        (self.inbox / "bad.3mf").write_bytes(b"not a zip")
        write_zip(self.inbox / "evil_stls.zip", {"../x.stl": b"x"})
        (self.inbox / "notes.txt").write_text("x")
        c = self.w.run(once=True)
        self.assertEqual((c["created"], c["skipped"], c["errors"]), (1, 3, 0))
        self.assertEqual(sorted(p.name for p in self.inbox.iterdir()),
                         ["SomeApp.zip", "bad.3mf", "evil_stls.zip", "good.3mf", "notes.txt"])
        w2 = Watcher(self.inbox, self.dest, settle=5, log=self.logs.append, clock=lambda: self.now)
        self.assertEqual(w2.run(once=True)["exists"], 1)          # idempotent across runs

    def test_error_does_not_crash(self):
        write_3mf(self.inbox / "a.3mf")
        with unittest.mock.patch.object(projects, "create_project", side_effect=RuntimeError("boom")):
            c = self.w.run(once=True)
        self.assertEqual(c["errors"], 1)
        self.assertIn("error a.3mf: boom", self.logs)

    def test_cli_once(self):
        write_3mf(self.inbox / "a.3mf")
        os.utime(self.inbox / "a.3mf", (time.time() - 60, time.time() - 60))
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = cli.main(["watch", str(self.inbox), "--destination", str(self.dest), "--once"])
        self.assertEqual(code, 0)
        self.assertIn("summary: created 1", out.getvalue())

    def test_cli_create_render_off_by_default(self):
        src = write_3mf(self.d / "a.3mf")
        with unittest.mock.patch.object(projects, "render_project") as rp, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["project", "create", str(src), "-d", str(self.dest)]), 0)
            rp.assert_not_called()
            src2 = write_3mf(self.d / "b.3mf", {**MW, "DesignProfileId": "2"})
            rp.return_value = 0
            cli.main(["project", "create", str(src2), "-d", str(self.dest), "--render"])
            rp.assert_called_once()


@unittest.skipUnless(shutil.which("git"), "git not installed")
class GitWarning(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir()
        self.addCleanup(shutil.rmtree, self.d)

    def git(self, *a):
        subprocess.run(["git", "-C", str(self.d), *a], check=True, capture_output=True)

    def test_warning(self):
        self.assertIsNone(git_publish_warning(self.d / "projects"))   # not a repo
        self.git("init", "-q")
        self.assertIn("no remote yet", git_publish_warning(self.d / "projects"))
        self.git("remote", "add", "origin", "https://git.example/x.git")
        w = git_publish_warning(self.d / "projects")
        self.assertIn("with a remote", w)
        self.assertIn("/projects/", w)
        self.assertFalse((self.d / ".gitignore").exists())            # never edits the repo
        (self.d / ".gitignore").write_text("/projects/\n")
        self.assertIsNone(git_publish_warning(self.d / "projects"))


class NoNetwork(unittest.TestCase):
    def test_module_imports_nothing_networky(self):
        import ast
        tree = ast.parse(Path(projects.__file__).read_text())
        names = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                names |= {a.name for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module:
                names.add(n.module)
        for bad in ("socket", "urllib", "urllib.request", "http", "http.client", "ssl", "requests"):
            self.assertNotIn(bad, names)


if __name__ == "__main__":
    unittest.main()
