import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tulda.adapters import load_registry, plan, render_argv
from tulda.adapters.registry import Registry
from tulda.errors import ConfigError, MissingRequirement, NoAdapter
from tulda import runner

CONF = """
[adapter.a-png]
accepts=[".x"]
produces="png"
stage="source"
requires=["sh"]
cmd=["sh","-c","true","{out}"]
[adapter.a-png-hi]
accepts=[".x"]
produces="png"
stage="built"
priority=5
requires=["sh"]
cmd=["sh","-c","true","{out}"]
[adapter.to-mid]
accepts=[".y"]
produces="mid"
requires=["sh"]
cmd=["sh","{in}","{out}"]
[adapter.mid-png]
accepts=[".mid"]
produces="png"
stage="built"
requires=["sh"]
cmd=["sh","{in}","{out}"]
[adapter.mid-mid2]
accepts=[".mid"]
produces="mid2"
requires=["sh"]
cmd=["sh","{in}","{out}"]
[adapter.mid2-png]
accepts=[".mid2"]
produces="png"
stage="built"
requires=["sh"]
cmd=["sh","{in}","{out}"]
[adapter.needs-nothing-real]
accepts=[".z"]
produces="png"
stage="source"
requires=["definitely-not-a-binary-xyz"]
cmd=["definitely-not-a-binary-xyz","{out}"]
[chain.mine]
steps=["to-mid","mid-png"]
"""


def reg_from(text):
    d = tempfile.mkdtemp()
    p = Path(d) / "c.toml"
    p.write_text(text)
    r = Registry()
    r.merge_file(p)
    return r


class Templating(unittest.TestCase):
    def test_basic_and_escape(self):
        self.assertEqual(render_argv(["a", "{w}x{h}", "{{lit}}", "{in}"],
                                     {"w": 1, "h": 2, "in": "/p q"}), ["a", "1x2", "{lit}", "/p q"])

    def test_list_expansion(self):
        self.assertEqual(render_argv(["x", "{f}", "y"], {"f": ["--a", "--b"]}), ["x", "--a", "--b", "y"])
        self.assertEqual(render_argv(["x", "{f}"], {"f": []}), ["x"])

    def test_unknown(self):
        with self.assertRaises(ConfigError):
            render_argv(["{nope}"], {})

    def test_no_shell_metachar_interpretation(self):
        self.assertEqual(render_argv(["{in}"], {"in": "a; rm -rf /"}), ["a; rm -rf /"])

    def test_doctor_errors(self):
        r = reg_from('[adapter.b]\naccepts=[".a"]\nproduces="png"\nstage="source"\ncmd=["t","{bogus}"]')
        errs = r.adapters["b"].config_errors({})
        self.assertTrue(any("bogus" in e for e in errs))
        self.assertTrue(any("{out}" in e for e in errs))

    def test_builtin_openscad_argv(self):
        r = load_registry(None, user_config=Path("/nonexistent"))
        a = r.adapters["openscad"]
        from tulda.adapters.registry import option_values
        v = option_values(r, a, {"w": 800})
        v.update({"in": "/a.scad", "out": "/o.png"})
        argv = render_argv(a.cmd, v)
        self.assertIn("--imgsize=800,1200", argv)
        self.assertEqual(argv[-1], "/a.scad")
        self.assertNotIn("{backend_args}", argv)


class Selection(unittest.TestCase):
    def setUp(self):
        self.r = reg_from(CONF)

    def test_priority_wins(self):
        self.assertEqual([a.id for a in plan(self.r, Path("f.x"))[0]], ["a-png-hi"])

    def test_extension_case_insensitive(self):
        self.assertEqual(plan(self.r, Path("F.X"))[0][0].id, "a-png-hi")

    def test_stage_filter(self):
        self.assertEqual(plan(self.r, Path("f.x"), stage="source")[0][0].id, "a-png")

    def test_all(self):
        self.assertEqual([c[0].id for c in plan(self.r, Path("f.x"), all_=True)], ["a-png-hi", "a-png"])

    def test_chain_bfs_shortest(self):
        self.assertEqual([a.id for a in plan(self.r, Path("f.y"))[0]], ["to-mid", "mid-png"])

    def test_chain_depth_limit(self):
        # .y -> mid -> mid2 -> png would be depth 3 but a 2-step exists; remove it
        r = reg_from(CONF)
        r.adapters["mid-png"].enabled = False
        self.assertEqual([a.id for a in plan(r, Path("f.y"))[0]],
                         ["to-mid", "mid-mid2", "mid2-png"])

    def test_chain_too_deep(self):
        r = reg_from("""
[adapter.s1]
accepts=[".a"]
produces="b"
cmd=["x","{out}"]
[adapter.s2]
accepts=[".b"]
produces="c"
cmd=["x","{out}"]
[adapter.s3]
accepts=[".c"]
produces="d"
cmd=["x","{out}"]
[adapter.s4]
accepts=[".d"]
produces="png"
stage="built"
cmd=["x","{out}"]
""")
        with self.assertRaises(NoAdapter):
            plan(r, Path("f.a"))

    def test_explicit_adapter_and_chain(self):
        self.assertEqual(plan(self.r, Path("f.x"), adapter="a-png")[0][0].id, "a-png")
        self.assertEqual([a.id for a in plan(self.r, Path("f.y"), adapter="mine")[0]], ["to-mid", "mid-png"])
        self.assertEqual([a.id for a in plan(self.r, Path("f.y"), adapter="to-mid,mid-png")[0]], ["to-mid", "mid-png"])

    def test_explicit_errors(self):
        with self.assertRaises(NoAdapter):
            plan(self.r, Path("f.y"), adapter="a-png")  # wrong ext
        with self.assertRaises(NoAdapter):
            plan(self.r, Path("f.y"), adapter="to-mid")  # not png
        with self.assertRaises(NoAdapter):
            plan(self.r, Path("f.y"), adapter="ghost")

    def test_missing_requirement_and_no_adapter(self):
        with self.assertRaises(MissingRequirement):
            plan(self.r, Path("f.z"))
        with self.assertRaises(NoAdapter) as cm:
            plan(self.r, Path("f.unknown"))
        self.assertIn("supported extensions", str(cm.exception))


