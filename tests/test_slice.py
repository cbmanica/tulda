import json
import os
import tempfile
import unittest
import unittest.mock
import zipfile
from pathlib import Path

from tulda import slicesummary as ss
from tulda.adapters import load_registry
from tulda.adapters.api import Context
from tulda.adapters.selection import plan_special
from tulda.adapters.slicing import DEFAULT_BIN, bambu_slice
from tulda.items import read_item

GCODE = """\
; HEADER_BLOCK_START
; BambuStudio 02.09.01.70
; model printing time: 22m 54s; total estimated time: 29m 56s
; total layer number: 2
; max_z_height: 0.40
; HEADER_BLOCK_END

; CONFIG_BLOCK_START
; enable_support = {sup}
; support_type = tree(auto)
; brim_type = outer_only
; skirt_loops = 0
; layer_height = 0.2
; change_filament_gcode = ;ignored = value
; CONFIG_BLOCK_END
M83
T0
; FEATURE: Brim
G1 X1 Y1 E1.5
G1 X2 Y1 E-0.8
; CHANGE_LAYER
; FEATURE: Outer wall
G1 X1 Y1 E2.0
G1 X1 Y2 E.5
G1 X1 Y3
; FEATURE: Support
{support_line}
T1
; FEATURE: Prime tower
G1 X5 Y5 E4
; CHANGE_LAYER
; FEATURE: Outer wall
G1 X1 Y1 E1.0
"""

INFO = """<config><plate>
<metadata key="prediction" value="1796"/><metadata key="weight" value="6.14"/>
<metadata key="support_used" value="false"/>
<filament id="1" type="PETG" color="#696969" used_m="1.69" used_g="5.56" used_for_support="false"/>
<filament id="2" type="PETG" color="#FFB000" used_m="0.18" used_g="0.58" used_for_support="false"/>
</plate></config>"""


def parsed(sup=0, support_line="G1 X9 Y9 E3"):
    return ss.parse_gcode(GCODE.format(sup=sup, support_line=support_line).splitlines(True))


class ParseTest(unittest.TestCase):
    def test_features_layers_tools(self):
        p = parsed()
        self.assertEqual(p["layers"], 2)
        f = p["features"]
        self.assertAlmostEqual(f["Brim"]["length_mm"], 1.5)       # retraction ignored
        self.assertAlmostEqual(f["Outer wall"]["length_mm"], 3.5)
        self.assertEqual(f["Outer wall"]["segments"], 2)
        self.assertAlmostEqual(f["Prime tower"]["length_mm"], 4.0)
        self.assertAlmostEqual(p["tool_mm"][0], 1.5 + 2.5 + 3)
        self.assertAlmostEqual(p["tool_mm"][1], 5.0)
        self.assertEqual(p["settings"]["support_type"], "tree(auto)")
        self.assertNotIn("change_filament_gcode", p["settings"])

    def test_absolute_extrusion(self):
        p = ss.parse_gcode(["M82\n", "; FEATURE: Outer wall\n", "G92 E0\n", "G1 X1 E1\n",
                            "G1 X2 E3\n", "G1 X3 E2.5\n", "G1 X4 E4\n"])
        self.assertAlmostEqual(p["features"]["Outer wall"]["length_mm"], 1 + 2 + 1.5)

    def test_slice_info(self):
        i = ss.parse_slice_info(INFO)
        self.assertEqual(i["prediction_s"], 1796)
        self.assertEqual([f["used_g"] for f in i["filaments"]], [5.56, 0.58])
        self.assertFalse(i["support_used"])
        self.assertEqual(ss.parse_slice_info("not xml"), {})


class SummaryTest(unittest.TestCase):
    def test_no_supports(self):
        s = ss.summarize(parsed(0, "G1 X9 Y9"), ss.parse_slice_info(INFO))
        self.assertFalse(s["supports"]["generated"])
        self.assertIn("No supports needed", s["headline"])
        self.assertTrue(s["adhesion"]["brim"])
        self.assertFalse(s["adhesion"]["skirt"])
        self.assertTrue(s["adhesion"]["prime_tower"])
        self.assertEqual((s["print_time"], s["layers"], s["total_g"]), ("29m 56s", 2, 6.14))
        self.assertEqual(s["model_time"], "22m 54s")
        self.assertIn("supports: no", ss.card_lines(s)[1])

    def test_supports(self):
        s = ss.summarize(parsed(1), None)
        self.assertTrue(s["supports"]["generated"])
        self.assertTrue(s["headline"].startswith("Supports generated"))
        self.assertEqual(len(s["filaments"]), 2)          # fallback from per-tool extrusion
        self.assertIn("supports: YES", ss.card_lines(s)[1])

    def test_support_enabled_but_unused(self):
        s = ss.summarize(parsed(1, "G1 X9 Y9"), ss.parse_slice_info(INFO))
        self.assertFalse(s["supports"]["generated"])
        self.assertIn("support is enabled", s["headline"])

    def test_html_escapes(self):
        s = ss.summarize(parsed(0, "G1 X9 Y9"), ss.parse_slice_info(INFO))
        h = ss.render_html(s, "<script>x</script>")
        self.assertNotIn("<script>", h)
        self.assertIn("No supports needed", h)


