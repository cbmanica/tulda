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
        # a readable 3mf qualifies with or without MakerWorld metadata
        self.assertEqual(detect(write_3mf(self.d / "b.3mf", {"Application": "BambuStudio-01"})),
                         (True, "readable 3mf"))
        self.assertTrue(detect(write_3mf(self.d / "c.3mf", {"Title": "", "Designer": ""}))[0])
        (self.d / "junk.3mf").write_bytes(b"junk")
        ok, reason = detect(self.d / "junk.3mf")
        self.assertFalse(ok)
        self.assertTrue(reason.startswith("not a readable 3mf"), reason)
        self.assertNotIn("not a readable 3mf: not a readable 3mf", reason)
        zip_no_model = write_zip(self.d / "nomodel.3mf", {"readme.txt": "x"})
        self.assertFalse(detect(zip_no_model)[0])

    def test_bare_stl_not_detected(self):
        (self.d / "part.stl").write_bytes(b"solid x\nendsolid x\n")
        self.assertEqual(detect(self.d / "part.stl"), (False, "not a .3mf or .zip"))

    def test_zip_with_3mf_without_metadata(self):
        # e.g. a Studio project with every MakerWorld field empty except an unrecorded one
        bare = threemf_bytes({"Title": "", "Designer": "", "License": "", "DesignerUserId": "1"})
        ok, reason = detect(write_zip(self.d / "x.zip", {"m/a.3mf": bare}))
        self.assertTrue(ok)
        self.assertEqual(reason, "zip with 1 model file(s) (.3mf)")
        z = write_zip(self.d / "y.zip", {"m/a.3mf": threemf_bytes(MW), "readme.txt": "hi"})
        self.assertEqual(detect(z), (True, "zip with 1 model file(s) (.3mf) and 1 companion file(s)"))

    def test_zip_stl_layout(self):
        stl = b"solid x\nendsolid x\n"
        # the zip's name no longer matters, only its listing
        for name in ("Thing(maker)_stls.zip", "plain.zip", "plain2.zip", "Thing_stls_v2.zip",
                     "Thing_stls (x).zip", "Cable+Tidy+Box+-+No+Supports.zip"):
            with self.subTest(name):
                self.assertTrue(detect(write_zip(self.d / name, {"a.stl": stl}))[0])
        ok, reason = detect(write_zip(self.d / "App_stls.zip", {"a.stl": stl, "x.dylib": b"x"}))
        self.assertFalse(ok)
        self.assertIn("neither models nor", reason)
        self.assertIn("x.dylib", reason)
        self.assertFalse(detect(write_zip(self.d / "app.zip", {"App.app/Contents/x": b"x"}))[0])

    def test_zip_stl_layout_variants(self):
        stl = b"solid x\nendsolid x\n"
        yes = {
            "Thing_stls (1).zip": {"a.stl": stl},
            "Thing_STEPS.zip": {"a.step": b"ISO"},
            "T_stls.zip": {"a.stl": stl, "README.md": "x", "LICENSE.txt": "x", "pic.jpg": b"x"},
            "objs.zip": {"obj_1_A.obj": b"v", "obj_1_A.mtl": b"m", "notes.txt": "x"},
            "nest.zip": {"Thing/obj_1_A.stl": stl, "Thing/obj_2_B.stl": stl},
            "mixed.zip": {"obj_1_A.stl": stl, "b.STL": stl, "c.stp": b"ISO", "manual.pdf": b"%PDF"},
            "mac.zip": {"a.3mf": threemf_bytes(MW), "__MACOSX/._a.3mf": b"x", ".DS_Store": b"x"},
        }
        for name, members in yes.items():
            with self.subTest(name):
                self.assertTrue(detect(write_zip(self.d / name, members))[0])
        no = {
            "only_docs_stls.zip": ({"README.md": "x", "pic.png": b"x"}, "no model file"),
            "T_stls.zip.zip": ({"a.stl": stl, "run.sh": b"x"}, "run.sh"),
            "T2_stls.zip": ({"a.stl": stl, "x.exe": b"x"}, "x.exe"),
            "py.zip": ({"a.stl": stl, "tool.py": b"x"}, "tool.py"),
            "noext.zip": ({"a.stl": stl, "bin/thing": b"x"}, "bin/thing"),
            "nested.zip": ({"a.stl": stl, "more.zip": b"PK"}, "more.zip"),
            "many.zip": ({"a.stl": stl, **{f"x{i}.so": b"x" for i in range(5)}}, "(+2 more)"),
            "empty.zip": ({}, "zip is empty"),
        }
        for name, (members, why) in no.items():
            with self.subTest(name):
                ok, reason = detect(write_zip(self.d / name, members))
                self.assertFalse(ok)
                self.assertIn(why, reason)

    def test_base_name_strips_dup_suffix(self):
        self.assertEqual(base_name(None, "Kitty(Designer)_stls (1)"), "Kitty-Designer")
        self.assertEqual(base_name(None, "Kitty_steps-3"), "Kitty")

    def test_plus_separated_name(self):
        self.assertEqual(base_name(None, "Cable+Tidy+Box+-+No+Supports"),
                         "Cable-Tidy-Box-No-Supports")
        self.assertEqual(base_name("", "A+B++C"), "A-B-C")

    def test_zip_named_from_stem_when_title_empty(self):
        bare = threemf_bytes({"Title": "", "Designer": "", "DesignerUserId": "1"})
        src = write_zip(self.d / "Tidy+Box+-+No+Supports.zip", {"Tidy Box.3mf": bare})
        state, proj = create_project(src, self.d / "out")
        self.assertEqual((state, proj.name), ("created", "Tidy-Box-No-Supports"))
        det = json.loads((proj / projects.MANIFEST).read_text())["detection"]
        self.assertEqual((det["qualifies"], det["forced"], det["makerworld_metadata"]), (True, False, False))
        self.assertTrue((proj / "files" / "Tidy Box.3mf").is_file())

    def test_force(self):
        src = write_zip(self.d / "misc.zip", {"a.stl": b"solid", "setup.sh": b"echo"})
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
        self.assertEqual([p.name for p in self.dest.iterdir() if p.name != projects.LOCK_NAME],
                         [proj.name])                             # no staging left

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
        with self.assertRaises(NotMakerWorld):         # .exe and a nested zip: detection says no
            create_project(src, self.dest)
        _, proj = create_project(src, self.dest, force=True)
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
        self.assertEqual(len([p for p in self.dest.iterdir() if not p.name.startswith(".")]), 2)

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


