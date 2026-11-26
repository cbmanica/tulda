"""Model dimensions: per-format measurement, sidecar + catalog storage, migration, backfill, UI."""

import json
import sqlite3
import tempfile
import unittest
from array import array
from pathlib import Path

from tulda import catalog as cm
from tulda import dims, mesh, runner, viewer
from tulda.adapters import plan_jobs
from tulda.catalog import Catalog
from tulda.gallery import noscript_list
from tulda.watchpage import _file_row

from tests.test_mesh import UNIT_CUBE_PTS, UNIT_CUBE_TRIS, model_xml, no_user_config, tri_mesh, write_3mf

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp()).resolve()


def cube_tris(sx, sy, sz, ox=0.0, oy=0.0, oz=0.0) -> array:
    out = array("f")
    for a, b, c in UNIT_CUBE_TRIS:
        for i in (a, b, c):
            x, y, z = UNIT_CUBE_PTS[i]
            out.extend((ox + x * sx, oy + y * sy, oz + z * sz))
    return out


def ascii_stl(tris: array) -> str:
    lines = ["solid cube"]
    for i in range(0, len(tris), 9):
        lines.append(" facet normal 0 0 0\n  outer loop")
        for k in range(i, i + 9, 3):
            lines.append(f"   vertex {tris[k]} {tris[k + 1]} {tris[k + 2]}")
        lines.append("  endloop\n endfacet")
    lines.append("endsolid cube")
    return "\n".join(lines) + "\n"


class Stl(unittest.TestCase):
    def test_binary_and_ascii_cube(self):
        d = tmpdir()
        t = cube_tris(60, 46.8, 13.8, ox=-5, oy=2, oz=1)
        (d / "b.stl").write_bytes(mesh.write_stl(t))
        (d / "a.stl").write_text(ascii_stl(t))
        for name in ("b.stl", "a.stl"):
            r = dims.for_stl(d / name)
            self.assertEqual(r["scope"], "model")
            for got, want in zip(r["size"], (60, 46.8, 13.8)):
                self.assertAlmostEqual(got, want, places=3)
            self.assertAlmostEqual(r["min"][0], -5, places=3)
            self.assertEqual(dims.fmt(r), "60.0 × 46.8 × 13.8 mm")

    def test_empty_stl_refused(self):
        d = tmpdir()
        (d / "e.stl").write_bytes(mesh.write_stl(array("f")))
        with self.assertRaises(mesh.MeshError):
            dims.for_stl(d / "e.stl")


