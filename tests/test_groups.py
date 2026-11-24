import http.client
import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tulda import cli, runner
from tulda.adapters import load_registry, parse_views, plan_jobs
from tulda.adapters.registry import Registry
from tulda.errors import ConfigError, NoAdapter
from tulda.server import make_server, scan

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16
MK = r'''
[adapter.mk]
accepts=[".q"]
produces="png"
stage="source"
requires=["sh"]
default_views=["one","two"]
cmd=["sh","-c","printf '\\211PNG\\r\\n\\032\\n' > \"$0\"; echo \"$1\" > /dev/null","{out}","{tag}"]
[adapter.mk.options]
tag="none"
[adapter.mk.views]
one={options={tag="1"}}
two={options={tag="2"}}
three={options={tag="3"}}
'''


def reg_from(text):
    p = Path(tempfile.mkdtemp()) / "c.toml"
    p.write_text(text)
    r = Registry()
    r.merge_file(p)
    return r


def no_user_config():
    return load_registry(None, user_config=Path("/nonexistent"))


class ViewParsing(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_views(["iso,bottom", "top"], "bottom, front"), ["iso", "bottom", "top", "front"])
        self.assertEqual(parse_views([], None), [])
        self.assertEqual(parse_views(["ISO"]), ["iso"])

    def test_builtin_defaults(self):
        r = no_user_config()
        self.assertEqual(r.adapters["openscad"].default_views, ["iso", "bottom"])
        self.assertEqual(r.adapters["bambu-export-png"].default_views, ["iso", "bottom"])
        for a in r.adapters.values():
            self.assertEqual(a.config_errors(r.defaults), [], a.id)
        cam = r.adapters["openscad"].views["bottom"]["options"]["camera_args"]
        self.assertEqual(cam[0], "--camera=0,0,0,180,0,0,0")
        self.assertEqual(r.adapters["bambu-export-png"].views["bottom"]["options"]["camera_view"], 5)

    def test_plan_jobs_defaults_and_explicit(self):
        r = reg_from(MK)
        f = Path("a.q")
        self.assertEqual([j.view for j in plan_jobs(r, f)], ["one", "two"])
        self.assertEqual([j.view for j in plan_jobs(r, f, views=["three"])], ["three"])
        self.assertEqual(plan_jobs(r, f, views=["three"])[0].options, {"tag": "3"})
        with self.assertRaises(NoAdapter) as cm:
            plan_jobs(r, f, views=["nope"])
        self.assertIn("known views: one, two, three", str(cm.exception))

    def test_adapter_without_views_has_one_viewless_job(self):
        r = reg_from('[adapter.p]\naccepts=[".q"]\nproduces="png"\nstage="source"\nrequires=["sh"]\ncmd=["sh","{out}"]')
        jobs = plan_jobs(r, Path("a.q"))
        self.assertEqual([j.view for j in jobs], [None])

    def test_bad_view_config(self):
        for extra in ('default_views=["x"]', 'views={x={}}', 'views={"Bad Name"={options={}}}'):
            with self.assertRaises(ConfigError, msg=extra):
                reg_from(f'[adapter.p]\naccepts=[".q"]\nproduces="png"\nstage="source"\n'
                         f'cmd=["sh","{{out}}"]\n{extra}')

    def test_bambu_view_argv(self):
        r = no_user_config()
        a = r.adapters["bambu-export-png"]
        from tulda.adapters.registry import option_values, render_argv
        opts = option_values(r, a, dict(a.views["bottom"]["options"]))
        argv = render_argv(a.cmd, dict(opts, **{"in": "/m.3mf", "out": "/o.png", "workdir": "/w"}))
        i = argv.index("--camera-view")
        self.assertEqual(argv[i + 1], "5")

    def test_openscad_view_argv(self):
        r = no_user_config()
        a = r.adapters["openscad"]
        from tulda.adapters.registry import option_values, render_argv
        opts = option_values(r, a, dict(a.views["bottom"]["options"]))
        argv = render_argv(a.cmd, dict(opts, **{"in": "/a.scad", "out": "/o.png"}))
        self.assertIn("--camera=0,0,0,180,0,0,0", argv)
        self.assertIn("--viewall", argv)
        self.assertEqual(argv[-1], "/a.scad")


