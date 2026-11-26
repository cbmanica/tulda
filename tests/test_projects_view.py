"""/_p/ and /api/projects: the Projects tab (sorting, escaping, empty root, tabs, CSP)."""
import http.client
import json
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path

from tulda.projectspage import parse_sort, project_rows, render_projects_page, sort_rows
from tulda.server import PAGE_CSP, make_server

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16
EVIL = '<img src=x onerror=alert(1)>"\'&'
CJK = "收纳盒-抽屉"
T_OLD, T_MID, T_NEW = 1_700_000_000, 1_700_100_000, 1_700_200_000


def _render(root: Path, project: str, stem: str, mtime: float, gid: str, trigger=None):
    d = root / project
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stem}.png").write_bytes(PNG)
    (d / f"{stem}.json").write_text(json.dumps({"adapter": "openscad", "stage": "source",
                                                "group_id": gid, "trigger": trigger}))
    os.utime(d / f"{stem}.png", (mtime, mtime))


def _manifest(dest: Path, name: str, created: str):
    (dest / name).mkdir(parents=True)
    (dest / name / "tulda-project.json").write_text(json.dumps({"tulda_project": 1, "created_utc": created}))


class Server(unittest.TestCase):
    root_setup = True

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp()).resolve()
        cls.root = cls.tmp / "renders"
        cls.root.mkdir()
        cfg = None
        if cls.root_setup:
            _render(cls.root, "beta", "b1", T_OLD, "20231114T000000Z-b")
            _render(cls.root, "Alpha", "a1", T_MID, "20231115T000000Z-a", "web")
            _render(cls.root, "Alpha", "a2", T_MID - 10, "20231115T000000Z-a", "web")
            _render(cls.root, EVIL, "e1", T_NEW, "20231116T000000Z-e", "watch")
            _render(cls.root, CJK, "c1", T_OLD - 100, "20231113T000000Z-c")
            dest = cls.tmp / "designs"
            _manifest(dest, "Alpha", "2023-01-01T00:00:00Z")
            _manifest(dest, "zeta-unrendered", "2023-11-14T23:00:00Z")
            _manifest(dest, EVIL + "-src", "2020-01-01T00:00:00Z")
            cfg = cls.tmp / "config.toml"
            cfg.write_text(f'[projects]\ndefault_destination = "{dest}"\n')
        cls.srv = make_server(cls.root, "127.0.0.1", 0, watch_state_dir=cls.tmp / "state",
                              watch_config=cfg)
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
        body = r.read()
        c.close()
        return r.status, body.decode(), r


