"""Mesh converters (STL, GLB writer, 3MF reader), mesh adapters, interactive-view planning and
the catalog migration for `kind`/`info`. Fixtures are tiny synthetic files built here."""

import json
import shutil
import sqlite3
import struct
import tempfile
import unittest
import zipfile
from array import array
from pathlib import Path

from tulda import catalog as cm
from tulda import mesh, runner, threemf
from tulda.adapters import load_registry, plan_jobs
from tulda.adapters.api import Context
from tulda.adapters.meshes import scad_mesh, stl_mesh, threemf_mesh
from tulda.catalog import Catalog
from tulda.errors import MissingRequirement, NoAdapter, StepFailed

FOUR = array("f", [   # four triangles (two squares), not a closed solid
    0, 0, 0, 1, 0, 0, 1, 1, 0,   0, 0, 0, 1, 1, 0, 0, 1, 0,
    0, 0, 1, 1, 1, 1, 1, 0, 1,   0, 0, 1, 0, 1, 1, 1, 1, 1])


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp()).resolve()


def glb_parts(data: bytes):
    """(json, bin) of a GLB."""
    j = mesh.read_glb_json(data)
    jl = struct.unpack_from("<I", data, 12)[0]
    bl = struct.unpack_from("<I", data, 20 + jl)[0]
    return j, data[28 + jl:28 + jl + bl]


def model_xml(objects: str, build: str, unit: str = "millimeter", extra: str = "") -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<model unit="{unit}" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
            f'{extra}<resources>{objects}</resources><build>{build}</build></model>')


def tri_mesh(oid: str, pts, tris, attrs: str = "") -> str:
    v = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in pts)
    t = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"{attrs}/>' for a, b, c in tris)
    return f'<object id="{oid}" type="model"><mesh><vertices>{v}</vertices><triangles>{t}</triangles></mesh></object>'


UNIT_FOUR_PTS = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)]
UNIT_FOUR_TRIS = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
                  (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]