class GroupRun(unittest.TestCase):
    def test_make_group(self):
        now = datetime(2026, 11, 24, 22, 15, 3, tzinfo=timezone.utc)
        g = runner.make_group("NFC Pizza: test!", [Path("/x/a.scad"), Path("/y/b.3mf")], "tulda render a.scad b.3mf", now)
        self.assertEqual(g["id"], "20261124T221503Z-nfc-pizza-test")
        self.assertEqual(g["sources"], ["a.scad", "b.3mf"])
        g2 = runner.make_group(None, [Path("/x/Part One.scad")], "c", now)
        self.assertEqual(g2["id"], "20261124T221503Z-part-one")
        self.assertIsNone(g2["title"])
        self.assertTrue(runner.make_group(None, [Path("/x/___.scad")], "c")["id"].endswith("-render"))

    def test_sidecar_fields_and_view_filename(self):
        r = reg_from(MK)
        d = Path(tempfile.mkdtemp())
        f = d / "m.q"
        f.write_text("x")
        g = runner.make_group("T", [f], "tulda render m.q")
        png = runner.run_chain(r, [r.adapters["mk"]], f, d / "r", "p", group=g, view="two",
                               view_opts={"tag": "2"}, seq=4)
        self.assertIn("-mk-two-m", png.name)
        sc = json.loads(png.with_suffix(".json").read_text())
        self.assertEqual((sc["group_id"], sc["group_title"], sc["view"], sc["seq"]), (g["id"], "T", "two", 4))
        self.assertEqual(sc["group_sources"], ["m.q"])
        self.assertEqual(sc["request_cmd"], "tulda render m.q")
        self.assertEqual(sc["options"]["tag"], "2")

    def test_cli_multi_file_one_group(self):
        d = Path(tempfile.mkdtemp())
        (d / "one").mkdir()
        (d / "two").mkdir()
        (d / "one" / "a.q").write_text("x")
        (d / "two" / "b.q").write_text("y")
        conf = d / "c.toml"
        conf.write_text(MK)
        root = d / "renders"
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("tulda.adapters.registry.user_config_path",
                                              return_value=Path("/nonexistent")):
            code = cli.main(["render", str(d / "one" / "a.q"), str(d / "two" / "b.q"), "--config", str(conf),
                             "--renders-root", str(root), "--title", "Both", "--views", "one,three"])
        self.assertEqual(code, 0, out.getvalue())
        items = scan(root.resolve())
        self.assertEqual(len(items), 4)
        self.assertEqual(len({i["group_id"] for i in items}), 1)
        self.assertEqual({i["project"] for i in items}, {"one", "two"})
        self.assertEqual({i["view"] for i in items}, {"one", "three"})
        self.assertEqual({i["group_title"] for i in items}, {"Both"})
        self.assertEqual(items[0]["group_sources"], ["a.q", "b.q"])
        self.assertEqual(sorted(i["seq"] for i in items), [0, 1, 2, 3])
        self.assertIn("/_g/", out.getvalue())
        self.assertNotIn(str(d), items[0]["request_cmd"])
        self.assertIn("--views one,three", items[0]["request_cmd"])

    def test_cli_unknown_view_continues_and_fails(self):
        d = Path(tempfile.mkdtemp())
        (d / "a.q").write_text("x")
        conf = d / "c.toml"
        conf.write_text(MK)
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = cli.main(["render", str(d / "a.q"), "--config", str(conf),
                             "--renders-root", str(d / "r"), "--view", "zzz"])
        self.assertNotEqual(code, 0)
        self.assertIn("no view 'zzz'", err.getvalue())


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp()).resolve() / "renders"
        for proj in ("nfc", "build"):
            (cls.root / proj).mkdir(parents=True)

        def put(proj, name, meta=None):
            (cls.root / proj / f"{name}.png").write_bytes(PNG)
            if meta is not None:
                (cls.root / proj / f"{name}.json").write_text(json.dumps(meta))
        gid = "20261124T221503Z-pizza"
        base = {"adapter": "openscad", "stage": "source", "group_id": gid, "group_title": "Pizza",
                "group_sources": ["a.scad", "b.3mf"], "request_cmd": "tulda render a.scad b.3mf"}
        put("nfc", "a-iso", dict(base, view="iso", seq=0))
        put("nfc", "a-bottom", dict(base, view="bottom", seq=1))
        put("build", "b-bottom", dict(base, adapter="bambu-export-png", stage="sliced", view="bottom", seq=2))
        put("build", "old", {"adapter": "openscad", "stage": "source"})   # legacy sidecar
        put("build", "bare")                                              # no sidecar
        put("nfc", "evil", {"group_id": "../../etc", "group_title": 5, "adapter": "x", "stage": "source"})
        (cls.root / "_g").mkdir()
        (cls.root / "_g" / "z.png").write_bytes(PNG)
        cls.srv = make_server(cls.root, "127.0.0.1", 0)
        cls.port = cls.srv.server_address[1]
        cls.gid = gid
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        b = r.read()
        c.close()
        return r.status, b


