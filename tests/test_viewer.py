"""Viewer page, static allowlist, content types, cache headers and read-only guarantees."""

import http.client
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path

from tulda import server, viewer
from tulda.server import STATIC_FILES, STATIC_ROOT, make_server

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16
GLB = b"glTF" + b"\0" * 40
GID = "20261124T100000Z-demo"


def sidecar(**kw):
    d = {"adapter": "mesh-3mf", "stage": "mesh", "source": "a<b>.3mf", "group_id": GID, "group_title": "Demo & co",
         "created_utc": "2026-11-24T10:00:00Z", "group_created_utc": "2026-11-24T10:00:00Z"}
    d.update(kw)
    return json.dumps(d)


class ViewerServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp()).resolve()
        cls.root = cls.tmp / "renders"
        p = cls.root / "proj"
        p.mkdir(parents=True)
        (p / "m.glb").write_bytes(GLB)
        (p / "m.json").write_text(sidecar(view="interactive", kind="mesh", seq=0, info={
            "triangles": 12, "parts": [{"name": "body", "color": "#696969", "triangles": 12, "visible": True}],
            "bbox": [[0, 0, 0], [1, 1, 1]], "secret": "not exposed"}))
        (p / "t.png").write_bytes(PNG)
        (p / "t.json").write_text(sidecar(view="turntable", kind="turntable", adapter="turntable-scad", seq=1,
                                          info={"frames": 3, "frames_dir": "t.frames"}))
        (p / "t.frames").mkdir()
        for i in range(3):
            (p / "t.frames" / f"f{i:03d}.png").write_bytes(PNG)
        (p / "plain.png").write_bytes(PNG)
        (cls.root / ".secret").mkdir()
        cls.srv = make_server(cls.root, "127.0.0.1", 0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def req(self, method, path, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        c.putheader("Host", host or f"127.0.0.1:{self.port}")
        c.endheaders()
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body, r

    # -- static allowlist
    def test_every_allowlisted_file_exists_and_serves(self):
        for rel, ctype in STATIC_FILES.items():
            self.assertTrue((STATIC_ROOT / rel).is_file(), rel)
            s, b, r = self.req("GET", "/_s/" + rel)
            self.assertEqual(s, 200, rel)
            self.assertEqual(r.getheader("Content-Type"), ctype, rel)
            self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
            self.assertEqual(b, (STATIC_ROOT / rel).read_bytes())

    def test_static_cache_headers(self):
        _, _, r = self.req("GET", "/_s/" + viewer.VENDOR.split("/_s/")[1] + "build/three.module.js")
        self.assertEqual(r.getheader("Cache-Control"), "public, max-age=31536000, immutable")
        self.assertEqual(r.getheader("Content-Type"), "application/javascript")
        _, _, r = self.req("GET", "/_s/viewer.js")
        self.assertEqual(r.getheader("Cache-Control"), "no-cache")

    def test_static_not_allowlisted(self):
        for p in ("/_s/", "/_s/server.py", "/_s/../server.py", "/_s/%2e%2e/server.py", "/_s/vendor/three-0.187.0/package.json",
                  "/_s/vendor/three-0.187.0/VENDOR.md", "/_s/vendor/../viewer.js", "/_s/viewer.js/x",
                  "/_s/vendor/three-0.187.0/build/three.module.js%00", "/_s/..%2fserver.py", "/_s/.hidden"):
            s, b, _ = self.req("GET", p)
            self.assertIn(s, (400, 403, 404), p)
            self.assertNotIn(b"import ", b[:200] if "server.py" in p else b"", p)

    def test_unlisted_static_files_are_only_docs(self):
        listed = set(STATIC_FILES)
        on_disk = {str(p.relative_to(STATIC_ROOT)) for p in STATIC_ROOT.rglob("*") if p.is_file()}
        extra = on_disk - listed
        self.assertTrue(all(x.endswith("VENDOR.md") for x in extra), extra)

    def test_vendor_licence_and_notice_present(self):
        self.assertIn("MIT License", (STATIC_ROOT / "vendor" / "three-0.187.0" / "LICENSE").read_text())
        notice = (Path(__file__).resolve().parent.parent / "NOTICE").read_text()
        self.assertIn("three.js", notice)
        self.assertIn("MIT", notice)

    # -- viewer page
    def test_viewer_page(self):
        s, b, r = self.req("GET", "/_v/proj/m/")
        self.assertEqual(s, 200)
        self.assertEqual(r.getheader("Content-Type"), "text/html; charset=utf-8")
        self.assertIn("no-store", r.getheader("Cache-Control"))
        csp = r.getheader("Content-Security-Policy")
        self.assertIn("script-src 'self' 'unsafe-inline'", csp)
        self.assertIn("default-src 'none'", csp)
        html = b.decode()
        self.assertIn('<script type="importmap">', html)
        self.assertIn("/_s/vendor/three-0.187.0/build/three.module.js", html)
        self.assertIn('src="/_s/viewer.js"', html)
        self.assertNotIn("http://", html.replace("http://schemas", "").replace("http://www.w3.org", ""))
        self.assertNotIn("cdn", html.lower())
        self.assertIn('id="reset"', html)
        self.assertIn('data-preset="bottom"', html)
        data = json.loads(re.search(r'id="vdata">(.*?)</script>', html, re.S).group(1))
        self.assertEqual(data["glb"], "/proj/m.glb")
        self.assertEqual(data["info"]["triangles"], 12)
        self.assertNotIn("secret", data["info"])
        self.assertIn("&amp;", html)                       # titles are escaped
        self.assertNotIn("a<b>", html)

    def test_viewer_head_and_missing(self):
        self.assertEqual(self.req("HEAD", "/_v/proj/m/")[:2], (200, b""))
        for p in ("/_v/proj/nope/", "/_v/nope/m/", "/_v/proj/plain/", "/_v/proj/", "/_v/", "/_v/proj/m/extra/",
                  "/_v/.secret/m/", "/_v/proj/.m/", "/_v/proj/..%2fm/"):
            self.assertEqual(self.req("GET", p)[0], 404, p)

    def test_viewer_bad_host_and_methods(self):
        self.assertEqual(self.req("GET", "/_v/proj/m/", host="evil.example.com")[0], 403)
        self.assertEqual(self.req("GET", "/_s/viewer.js", host="evil.example.com")[0], 403)
        for m in ("POST", "PUT", "DELETE", "PATCH"):
            for p in ("/_v/proj/m/", "/_s/viewer.js", "/proj/m.glb"):
                self.assertEqual(self.req(m, p)[0], 405, (m, p))

    def test_turntable_page_has_frame_data(self):
        s, b, _ = self.req("GET", "/_v/proj/t/")
        self.assertEqual(s, 200)
        data = json.loads(re.search(r'id="vdata">(.*?)</script>', b.decode(), re.S).group(1))
        self.assertIsNone(data["glb"])
        self.assertEqual(data["turntable"]["frames"], 3)
        self.assertEqual(data["turntable"]["base"], "/proj/t.frames/")

    # -- files
    def test_glb_content_type_and_cache(self):
        s, b, r = self.req("GET", "/proj/m.glb")
        self.assertEqual((s, b), (200, GLB))
        self.assertEqual(r.getheader("Content-Type"), "model/gltf-binary")
        self.assertIn("max-age=3600", r.getheader("Cache-Control"))
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")

    def test_frames_served_and_safe(self):
        s, b, r = self.req("GET", "/proj/t.frames/f001.png")
        self.assertEqual((s, b), (200, PNG))
        self.assertIn("max-age=3600", r.getheader("Cache-Control"))
        self.assertEqual(self.req("GET", "/proj/t.frames/f009.png")[0], 404)
        for p in ("/proj/t.frames/../../.secret/x.png", "/proj/t.frames/%2e%2e/%2e%2e/%2e%2e/x.png", "/proj/t.frames/.f.png"):
            self.assertIn(self.req("GET", p)[0], (400, 403, 404), p)
        self.assertEqual(self.req("GET", "/proj/m.json")[0], 200)
        self.assertEqual(self.req("GET", "/proj/.hidden.glb")[0], 403)

    def test_png_cache_unchanged(self):
        _, _, r = self.req("GET", "/proj/plain.png")
        self.assertEqual(r.getheader("Cache-Control"), "no-cache")

    def test_favicon_is_quiet(self):
        self.assertEqual(self.req("GET", "/favicon.ico")[0], 204)

    # -- group UI data
    def test_group_page_marks_kinds(self):
        s, b, _ = self.req("GET", f"/_g/{GID}/")
        self.assertEqual(s, 200)
        html = b.decode()
        self.assertIn('"kind": "mesh"', html)
        self.assertIn('"viewer_url": "/_v/proj/m/"', html)
        self.assertIn("t3d", html)                          # "3D" tile styling/markup present
        self.assertIn("viewable", html)

    def test_reserved_names_not_projects(self):
        (self.root / "_v").mkdir(exist_ok=True)
        (self.root / "_v" / "x.png").write_bytes(PNG)
        self.srv.catalog.sync(force=True)
        s, b, _ = self.req("GET", "/api/index")
        self.assertNotIn("_v", {i["project"] for i in json.loads(b)})


if __name__ == "__main__":
    unittest.main()