def write_3mf(path: Path, model: str, extra_files: dict | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("3D/3dmodel.model", model)
        for k, v in (extra_files or {}).items():
            z.writestr(k, v)
    return path


class Stl(unittest.TestCase):
    def test_binary_roundtrip(self):
        data = mesh.write_stl(FOUR)
        self.assertEqual(len(data), 84 + 50 * 4)
        self.assertEqual(list(mesh.read_stl(data)), list(FOUR))

    def test_ascii(self):
        txt = b"solid x\nfacet normal 0 0 1\n outer loop\n vertex 0 0 0\n vertex 1 0 0\n vertex 0 1 0\n" \
              b" endloop\nendfacet\nendsolid x\n"
        self.assertEqual(list(mesh.read_stl(txt)), [0, 0, 0, 1, 0, 0, 0, 1, 0])

    def test_garbage(self):
        with self.assertRaises(mesh.MeshError):
            mesh.read_stl(b"definitely not an stl file" * 10)


class Glb(unittest.TestCase):
    def scene(self):
        return mesh.Scene([
            mesh.Part("body", FOUR, "#696969", obj="plate"),
            mesh.Part("inlay", array("f", [v + 5 for v in FOUR]), "#FFB000", obj="plate", extruder=2),
            mesh.Part("ghost", FOUR, "#00FF00", 0.5, subtype="modifier_part", visible=False)])

    def test_structure(self):
        data = mesh.write_glb(self.scene())
        self.assertEqual(data[:4], b"glTF")
        self.assertEqual(struct.unpack_from("<I", data, 8)[0], len(data))
        self.assertEqual(len(data) % 4, 0)
        j, binary = glb_parts(data)
        self.assertEqual(j["asset"]["version"], "2.0")
        self.assertEqual(len(j["accessors"]), 3)
        self.assertEqual(j["accessors"][0]["count"], 12)
        self.assertEqual(j["buffers"][0]["byteLength"], len(binary))
        names = [n["name"] for n in j["nodes"]]
        self.assertIn("body", names)
        self.assertIn("inlay", names)
        self.assertIn("plate", names)                      # object node
        root = j["nodes"][j["scenes"][0]["nodes"][0]]
        self.assertEqual(len(root["rotation"]), 4)         # Z-up -> Y-up
        self.assertEqual(j["scenes"][0]["extras"]["triangles"], 12)

    def test_materials_flat_colour_and_alpha(self):
        j, _ = glb_parts(mesh.write_glb(self.scene()))
        by = {m["name"]: m for m in j["materials"]}
        self.assertEqual(set(by), {"#696969", "#FFB000", "#00FF00"})
        self.assertEqual(by["#00FF00"]["alphaMode"], "BLEND")
        self.assertNotIn("alphaMode", by["#696969"])
        r, g, b, a = by["#FFB000"]["pbrMetallicRoughness"]["baseColorFactor"]
        self.assertAlmostEqual(r, 1.0)
        self.assertAlmostEqual(b, 0.0)
        self.assertLess(g, 0.5)                            # sRGB 0xB0 -> linear ~0.43
        self.assertTrue(by["#FFB000"]["doubleSided"])

    def test_node_extras(self):
        j, _ = glb_parts(mesh.write_glb(self.scene()))
        ex = {n["name"]: n["extras"] for n in j["nodes"] if "extras" in n}
        self.assertEqual(ex["inlay"]["extruder"], 2)
        self.assertEqual(ex["ghost"]["visible"], False)
        self.assertEqual(ex["ghost"]["subtype"], "modifier_part")

    def test_positions_in_binary(self):
        _, binary = glb_parts(mesh.write_glb(mesh.Scene([mesh.Part("c", FOUR)])))
        self.assertEqual(list(array("f", binary[:len(FOUR) * 4])), list(FOUR))

    def test_same_name_parts_ok(self):
        sc = mesh.Scene([mesh.Part("p", FOUR), mesh.Part("p", FOUR)])
        j, _ = glb_parts(mesh.write_glb(sc))
        self.assertEqual(len(j["meshes"]), 2)

    def test_empty(self):
        with self.assertRaises(mesh.MeshError):
            mesh.write_glb(mesh.Scene([]))
        with self.assertRaises(mesh.MeshError):
            mesh.Scene([]).check_size()

    def test_guardrails(self):
        sc = mesh.Scene([mesh.Part("c", FOUR)])
        old = mesh.WARN_TRIANGLES, mesh.MAX_TRIANGLES
        try:
            mesh.WARN_TRIANGLES, mesh.MAX_TRIANGLES = 3, 100
            sc.check_size()
            self.assertTrue(sc.warnings and "large mesh" in sc.warnings[0])
            mesh.MAX_TRIANGLES = 3
            with self.assertRaises(mesh.MeshError):
                sc.check_size()
        finally:
            mesh.WARN_TRIANGLES, mesh.MAX_TRIANGLES = old

    def test_bbox(self):
        self.assertEqual(self.scene().bbox(), [[0, 0, 0], [6, 6, 6]])   # hidden part ignored


class ThreeMF(unittest.TestCase):
    def test_build_transform_and_unit(self):
        d = tmpdir()
        m = model_xml(tri_mesh("1", UNIT_FOUR_PTS, UNIT_FOUR_TRIS),
                      '<item objectid="1" transform="2 0 0 0 2 0 0 0 2 10 20 30"/>', unit="millimeter")
        sc = threemf.load(write_3mf(d / "a.3mf", m))
        self.assertEqual(sc.bbox(), [[10, 20, 30], [12, 22, 32]])
        self.assertEqual(sc.n_tris, 12)
        m = model_xml(tri_mesh("1", UNIT_FOUR_PTS, UNIT_FOUR_TRIS), '<item objectid="1"/>', unit="centimeter")
        self.assertEqual(threemf.load(write_3mf(d / "b.3mf", m)).bbox(), [[0, 0, 0], [10, 10, 10]])

    def test_row_vector_rotation(self):
        # 3MF applies transforms to row vectors: x' = x*m00 + y*m10 + z*m20 + m30.
        # m = rotate +90 deg about Z: (1,0,0) -> (0,1,0) means m00=0 m01=1 m10=-1 m11=0
        d = tmpdir()
        pts = [(0, 0, 0), (1, 0, 0), (0, 0, 1)]
        m = model_xml(tri_mesh("1", pts, [(0, 1, 2)]), '<item objectid="1" transform="0 1 0 -1 0 0 0 0 1 0 0 0"/>')
        sc = threemf.load(write_3mf(d / "r.3mf", m))
        self.assertEqual(list(sc.parts[0].tris[3:6]), [0, 1, 0])

    def test_components_compose_with_item(self):
        d = tmpdir()
        objs = (tri_mesh("1", UNIT_FOUR_PTS, UNIT_FOUR_TRIS) + tri_mesh("2", UNIT_FOUR_PTS, UNIT_FOUR_TRIS) +
                '<object id="3" type="model"><components><component objectid="1"/>'
                '<component objectid="2" transform="1 0 0 0 1 0 0 0 1 5 0 0"/></components></object>')
        m = model_xml(objs, '<item objectid="3" transform="1 0 0 0 1 0 0 0 1 100 0 0"/>')
        sc = threemf.load(write_3mf(d / "c.3mf", m))
        self.assertEqual(len(sc.parts), 2)
        self.assertEqual(sc.bbox(), [[100, 0, 0], [106, 1, 1]])
        self.assertEqual({p.obj for p in sc.parts}, {"object 3"})

    def test_mirror_flips_winding(self):
        d = tmpdir()
        pts = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
        m = model_xml(tri_mesh("1", pts, [(0, 1, 2)]), '<item objectid="1" transform="-1 0 0 0 1 0 0 0 1 0 0 0"/>')
        t = list(threemf.load(write_3mf(d / "m.3mf", m)).parts[0].tris)
        self.assertEqual(t, [0, 0, 0, 0, 1, 0, -1, 0, 0])     # vertices 2 and 3 swapped

    def test_production_path_component(self):
        d = tmpdir()
        sub = model_xml(tri_mesh("7", UNIT_FOUR_PTS, UNIT_FOUR_TRIS), "")
        top = ('<?xml version="1.0"?><model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"'
               ' xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06"><resources>'
               '<object id="1" type="model"><components><component p:path="/3D/Objects/o.model" objectid="7"/>'
               '</components></object></resources><build><item objectid="1"/></build></model>')
        sc = threemf.load(write_3mf(d / "p.3mf", top, {"3D/Objects/o.model": sub}))
        self.assertEqual(sc.n_tris, 12)

    def test_non_printable_item_skipped(self):
        d = tmpdir()
        m = model_xml(tri_mesh("1", UNIT_FOUR_PTS, UNIT_FOUR_TRIS),
                      '<item objectid="1" printable="0"/><item objectid="1" transform="1 0 0 0 1 0 0 0 1 9 0 0"/>')
        self.assertEqual(threemf.load(write_3mf(d / "n.3mf", m)).bbox(), [[9, 0, 0], [10, 1, 1]])

    def test_errors(self):
        d = tmpdir()
        (d / "bad.3mf").write_text("not a zip")
        with self.assertRaises(mesh.MeshError):
            threemf.load(d / "bad.3mf")
        with self.assertRaises(mesh.MeshError):      # no build items
            threemf.load(write_3mf(d / "e.3mf", model_xml(tri_mesh("1", UNIT_FOUR_PTS, UNIT_FOUR_TRIS), "")))
        with self.assertRaises(mesh.MeshError):      # unknown component target
            objs = '<object id="3"><components><component objectid="99"/></components></object>'
            threemf.load(write_3mf(d / "u.3mf", model_xml(objs, '<item objectid="3"/>')))
        with self.assertRaises(mesh.MeshError):      # cycle
            objs = '<object id="3"><components><component objectid="3"/></components></object>'
            threemf.load(write_3mf(d / "cy.3mf", model_xml(objs, '<item objectid="3"/>')))
        with self.assertRaises(mesh.MeshError):      # entity declarations refused
            x = '<?xml version="1.0"?><!DOCTYPE m [<!ENTITY a "b">]><model/>'
            threemf.load(write_3mf(d / "x.3mf", x))
        with self.assertRaises(mesh.MeshError):      # triangle with missing vertex
            threemf.load(write_3mf(d / "t.3mf", model_xml(tri_mesh("1", [(0, 0, 0)], [(0, 1, 2)]),
                                                          '<item objectid="1"/>')))


class Adapters(unittest.TestCase):
    def ctx(self, src: Path) -> Context:
        d = tmpdir()
        return Context(src, d / "out.glb", {}, d)

    def test_threemf_mesh(self):
        d = tmpdir()
        src = write_3mf(d / "a.3mf", model_xml(tri_mesh("1", UNIT_FOUR_PTS, UNIT_FOUR_TRIS), '<item objectid="1"/>'))
        ctx = self.ctx(src)
        res = threemf_mesh(ctx)
        self.assertTrue(res.ok)
        info = res.meta["render_info"]
        self.assertEqual(info["triangles"], 12)
        self.assertEqual(info["bbox"], [[0, 0, 0], [1, 1, 1]])
        self.assertEqual(ctx.out_path.read_bytes()[:4], b"glTF")

    def test_stl_mesh(self):
        d = tmpdir()
        (d / "c.stl").write_bytes(mesh.write_stl(FOUR))
        ctx = self.ctx(d / "c.stl")
        self.assertEqual(stl_mesh(ctx).meta["render_info"]["triangles"], 4)

    @unittest.skipUnless(shutil.which("openscad"), "openscad not installed")
    def test_scad_mesh(self):
        d = tmpdir()
        (d / "c.scad").write_text("cube([10, 20, 30]);\n")
        ctx = self.ctx(d / "c.scad")
        res = scad_mesh(ctx)
        self.assertTrue(res.ok, res.message)
        self.assertEqual(res.meta["render_info"]["bbox"], [[0, 0, 0], [10, 20, 30]])
        self.assertEqual(res.meta["render_info"]["triangles"], 12)

    @unittest.skipUnless(shutil.which("openscad"), "openscad not installed")
    def test_scad_mesh_failure_is_a_result(self):
        d = tmpdir()
        (d / "bad.scad").write_text("this is not scad (;\n")
        res = scad_mesh(self.ctx(d / "bad.scad"))
        self.assertFalse(res.ok)


def no_user_config():
    return load_registry(None, user_config=Path("/nonexistent"))


class Planning(unittest.TestCase):
    def test_interactive_only(self):
        reg = no_user_config()
        jobs = plan_jobs(reg, Path("x.3mf"), views=["interactive"])
        self.assertEqual([(j.chain[-1].id, j.view, j.kind) for j in jobs], [("mesh-3mf", "interactive", "mesh")])

    def test_mixed_with_png_views(self):
        reg = no_user_config()
        jobs = plan_jobs(reg, Path("x.3mf"), views=["iso", "interactive", "bottom"])
        self.assertEqual([(j.chain[-1].id, j.view) for j in jobs],
                         [("bambu-export-png", "iso"), ("bambu-export-png", "bottom"), ("mesh-3mf", "interactive")])

    def test_defaults_do_not_include_interactive(self):
        reg = no_user_config()
        jobs = plan_jobs(reg, Path("x.scad"))
        self.assertEqual([j.view for j in jobs], ["iso", "bottom"])
        self.assertTrue(all(j.kind == "image" for j in jobs))

    def test_mesh_adapters_never_chosen_for_png(self):
        reg = no_user_config()
        for ext in (".stl", ".glb"):
            with self.assertRaises(NoAdapter):
                plan_jobs(reg, Path("x" + ext))
        self.assertEqual(plan_jobs(reg, Path("x.stl"), views=["interactive"])[0].chain[0].id, "mesh-stl")

    def test_unsupported_source(self):
        with self.assertRaises(NoAdapter):
            plan_jobs(no_user_config(), Path("x.txt"), views=["interactive"])


class RunChain(unittest.TestCase):
    def test_mesh_render_published_and_indexed(self):
        d = tmpdir()
        src = write_3mf(d / "a.3mf", model_xml(tri_mesh("1", UNIT_FOUR_PTS, UNIT_FOUR_TRIS), '<item objectid="1"/>'))
        reg = no_user_config()
        job = plan_jobs(reg, src, views=["interactive"])[0]
        root = d / "renders"
        group = runner.make_group("t", [src], "tulda render a.3mf --views interactive")
        glb = runner.run_chain(reg, job.chain, src, root, "proj", None, False, group=group,
                               view=job.view, view_opts=job.options, seq=0)
        self.assertEqual(glb.suffix, ".glb")
        self.assertIn("-mesh-3mf-interactive-a", glb.name)
        side = json.loads(glb.with_suffix(".json").read_text())
        self.assertEqual((side["view"], side["kind"], side["stage"]), ("interactive", "mesh", "mesh"))
        self.assertEqual(side["info"]["triangles"], 12)
        self.assertEqual(side["group_id"], group["id"])
        self.assertNotIn("render_info", side["steps"][0])
        cat = Catalog(root, log=lambda m: None)
        g = cat.get_group(group["id"])
        self.assertEqual(len(g["items"]), 1)
        it = g["items"][0]
        self.assertEqual((it["kind"], it["view"], it["info"]["triangles"]), ("mesh", "interactive", 12))
        self.assertEqual(it["viewer_url"], f"/_v/proj/{glb.stem}/")
        self.assertEqual(it["url"], f"/proj/{glb.name}")

    def test_bad_input_is_a_failed_step_with_fail_json(self):
        d = tmpdir()
        src = d / "bad.3mf"
        src.write_text("nope")
        reg = no_user_config()
        job = plan_jobs(reg, src, views=["interactive"])[0]
        with self.assertRaises(StepFailed):
            runner.run_chain(reg, job.chain, src, d / "r", "proj", view=job.view)
        self.assertTrue(list((d / "r" / "proj").glob("*.fail.json")))


class MigrationV4(unittest.TestCase):
    def test_upgrade_from_v3_keeps_rows_as_images(self):
        root = tmpdir() / "renders"
        (root / "p").mkdir(parents=True)
        (root / "p" / "x.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 8)
        (root / "p" / "x.json").write_text(json.dumps({"adapter": "openscad", "stage": "source", "source": "x.scad",
                                                      "group_id": "20261124T100000Z-x", "created_utc": "2026-11-24T10:00:00Z"}))
        (root / ".tulda").mkdir()
        raw = sqlite3.connect(root / ".tulda" / "catalog.db", isolation_level=None)
        for m in (cm._m1, cm._m2, cm._m3):
            m(raw)
        raw.execute("PRAGMA user_version=3")
        raw.execute("INSERT INTO groups(id,sources,day,last_mtime,n_renders,starred) VALUES('20261124T100000Z-x','[]',"
                    "'2026-11-24',1,1,1)")
        raw.execute("INSERT INTO renders(path,project,file,group_id,mtime) VALUES('p/x.png','p','x.png','20261124T100000Z-x',1)")
        raw.close()
        cat = Catalog(root, log=lambda m: None)
        self.assertEqual(cat.stats()["schema_version"], 4)
        self.assertEqual(cm.SCHEMA_VERSION, 4)
        g = cat.get_group("20261124T100000Z-x")
        self.assertTrue(g["starred"])
        self.assertEqual((g["items"][0]["kind"], g["items"][0]["info"], g["items"][0]["viewer_url"]),
                         ("image", None, None))
        with cat.conn() as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(renders)")}
        self.assertTrue({"kind", "info"} <= cols)

    def test_glb_row_and_lookup(self):
        root = tmpdir() / "renders"
        (root / "p").mkdir(parents=True)
        (root / "p" / "m.glb").write_bytes(b"glTF" + b"0" * 20)
        (root / "p" / "m.json").write_text(json.dumps({
            "adapter": "mesh-3mf", "stage": "mesh", "source": "a.3mf", "view": "interactive", "kind": "mesh",
            "info": {"triangles": 5}, "group_id": "20261124T100000Z-g", "created_utc": "2026-11-24T10:00:00Z"}))
        cat = Catalog(root, log=lambda m: None)
        r = cat.get_render("p", "m")
        self.assertEqual((r["kind"], r["info"]["triangles"], r["turntable"]), ("mesh", 5, None))
        self.assertIsNone(cat.get_render("p", "nope"))
        self.assertIsNone(cat.get_render("q", "m"))
        self.assertEqual(cat.check(), [])
        res = cat.delete_group("20261124T100000Z-g")
        self.assertEqual(sorted(res["deleted"]), ["p/m.glb", "p/m.json"])
        self.assertFalse((root / "p" / "m.glb").exists())


if __name__ == "__main__":
    unittest.main()