class IndexGrouping(Base):
    def test_scan_groups_across_projects(self):
        items = scan(self.root)
        grouped = [i for i in items if i["group_id"] == self.gid]
        self.assertEqual({i["project"] for i in grouped}, {"nfc", "build"})
        self.assertEqual(sorted(i["view"] for i in grouped), ["bottom", "bottom", "iso"])
        self.assertTrue(all(not i["legacy"] and i["group_title"] == "Pizza" for i in grouped))

    def test_legacy_and_invalid_group_ids(self):
        items = {i["file"]: i for i in scan(self.root)}
        for n in ("old.png", "bare.png", "evil.png"):
            self.assertTrue(items[n]["legacy"], n)
            self.assertRegex(items[n]["group_id"], r"^legacy-\d{8}$")
            self.assertIsNone(items[n]["view"])
        self.assertEqual(items["evil.png"]["group_title"].startswith("Earlier renders"), True)
        self.assertNotIn("z.png", items)   # a project dir named like the group route is not listed

    def test_api_index_keeps_working(self):
        s, b = self.get("/api/index")
        self.assertEqual(s, 200)
        self.assertIn("group_id", json.loads(b)[0])

    def test_pages(self):
        for p in ("/", "/nfc/", "/build/"):
            s, b = self.get(p)
            self.assertEqual(s, 200, p)
            self.assertIn(b"Recent requests", b)
            self.assertIn(self.gid.encode(), b)
        self.assertEqual(self.get("/nfc/a-iso.png")[0], 200)
        self.assertEqual(self.get("/nfc/a-iso.json")[0], 200)

    def test_group_page(self):
        s, b = self.get(f"/_g/{self.gid}/")
        self.assertEqual(s, 200)
        self.assertIn(b'"group": "' + self.gid.encode(), b)
        self.assertEqual(self.get(f"/_g/{self.gid}")[0], 200)

    def test_group_page_safety(self):
        for p in ("/_g/", "/_g", "/_g/nope/", f"/_g/{self.gid}/extra", "/_g/..%2f..%2fetc/", "/_g/%2e%2e/",
                  "/_g/a%00b/", "/_g/a%20b/", "/_g/.hidden/"):
            self.assertIn(self.get(p)[0], (400, 403, 404), p)
        self.assertEqual(self.get("/_g/z.png")[0], 404)

    def test_embedded_json_is_escaped(self):
        s, b = self.get("/")
        data = b.split(b'id="data">', 1)[1].split(b"</script>", 1)[0]
        self.assertNotIn(b"<", data)


if __name__ == "__main__":
    unittest.main()
