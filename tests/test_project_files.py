"""Project files and 3mf details: parsing (plates, filaments, settings, overrides, colour changes,
slice estimates, post-processing), per-plate meshes and slicing options, `--plates`, and the
read-only project page /_p/<project>/ (escaping, 404/405, traversal, no slicing on GET)."""
import ast
import hashlib
import http.client
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from tulda import projectfiles as pf
from tulda import threemf
from tulda.catalog import render_plate
from tulda.cli import parse_plates, source_plates
from tulda.errors import TuldaError
from tulda.mesh import MeshError
from tulda.server import PAGE_CSP, make_server
from tulda.slicesummary import parse_slice_info

PKG = Path(__file__).resolve().parent.parent / "tulda"
EVIL = '<img src=x onerror=alert(1)>"\'&'
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16


def _cube(oid: int, x: float) -> str:
    v = "".join(f'<vertex x="{x + a}" y="{b}" z="{c}"/>' for a in (0, 1) for b in (0, 1) for c in (0, 1))
    t = '<triangle v1="0" v2="1" v3="2"/><triangle v1="1" v2="3" v3="2"/><triangle v1="4" v2="5" v3="6"/>'
    return f'<object id="{oid}" type="model"><mesh><vertices>{v}</vertices><triangles>{t}</triangles></mesh></object>'


MODEL = ('<?xml version="1.0"?><model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
         '<metadata name="Application">BambuStudio-01.00</metadata><resources>'
         + _cube(1, 0) + _cube(2, 50) + _cube(3, 100) +
         '</resources><build><item objectid="1"/><item objectid="2"/><item objectid="3"/></build></model>')


def model_settings(plate2_name=EVIL) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?><config>
<object id="1"><metadata key="name" value="body"/><metadata key="extruder" value="1"/>
  <metadata key="layer_height" value="0.12"/>
  <part id="1" subtype="normal_part"><metadata key="name" value="body"/><metadata key="matrix" value="1 0 0 0"/>
  <metadata key="wall_loops" value="6"/></part></object>
<object id="2"><metadata key="name" value="{EVIL.replace('&', '&amp;').replace('<', '&lt;').replace('"', '&quot;')}"/>
  <metadata key="extruder" value="3"/></object>
<object id="3"><metadata key="name" value="arm"/><metadata key="extruder" value="4"/></object>
<plate><metadata key="plater_id" value="1"/><metadata key="plater_name" value="base"/>
  <metadata key="thumbnail_file" value="Metadata/plate_1.png"/>
  <model_instance><metadata key="object_id" value="1"/><metadata key="instance_id" value="0"/></model_instance></plate>
<plate><metadata key="plater_id" value="2"/>
  <metadata key="plater_name" value="{plate2_name.replace('&', '&amp;').replace('<', '&lt;').replace('"', '&quot;')}"/>
  <metadata key="curr_bed_type" value="Cool Plate"/>
  <model_instance><metadata key="object_id" value="2"/><metadata key="instance_id" value="0"/></model_instance>
  <model_instance><metadata key="object_id" value="3"/><metadata key="instance_id" value="0"/></model_instance></plate>