class Running(unittest.TestCase):
    def test_real_subprocess_chain_and_sidecar(self):
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 20
        r = reg_from(f"""
[adapter.mk]
accepts=[".q"]
produces="png"
stage="source"
requires=["sh"]
cmd=["sh","-c","printf '\\\\211PNG\\\\r\\\\n\\\\032\\\\n' > \\"$0\\"","{{out}}"]
""")
        d = Path(tempfile.mkdtemp())
        (d / "src").mkdir()
        f = d / "src" / "m.q"
        f.write_text("hi")
        out = runner.run_chain(r, r.adapters["mk"] and [r.adapters["mk"]], f, d / "renders", "proj")
        self.assertTrue(out.name.endswith("-mk-m.png"))
        import json
        sc = json.loads(out.with_suffix(".json").read_text())
        self.assertEqual((sc["adapter"], sc["stage"], sc["source_type"]), ("mk", "source", "q"))
        self.assertEqual(len(sc["source_sha256"]), 64)

    def test_failure_writes_fail_json_no_png(self):
        r = reg_from('[adapter.bad]\naccepts=[".q"]\nproduces="png"\nstage="source"\nrequires=["sh"]\n'
                     'cmd=["sh","-c","echo boom >&2; exit 3","{out}"]')
        d = Path(tempfile.mkdtemp())
        f = d / "m.q"
        f.write_text("x")
        from tulda.errors import StepFailed
        with self.assertRaises(StepFailed):
            runner.run_chain(r, [r.adapters["bad"]], f, d / "r", "p")
        names = [p.name for p in (d / "r" / "p").iterdir()]
        self.assertTrue(any(n.endswith(".fail.json") for n in names))
        self.assertFalse(any(n.endswith(".png") for n in names))

    def test_timeout(self):
        r = reg_from('[adapter.slow]\naccepts=[".q"]\nproduces="png"\nstage="source"\nrequires=["sh"]\n'
                     'timeout=0.3\ncmd=["sh","-c","sleep 5","{out}"]')
        d = Path(tempfile.mkdtemp())
        f = d / "m.q"
        f.write_text("x")
        from tulda.errors import StepTimeout
        with self.assertRaises(StepTimeout):
            runner.run_chain(r, [r.adapters["slow"]], f, d / "r", "p")


class BambuExportPng(unittest.TestCase):
    """Subprocess mocked: the adapter must create plate_1_0.png in {workdir}."""

    def test_command_and_outglob(self):
        r = load_registry(None, user_config=Path("/nonexistent"))
        a = r.adapters["bambu-export-png"]
        a.requires = []  # do not depend on Bambu Studio being installed
        d = Path(tempfile.mkdtemp())
        f = d / "m.3mf"
        f.write_bytes(b"PK")
        captured = {}

        real_popen = runner.subprocess.Popen

        def popen(argv, **kw):
            return real_popen(argv, **kw) if argv[0] == "git" else FakeProc(argv, **kw)

        class FakeProc:
            returncode = 0

            def __init__(self, argv, **kw):
                captured["argv"] = argv
                outdir = Path(argv[argv.index("--outputdir") + 1])
                assert outdir.is_dir(), "outputdir must exist before the call"
                (outdir / "plate_1_0.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 10)
                (outdir / "result.json").write_text("{}")

            def communicate(self, timeout=None):
                return "", ""

        with mock.patch("tulda.runner.subprocess.Popen", popen):
            png = runner.run_chain(r, [a], f, d / "r", "p")
        argv = captured["argv"]
        self.assertEqual(argv[1:3], ["--export-png", "1"])
        self.assertEqual(argv[-1], str(f.resolve()))
        self.assertTrue(png.name.endswith("-bambu-export-png-m.png"))
        self.assertEqual(plan(r, Path("x.3mf"))[0][0].id, "bambu-export-png")


if __name__ == "__main__":
    unittest.main()
