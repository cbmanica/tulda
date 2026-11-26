"""Provenance: self-reported 3mf <metadata> (licence, designer, title, MakerWorld ids) parsed,
recorded in sidecars and the catalog, and shown on the group page. Fixtures are synthetic."""

import io
import json
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tulda import catalog as cm
from tulda import cli, gallery, items, runner, threemf
from tulda.adapters import load_registry, plan_jobs
from tulda.catalog import Catalog
from tulda.mesh import MeshError

NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
CUBE = ('<object id="1" type="model"><mesh><vertices>'
        '<vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/>'
        '<vertex x="0" y="0" z="1"/></vertices><triangles><triangle v1="0" v2="2" v3="1"/>'
        '<triangle v1="0" v2="1" v3="3"/><triangle v1="1" v2="2" v3="3"/><triangle v1="0" v2="3" v3="2"/>'
        '</triangles></mesh></object>')


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp()).resolve()


def md(name, value=None):
    if value is None:
        return f'<metadata name="{name}"/>'
    return f'<metadata name="{name}">{value}</metadata>'


def model(head: str = "", tail: str = "") -> str:
    """Bambu puts some metadata before <resources> and some after <build>."""
    return (f'<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" xmlns="{NS}">{head}'
            f'<resources>{CUBE}</resources><build><item objectid="1"/></build>{tail}</model>')


def write(path: Path, xml, extra: dict | None = None) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("3D/3dmodel.model", xml)
        for k, v in (extra or {}).items():
            z.writestr(k, v)
    return path


POPULATED = model(
    md("Application", "BambuStudio-00.00.00.00") + md("Copyright", "") + md("Designer", "Example Designer")
    + md("License", "Example Licence") + md("Origin", "profile") + md("Title", "Ünïcode Bøx 箱"),
    md("CopyRight", "(c) Example") + md("DesignRegion", "xx") + md("DesignModelId", "USexample0000001")
    + md("DesignProfileId", "1234567"))
EMPTY = model(md("Title", "") + md("Designer") + md("License"), md("DesignModelId", ""))
FMI = model(md("Title") + md("Designer") + md("License") + md("Application", "Parametric"),
            md("DesignModelId", "FMI_example01"))