</config>"""


PROJECT = {"filament_colour": ["#00808A", "#C12E1F", "#8E9091", "red;background:url(x)"],
           "filament_type": ["PETG", "PETG", "PLA", "PLA"], "filament_vendor": ["Acme"] * 4,
           "filament_settings_id": ["Generic PETG"] * 4, "layer_height": "0.2",
           "wall_loops": "3", "sparse_infill_density": "15%", "enable_support": "0",
           "elefant_foot_compensation": "0.15", "xy_hole_compensation": "0.05",
           "xy_contour_compensation": "0", "nozzle_diameter": ["0.4"],
           "different_settings_to_system": ["wall_loops;xy_hole_compensation", "", ""],
           "post_process": []}

CHANGES = """<?xml version="1.0" encoding="utf-8"?><custom_gcodes_per_layer><plate><plate_info id="1"/>
<layer top_z="7.4" type="2" extruder="2" color="#C12E1F" extra="" gcode="tool_change"/>
<layer top_z="9" type="4" extruder="1" color="" extra="" gcode="M117 &lt;hi&gt;"/>
<mode value="MultiAsSingle"/></plate></custom_gcodes_per_layer>"""

SLICE_INFO = """<?xml version="1.0" encoding="UTF-8"?><config><plate>
<metadata key="index" value="1"/><metadata key="prediction" value="2022"/><metadata key="weight" value="13.91"/>
<filament id="1" type="PETG" color="#00808A" used_m="4.06" used_g="13.39"/>
<filament id="2" type="PETG" color="#C12E1F" used_m="0.16" used_g="0.52"/></plate>
<plate><metadata key="index" value="2"/></plate></config>"""


def write_project_3mf(path: Path, project=None, plate2_name=EVIL, extra=None) -> Path:
    files = {"Metadata/model_settings.config": model_settings(plate2_name),
             "Metadata/project_settings.config": json.dumps(PROJECT if project is None else project),
             "Metadata/custom_gcode_per_layer.xml": CHANGES,
             "Metadata/slice_info.config": SLICE_INFO, **(extra or {})}
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("3D/3dmodel.model", MODEL)
        for k, v in files.items():
            z.writestr(k, v)
    return path


class Parse(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.f = write_project_3mf(self.tmp / "x.3mf")
        self.d = pf.read_3mf(self.f)

    def test_plates_objects_and_filaments(self):
        d = self.d
        self.assertIsNone(d["error"])
        self.assertTrue(d["bambu"])
        self.assertEqual([p["n"] for p in d["plates"]], [1, 2])
        self.assertEqual(d["plates"][0]["name"], "base")
        self.assertEqual(d["plates"][1]["name"], EVIL)            # raw here; escaped on the page
        self.assertEqual([o["name"] for o in d["plates"][1]["objects"]], [EVIL, "arm"])
        self.assertEqual(d["plates"][0]["filaments"], [1, 2])     # 2 via the colour change
        self.assertEqual(d["plates"][1]["filaments"], [3, 4])
        self.assertEqual(d["plates"][1]["overrides"], {"curr_bed_type": "Cool Plate"})

    def test_filament_table(self):
        f = self.d["filaments"]
        self.assertEqual([x["colour"] for x in f], ["#00808A", "#C12E1F", "#8E9091", None])
        self.assertEqual(f[0]["type"], "PETG")
        self.assertEqual(f[0]["used_by"], ["plate 1: body"])
        self.assertEqual(f[1]["used_by"], ["plate 1: from Z 7.4 mm"])
        self.assertEqual(f[3]["used_by"], ["plate 2: arm"])

    def test_settings_and_overrides(self):
        s = {x["key"]: x["value"] for x in self.d["settings"]}
        self.assertEqual(s["layer_height"], "0.2")
        self.assertEqual(s["wall_loops"], "3")
        self.assertEqual(s["enable_support"], "off")
        self.assertEqual(s["elefant_foot_compensation"], "0.15")
        self.assertEqual(s["xy_hole_compensation"], "0.05")
        self.assertEqual(s["nozzle_diameter"], "0.4")
        self.assertEqual(self.d["changed"], ["wall_loops", "xy_hole_compensation"])
        ov = {(o["object"], o["part"]): o["settings"] for o in self.d["overrides"]}
        self.assertEqual(ov[("body", None)], {"layer_height": "0.12"})
        self.assertEqual(ov[("body", "body")], {"wall_loops": "6"})       # matrix is not a setting

    def test_colour_changes(self):
        c = self.d["changes"]
        self.assertEqual(len(c), 2)
        self.assertEqual((c[0]["plate"], c[0]["z"], c[0]["type"], c[0]["extruder"], c[0]["colour"]),
                         (1, 7.4, "filament change", 2, "#C12E1F"))
        self.assertIsNone(c[0]["gcode"])
        self.assertEqual(c[1]["type"], "custom G-code")
        self.assertEqual(c[1]["gcode"], "M117 <hi>")

    def test_stored_slice_estimates(self):
        st = self.d["plates"][0]["stored"]
        self.assertEqual((st["prediction_s"], st["weight_g"]), (2022.0, 13.91))
        self.assertEqual([(f["id"], f["used_g"], f["used_m"]) for f in st["filaments"]],
                         [(1, 13.39, 4.06), (2, 0.52, 0.16)])
        self.assertIsNone(self.d["plates"][1]["stored"])         # no prediction for plate 2

    def test_post_process_flag(self):
        self.assertEqual(self.d["post_process"], [])
        proj = dict(PROJECT, post_process=["/bin/echo pwned"])
        d = pf.read_3mf(write_project_3mf(self.tmp / "pp.3mf", proj))
        self.assertEqual(len(d["post_process"]), 1)
        self.assertIn("/bin/echo pwned", d["post_process"][0]["command"])
        self.assertEqual(d["post_process"][0]["config"], "Metadata/project_settings.config")

    def test_plain_3mf_has_one_plate(self):
        with zipfile.ZipFile(self.tmp / "plain.3mf", "w") as z:
            z.writestr("3D/3dmodel.model", MODEL)
        d = pf.read_3mf(self.tmp / "plain.3mf")
        self.assertFalse(d["bambu"])
        self.assertEqual([p["n"] for p in d["plates"]], [1])

    def test_dtd_and_entities_refused(self):
        bomb = ('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]>'
                '<config><plate><metadata key="index" value="1"/><metadata key="prediction" value="&b;"/>'
                '</plate></config>')
        pad = "<!-- " + "x" * 5000 + " -->"     # DTD past the head that threemf._read checks
        late = '<?xml version="1.0"?>' + pad + bomb[len('<?xml version="1.0"?>'):]
        for body in (bomb, late):
            f = write_project_3mf(self.tmp / "b.3mf", extra={"Metadata/custom_gcode_per_layer.xml": body,
                                                              "Metadata/slice_info.config": body})
            d = pf.read_3mf(f)
            self.assertEqual(d["changes"], [])
            self.assertTrue(all(p["stored"] is None for p in d["plates"]))
        self.assertEqual(parse_slice_info(bomb), {})

    def test_oversized_member_ignored(self):
        with mock.patch.dict(pf.MEMBER_LIMITS, {"Metadata/model_settings.config": 100}):
            d = pf.read_3mf(self.f)
        self.assertEqual([p["n"] for p in d["plates"]], [1])      # fell back: no plates read

    def test_not_a_zip(self):
        (self.tmp / "bad.3mf").write_bytes(b"nope")
        self.assertTrue(pf.read_3mf(self.tmp / "bad.3mf")["error"])

    def test_list_files_skips_dots_and_symlinks(self):
        root = self.tmp / "proj"
        (root / "sub").mkdir(parents=True)
        (root / "a.scad").write_text("x")
        (root / "sub" / "b.3mf").write_bytes(b"x")
        (root / ".git").mkdir()
        (root / ".git" / "c").write_text("x")
        (root / "link.stl").symlink_to(self.f)
        files, trunc = pf.list_files(root)
        self.assertEqual([(f["rel"], f["type"]) for f in files], [("a.scad", "scad"), ("sub/b.3mf", "3mf")])
        self.assertFalse(trunc)
        self.assertIsNone(pf.safe_member_path(root, "link.stl"))

    def test_summary_text(self):
        root = self.tmp / "proj2"
        root.mkdir()
        write_project_3mf(root / "a.3mf")
        write_project_3mf(root / "b.3mf")
        (root / "c.scad").write_text("x")
        s = pf.folder_summary(root)
        self.assertEqual(pf.summary_text(s), "2 × 3mf (4 plates), 1 × scad")


class PlateRendering(unittest.TestCase):
    def setUp(self):
        self.f = write_project_3mf(Path(tempfile.mkdtemp()) / "x.3mf")

    def test_mesh_per_plate(self):
        self.assertEqual({p.obj for p in threemf.load(self.f).parts}, {"body", EVIL, "arm"})
        sc = threemf.load(self.f, 2)
        self.assertEqual({p.obj for p in sc.parts}, {EVIL, "arm"})
        self.assertEqual(sc.extras["plate"], 2)
        with self.assertRaises(MeshError):
            threemf.load(self.f, 5)

    def test_parse_plates_and_source_plates(self):
        self.assertIsNone(parse_plates(None))
        self.assertEqual(parse_plates("all"), "all")
        self.assertEqual(parse_plates("2, 1,2"), [2, 1])
        for bad in ("0", "x", "", "1,-2"):
            with self.assertRaises(TuldaError):
                parse_plates(bad)
        self.assertEqual(source_plates(self.f, "all"), [1, 2])
        self.assertEqual(source_plates(self.f, [2]), [2])
        self.assertEqual(source_plates(self.f, None), [None])
        self.assertEqual(source_plates(Path("x.scad"), "all"), [None])
        with self.assertRaises(TuldaError):
            source_plates(self.f, [3])

    def test_slice_uses_plate_and_blanks_post_process(self):
        from tulda.adapters import slicing
        from tulda.adapters.api import Context
        tmp = Path(tempfile.mkdtemp())
        src = write_project_3mf(tmp / "pp.3mf", dict(PROJECT, post_process=["/bin/echo pwned"]))
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv
            copy = Path(argv[-1])
            with zipfile.ZipFile(copy) as z:
                seen["pp"] = json.loads(z.read("Metadata/project_settings.config"))["post_process"]
            out = Path(argv[argv.index("--outputdir") + 1]) / "sliced.3mf"
            with zipfile.ZipFile(out, "w") as z:
                z.writestr("Metadata/plate_2.gcode", "; CHANGE_LAYER\nG1 X1 E1\n")
                z.writestr("Metadata/slice_info.config", SLICE_INFO.replace('value="2"/></plate>',
                           'value="2"/><metadata key="prediction" value="77"/></plate>'))
            return mock.Mock(returncode=0, stdout="", stderr="")

        ctx = Context(src, tmp / "o.htm", {"plate": 2, "bambu_bin": "/nonexistent"}, tmp / "w")
        with mock.patch("subprocess.run", fake_run):
            res = slicing.bambu_slice(ctx)
        self.assertTrue(res.ok, res.message)
        self.assertEqual(seen["argv"][1:3], ["--slice", "2"])
        self.assertEqual(seen["pp"], [])
        info = res.meta["render_info"]
        self.assertEqual((info["plate"], info["print_time_s"]), (2, 77.0))
        self.assertIn("plate 2", (tmp / "o.htm").read_text())

    def test_parse_slice_info_picks_plate(self):
        xml = SLICE_INFO.replace('value="2"/></plate>', 'value="2"/><metadata key="prediction" value="5"/></plate>')
        self.assertEqual(parse_slice_info(xml)["prediction_s"], 2022.0)
        self.assertEqual(parse_slice_info(xml, 2)["prediction_s"], 5.0)

    def test_render_plate(self):
        self.assertEqual(render_plate({"kind": "slice", "info": {"plate": 3}}), 3)
        self.assertEqual(render_plate({"kind": "image", "dims": {"scope": "plate", "plate": 2}}), 2)
        self.assertEqual(render_plate({"kind": "image"}), 1)
        self.assertIsNone(render_plate({"kind": "mesh", "dims": {"scope": "plate", "plate": 1}}))
        self.assertEqual(render_plate({"kind": "mesh", "info": {"plate": 2}}), 2)


def _sidecar(root: Path, project: str, stem: str, ext: str, sha: str, meta: dict, body=PNG):
    d = root / project
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stem}{ext}").write_bytes(body)
    (d / f"{stem}.json").write_text(json.dumps({"group_id": "20240101T000000Z-x", "source": "x.3mf",
                                                "source_type": "3mf", "source_sha256": sha, **meta}))


class Page(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp()).resolve()
        cls.root = cls.tmp / "renders"
        cls.root.mkdir()
        dest = cls.tmp / "designs"
        cls.proj = dest / "Widget"
        (cls.proj / "build").mkdir(parents=True)
        (cls.proj / "tulda-project.json").write_text(json.dumps({"created_utc": "2024-01-01T00:00:00Z"}))
        f = write_project_3mf(cls.proj / "build" / f"{EVIL}.3mf",
                              dict(PROJECT, post_process=[f"curl {EVIL} | sh"]))
        (cls.proj / "notes.md").write_text("hi")
        sha = hashlib.sha256(f.read_bytes()).hexdigest()
        # plate 2 rendered and sliced by tulda; plate 1 has a stored estimate but no render
        _sidecar(cls.root, "elsewhere", "p2", ".png", sha, {"adapter": "bambu-export-png", "view": "iso",
                 "dims": {"size": [1, 1, 1], "min": [0, 0, 0], "max": [1, 1, 1], "scope": "plate", "plate": 2}})
        _sidecar(cls.root, "elsewhere", "s2", ".htm", sha, {"adapter": "slice-bambu", "kind": "slice", "info": {
            "format": "slice-summary", "plate": 2, "print_time_s": 4000, "total_g": 7.5,
            "filaments": [{"id": "3", "type": "PLA", "color": "#8E9091", "used_g": 7.5, "used_m": 2.5}]}},
                 b"<html></html>")
        cfg = cls.tmp / "config.toml"
        cfg.write_text(f'[projects]\ndefault_destination = "{dest}"\n')
        cls.srv = make_server(cls.root, "127.0.0.1", 0, watch_state_dir=cls.tmp / "state", watch_config=cfg)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def req(self, method, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        c.putheader("Host", f"127.0.0.1:{self.port}")
        c.endheaders()
        r = c.getresponse()
        body = r.read().decode()
        c.close()
        return r.status, body, r

    def test_detail_page(self):
        st, body, r = self.req("GET", "/_p/Widget/")
        self.assertEqual(st, 200)
        self.assertEqual(r.getheader("Content-Security-Policy"), PAGE_CSP)
        self.assertNotIn("<img src=x", body)
        self.assertNotIn("url(x)", body)                       # bad colour never reaches a style
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", body)
        self.assertNotIn(str(self.tmp), body)
        self.assertIn("designs/Widget", body)
        self.assertIn("Post-processing script embedded", body)
        self.assertIn("2 plates", body)
        self.assertIn("Bambu Studio estimate stored in the file", body)   # plate 1
        self.assertIn("1h 06m", body)                                     # plate 2, sliced by tulda
        self.assertIn('/elsewhere/p2.png', body)
        self.assertIn("--plates all", body)
        self.assertIn("Elephant foot", body)
        self.assertIn("filament change", body)
        self.assertEqual(body.count("<script>"), 1)            # only the local-time script

    def test_detail_json(self):
        st, body, _ = self.req("GET", "/api/projects/Widget")
        self.assertEqual(st, 200)
        self.assertNotIn(str(self.tmp), body)
        d = json.loads(body)
        t = d["threemf"][0]
        self.assertEqual(t["name"], f"build/{EVIL}.3mf")
        p1, p2 = t["plates"]
        self.assertEqual((p1["estimate"]["source"], p1["image"]), ("file", None))
        self.assertEqual(p2["estimate"]["source"], "slice")
        self.assertEqual(p2["estimate"]["prediction_s"], 4000)
        self.assertEqual(p2["image"]["url"], "/elsewhere/p2.png")
        self.assertTrue(t["post_process"])

    def test_list_row_summary(self):
        st, body, _ = self.req("GET", "/_p/")
        self.assertIn("1 × 3mf (2 plates), 1 × text, 1 × json", body)
        self.assertIn("post-processing script", body)
        self.assertIn('href="/_p/Widget/"', body)
        rows = json.loads(self.req("GET", "/api/projects")[1])["projects"]
        w = next(r for r in rows if r["name"] == "Widget")
        self.assertEqual((w["files"]["threemf"], w["files"]["plates"]), (1, 2))
        self.assertNotIn("path", w)
        self.assertNotIn(str(self.tmp), json.dumps(rows))

    def test_not_found_and_traversal(self):
        for p in ("/_p/nope/", "/_p/../etc/", "/_p/..%2Fdesigns/", "/_p/.hidden/", "/_p/Widget/x/",
                  "/api/projects/nope", "/api/projects/Widget/x", "/_p/%00/"):
            self.assertEqual(self.req("GET", p)[0], 404, p)
        self.assertEqual(self.req("GET", "/_p/elsewhere/")[0], 200)    # renders-only project

    def test_read_only(self):
        for m in ("POST", "PUT", "DELETE", "PATCH"):
            self.assertEqual(self.req(m, "/_p/Widget/")[0], 405, m)
            self.assertEqual(self.req(m, "/api/projects/Widget")[0], 405, m)
        before = sorted(p.name for p in self.root.rglob("*") if ".tulda" not in p.parts)
        src_before = sorted((p.name, p.stat().st_mtime_ns) for p in self.proj.rglob("*"))
        with mock.patch("subprocess.run", side_effect=AssertionError("no subprocess on GET")), \
             mock.patch("subprocess.Popen", side_effect=AssertionError("no subprocess on GET")):
            self.assertEqual(self.req("GET", "/_p/Widget/")[0], 200)
            self.assertEqual(self.req("GET", "/api/projects/Widget")[0], 200)
        self.assertEqual(before, sorted(p.name for p in self.root.rglob("*") if ".tulda" not in p.parts))
        self.assertEqual(src_before, sorted((p.name, p.stat().st_mtime_ns) for p in self.proj.rglob("*")))


class ModulesOnlyRead(unittest.TestCase):
    def test_no_writes_or_subprocesses(self):
        for name in ("projectfiles.py", "projectdetail.py"):
            tree = ast.parse((PKG / name).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level:
                    self.assertNotIn((node.module or "").split(".")[0],
                                     {"runner", "adapters", "cli", "manage", "service", "projects", "watchd"}, name)
                if isinstance(node, ast.Import):
                    self.assertFalse({a.name for a in node.names} & {"subprocess", "shutil"}, name)
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, {"write_text", "write_bytes", "unlink", "rmtree", "mkdir",
                                                 "rename", "replace", "system", "writestr"}, name)


if __name__ == "__main__":
    unittest.main()