class ProjectsPage(Server):
    def names(self, sort=None):
        q = f"?sort={sort}" if sort else ""
        return [p["name"] for p in json.loads(self.req("GET", f"/api/projects{q}")[1])["projects"]]

    def test_default_sort_is_newest_activity_first(self):
        # renders by newest mtime, then project folders by creation time; undated never first
        self.assertEqual(self.names(), [EVIL, "Alpha", "zeta-unrendered", "beta", CJK, EVIL + "-src"])
        self.assertEqual(self.names("date"), self.names())
        self.assertEqual(self.names("bogus"), self.names())

    def test_name_sort_is_case_insensitive(self):
        self.assertEqual(self.names("name"),
                         sorted([EVIL, "Alpha", "zeta-unrendered", "beta", CJK, EVIL + "-src"],
                                key=lambda n: (n.casefold(), n)))
        html = self.req("GET", "/_p/?sort=name")[1]
        self.assertLess(html.index(">Alpha</a>"), html.index(">beta</a>"))
        self.assertIn('class="sb on" href="/_p/?sort=name" aria-current=true', html)

    def test_row_content(self):
        p = {x["name"]: x for x in json.loads(self.req("GET", "/api/projects")[1])["projects"]}
        a = p["Alpha"]
        self.assertEqual((a["renders"], a["requests"], a["trigger"]), (2, 1, "web"))
        self.assertEqual(a["thumb"], "/Alpha/a1.png")
        self.assertEqual(a["source"], "designs/Alpha")
        self.assertEqual(a["url"], "/Alpha/")
        z = p["zeta-unrendered"]
        self.assertEqual((z["renders"], z["url"], z["thumb"]), (0, None, None))
        self.assertIsNone(p["beta"]["source"])
        html = self.req("GET", "/_p/")[1]
        self.assertIn("2 renders in 1 request", html)
        self.assertIn('<time class=lt datetime="2023-11-16T02:00:00Z"', html)
        self.assertIn("no renders yet", html)
        self.assertNotIn(str(self.tmp), html)    # folder names only, no absolute paths
        self.assertNotIn(str(self.tmp), self.req("GET", "/api/projects")[1])

    def test_names_and_paths_are_escaped(self):
        html = self.req("GET", "/_p/")[1]
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;&quot;&#x27;&amp;", html)
        self.assertIn("designs/&lt;img src=x", html)     # source label
        self.assertIn('href="/%3Cimg%20src%3Dx%20onerror%3Dalert%281%29%3E%22%27%26/"', html)
        self.assertIn(f">{CJK}</a>", html)
        self.assertIn("/%E6%94%B6%E7%BA%B3%E7%9B%92-%E6%8A%BD%E5%B1%89/c1.png", html)
        # every link and image on the page is a quoted same-origin path
        for url in re.findall(r'(?:href|src)="([^"]*)"', html):
            self.assertTrue(url.startswith("/") and not url.startswith("//"), url)
            self.assertNotRegex(url, r"[<>\"' ]")

    def test_csp_unchanged_and_no_new_script(self):
        s, html, r = self.req("GET", "/_p/")
        self.assertEqual(s, 200)
        self.assertEqual(r.getheader("Content-Security-Policy"), PAGE_CSP)
        self.assertNotIn("unsafe-inline", PAGE_CSP.split("script-src", 1)[1].split(";")[0])
        from tulda.timefmt import LOCAL_SCRIPT
        self.assertEqual(html.count("<script"), 1)
        self.assertIn(LOCAL_SCRIPT, html)

    def test_routes(self):
        self.assertEqual(self.req("GET", "/_p/extra")[0], 404)
        self.assertEqual(self.req("HEAD", "/_p/")[0], 200)
        for m in ("POST", "PUT", "DELETE"):
            self.assertEqual(self.req(m, "/_p/")[0], 405)

    def test_tab_on_every_page(self):
        pages = {"/": "gallery", "/search?q=x": "gallery", "/_d/2023-11-15/": "gallery",
                 "/Alpha/": "gallery", "/_g/20231115T000000Z-a/": "gallery",
                 "/_w/": "watcher", "/_p/": "projects", "/_p/?sort=name": "projects"}
        for path, cur in pages.items():
            s, html, _ = self.req("GET", path)
            self.assertEqual(s, 200, path)
            nav = re.search(r'<nav class="tabs".*?</nav>', html).group(0)
            self.assertIn('href="/_p/"', nav, path)
            self.assertEqual(nav.count('class="tab on"'), 1, path)
            self.assertIn(f'class="tab on" href="{ {"gallery": "/", "watcher": "/_w/", "projects": "/_p/"}[cur]}"',
                          nav, path)
        from tulda.viewer import render_viewer
        v = render_viewer({"kind": "mesh", "project": "p", "file": "a.glb", "url": "/p/a.glb",
                           "group_id": "g1", "source": "a.3mf", "info": {}})
        self.assertIn('class="tab" href="/_p/">Projects', v)

    def test_reserved_dir_not_listed(self):
        from tulda.items import RESERVED_DIRS
        self.assertIn("_p", RESERVED_DIRS)


class EmptyRoot(Server):
    root_setup = False

    def test_empty_page_and_api(self):
        s, html, _ = self.req("GET", "/_p/")
        self.assertEqual(s, 200)
        self.assertIn("No projects yet", html)
        self.assertIn('class="tab on" href="/_p/"', html)
        self.assertEqual(json.loads(self.req("GET", "/api/projects")[1]),
                         {"sort": "date", "projects": []})
        self.assertIn("No projects yet", self.req("GET", "/_p/?sort=name")[1])


class Unit(unittest.TestCase):
    def test_parse_sort(self):
        self.assertEqual(parse_sort({}), "date")
        self.assertEqual(parse_sort({"sort": ["name"]}), "name")
        self.assertEqual(parse_sort({"sort": ["<x>"]}), "date")

    def test_sort_ties_and_undated(self):
        rows = project_rows([{"name": "b", "renders": 1, "requests": 1, "mtime": T_OLD},
                             {"name": "a", "renders": 1, "requests": 1, "mtime": T_OLD},
                             {"name": "C", "renders": 1, "requests": 1, "mtime": T_NEW}],
                            {"u": {"source": "d/u", "created": None}})
        self.assertEqual([r["name"] for r in sort_rows(rows, "date")], ["C", "a", "b", "u"])
        self.assertEqual([r["name"] for r in sort_rows(rows, "name")], ["a", "b", "C", "u"])

    def test_render_escapes_bad_sort(self):
        html = render_projects_page([], '"><script>')
        self.assertNotIn('"><script>', html)
        self.assertIn('class="sb on" href="/_p/?sort=date"', html)


if __name__ == "__main__":
    unittest.main()
