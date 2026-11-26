"""Render triggers (manual / web / watch, plus --note) and viewer-local timestamps (<time>
elements with UTC fallback, hashed inline script in the CSP, UTC watcher event times).
All fixtures are synthetic."""

import base64
import hashlib
import http.client
import io
import json
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tulda import catalog as cm
from tulda import cli, projects, timefmt, webrender
from tulda.catalog import Catalog, Filters
from tulda.gallery import noscript_list, render_index, trigger_badge
from tulda.server import PAGE_CSP, make_server, parse_filters
from tulda.watchevents import EventLog, display_events
from tulda.watchpage import render_watch_page

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16
MK = r'''
[adapter.mk]
accepts=[".q"]
produces="png"
stage="source"
requires=["sh"]
default_views=["one"]
cmd=["sh","-c","printf '\\211PNG\\r\\n\\032\\n' > \"$0\"","{out}"]
[adapter.mk.views]
one={options={}}
'''
XSS = '<img src=x onerror=alert(1)>"\'&'
Z_TIME = re.compile(r'<time class=lt datetime="\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ" title="[^"]+ UTC">'
                    r'\d{4}-\d\d-\d\d \d\d:\d\d UTC</time>')


def render_cli(*extra):
    d = Path(tempfile.mkdtemp())
    (d / "p").mkdir()
    (d / "p" / "a.q").write_text("x")
    (d / "c.toml").write_text(MK)
    root = d / "renders"
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err), \
            mock.patch("tulda.adapters.registry.user_config_path", return_value=Path("/nonexistent")):
        code = cli.main(["render", str(d / "p" / "a.q"), "--config", str(d / "c.toml"),
                         "--renders-root", str(root), *extra])
    assert code == 0, out.getvalue() + err.getvalue()
    side = json.loads(next((root / "p").glob("*.json")).read_text())
    return root, side


def mk(root: Path, gid: str, created: str, trigger=None, note=None, name="a"):
    d = root / "p"
    d.mkdir(parents=True, exist_ok=True)
    meta = {"adapter": "openscad", "stage": "source", "source": f"{name}.scad", "view": "iso",
            "created_utc": created, "group_id": gid, "group_title": "T", "group_sources": [f"{name}.scad"],
            "request_cmd": "tulda render x", "group_created_utc": created, "seq": 0}
    if trigger:
        meta["trigger"] = trigger
    if note:
        meta["trigger_note"] = note
    (d / f"{gid}-{name}.png").write_bytes(PNG)
    (d / f"{gid}-{name}.json").write_text(json.dumps(meta))


class TriggerRecorded(unittest.TestCase):
    def test_cli_default_is_manual(self):
        root, side = render_cli()
        self.assertEqual(side["trigger"], "manual")
        self.assertNotIn("trigger_note", side)
        g = Catalog(root, log=lambda m: None).list_groups()[0][0]
        self.assertEqual((g["trigger"], g["trigger_note"]), ("manual", None))

    def test_cli_trigger_and_note_cleaned(self):
        root, side = render_cli("--trigger", "web", "--note", "rendered by\nassistant\x07 " + "x" * 300)
        self.assertEqual(side["trigger"], "web")
        self.assertTrue(side["trigger_note"].startswith("rendered by assistant x"))
        self.assertEqual(len(side["trigger_note"]), 200)
        g = Catalog(root, log=lambda m: None).list_groups()[0][0]
        self.assertEqual(g["trigger"], "web")

    def test_cli_rejects_unknown_trigger(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["render", "a.q", "--trigger", "bogus"])

    def test_web_worker_passes_web(self):
        calls = []

        class P:
            pid, returncode = 1, 0

            def __init__(self, argv, **kw):
                calls.append(argv)

            def communicate(self, timeout=None):
                return "", ""
        f = {"path": Path("/tmp/proj/files/a.3mf"), "ext": ".3mf", "rel": "files/a.3mf"}
        with mock.patch("subprocess.Popen", P):
            webrender.cli_render({"folder": "Proj", "title": "Proj"}, f, Path("/tmp/r"))
        self.assertIn("--trigger=web", calls[0])
        self.assertLess(calls[0].index("--trigger=web"), calls[0].index("--"))

    def test_watcher_passes_watch(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        proj = d / "Proj"
        proj.mkdir()
        (proj / projects.MANIFEST).write_text(json.dumps({"files": ["files/a.scad"]}))
        with mock.patch("tulda.cli.main", return_value=0) as m:
            projects.render_project(proj, lambda s: None, trigger="watch")
            projects.render_project(proj, lambda s: None)
        self.assertEqual(m.call_args_list[0].args[0][-2:], ["--trigger", "watch"])
        self.assertEqual(m.call_args_list[1].args[0][-2:], ["--trigger", "manual"])

    def test_watcher_loop_renders_with_watch(self):
        from tests.test_projects import write_3mf
        d = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, d)
        inbox, dest = d / "in", d / "out"
        inbox.mkdir()
        now = time.time() + 100
        w = projects.Watcher(inbox, dest, settle=5, render=True, log=lambda s: None, clock=lambda: now)
        w.poll()                                   # baseline (empty)
        write_3mf(inbox / "a.3mf")
        with mock.patch.object(projects, "render_project", return_value=0) as rp:
            w.poll()
            w.poll()
        rp.assert_called_once()
        self.assertEqual(rp.call_args.kwargs.get("trigger"), "watch")