class ThreeMF(unittest.TestCase):
    def test_transform_and_inch_unit(self):
        d = tmpdir()
        # unit cube scaled 2 x 3 x 1 and shifted; in inches -> mm
        m = model_xml(tri_mesh("1", UNIT_CUBE_PTS, UNIT_CUBE_TRIS),
                      '<item objectid="1" transform="2 0 0 0 3 0 0 0 1 10 0 0"/>', unit="inch")
        r = dims.for_3mf(write_3mf(d / "i.3mf", m))
        self.assertEqual((r["scope"], r["from"]), ("model", "3mf"))
        for got, want in zip(r["size"], (50.8, 76.2, 25.4)):
            self.assertAlmostEqual(got, want, places=3)
        self.assertAlmostEqual(r["min"][0], 254.0, places=3)

    def test_rotation_and_component_transform(self):
        d = tmpdir()
        objs = (tri_mesh("1", UNIT_CUBE_PTS, UNIT_CUBE_TRIS)
                + '<object id="2" type="model"><components>'
                  '<component objectid="1" transform="5 0 0 0 1 0 0 0 1 0 0 0"/></components></object>')
        # build item rotates 90 degrees about Z: the 5 mm side ends up along Y
        m = model_xml(objs, '<item objectid="2" transform="0 1 0 -1 0 0 0 0 1 0 0 0"/>')
        r = dims.for_3mf(write_3mf(d / "r.3mf", m))
        for got, want in zip(r["size"], (1, 5, 1)):
            self.assertAlmostEqual(got, want, places=4)

    def test_multi_object_build_union_skips_modifiers_and_unprintable(self):
        d = tmpdir()
        objs = (tri_mesh("1", UNIT_CUBE_PTS, UNIT_CUBE_TRIS) + tri_mesh("2", UNIT_CUBE_PTS, UNIT_CUBE_TRIS)
                + tri_mesh("3", UNIT_CUBE_PTS, UNIT_CUBE_TRIS)
                + '<object id="4" type="model"><components><component objectid="2"/>'
                  '<component objectid="3" transform="9 0 0 0 9 0 0 0 9 0 0 0"/></components></object>')
        m = model_xml(objs, '<item objectid="1" transform="1 0 0 0 1 0 0 0 1 10 0 0"/>'
                            '<item objectid="4"/>'
                            '<item objectid="1" printable="0" transform="1 0 0 0 1 0 0 0 1 500 0 0"/>')
        settings = ('<?xml version="1.0"?><config><object id="4"><part id="2" subtype="normal_part"/>'
                    '<part id="3" subtype="modifier_part"/></object></config>')
        r = dims.for_3mf(write_3mf(d / "m.3mf", m, {"Metadata/model_settings.config": settings}))
        self.assertEqual(r["size"], [11.0, 1.0, 1.0])            # 0..11 in X; the 9x modifier is left out

    def test_bambu_plates(self):
        d = tmpdir()
        objs = tri_mesh("1", UNIT_CUBE_PTS, UNIT_CUBE_TRIS) + tri_mesh("2", UNIT_CUBE_PTS, UNIT_CUBE_TRIS)
        m = model_xml(objs, '<item objectid="1" transform="10 0 0 0 20 0 0 0 5 0 0 0"/>'
                            '<item objectid="2" transform="3 0 0 0 3 0 0 0 3 300 0 0"/>'
                            '<item objectid="2" transform="3 0 0 0 3 0 0 0 3 310 0 0"/>')

        def plate(n, *inst):
            mi = "".join(f'<model_instance><metadata key="object_id" value="{o}"/>'
                         f'<metadata key="instance_id" value="{i}"/></model_instance>' for o, i in inst)
            return f'<plate><metadata key="plater_id" value="{n}"/>{mi}</plate>'
        cfg = f'<?xml version="1.0"?><config>{plate(1, (1, 0))}{plate(2, (2, 0), (2, 1))}{plate(3)}</config>'
        f = write_3mf(d / "p.3mf", m, {"Metadata/model_settings.config": cfg})
        r = dims.for_3mf(f)
        self.assertEqual((r["scope"], r["plate"], r["size"]), ("plate", 1, [10.0, 20.0, 5.0]))
        self.assertEqual(r["plates"], [{"plate": 1, "size": [10.0, 20.0, 5.0]},
                                       {"plate": 2, "size": [13.0, 3.0, 3.0]}, {"plate": 3, "size": None}])
        self.assertEqual(dims.fmt(r), "10.0 × 20.0 × 5.0 mm (plate 1 of 3)")
        r2 = dims.for_3mf(f, plate=2)
        self.assertEqual((r2["plate"], r2["size"]), (2, [13.0, 3.0, 3.0]))
        r3 = dims.for_3mf(f, plate=3)                             # empty plate: whole build, labelled
        self.assertEqual((r3["scope"], r3["size"]), ("build", [313.0, 20.0, 5.0]))
        self.assertEqual(dims.fmt(r3), "313.0 × 20.0 × 5.0 mm (whole build)")


class Records(unittest.TestCase):
    def test_clean_rejects_junk(self):
        self.assertIsNone(dims.clean({"size": [1, 2]}))
        self.assertIsNone(dims.clean({"size": [1, 2, float("nan")]}))
        self.assertIsNone(dims.clean({"size": [1, 2, -3]}))
        self.assertIsNone(dims.clean("<b>"))
        c = dims.clean({"size": [1, 2, 3], "scope": "<x>", "from": "evil", "plates": [{"plate": "1"}]})
        self.assertEqual(c, {"size": [1.0, 2.0, 3.0], "scope": "model", "from": None})
        self.assertEqual(dims.fmt(None), "size unknown")

    def test_glb_bbox(self):
        d = tmpdir()
        sc = mesh.Scene([mesh.Part("a", cube_tris(4, 5, 6)),
                         mesh.Part("m", cube_tris(100, 100, 100), visible=False)])
        (d / "a.glb").write_bytes(mesh.write_glb(sc))
        self.assertEqual(dims.for_glb(d / "a.glb")["size"], [4.0, 5.0, 6.0])
        self.assertEqual(dims.for_glb(d / "a.glb", "3mf")["scope"], "build")


