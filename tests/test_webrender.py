"""Watcher-page Render buttons: POST /api/render, the server's single write exception.
Rendering itself is mocked (RenderQueue gets a fake runner)."""
import hashlib
import http.client
import json
import os
import tempfile
import threading
import unittest
import urllib.parse
import zipfile
from pathlib import Path

from tulda import webrender
from tulda.server import make_server, new_render_state

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16
SCRIPT_CFG = json.dumps({"post_process": ["/usr/bin/env example-postproc"]})


def make_3mf(path: Path, script: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", "<model/>")
        if script:
            z.writestr("Metadata/project_settings.config", SCRIPT_CFG)


def manifest(proj: Path, files, **extra) -> None:
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "tulda-project.json").write_text(json.dumps(
        {"tulda_project": 1, "name": proj.name, "created_utc": "2026-01-01T00:00:00Z",
         "files": files, **extra}))


class Fake:
    """Render runner that blocks until released; records calls."""

    def __init__(self):
        self.calls, self.go = [], threading.Event()

    def __call__(self, project, f):
        self.calls.append((project["folder"], f["rel"]))
        self.go.wait(5)
        return True, "1 render(s)"


class RenderPost(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.root = self.tmp / "renders"
        dest = self.tmp / "dest"
        (self.tmp / "inbox").mkdir()
        # a zip import: files/ holds the models
        manifest(dest / "Example-Caddy", ["source/example.zip", "files/part_a.3mf", "files/part_b.stl"])
        (dest / "Example-Caddy" / "source").mkdir()
        (dest / "Example-Caddy" / "source" / "example.zip").write_bytes(b"PK")
        make_3mf(dest / "Example-Caddy" / "files" / "part_a.3mf")
        (dest / "Example-Caddy" / "files" / "part_b.stl").write_bytes(b"solid x\nendsolid x\n")
        # a bare 3mf import; the name is attacker-controlled markup
        self.evil = '<img src=x onerror=alert(1)>"&'
        manifest(dest / self.evil, ["source/box.3mf"])
        make_3mf(dest / self.evil / "source" / "box.3mf")
        # held: manifest says a script is present and not approved
        manifest(dest / "Held-Project", ["source/h.3mf"], post_process={"present": True, "approved": False})
        make_3mf(dest / "Held-Project" / "source" / "h.3mf", script=True)
        # older manifest without post_process; a fresh scan finds the script
        manifest(dest / "Scan-Project", ["source/s.3mf"])
        make_3mf(dest / "Scan-Project" / "source" / "s.3mf", script=True)
        # symlink escapes: a project folder and a file pointing outside
        make_3mf(self.tmp / "outside" / "secret.3mf")
        manifest(self.tmp / "outside" / "Linked", ["files/x.3mf"])
        make_3mf(self.tmp / "outside" / "Linked" / "files" / "x.3mf")
        os.symlink(self.tmp / "outside" / "Linked", dest / "Linked")
        os.symlink(self.tmp / "outside" / "secret.3mf", dest / "Example-Caddy" / "files" / "escape.3mf")
        self.dest = dest
        self.cfg = self.tmp / "config.toml"
        self.cfg.write_text(f'[[watch.dir]]\npath = "{self.tmp / "inbox"}"\ndestination = "{dest}"\n')
        self.fake = Fake()
        self.state = new_render_state(self.root, log=lambda m: None, run=self.fake)
        self.srv = make_server(self.root, "127.0.0.1", 0, watch_state_dir=self.tmp / "state",
                               log=lambda m: None, watch_config=self.cfg, render=self.state)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.fake.go.set()
        self.srv.shutdown()
        self.srv.server_close()

    # -- helpers
    def req(self, method, path, body=b"", headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        h = {"Host": f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        if method == "POST":
            h.setdefault("Content-Type", "application/x-www-form-urlencoded")
            h["Content-Length"] = str(len(body))
        for k, v in h.items():
            c.putheader(k, v)
        c.endheaders()
        if body:
            c.send(body)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data, r

    def projects(self):
        return {p["folder"]: p for p in json.loads(self.req("GET", "/api/watch")[1])["projects"]}

    def fid(self, folder, rel):
        return next(f["id"] for f in self.projects()[folder]["files"] if f["file"] == rel)

    def post(self, fid, token=None, origin="same", extra=None):
        h = dict(extra or {})
        if origin == "same":
            h["Origin"] = f"http://127.0.0.1:{self.port}"
        elif origin is not None:
            h["Origin"] = origin
        tok = self.state["token"] if token is None else token
        body = urllib.parse.urlencode({"token": tok, "id": fid}).encode()
        return self.req("POST", "/api/render", body, h)

    # -- listing and tabs
    def test_lists_projects_files_and_skips_escapes(self):
        ps = self.projects()
        self.assertEqual(set(ps), {"Example-Caddy", self.evil, "Held-Project", "Scan-Project"})
        self.assertEqual([f["file"] for f in ps["Example-Caddy"]["files"]],
                         ["files/part_a.3mf", "files/part_b.stl"])  # no zip, no symlinked file
        self.assertEqual([f["file"] for f in ps[self.evil]["files"]], ["source/box.3mf"])
        self.assertIn("not approved", ps["Held-Project"]["files"][0]["hold"])
        self.assertIn("not approved", ps["Scan-Project"]["files"][0]["hold"])
        self.assertIsNone(ps["Example-Caddy"]["files"][0]["hold"])
        blob = self.req("GET", "/api/watch")[1].decode()
        self.assertNotIn(str(self.tmp), blob)      # no absolute paths leave the server

    def test_watch_page_has_tabs_buttons_token_and_escapes_names(self):
        s, b, r = self.req("GET", "/_w/")
        html = b.decode()
        self.assertEqual(s, 200)
        self.assertIn('class="tab on" href="/_w/" aria-current=page>Watcher', html)
        self.assertIn('class="tab" href="/">Gallery', html)
        self.assertIn(f'name=token value="{self.state["token"]}"', html)
        self.assertIn('action="/api/render"', html)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;&quot;&amp;", html)
        self.assertEqual(r.getheader("Referrer-Policy"), "same-origin")
        self.assertIn("frame-ancestors 'none'", r.getheader("Content-Security-Policy"))
        self.assertIn("held: post-processing script not approved", html)

    def test_gallery_pages_have_tabs(self):
        for path in ("/", "/search?q=x"):
            html = self.req("GET", path)[1].decode()
            self.assertIn('<nav class="tabs" aria-label="Sections">', html, path)
            self.assertIn('class="tab on" href="/" aria-current=page>Gallery', html, path)
            self.assertIn('href="/_w/">Watcher', html, path)
        from tulda.viewer import render_viewer
        v = render_viewer({"kind": "mesh", "project": "p", "file": "a.glb", "url": "/p/a.glb",
                           "group_id": "g1", "source": "a.3mf", "info": {}})
        self.assertIn('<nav class="tabs"', v)

    # -- the POST
    def test_queued_then_redirect_back_and_status_shown(self):
        fid = self.fid("Example-Caddy", "files/part_a.3mf")
        s, _, r = self.post(fid)
        self.assertEqual(s, 303)
        loc = r.getheader("Location")
        self.assertTrue(loc.startswith("/_w/?r=queued#p-"), loc)
        html = self.req("GET", "/_w/?r=queued")[1].decode()
        self.assertIn("Render queued.", html)
        self.assertIn("rendering", html)
        self.assertIn('content="5"', html)          # faster refresh while busy
        self.fake.go.set()
        self.assertTrue(webrender.wait_idle(self.state["queue"]))
        self.assertEqual(self.fake.calls, [("Example-Caddy", "files/part_a.3mf")])
        self.assertIn("done: 1 render(s)", self.req("GET", "/_w/")[1].decode())

    def test_token_required(self):
        fid = self.fid("Example-Caddy", "files/part_a.3mf")
        for tok in ("", "wrong"):
            s, b, _ = self.post(fid, token=tok)
            self.assertEqual(s, 403, tok)
        body = urllib.parse.urlencode({"id": fid}).encode()
        s, _, _ = self.req("POST", "/api/render", body, {"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(s, 403)
        self.assertEqual(self.fake.calls, [])

    def test_bad_origin_rejected(self):
        fid = self.fid("Example-Caddy", "files/part_a.3mf")
        for origin in (None, "null", "http://evil.example", f"http://localhost:{self.port}",
                       f"https://127.0.0.1:{self.port}", "http://127.0.0.1:1"):
            s, _, _ = self.post(fid, origin=origin)
            self.assertEqual(s, 403, origin)
        s, _, _ = self.post(fid, extra={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(s, 403)
        s, _, _ = self.post(fid, extra={"Host": "evil.example"})
        self.assertEqual(s, 403)
        self.assertEqual(self.fake.calls, [])

    def test_id_outside_eligible_set_rejected(self):
        secret = self.state["secret"]
        for fid in ("0" * 32, "../../etc/passwd", "x",
                    webrender.file_id(secret, self.dest, "Linked", "files/x.3mf"),
                    webrender.file_id(secret, self.dest, "Example-Caddy", "files/escape.3mf"),
                    webrender.file_id(secret, self.dest, "Example-Caddy", "source/example.zip"),
                    webrender.file_id(secret, self.tmp / "outside", "Linked", "files/x.3mf")):
            s, _, _ = self.post(fid)
            self.assertEqual(s, 404, fid)
        self.assertEqual(self.fake.calls, [])

    def test_symlink_escape_rejected_by_validators(self):
        self.assertIsNone(webrender.safe_project(self.dest, "Linked"))
        proj = self.dest / "Example-Caddy"
        self.assertIsNone(webrender.safe_file(proj, "files/escape.3mf"))
        for rel in ("../Held-Project/source/h.3mf", "/etc/passwd", "files/../files/part_a.3mf",
                    "source/example.zip", ".hidden/x.3mf", "files\\part_a.3mf"):
            self.assertIsNone(webrender.safe_file(proj, rel), rel)
        self.assertEqual(webrender.safe_file(proj, "files/part_a.3mf"), proj / "files" / "part_a.3mf")
        # a symlinked directory inside the project is refused too
        os.symlink(self.tmp / "outside", proj / "files" / "sub")
        self.assertIsNone(webrender.safe_file(proj, "files/sub/secret.3mf"))

    def test_post_process_hold_refused(self):
        for folder, rel in (("Held-Project", "source/h.3mf"), ("Scan-Project", "source/s.3mf")):
            fid = self.fid(folder, rel)
            s, _, r = self.post(fid)
            self.assertEqual(s, 303)
            self.assertIn("r=held", r.getheader("Location"))
            s, b, _ = self.post(fid, extra={"Accept": "application/json"})
            self.assertEqual(s, 409)
            self.assertIn("not approved", json.loads(b)["reason"])
        self.assertEqual(self.fake.calls, [])

    def test_approved_script_is_allowed_but_a_new_one_is_not(self):
        proj = self.dest / "Scan-Project"
        cmd = "/usr/bin/env example-postproc"
        manifest(proj, ["source/s.3mf"], post_process={
            "present": True, "approved": True,
            "findings": [{"config": "Metadata/project_settings.config", "key": "post_process", "command": cmd}]})
        self.assertIsNone(self.projects()["Scan-Project"]["files"][0]["hold"])
        manifest(proj, ["source/s.3mf"], post_process={
            "present": True, "approved": True, "findings": [{"command": "something else"}]})
        self.assertIsNotNone(self.projects()["Scan-Project"]["files"][0]["hold"])

    def test_tulda_toml_in_project_holds(self):
        (self.dest / "Example-Caddy" / "files" / "tulda.toml").write_text("[adapter.x]\n")
        self.assertIn("tulda.toml", self.projects()["Example-Caddy"]["files"][0]["hold"])

    def test_queue_dedupes_repeated_clicks(self):
        fid = self.fid("Example-Caddy", "files/part_a.3mf")
        h = {"Accept": "application/json"}
        results = [json.loads(self.post(fid, extra=h)[1])["result"] for _ in range(3)]
        self.assertEqual(results, ["queued", "duplicate", "duplicate"])
        self.fake.go.set()
        self.assertTrue(webrender.wait_idle(self.state["queue"]))
        self.assertEqual(len(self.fake.calls), 1)

    def test_queue_bounded(self):
        q = webrender.RenderQueue(self.fake, max_pending=1, log=lambda m: None)
        p = {"folder": "p"}
        self.assertEqual(q.submit(p, {"id": "a" * 32, "rel": "a.3mf"})[0], "queued")
        for _ in range(100):                        # wait for the worker to take job a
            if q.snapshot()["a" * 32]["state"] == "rendering":
                break
            threading.Event().wait(0.01)
        self.assertEqual(q.submit(p, {"id": "b" * 32, "rel": "b.3mf"})[0], "queued")
        self.assertEqual(q.submit(p, {"id": "c" * 32, "rel": "c.3mf"})[0], "full")
        self.fake.go.set()
        self.assertTrue(webrender.wait_idle(q))

    def test_runner_failure_is_reported(self):
        q = webrender.RenderQueue(lambda p, f: (_ for _ in ()).throw(RuntimeError("boom")), log=lambda m: None)
        q.submit({"folder": "p"}, {"id": "d" * 32, "rel": "d.3mf"})
        self.assertTrue(webrender.wait_idle(q))
        j = q.snapshot()["d" * 32]
        self.assertEqual((j["state"], j["message"]), ("failed", "RuntimeError: boom"))

    def test_rendered_detection_by_sha256(self):
        src = self.dest / "Example-Caddy" / "files" / "part_a.3mf"
        sha = hashlib.sha256(src.read_bytes()).hexdigest()
        rp = self.root / "files"                    # rendered under another project name
        rp.mkdir(parents=True)

        def add(view):
            (rp / f"20260101T000000Z-x-{view}-part_a.png").write_bytes(PNG)
            (rp / f"20260101T000000Z-x-{view}-part_a.json").write_text(json.dumps(
                {"adapter": "x", "stage": "sliced", "source": "renamed.3mf", "source_sha256": sha,
                 "view": view, "group_id": "20260101T000000Z-part-a", "group_title": None}))
            self.srv.catalog.sync(force=True)
        add("iso")
        f = self.projects()["Example-Caddy"]["files"][0]
        self.assertEqual([g["id"] for g in f["rendered"]], ["20260101T000000Z-part-a"])
        self.assertEqual(f["missing_views"], ["bottom", "interactive"])
        html = self.req("GET", "/_w/")[1].decode()
        self.assertIn('href="/_g/20260101T000000Z-part-a/">rendered:', html)
        self.assertIn("Render all views", html)    # partial: offered again
        add("bottom")
        add("interactive")
        f = self.projects()["Example-Caddy"]["files"][0]
        self.assertEqual(f["missing_views"], [])
        self.assertEqual(self.projects()["Example-Caddy"]["files"][1]["rendered"], [])
        html = self.req("GET", "/_w/")[1].decode()
        self.assertIn('href="/_g/20260101T000000Z-part-a/">rendered:', html)
        self.assertNotIn(f'value="{f["id"]}"', html)  # no button for a rendered file
        s, b, _ = self.post(f["id"], extra={"Accept": "application/json"})
        self.assertEqual((s, json.loads(b)["result"]), (409, "rendered"))
        self.assertEqual(self.fake.calls, [])

    def test_other_methods_and_paths_still_405(self):
        for m in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            self.assertEqual(self.req(m, "/api/render")[0], 405)
        tok = urllib.parse.urlencode({"token": self.state["token"], "id": "0" * 32}).encode()
        for path in ("/", "/api/watch", "/_w/", "/api/render/x", "/api/renderx"):
            self.assertEqual(self.req("POST", path, tok, {"Origin": f"http://127.0.0.1:{self.port}"})[0], 405, path)

    def test_bad_bodies(self):
        o = {"Origin": f"http://127.0.0.1:{self.port}"}
        self.assertEqual(self.req("POST", "/api/render", b'{"id":1}',
                                  {**o, "Content-Type": "application/json"})[0], 415)
        self.assertEqual(self.req("POST", "/api/render", b"x" * 5000, o)[0], 400)
        self.assertEqual(self.req("POST", "/api/render", b"\xff\xfe", o)[0], 400)

    def test_no_config_means_no_buttons(self):
        srv = make_server(self.root, "127.0.0.1", 0, watch_state_dir=self.tmp / "state", log=lambda m: None)
        try:
            self.assertIsNone(srv.watch_config)
            self.assertEqual(webrender.watch_destinations(None), [])
            self.assertEqual(webrender.watch_destinations(self.tmp / "missing.toml"), [])
        finally:
            srv.server_close()


class Helpers(unittest.TestCase):
    def test_constants_match_projects(self):
        from tulda import projects
        self.assertEqual(webrender.MANIFEST, projects.MANIFEST)
        self.assertEqual(webrender.RENDER_EXT, projects.RENDER_EXT)
        self.assertEqual(set(webrender.VIEWS), projects.RENDER_EXT)
        self.assertNotIn("slice", ",".join(webrender.VIEWS.values()))

    def test_cli_render_argv_uses_the_cli_path(self):
        from unittest import mock
        calls = []

        class P:
            pid, returncode = 1, 0

            def __init__(self, argv, **kw):
                calls.append((argv, kw))

            def communicate(self, timeout=None):
                return "rendered /x/a.png  (j)\nrendered /x/b.png  (j)\n", ""
        f = {"path": Path("/tmp/proj/files/-a.3mf"), "ext": ".3mf", "rel": "files/-a.3mf"}
        with mock.patch("subprocess.Popen", P):
            ok, msg = webrender.cli_render({"folder": "Proj", "title": "-Proj"}, f, Path("/tmp/r"))
        argv, kw = calls[0]
        self.assertEqual(argv[1:5], ["-P", "-m", "tulda", "render"])
        self.assertIn("--project=Proj", argv)
        self.assertIn("--title=-Proj", argv)
        self.assertIn("--views=iso,bottom,interactive", argv)
        self.assertEqual(argv[-2:], ["--", "/tmp/proj/files/-a.3mf"])
        self.assertEqual(kw["cwd"], "/tmp/r")
        self.assertEqual((ok, msg), (True, "2 render(s)"))

    def test_cli_default_project_is_the_project_folder(self):
        from tulda.cli import default_project
        tmp = Path(tempfile.mkdtemp()).resolve()
        manifest(tmp / "My-Project", ["files/sub/a.3mf"])
        (tmp / "My-Project" / "files" / "sub").mkdir(parents=True)
        self.assertEqual(default_project(tmp / "My-Project" / "files" / "sub" / "a.3mf"), "My-Project")
        self.assertEqual(default_project(tmp / "My-Project" / "source" / "a.3mf"), "My-Project")
        (tmp / "plain").mkdir()
        self.assertEqual(default_project(tmp / "plain" / "a.scad"), "plain")


if __name__ == "__main__":
    unittest.main()