class CatalogMigration(unittest.TestCase):
    def test_old_v5_db_upgrades_and_backfills_from_sidecars(self):
        root = Path(tempfile.mkdtemp()).resolve() / "renders"
        mk(root, "20261101T000000Z-old", "2026-11-01T00:00:00Z")                 # no trigger recorded
        mk(root, "20261102T000000Z-web", "2026-11-02T00:00:00Z", trigger="web", note="by hand", name="b")
        (root / ".tulda").mkdir()
        raw = sqlite3.connect(root / ".tulda" / "catalog.db", isolation_level=None)
        for m in cm.MIGRATIONS[:5]:
            m(raw)
        raw.execute("PRAGMA user_version=5")
        raw.execute("INSERT INTO groups(id,title,sources,day,created_utc,last_mtime,n_renders,search_text,starred)"
                    " VALUES('20261101T000000Z-old','T','[]','2026-11-01','2026-11-01T00:00:00Z',1,1,'t',1)")
        raw.close()
        cat = Catalog(root, log=lambda m: None)
        self.assertEqual(cat.stats()["schema_version"], cm.SCHEMA_VERSION)
        old, web = cat.get_group("20261101T000000Z-old"), cat.get_group("20261102T000000Z-web")
        self.assertTrue(old["starred"])                                      # v5 data kept
        self.assertIsNone(old["trigger"])                                    # unknown, not guessed
        self.assertEqual((web["trigger"], web["trigger_note"]), ("web", "by hand"))
        ids = lambda f: [g["id"] for g in cat.list_groups(f)[0]]
        self.assertEqual(ids(Filters(triggers=["web"])), ["20261102T000000Z-web"])
        self.assertEqual(ids(Filters(triggers=["unknown"])), ["20261101T000000Z-old"])
        self.assertEqual(dict(cat.facets()["triggers"]), {"web": 1, "unknown": 1})

    def test_bogus_sidecar_trigger_ignored(self):
        root = Path(tempfile.mkdtemp()).resolve() / "renders"
        mk(root, "20261101T000000Z-x", "2026-11-01T00:00:00Z", trigger=XSS, note="a\x00b")
        g = Catalog(root, log=lambda m: None).get_group("20261101T000000Z-x")
        self.assertIsNone(g["trigger"])
        self.assertEqual(g["trigger_note"], "a b")

    def test_filter_parsing_drops_unknown_values(self):
        f = parse_filters({"trigger": ["web", "nope", "unknown"]})
        self.assertEqual(f.triggers, ["web", "unknown"])


class Badges(unittest.TestCase):
    def test_badge_escaped_and_unknown_empty(self):
        self.assertEqual(trigger_badge(None), "")
        self.assertEqual(trigger_badge(XSS), "")
        b = trigger_badge("manual", XSS)
        self.assertIn(">manual</span>", b)
        self.assertNotIn("<img", b)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;&quot;&#x27;&amp;", b)

    def test_noscript_list_escapes_note_and_title(self):
        g = {"id": "20261101T000000Z-x", "title": XSS, "created": "2026-11-01T00:00:00Z",
             "trigger": "watch", "trigger_note": XSS, "n_renders": 1, "items": []}
        h = noscript_list({"groups": [g]})
        self.assertNotIn("<img", h)
        self.assertIn('class="trg trg-watch"', h)
        self.assertRegex(h, Z_TIME)

    def test_watch_page_rendered_link_badge(self):
        proj = {"folder": "P", "title": "P", "destination": "d", "created": "2026-01-01T00:00:00Z",
                "anchor": "p-1", "files": [{"id": "f1", "file": "a.3mf", "type": "3mf", "missing_views": [],
                                            "hold": None, "job": None,
                                            "rendered": [{"id": "g1", "title": XSS, "views": ["iso"],
                                                          "trashed": False, "trigger": "web"}]}]}
        h = render_watch_page(None, [], [proj], "tok")
        self.assertIn('<span class="trg trg-web"', h)
        self.assertNotIn("<img", h)