class Render(unittest.TestCase):
    def test_sidecar_and_catalog_get_dims(self):
        d = tmpdir()
        src = write_3mf(d / "a.3mf", model_xml(tri_mesh("1", UNIT_CUBE_PTS, UNIT_CUBE_TRIS),
                                               '<item objectid="1" transform="7 0 0 0 8 0 0 0 9 0 0 0"/>'))
        reg = no_user_config()
        job = plan_jobs(reg, src, views=["interactive"])[0]
        root = d / "renders"
        g = runner.make_group(None, [src], "tulda render a.3mf")
        glb = runner.run_chain(reg, job.chain, src, root, "proj", group=g, view=job.view, view_opts=job.options)
        side = json.loads(glb.with_suffix(".json").read_text())
        self.assertEqual(side["dims"]["size"], [7.0, 8.0, 9.0])
        it = Catalog(root, log=lambda m: None).get_group(g["id"])["items"][0]
        self.assertEqual(it["dims_text"], "7.0 × 8.0 × 9.0 mm")

    def test_unknown_source_type_gets_no_dims(self):
        reg = no_user_config()
        self.assertIsNone(runner.render_dims([reg.adapters["openscad"]], Path("/x/m.q"), None, {}, None))


def mk(root: Path, name: str, meta: dict, ext=".png", body=PNG):
    p = root / "proj"
    p.mkdir(parents=True, exist_ok=True)
    (p / f"{name}{ext}").write_bytes(body)
    (p / f"{name}.json").write_text(json.dumps(meta))
    return p / f"{name}{ext}"


GID = "20261126T100000Z-demo"


def meta(**kw):
    m = {"adapter": "openscad", "stage": "source", "source": "a.scad", "source_type": "scad",
         "source_sha256": "a" * 64, "group_id": GID, "group_created_utc": "2026-11-26T10:00:00Z", "view": "iso"}
    m.update(kw)
    return m


