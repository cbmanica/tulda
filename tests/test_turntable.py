"""Turntable adapter (scad -> PNG frames), its planning, the page wiring and the widget script."""

import math
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from tulda import runner, viewer
from tulda.adapters import load_registry, plan_jobs
from tulda.adapters.api import Context
from tulda.adapters.turntable import camera_args, scad_turntable
from tulda.errors import NoAdapter
from tulda.server import STATIC_FILES, STATIC_ROOT

BBOX = [[-10, -5, 0], [10, 5, 4]]


class Planning(unittest.TestCase):
    def test_turntable_planned_for_scad_only(self):
        reg = load_registry()
        jobs = plan_jobs(reg, Path("a.scad"), views=["iso", "turntable"])
        self.assertEqual([(j.view, j.chain[-1].id) for j in jobs],
                         [("iso", "openscad"), ("turntable", "turntable-scad")])
        self.assertEqual(jobs[1].chain[-1].kind, "turntable")
        with self.assertRaises(NoAdapter):
            plan_jobs(reg, Path("a.3mf"), views=["turntable"])

    def test_default_options(self):
        self.assertEqual(load_registry().adapters["turntable-scad"].options["frames"], 36)


class Camera(unittest.TestCase):
    def cam(self, i, n=36):
        return camera_args(BBOX, i, n)[0].split("=")[1].split(",")

    def test_fixed_centre_and_distance_azimuth_steps(self):
        a, b, c = self.cam(0), self.cam(9), self.cam(35)
        self.assertEqual(a[:3], ["0.0000", "0.0000", "2.0000"])
        for k in (0, 1, 2, 3, 4, 6):
            self.assertEqual(a[k], b[k], k)          # only rz (index 5) changes
        self.assertEqual(float(a[5]), 25.0)
        self.assertEqual(float(b[5]), 115.0)
        self.assertAlmostEqual(float(c[5]), (25 + 350) % 360)
        self.assertGreater(float(a[6]), math.dist(*BBOX) / 2)

    def test_no_viewall(self):
        self.assertTrue(all("viewall" not in x for x in camera_args(BBOX, 0, 4)))


class Adapter(unittest.TestCase):
    def ctx(self, d, **opts):
        (d / "c.scad").write_text("cube([4,3,2]);\n")
        return Context(d / "c.scad", d / "o.png", opts, d)

    def test_bad_options(self):
        d = Path(tempfile.mkdtemp())
        self.assertFalse(scad_turntable(self.ctx(d, frames=1)).ok)
        self.assertFalse(scad_turntable(self.ctx(d, frames="x")).ok)
        self.assertFalse(scad_turntable(self.ctx(d, frames=100000)).ok)

    @unittest.skipUnless(shutil.which("openscad"), "openscad not installed")
    def test_renders_frames(self):
        d = Path(tempfile.mkdtemp())
        res = scad_turntable(self.ctx(d, frames=4, frame_width=80, frame_height=60))
        self.assertTrue(res.ok, res.message)
        self.assertEqual(res.meta["render_info"]["frames"], 4)
        fr = sorted((d / "frames").glob("f*.png"))
        self.assertEqual([p.name for p in fr], ["f000.png", "f001.png", "f002.png", "f003.png"])
        self.assertEqual(fr[0].read_bytes(), (d / "o.png").read_bytes())
        self.assertTrue(all(p.read_bytes()[:8] == runner.PNG_MAGIC for p in fr))
        self.assertNotEqual(fr[0].read_bytes(), fr[1].read_bytes())

    def test_publish_frames_requires_frames(self):
        d = Path(tempfile.mkdtemp())
        with self.assertRaises(Exception):
            runner.publish_frames(d, d / "x.frames")


class Widget(unittest.TestCase):
    def test_script_allowlisted_and_wired(self):
        self.assertIn("turntable.js", STATIC_FILES)
        self.assertTrue((STATIC_ROOT / "turntable.js").is_file())
        page = viewer.PAGE
        self.assertLess(page.index("/_s/turntable.js"), page.index("/_s/viewer.js"))
        js = (STATIC_ROOT / "turntable.js").read_text()
        for needle in ("pointerdown", "pointermove", "ArrowLeft", "tuldaTurntable"):
            self.assertIn(needle, js)
        self.assertNotRegex(js, r"https?://")

    def test_viewer_js_falls_back(self):
        js = (STATIC_ROOT / "viewer.js").read_text()
        self.assertIn("tuldaTurntable.show", js)
        self.assertIn("!D.glb && D.turntable", js)     # turntable-only renders skip WebGL

    def test_frame_url_pattern_matches_adapter_names(self):
        r = {"kind": "turntable", "project": "p", "file": "t.png", "url": "/p/t.png",
             "info": {"frames": 36, "frames_dir": "t.frames"}, "group_id": "g"}
        t = viewer.viewer_data(r)["turntable"]
        self.assertEqual(t["pattern"].replace("{i}", "%03d" % 35), "f035.png")
        self.assertEqual(t["frames"], 36)


if __name__ == "__main__":
    unittest.main()