class CatalogTest(unittest.TestCase):
    def test_htm_is_a_slice_item(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            p = root / "proj"
            p.mkdir()
            (p / "a.htm").write_text("<html></html>")
            (p / "a.json").write_text(json.dumps({"kind": "slice", "info": {"layers": 3}, "source": "m.3mf"}))
            it = read_item(root, p, p / "a.htm")
            self.assertEqual((it["kind"], it["info"]), ("slice", {"layers": 3}))
            self.assertEqual(it["json_url"], "/proj/a.json")

    def test_builtin_registry_has_slice_for_3mf(self):
        reg = load_registry(None, None)
        a = reg.adapters["slice-bambu"]
        self.assertEqual((a.kind, a.produces), ("slice", "htm"))
        if not a.missing():
            self.assertEqual(plan_special(reg, Path("x.3mf"), "slice")[0].id, "slice-bambu")


class PostProcessNeutralised(unittest.TestCase):
    """Embedded post_process commands are removed from the copy that is sliced; never executed."""

    def make(self, d: Path, cfg_bytes: bytes, name="Metadata/project_settings.config") -> Path:
        src = d / "in.3mf"
        with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("3D/3dmodel.model", "<model/>")
            z.writestr(name, cfg_bytes)
            z.writestr("Metadata/plate_1.png", b"\x89PNG")
        return src

    def test_post_process_emptied(self):
        from tulda.adapters.slicing import safe_copy
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            cfg = {"layer_height": "0.2", "post_process": ["/bin/sh -c 'echo never-run'"]}
            src = self.make(d, json.dumps(cfg).encode())
            changed = safe_copy(src, d / "copy.3mf")
            self.assertEqual(changed, ["Metadata/project_settings.config"])
            with zipfile.ZipFile(d / "copy.3mf") as z:
                out = json.loads(z.read("Metadata/project_settings.config"))
                self.assertEqual(out, {"layer_height": "0.2", "post_process": []})
                self.assertEqual(z.read("Metadata/plate_1.png"), b"\x89PNG")
                self.assertEqual(z.read("3D/3dmodel.model"), b"<model/>")
            with zipfile.ZipFile(src) as z:                 # the source is untouched
                self.assertIn(b"never-run", z.read("Metadata/project_settings.config"))

    def test_clean_file_copied_unchanged(self):
        from tulda.adapters.slicing import safe_copy
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            raw = json.dumps({"post_process": [], "note": "x"}).encode()
            src = self.make(d, raw)
            safe_copy(src, d / "copy.3mf")
            with zipfile.ZipFile(d / "copy.3mf") as z:
                self.assertEqual(json.loads(z.read("Metadata/project_settings.config"))["post_process"], [])

    def test_non_json_config_mentioning_post_process_refused(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = self.make(d, b'<config><metadata key="post_process" value="x"/></config>',
                            "Metadata/model_settings.config")
            calls = []
            with unittest.mock.patch("subprocess.run", side_effect=lambda *a, **k: calls.append(a)):
                r = bambu_slice(Context(src, d / "o.htm", {"bambu_bin": "/nonexistent"}, d / "w"))
            self.assertFalse(r.ok)
            self.assertIn("post_process", r.message)
            self.assertEqual(calls, [])                     # the slicer was never started

    def test_slicer_gets_neutralised_copy(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = self.make(d, json.dumps({"post_process": ["touch /nonexistent/x"]}).encode())
            seen = {}

            def fake_run(argv, **kw):
                with zipfile.ZipFile(argv[-1]) as z:
                    seen["pp"] = json.loads(z.read("Metadata/project_settings.config"))["post_process"]
                raise OSError("no slicer in tests")
            with unittest.mock.patch("subprocess.run", side_effect=fake_run):
                r = bambu_slice(Context(src, d / "o.htm", {"bambu_bin": "/nonexistent"}, d / "w"))
            self.assertFalse(r.ok)
            self.assertEqual(seen["pp"], [])


@unittest.skipUnless(os.access(DEFAULT_BIN, os.X_OK), "Bambu Studio not installed")
class RealSlicerTest(unittest.TestCase):
    def test_bad_3mf_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            bad = d / "bad.3mf"
            with zipfile.ZipFile(bad, "w") as z:
                z.writestr("hello.txt", "x")
            r = bambu_slice(Context(bad, d / "o.htm", {}, d / "w"))
            self.assertFalse(r.ok)


if __name__ == "__main__":
    unittest.main()