def threemf_with_config(cfg, name="Metadata/project_settings.config", meta=MW) -> bytes:
    buf = io.BytesIO(threemf_bytes(meta))
    with zipfile.ZipFile(buf, "a") as z:
        z.writestr(name, cfg if isinstance(cfg, (bytes, str)) else json.dumps(cfg))
    return buf.getvalue()


PP = {"layer_height": "0.2", "post_process": ["/usr/bin/env python3 /opt/example/fix.py"]}


class PostProcessHold(unittest.TestCase):
    """A 3mf carrying a post-processing script is never imported without explicit approval."""

    def setUp(self):
        self.d = tmpdir()
        self.addCleanup(shutil.rmtree, self.d)
        self.dest = self.d / "out"

    def projects_in_dest(self):
        return [p.name for p in self.dest.iterdir() if not p.name.startswith(".")] \
            if self.dest.exists() else []

    def assert_nothing_created(self):
        self.assertEqual(self.projects_in_dest(), [])
        if self.dest.exists():
            self.assertEqual([p for p in self.dest.iterdir() if p.name.startswith(projects.STAGING_PREFIX)], [])

    def test_bare_3mf_held_force_does_not_bypass(self):
        src = self.d / "a.3mf"
        src.write_bytes(threemf_with_config(PP))
        for force in (False, True):
            with self.assertRaises(projects.PostProcessHeld) as cm:
                create_project(src, self.dest, force=force)
            self.assertIn("post-processing script found in Metadata/project_settings.config: "
                          "/usr/bin/env python3 /opt/example/fix.py", str(cm.exception))
            self.assert_nothing_created()

    def test_approved_import_records_script_and_keeps_source(self):
        src = self.d / "a.3mf"
        src.write_bytes(threemf_with_config(PP))
        state, proj = create_project(src, self.dest, allow_post_process=True)
        self.assertEqual(state, "created")
        self.assertEqual((proj / "source" / "a.3mf").read_bytes(), src.read_bytes())
        pp = json.loads((proj / projects.MANIFEST).read_text())["post_process"]
        self.assertEqual((pp["present"], pp["approved"]), (True, True))
        self.assertEqual(pp["findings"][0]["command"], PP["post_process"][0])
        self.assertEqual(pp["findings"][0]["config"], "Metadata/project_settings.config")

    def test_empty_post_process_not_held(self):
        for cfg in ({"post_process": []}, {"post_process": ["", "  "]}, {"post_process": ""}):
            with self.subTest(cfg):
                src = self.d / "e.3mf"
                src.write_bytes(threemf_with_config(cfg, meta={**MW, "Title": json.dumps(cfg)}))
                state, proj = create_project(src, self.dest)
                self.assertEqual(state, "created")
                pp = json.loads((proj / projects.MANIFEST).read_text())["post_process"]
                self.assertEqual((pp["present"], pp["approved"], pp["findings"]), (False, False, []))

    def test_in_zip(self):
        src = write_zip(self.d / "Thing.zip", {"models/a.3mf": threemf_with_config(PP), "readme.txt": "x"})
        with self.assertRaises(projects.PostProcessHeld) as cm:
            create_project(src, self.dest)
        self.assertIn("models/a.3mf:Metadata/project_settings.config", str(cm.exception))
        self.assert_nothing_created()

    def test_in_nested_zip(self):
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as z:
            z.writestr("deep.3mf", threemf_with_config(PP))
        src = write_zip(self.d / "pack.zip", {"a.stl": b"solid", "inner.zip": inner.getvalue()})
        with self.assertRaises(NotMakerWorld):                # nested zip: detection says no
            create_project(src, self.dest)
        with self.assertRaises(projects.PostProcessHeld) as cm:
            create_project(src, self.dest, force=True)
        self.assertIn("inner/deep.3mf", str(cm.exception))
        self.assert_nothing_created()
        state, proj = create_project(src, self.dest, force=True, allow_post_process=True)
        self.assertEqual(state, "created")

    def test_other_formats(self):
        cases = {
            "xml": ('<config><object id="1"><metadata key="post_process" value="/bin/sh x.sh"/>'
                    '</object></config>', "Metadata/model_settings.config", "/bin/sh x.sh"),
            "ini": ("; layer_height = 0.2\n; post_process = \"/usr/local/bin/tool\"\n",
                    "Metadata/Slic3r_PE.config", "/usr/local/bin/tool"),
            "process json": ({"inherits": "x", "post_process": "perl -e 1"},
                             "Metadata/process_settings_1.config", "perl -e 1"),
        }
        for label, (cfg, name, cmd) in cases.items():
            with self.subTest(label):
                src = self.d / f"{label.replace(' ', '_')}.3mf"
                src.write_bytes(threemf_with_config(cfg, name))
                with self.assertRaises(projects.PostProcessHeld) as cm:
                    create_project(src, self.dest)
                self.assertEqual(cm.exception.findings[0]["command"], cmd)
                self.assertEqual(cm.exception.findings[0]["config"], name)

    def test_xml_empty_value_not_held(self):
        src = self.d / "x.3mf"
        src.write_bytes(threemf_with_config('<config><metadata key="post_process" value=""/></config>',
                                            "Metadata/model_settings.config"))
        self.assertEqual(create_project(src, self.dest)[0], "created")

    def test_control_chars_escaped_and_truncated(self):
        evil = "rm -rf ~\x1b[2K\rcreated Fake <- x.3mf\n" + "A" * 500
        msg = projects.held_summary([{"file": None, "config": "c", "key": "post_process", "command": evil}])
        self.assertNotIn("\x1b", msg)
        self.assertNotIn("\r", msg)
        self.assertNotIn("\n", msg)
        self.assertIn("\\x1b[2K\\x0dcreated Fake", msg)
        self.assertLess(len(msg), 300)

    def test_watcher_holds_once_and_records_event(self):
        inbox = self.d / "inbox"
        inbox.mkdir()
        logs, events = [], []
        now = time.time() + 100
        w = Watcher(inbox, self.dest, settle=5, log=logs.append, clock=lambda: now,
                    force=True, events=lambda *a, **k: events.append((a, k)))
        src = inbox / "Thing.3mf"
        src.write_bytes(threemf_with_config(PP))
        for _ in range(3):
            w.poll()
        os.utime(src, (time.time() - 30, time.time() - 30))   # touched: same content, new key
        for _ in range(3):
            c = w.poll()
        held = [m for m in logs if m.startswith("held ")]
        self.assertEqual(len(held), 1, logs)
        self.assertTrue(held[0].startswith("held Thing.3mf: post-processing script found in "
                                           "Metadata/project_settings.config: /usr/bin/env python3"))
        self.assertIn("--allow-post-process", held[0])
        self.assert_nothing_created()
        (args, extra), = events
        self.assertEqual(args[1:3], ("Thing.3mf", "held"))
        self.assertIn("/usr/bin/env python3 /opt/example/fix.py", extra["script"])
        self.assertIn("tulda project create", extra["approve"])
        self.assertIn("--allow-post-process", extra["approve"])

    def test_cli_exit_codes(self):
        src = self.d / "a.3mf"
        src.write_bytes(threemf_with_config(PP))
        for extra, want in ((["--force"], 3), ([], 3), (["--allow-post-process"], 0)):
            err, out = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(["project", "create", str(src), "-d", str(self.dest), *extra])
            self.assertEqual(code, want, err.getvalue())
            if want:
                self.assertIn("post-processing script found", err.getvalue())
                self.assertIn("--allow-post-process", err.getvalue())
                self.assertIn("nothing was imported", err.getvalue())
                self.assert_nothing_created()
            else:
                self.assertIn("approved", out.getvalue())

    def test_info_shows_script(self):
        src = self.d / "a.3mf"
        src.write_bytes(threemf_with_config(PP))
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            cli.main(["info", str(src)])
        self.assertIn("WARNING: post-processing script in Metadata/project_settings.config", out.getvalue())
        self.assertIn("/opt/example/fix.py", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(["info", "--json", str(src)])
        self.assertEqual(json.loads(out.getvalue())["post_process"][0]["key"], "post_process")


class BacklogBaseline(unittest.TestCase):
    """backlog = "ignore": files already in a folder when it is first watched are never imported."""

    def setUp(self):
        self.d = tmpdir()
        self.addCleanup(shutil.rmtree, self.d)
        self.inbox, self.dest, self.sdir = self.d / "Downloads", self.d / "projects", self.d / "state"
        self.inbox.mkdir()
        self.logs = []
        self.now = time.time() + 100

    def watcher(self, adopt=False):
        bl = projects.Baseline(self.sdir, self.inbox, adopt=adopt, log=self.logs.append)
        return Watcher(self.inbox, self.dest, settle=5, log=self.logs.append,
                       clock=lambda: self.now, baseline=bl)

    def test_backlog_ignored_new_files_imported(self):
        write_3mf(self.inbox / "old.3mf")
        write_zip(self.inbox / "old_stls.zip", {"a.stl": b"solid"})
        (self.inbox / "own-part.stl").write_bytes(b"solid")
        w = self.watcher()
        for _ in range(3):
            c = w.poll()
            self.assertEqual((c["created"], c["skipped"]), (0, 0))
        self.assertFalse(self.dest.exists())
        self.assertTrue(any("ignoring 3 file(s) already there" in m for m in self.logs))
        bl = projects.baseline_file(self.sdir, self.inbox)
        self.assertEqual(oct(bl.stat().st_mode & 0o777), "0o600")
        self.assertEqual(sorted(json.loads(bl.read_text())["files"]),
                         ["old.3mf", "old_stls.zip", "own-part.stl"])
        write_3mf(self.inbox / "new.3mf", {**MW, "Title": "Fresh"})
        w.poll()
        w.poll()
        self.assertIn("created Fresh <- new.3mf", self.logs)
        # a restart reads the same baseline: the backlog stays ignored
        w2 = self.watcher()
        w2.poll()
        c = w2.poll()
        self.assertEqual(c["created"], 0)
        self.assertEqual(c["exists"], 1)                      # new.3mf seen again, not old ones

    def test_old_mtime_and_partial_rename_count_as_new(self):
        (self.inbox / "x.zip.partial").write_bytes(b"partial")
        w = self.watcher()
        w.poll()
        # Taildrop style: finished by renaming the partial file, with an old mtime
        part = self.inbox / "x.zip.partial"
        write_zip(part, {"a.stl": b"solid"})
        os.utime(part, (time.time() - 10 * 86400, time.time() - 10 * 86400))
        part.rename(self.inbox / "x.zip")
        w.poll()
        w.poll()
        self.assertTrue(any(m.startswith("created x <- x.zip") for m in self.logs), self.logs)

    def test_replaced_file_same_name_counts_as_new(self):
        write_3mf(self.inbox / "a.3mf")
        w = self.watcher()
        w.poll()
        tmp = self.d / "a.3mf"
        write_3mf(tmp, {**MW, "Title": "Replaced"})
        os.replace(tmp, self.inbox / "a.3mf")             # new inode under the old name
        w.poll()
        w.poll()
        self.assertIn("created Replaced <- a.3mf", self.logs)

    def test_removed_entries_are_pruned(self):
        write_3mf(self.inbox / "a.3mf")
        w = self.watcher()
        w.poll()
        (self.inbox / "a.3mf").unlink()
        w.poll()
        self.assertEqual(json.loads(projects.baseline_file(self.sdir, self.inbox).read_text())["files"], {})

    def test_adopted_folder_keeps_old_behaviour(self):
        write_3mf(self.inbox / "a.3mf")
        w = self.watcher(adopt=True)
        w.poll()
        w.poll()
        self.assertTrue(any(m.startswith("created Example-Box") for m in self.logs))
        self.assertTrue(any("watched before baselines existed" in m for m in self.logs))

    def test_unlistable_folder_records_nothing(self):
        w = self.watcher()
        shutil.rmtree(self.inbox)
        w.poll()
        self.assertFalse(projects.baseline_file(self.sdir, self.inbox).exists())

    def test_corrupt_baseline_is_rerecorded_not_imported(self):
        write_3mf(self.inbox / "a.3mf")
        bl = projects.baseline_file(self.sdir, self.inbox)
        bl.parent.mkdir(parents=True)
        bl.write_text("{not json")
        w = self.watcher()
        w.poll()
        w.poll()
        self.assertFalse(self.dest.exists())
        self.assertTrue(any("unreadable" in m for m in self.logs))

    def test_events_recorded(self):
        events = []
        w = self.watcher()
        w.events = lambda *a: events.append(a)
        write_3mf(self.inbox / "old.3mf")
        w2 = Watcher(self.inbox, self.dest, settle=5, log=self.logs.append, clock=lambda: self.now,
                     events=lambda *a: events.append(a))
        w.poll()                                               # baseline: old.3mf ignored
        write_zip(self.inbox / "app.zip", {"run.sh": b"x"})
        (self.inbox / "bad.3mf").write_bytes(b"junk")
        w.poll()
        w.poll()
        self.assertEqual(events[0][:3], ("Downloads", "", "backlog"))
        self.assertIn(("Downloads", "app.zip", "skipped"), [e[:3] for e in events])
        self.assertIn(("Downloads", "bad.3mf", "skipped"), [e[:3] for e in events])
        self.assertNotIn("old.3mf", [e[1] for e in events])
        del w2

    def test_skip_and_create_each_log_one_line(self):
        w = self.watcher()
        w.poll()                                               # empty baseline
        write_zip(self.inbox / "SomeApp.zip", {"App.app/Contents/MacOS/App": b"x"})
        write_zip(self.inbox / "Tidy+Box.zip", {"Box.3mf": threemf_bytes({"Title": ""})})
        for _ in range(4):
            w.poll()
        self.assertEqual([m for m in self.logs if "SomeApp.zip" in m],
                         ["skip SomeApp.zip: zip contains 1 file(s) that are neither models nor "
                          "readme/image/PDF companions: App.app/Contents/MacOS/App"])
        self.assertEqual([m for m in self.logs if "Tidy+Box.zip" in m],
                         ["created Tidy-Box <- Tidy+Box.zip"])


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


class NewProject(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir()
        self.addCleanup(shutil.rmtree, self.d)
        self.dest = self.d / "designs"

    def test_contents(self):
        proj = projects.new_project("Cable Clip (v2)", self.dest,
                                    now=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
        self.assertEqual(proj.name, "Cable-Clip-v2")
        self.assertEqual(sorted(p.name for p in proj.iterdir()),
                         ["README.md", "cable_clip_v2.scad", "tulda-project.json"])
        m = json.loads((proj / "tulda-project.json").read_text())
        self.assertEqual((m["origin"], m["licence"], m["title"]), ("own", "", "Cable Clip (v2)"))
        self.assertEqual(m["files"], ["cable_clip_v2.scad", "README.md"])
        for k in ("source", "models", "detection", "provenance"):
            self.assertNotIn(k, m)
        scad = (proj / "cable_clip_v2.scad").read_text()
        self.assertTrue(scad.startswith("// cable_clip_v2.scad - Cable Clip (v2)"))
        for want in ('part = "main";', "module main()", 'if (part == "main") main();', "// ---- parameters"):
            self.assertIn(want, scad)
        self.assertEqual(scad.count("{"), scad.count("}"))
        self.assertIn("`cable_clip_v2.scad`", (proj / "README.md").read_text())
        self.assertFalse(list(self.dest.glob(projects.STAGING_PREFIX + "*")))
        self.assertIsNone(projects.find_existing(self.dest, "0" * 64))   # no source: never matches

    def test_collisions_and_unicode(self):
        a = projects.new_project("Box", self.dest)
        b = projects.new_project("box", self.dest)
        self.assertEqual((a.name, b.name), ("Box", "box-2"))
        self.assertTrue((b / "box_2.scad").is_file())
        c = projects.new_project("收纳盒", self.dest)
        self.assertEqual(c.name, "收纳盒")
        with self.assertRaises(projects.ProjectError):
            projects.new_project("!!!", self.dest)

    def test_cli_default_destination_from_config(self):
        cfg = self.d / "config.toml"
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertNotEqual(cli.main(["project", "new", "x", "--config", str(cfg)]), 0)
        self.assertIn("default_destination", err.getvalue())
        cfg.write_text(f'[projects]\ndefault_destination = "{self.dest}"\n')
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(cli.main(["project", "new", "Hook", "--config", str(cfg)]), 0)
        self.assertTrue((self.dest / "Hook" / "hook.scad").is_file())
        cfg.write_text('[projects]\ndefault_destination = "relative"\n')
        with redirect_stdout(out), redirect_stderr(err):
            self.assertNotEqual(cli.main(["project", "new", "y", "--config", str(cfg)]), 0)
        other = self.d / "other"
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(cli.main(["project", "new", "Hook", "-d", str(other), "--config", str(cfg)]), 0)
        self.assertTrue((other / "Hook").is_dir())

    def test_scad_renders_with_openscad(self):
        exe = shutil.which("openscad") or ("/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD"
                                           if os.path.exists("/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD") else None)
        if not exe:
            self.skipTest("OpenSCAD not installed")
        proj = projects.new_project("Probe", self.dest)
        r = subprocess.run([exe, "-o", str(self.d / "p.stl"), str(proj / "probe.scad")],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertGreater((self.d / "p.stl").stat().st_size, 0)