class Catalogue(unittest.TestCase):
    def test_upgrade_from_v6_backfills_from_mesh_info(self):
        root = tmpdir() / "renders"
        mk(root, "m", meta(adapter="mesh-scad", kind="mesh", view="interactive",
                           info={"bbox": [[0, 0, 0], [60, 46.8, 13.8]]}), ".glb", b"glTF" + b"\0" * 20)
        mk(root, "p", meta())
        (root / ".tulda").mkdir()
        raw = sqlite3.connect(root / ".tulda" / "catalog.db", isolation_level=None)
        for f in cm.MIGRATIONS[:6]:
            f(raw)
        raw.execute("PRAGMA user_version=6")
        raw.execute("INSERT INTO groups(id,sources,day,last_mtime,n_renders,starred) VALUES(?,'[]','2026-11-26',1,1,1)",
                    (GID,))
        raw.close()
        cat = Catalog(root, log=lambda m: None)
        self.assertEqual(cat.stats()["schema_version"], cm.SCHEMA_VERSION)
        g = cat.get_group(GID)
        self.assertTrue(g["starred"])
        by = {i["file"]: i for i in g["items"]}
        self.assertEqual(by["m.glb"]["dims_text"], "60.0 × 46.8 × 13.8 mm")
        self.assertEqual(by["p.png"]["dims"]["from"], "sibling")              # same source content
        self.assertEqual(by["p.png"]["dims_text"], "60.0 × 46.8 × 13.8 mm")
        with cat.conn() as c:
            self.assertIsNone(c.execute("SELECT dims FROM renders WHERE file='p.png'").fetchone()[0])

    def test_unknown_and_other_source_not_inherited(self):
        root = tmpdir() / "renders"
        mk(root, "m", meta(adapter="mesh-scad", kind="mesh", info={"bbox": [[0, 0, 0], [1, 1, 1]]}),
           ".glb", b"glTF" + b"\0" * 20)
        mk(root, "p", meta(source_sha256="b" * 64))
        g = Catalog(root, log=lambda m: None).get_group(GID)
        by = {i["file"]: i for i in g["items"]}
        self.assertIsNone(by["p.png"]["dims"])
        self.assertEqual(by["p.png"]["dims_text"], "size unknown")

    def test_backfill_from_source_glb_and_sibling(self):
        d = tmpdir()
        root = d / "renders"
        stl = d / "part.stl"
        stl.write_bytes(mesh.write_stl(cube_tris(3, 4, 5)))
        sha = runner.sha256(stl)
        mk(root, "s", meta(source="part.stl", source_type="stl", source_sha256=sha,
                           steps=[{"cmd": ["tool", "-o", "/x/out.png", str(stl)]}]))
        glb = mesh.write_glb(mesh.Scene([mesh.Part("a", cube_tris(2, 2, 2))]))
        mk(root, "g", meta(source="b.scad", source_sha256="c" * 64, kind="mesh"), ".glb", glb)
        mk(root, "q", meta(source="b.scad", source_sha256="c" * 64))
        mk(root, "u", meta(source="gone.stl", source_sha256="d" * 64,
                           steps=[{"cmd": ["tool", str(d / "gone.stl")]}]))
        changed = d / "c.stl"
        changed.write_bytes(mesh.write_stl(cube_tris(1, 1, 1)))
        mk(root, "x", meta(source="c.stl", source_sha256="e" * 64, steps=[{"cmd": [str(changed)]}]))
        st = dims.backfill(root, scad=False, log=lambda m: None)
        self.assertEqual((st["source"], st["glb"], st["sibling"], st["unknown"]), (1, 1, 1, 2))
        rd = lambda n: json.loads((root / "proj" / f"{n}.json").read_text()).get("dims")
        self.assertEqual(rd("s")["size"], [3.0, 4.0, 5.0])
        self.assertEqual(rd("g")["size"], [2.0, 2.0, 2.0])
        self.assertEqual(rd("q")["from"], "sibling")
        self.assertIsNone(rd("u"))
        self.assertIsNone(rd("x"))                                # source changed since: not measured
        self.assertEqual(dims.backfill(root, scad=False, log=lambda m: None)["had"], 3)


XSS = "<img src=x onerror=alert(1)>"


class Ui(unittest.TestCase):
    def test_viewer_overlay_escaped(self):
        r = {"kind": "mesh", "project": "p", "file": "m.glb", "url": "/p/m.glb", "source": XSS, "group_id": GID,
             "group_title": None, "info": {}, "turntable": None,
             "dims": dims.clean({"size": [60, 46.8, 13.8], "scope": "plate", "plate": 1,
                                 "plates": [{"plate": 1, "size": [60, 46.8, 13.8]}, {"plate": 2, "size": None}]})}
        h = viewer.render_viewer(r)
        self.assertIn('<div id="dimov" role="note"><b>60.0 × 46.8 × 13.8 mm</b>', h)
        self.assertIn("plate 1 of 2", h)
        self.assertIn("plate 2: empty", h)
        self.assertIn('id="scale"', h)
        self.assertNotIn(XSS, h)
        r["dims"] = None
        self.assertIn("<b>size unknown</b>", viewer.render_viewer(r))

    def test_watch_row_escapes_dims(self):
        f = {"id": "x", "file": "a.3mf", "rendered": [{"id": GID, "title": "t", "views": ["iso"],
                                                         "dims_text": XSS}], "missing_views": []}
        h = _file_row(f, "tok")
        self.assertIn("size &lt;img src=x", h)
        self.assertNotIn(XSS, h)

    def test_noscript_list_escapes(self):
        g = {"id": GID, "title": "t", "items": [{"source": XSS, "dims": {"size": [1, 2, 3]},
                                                 "dims_text": "1.0 × 2.0 × 3.0 mm"}]}
        h = noscript_list({"groups": [g]})
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;: 1.0 × 2.0 × 3.0 mm", h)
        self.assertNotIn(XSS, h)

    def test_gallery_script_renders_dims_with_text(self):
        from tulda.gallery import GALLERY_JS
        self.assertIn("dimEl(i)", GALLERY_JS)
        self.assertNotIn("innerHTML", GALLERY_JS)


if __name__ == "__main__":
    unittest.main()