class ServerPages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp()).resolve() / "renders"
        mk(cls.root, "20261126T152227Z-web", "2026-11-26T15:22:27Z", trigger="web", note=XSS)
        mk(cls.root, "20261125T000000Z-old", "2026-11-25T00:00:00Z", name="b")
        cls.sdir = Path(tempfile.mkdtemp())
        (cls.sdir / "watch-events.jsonl").write_text(
            json.dumps({"time": "2026-11-26T07:09:18-08:00", "dir": "in", "file": "old.3mf",
                        "outcome": "skipped", "reason": "r"}) + "\n")
        EventLog(cls.sdir).append("in", "new.3mf", "created", "Proj")
        cls.srv = make_server(cls.root, "127.0.0.1", 0, log=lambda m: None, watch_state_dir=cls.sdir)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        b = r.read().decode()
        c.close()
        return r, b

    def assert_scripts_hashed(self, r, html):
        csp = r.getheader("Content-Security-Policy")
        self.assertEqual(csp, PAGE_CSP)
        self.assertNotIn("unsafe-inline'", csp.split("script-src", 1)[1].split(";")[0])
        self.assertNotIn("'self'", csp.split("script-src", 1)[1].split(";")[0])
        self.assertIn("default-src 'none'", csp)
        scripts = re.findall(r"<script(?![^>]*type=\"application/json\")[^>]*>(.*?)</script>", html, re.S)
        self.assertTrue(scripts)
        for body in scripts:
            h = "'sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() + "'"
            self.assertIn(h, csp)

    def test_group_page(self):
        r, h = self.get("/_g/20261126T152227Z-web/")
        self.assert_scripts_hashed(r, h)
        self.assertIn('<time class=lt datetime="2026-11-26T15:22:27Z" title="2026-11-26 15:22:27 UTC">'
                      '2026-11-26 15:22 UTC</time>', h)
        self.assertIn('class="trg trg-web"', h)
        self.assertNotIn("<img src=x", h)
        data = json.loads(h.split('id="data">', 1)[1].split("</script>", 1)[0])
        self.assertEqual(data["groups"][0]["trigger_note"], XSS)          # rendered with textContent
        self.assertIn("tuldaTime", h)

    def test_gallery_and_date_pages(self):
        for p in ("/", "/_d/2026-11-26/", "/p/", "/search?q=web", "/?trigger=web"):
            r, h = self.get(p)
            self.assertEqual(r.status, 200, p)
            self.assert_scripts_hashed(r, h)
            self.assertRegex(h, Z_TIME)
        r, h = self.get("/?trigger=unknown")
        self.assertIn("20261125T000000Z-old", h.split("<noscript>", 1)[1])
        self.assertNotIn("20261126T152227Z-web/", h.split("<noscript>", 1)[1])

    def test_watch_page(self):
        r, h = self.get("/_w/")
        self.assert_scripts_hashed(r, h)
        self.assertIn('<time class=lt datetime="2026-11-26T15:09:18Z"', h)      # old offset line
        self.assertEqual(len(Z_TIME.findall(h)), 2)

    def test_api_unchanged_shape(self):
        r, b = self.get("/api/watch")
        ev = json.loads(b)["events"]
        self.assertEqual(ev[1]["time"], "2026-11-26T07:09:18-08:00")         # stored value as-is


class WatchEventTimes(unittest.TestCase):
    def test_written_in_utc_z_and_old_offsets_parsed(self):
        sdir = Path(tempfile.mkdtemp())
        EventLog(sdir).append("in", "a.3mf", "created", "P")
        t = display_events(sdir)[0]["time"]
        self.assertRegex(t, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertLess(abs(timefmt.parse_ts(t).timestamp() - time.time()), 5)
        self.assertEqual(timefmt.utc_iso(timefmt.parse_ts("2026-11-26T07:09:18-08:00")), "2026-11-26T15:09:18Z")
        self.assertEqual(timefmt.ts_key("2026-11-26T07:09:18-08:00"), timefmt.ts_key("2026-11-26T15:09:18Z"))
        self.assertIsNone(timefmt.parse_ts("garbage"))
        self.assertEqual(timefmt.time_html("<b>"), "&lt;b&gt;")

    def test_job_and_state_times_are_utc(self):
        from tulda import watchd
        self.assertTrue(watchd.now_iso().endswith("Z"))
        self.assertTrue(webrender._now().endswith("Z"))
        from tulda.server import Handler
        self.assertTrue(Handler.log_date_time_string(None).endswith("Z"))

    def test_local_script_matches_hash(self):
        self.assertEqual(timefmt.LOCAL_SCRIPT, f"<script>{timefmt.LOCAL_JS}</script>")
        self.assertIn(timefmt.LOCAL_HASH, PAGE_CSP)
        self.assertNotIn("</script", timefmt.LOCAL_JS)

    def test_render_index_has_local_script_before_gallery_script(self):
        h = render_index({"ctx": {"kind": "recent"}, "groups": [], "next": None, "has_more": False,
                          "page_size": 20, "facets": {}, "dates": [], "rev": 0, "qs": "", "filters": {}})
        self.assertLess(h.index("window.tuldaTime"), h.index("const D=JSON.parse"))


if __name__ == "__main__":
    unittest.main()