class Parse(unittest.TestCase):
    def test_populated_and_both_copyright_spellings(self):
        p = threemf.read_provenance(write(tmpdir() / "a.3mf", POPULATED))
        self.assertEqual(p["title"], "Ünïcode Bøx 箱")
        self.assertEqual(p["designer"], "Example Designer")
        self.assertEqual(p["license"], "Example Licence")
        self.assertEqual(p["copyright"], "(c) Example")        # empty "Copyright" skipped
        self.assertEqual((p["design_model_id"], p["design_profile_id"], p["design_region"]),
                         ("USexample0000001", "1234567", "xx"))
        self.assertEqual((p["origin"], p["kind"]), ("profile", "makerworld"))

    def test_empty_and_self_closing(self):
        p = threemf.read_provenance(write(tmpdir() / "e.3mf", EMPTY))
        self.assertEqual({k for k, v in p.items() if v}, set())
        self.assertIsNone(p["kind"])

    def test_no_metadata_at_all(self):
        p = threemf.read_provenance(write(tmpdir() / "n.3mf", model()))
        self.assertFalse(any(p.values()))

    def test_fmi_is_parametric(self):
        p = threemf.read_provenance(write(tmpdir() / "f.3mf", FMI))
        self.assertEqual((p["design_model_id"], p["kind"]), ("FMI_example01", "parametric"))

    def test_non_bambu_namespace_and_root_from_rels(self):
        xml = ('<?xml version="1.0"?><m:model xmlns:m="urn:other">'
               '<m:metadata name="Title">Other</m:metadata></m:model>')
        rels = ('<Relationships><Relationship Target="/3D/main.model" Id="r" '
                'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        d = tmpdir()
        with zipfile.ZipFile(d / "r.3mf", "w") as z:
            z.writestr("_rels/.rels", rels)
            z.writestr("3D/main.model", xml)
        self.assertEqual(threemf.read_provenance(d / "r.3mf")["title"], "Other")

    def test_nested_metadata_is_ignored(self):
        xml = model(md("Title", "Top"), '<extra>' + md("License", "nested") + '</extra>')
        p = threemf.read_provenance(write(tmpdir() / "x.3mf", xml))
        self.assertEqual((p["title"], p["license"]), ("Top", None))

    def test_values_capped_and_control_chars_dropped(self):
        xml = model(md("Title", "x" * 2000) + md("Designer", "  \n red&#x9;x  "))
        p = threemf.read_provenance(write(tmpdir() / "c.3mf", xml))
        self.assertEqual(len(p["title"]), threemf.MAX_META_VALUE)
        self.assertEqual(p["designer"], "red\tx")
        self.assertEqual(threemf._clean("a\x1b[31mb\x00"), "a[31mb")   # XML 1.0 cannot carry these

    def test_rejects_entities_and_doctype(self):
        bomb = ('<?xml version="1.0"?><!DOCTYPE model [<!ENTITY a "aaaaaaaaaa">'
                '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
                f'<model xmlns="{NS}"><metadata name="Title">&b;</metadata></model>')
        with self.assertRaises(MeshError):
            threemf.read_provenance(write(tmpdir() / "b.3mf", bomb))
        ext = ('<?xml version="1.0"?><!DOCTYPE m SYSTEM "file:///etc/passwd">'
               f'<model xmlns="{NS}"/>')
        with self.assertRaises(MeshError):
            threemf.read_provenance(write(tmpdir() / "s.3mf", ext))

    def test_malformed(self):
        d = tmpdir()
        (d / "z.3mf").write_bytes(b"not a zip")
        with self.assertRaises(MeshError):
            threemf.read_provenance(d / "z.3mf")
        with self.assertRaises(MeshError):
            threemf.read_provenance(write(d / "nomodel.3mf", "", {"other.txt": "x"}).with_name("nomodel.3mf"))
        with self.assertRaises(MeshError):
            threemf.read_provenance(write(d / "bad.3mf", "<model><unclosed"))
        # metadata read before a later syntax error are kept
        p = threemf.read_provenance(write(d / "late.3mf", f'<model xmlns="{NS}">{md("License", "L")}<oops></model>'))
        self.assertEqual(p["license"], "L")

    def test_oversized_member_refused(self):
        d = tmpdir()
        # highly compressible: tiny on disk, large uncompressed (zip-bomb shape)
        big = model(md("Title", "t")) + " " * (2 * 1024 * 1024)
        f = write(d / "big.3mf", big)
        self.assertLess(f.stat().st_size, 64 * 1024)
        with mock.patch.object(threemf, "MAX_META_SCAN", 1024 * 1024):
            with self.assertRaises(MeshError):
                threemf.read_provenance(f)
        self.assertEqual(threemf.read_provenance(f)["title"], "t")

    def test_lying_size_header_is_still_capped(self):
        f = write(tmpdir() / "lie.3mf", model(md("Title", "t")) + " " * 300_000)
        real = zipfile.ZipFile.getinfo

        def small(self, name):
            i = real(self, name)
            i.file_size = 10
            return i
        with mock.patch.object(threemf, "MAX_META_SCAN", 100_000), \
                mock.patch.object(zipfile.ZipFile, "getinfo", small):
            with self.assertRaises(MeshError):
                threemf.read_provenance(f)


class Display(unittest.TestCase):
    def test_lines(self):
        p = items.clean_provenance(threemf.read_provenance(write(tmpdir() / "a.3mf", POPULATED)))
        lines = dict(items.provenance_lines(p))
        self.assertEqual(lines["Licence"], "Example Licence")
        self.assertEqual(lines["Source"], "MakerWorld model")
        self.assertEqual(lines["Title"], "Ünïcode Bøx 箱")
        for v in lines.values():
            self.assertNotIn("http", v)                       # never a MakerWorld URL

    def test_empty_licence_is_unknown_not_unlicensed(self):
        p = items.clean_provenance(threemf.read_provenance(write(tmpdir() / "e.3mf", EMPTY)))
        lines = items.provenance_lines(p)
        self.assertEqual(lines, [["Licence", "unknown (not recorded in file)"]])
        self.assertNotIn("unlicensed", json.dumps(lines).lower())

    def test_fmi_line(self):
        p = items.clean_provenance(threemf.read_provenance(write(tmpdir() / "f.3mf", FMI)))
        self.assertIn(["Source", "MakerWorld model (Parametric Model Maker)"], items.provenance_lines(p))

    def test_clean_rejects_junk(self):
        self.assertIsNone(items.clean_provenance("x"))
        self.assertIsNone(items.clean_provenance({"error": "bad"}))
        p = items.clean_provenance({"title": 5, "license": ["x"], "kind": "evil", "extra": "y"})
        self.assertEqual(set(p), set(threemf.PROVENANCE_KEYS) | {"kind"})
        self.assertFalse(any(p.values()))
        self.assertEqual(items.provenance_lines(None), [])


def no_user_config():
    return load_registry(None, user_config=Path("/nonexistent"))


XSS = '&lt;/script&gt;&lt;img src=x onerror=alert(1)&gt;"\'&amp;'


class Recorded(unittest.TestCase):
    def render(self, xml, name="a.3mf"):
        d = tmpdir()
        src = write(d / name, xml)
        reg = no_user_config()
        job = plan_jobs(reg, src, views=["interactive"])[0]
        root = d / "renders"
        group = runner.make_group("t", [src], "tulda render a.3mf --views interactive")
        out = runner.run_chain(reg, job.chain, src, root, "proj", None, False, group=group,
                               view=job.view, view_opts=job.options, seq=0)
        return root, out, group

    def test_sidecar_catalog_and_page(self):
        root, out, group = self.render(model(md("Title", XSS) + md("License", "Example Licence"),
                                             md("DesignModelId", "USexample0000001")))
        side = json.loads(out.with_suffix(".json").read_text())
        self.assertEqual(side["provenance"]["title"], '</script><img src=x onerror=alert(1)>"\'&')
        self.assertEqual(side["provenance"]["kind"], "makerworld")
        cat = Catalog(root, log=lambda m: None)
        g = cat.get_group(group["id"])
        prov = dict(g["items"][0]["prov"])
        self.assertEqual(prov["Licence"], "Example Licence")
        self.assertEqual(prov["Source"], "MakerWorld model")
        page = gallery.render_index({"ctx": {"kind": "group", "group": group["id"]}, "groups": [g],
                                     "next": None, "facets": {}, "dates": [], "filters": {}, "qs": "",
                                     "rev": 0})
        data = page.split('id="data">', 1)[1].split("</script>", 1)[0]
        self.assertNotIn("<", data)
        self.assertNotIn("onerror=alert(1)>", page)
        blob = json.loads(data)
        self.assertIn("authoritative", blob["prov_caveat"])
        self.assertIn("provBlock", page)

    def test_non_3mf_source_has_no_provenance(self):
        self.assertIsNone(runner.source_provenance(Path("x.scad")))

    def test_unreadable_metadata_does_not_fail_or_claim_unknown(self):
        d = tmpdir()
        (d / "bad.3mf").write_bytes(b"nope")
        p = runner.source_provenance(d / "bad.3mf")
        self.assertIn("error", p)
        self.assertEqual(items.provenance_lines(items.clean_provenance(p)), [])

    def test_migration_v4_to_v5_backfills(self):
        root, out, group = self.render(model(md("License", "Example Licence")))
        db = root / ".tulda" / "catalog.db"
        db.unlink()
        for x in root.joinpath(".tulda").glob("catalog.db*"):
            x.unlink()
        raw = sqlite3.connect(db, isolation_level=None)
        for m in cm.MIGRATIONS[:4]:
            m(raw)
        raw.execute("PRAGMA user_version=4")
        raw.close()
        cat = Catalog(root, log=lambda m: None)
        self.assertEqual(cat.stats()["schema_version"], cm.SCHEMA_VERSION)
        cat.sync()
        it = cat.get_group(group["id"])["items"][0]
        self.assertEqual(dict(it["prov"])["Licence"], "Example Licence")


class InfoCli(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_info(self):
        f = write(tmpdir() / "a.3mf", POPULATED)
        code, out, _ = self.run_cli("info", str(f))
        self.assertEqual(code, 0)
        self.assertIn("Example Licence", out)
        self.assertIn("MakerWorld model", out)
        self.assertIn("authoritative", out)
        self.assertNotIn("http", out)
        code, out, _ = self.run_cli("info", "--json", str(f))
        self.assertEqual(json.loads(out)["provenance"]["kind"], "makerworld")

    def test_info_errors(self):
        d = tmpdir()
        (d / "x.scad").write_text("cube(1);")
        (d / "bad.3mf").write_bytes(b"nope")
        self.assertEqual(self.run_cli("info", str(d / "x.scad"))[0], 2)
        code, _, err = self.run_cli("info", str(d / "bad.3mf"))
        self.assertEqual(code, 1)
        self.assertIn("bad.3mf", err)


if __name__ == "__main__":
    unittest.main()
