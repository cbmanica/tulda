import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from tulda.netinfo import host_aliases
from tulda.server import MultiServer, PathError, make_server, safe_resolve

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp()).resolve()
        cls.root = cls.tmp / "renders"
        (cls.root / "proj").mkdir(parents=True)
        (cls.root / "proj" / "a.png").write_bytes(PNG)
        (cls.root / "proj" / "a.json").write_text(json.dumps({"adapter": "openscad", "stage": "source"}))
        (cls.root / "proj" / "notes.txt").write_text("no")
        (cls.root / "proj" / ".hidden.png").write_bytes(PNG)
        (cls.root / ".secret").mkdir()
        (cls.root / ".secret" / "s.png").write_bytes(PNG)
        (cls.tmp / "outside.png").write_bytes(PNG)
        (cls.tmp / "outdir").mkdir()
        (cls.tmp / "outdir" / "x.png").write_bytes(PNG)
        os.symlink(cls.tmp / "outside.png", cls.root / "proj" / "link.png")
        os.symlink(cls.tmp / "outdir", cls.root / "linkdir")
        os.symlink(cls.root / "proj" / "a.png", cls.root / "proj" / "inner.png")
        cls.srv = make_server(cls.root, "127.0.0.1", 0, watch_state_dir=cls.tmp / "state")
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def req(self, method, path, host=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in h.items():
            c.putheader(k, v)
        c.endheaders()
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body, r


class WatchPage(Base):
    """/_w/ and /api/watch: read-only watcher activity, attacker-controlled names escaped."""

    EVIL = '<img src=x onerror=alert(1)>"\'&.zip'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from tulda.watchevents import EventLog
        sdir = cls.tmp / "state"
        sdir.mkdir()
        (sdir / "watch.json").write_text(json.dumps({
            "pid": os.getpid(), "started": "2026-01-01T10:00:00+00:00", "mode": "config",
            "last_poll": "2026-01-01T10:05:00+00:00",
            "last_created": {"time": "2026-01-01T10:01:00+00:00", "project": "Tidy-Box",
                             "source": "Tidy+Box.zip"},
            "last_error": {"time": "2026-01-01T10:02:00+00:00",
                           "message": "x.zip: cannot write /Users/you/secret/dir/x.zip"},
            "dirs": [{"path": "/Users/you/inbox-example", "destination": "/Users/you/projects-example",
                      "error": None, "totals": {"created": 1, "exists": 0, "skipped": 1, "errors": 1}}]}))
        log = EventLog(sdir).append
        log("inbox-example", "", "backlog", None, "3 file(s) already in the folder when watching began")
        log("inbox-example", "Tidy+Box.zip", "created", "Tidy-Box", None)
        log("inbox-example", cls.EVIL, "skipped", None, "zip contains 1 file(s): <script>x</script>")
        log("inbox-example", "x.zip", "error", None, "cannot write /Users/you/secret/dir/x.zip")
        log("inbox-example", "held.3mf", "held", None,
            "post-processing script found in Metadata/project_settings.config: /usr/bin/fix",
            script="# Metadata/project_settings.config (post_process)\n/usr/bin/fix </pre><script>alert(1)</script>",
            approve="tulda project create ~/inbox-example/held.3mf --destination ~/projects-example "
                    "--allow-post-process")
        (sdir / "watch-events.jsonl").open("a").write("not json\n{\"outcome\": \"bogus\"}\n")

    def test_page_lists_events_newest_first_escaped(self):
        s, b, r = self.req("GET", "/_w/")
        self.assertEqual(s, 200)
        self.assertIn("script-src", r.getheader("Content-Security-Policy"))
        html = b.decode()
        self.assertNotIn("<img src=x", html)
        self.assertNotIn("<script>x", html)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;&quot;&#x27;&amp;.zip", html)
        self.assertIn("Watcher running", html)
        i_err, i_skip, i_created = (html.index("cannot write"), html.index("&lt;img"),
                                    html.index("<td class=f>Tidy+Box.zip</td>"))
        self.assertLess(i_err, i_skip)
        self.assertLess(i_skip, i_created)
        self.assertIn("imported", html)
        self.assertIn("backlog ignored", html)
        self.assertNotIn("/Users/you", html)                 # absolute paths reduced to names
        self.assertIn("inbox-example", html)
        s, _, _ = self.req("GET", "/_w/extra")
        self.assertEqual(s, 404)

    def test_api_json(self):
        s, b, _ = self.req("GET", "/api/watch")
        self.assertEqual(s, 200)
        d = json.loads(b)
        self.assertEqual([e["outcome"] for e in d["events"]],
                         ["held", "error", "skipped", "created", "backlog"])
        self.assertEqual(d["events"][3]["project"], "Tidy-Box")
        self.assertEqual(d["events"][1]["reason"], "cannot write x.zip")
        self.assertIn("/usr/bin/fix", d["events"][0]["script"])           # held: shown verbatim
        self.assertTrue(d["status"]["running"])
        self.assertNotIn("/Users/you", b.decode())

    def test_held_shows_script_escaped_and_approve_command(self):
        html = self.req("GET", "/_w/")[1].decode()
        self.assertIn("held (needs review)", html)
        self.assertIn("/usr/bin/fix &lt;/pre&gt;&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<script>alert", html)
        self.assertIn("tulda project create ~/inbox-example/held.3mf --destination "
                      "~/projects-example --allow-post-process", html)

    def test_read_only(self):
        for m in ("POST", "PUT", "DELETE"):
            self.assertEqual(self.req(m, "/_w/")[0], 405)
            self.assertEqual(self.req(m, "/api/watch")[0], 405)

    def test_main_page_links_watcher(self):
        s, b, _ = self.req("GET", "/")
        self.assertIn(b'href="/_w/"', b)

    def test_missing_state(self):
        from tulda.watchevents import display_events, watcher_status
        self.assertIsNone(watcher_status(self.tmp / "nope"))
        self.assertEqual(display_events(self.tmp / "nope"), [])


class HttpSafety(Base):
    def test_serves_png_and_index(self):
        s, b, r = self.req("GET", "/proj/a.png")
        self.assertEqual((s, b), (200, PNG))
        self.assertEqual(r.getheader("Content-Type"), "image/png")
        s, b, _ = self.req("GET", "/")
        self.assertEqual(s, 200)
        self.assertIn(b"a.png", b)
        s, b, _ = self.req("GET", "/proj/")
        self.assertEqual(s, 200)

    def test_head(self):
        s, b, r = self.req("HEAD", "/proj/a.png")
        self.assertEqual((s, b), (200, b""))
        self.assertEqual(r.getheader("Content-Length"), str(len(PNG)))

    def test_non_get_methods(self):
        for m in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            s, _, r = self.req(m, "/proj/a.png")
            self.assertEqual(s, 405, m)
            self.assertEqual(r.getheader("Allow"), "GET, HEAD")

    def test_traversal(self):
        for p in ("/../outside.png", "/proj/../../outside.png", "/%2e%2e/outside.png",
                  "/proj/%2e%2e/%2e%2e/outside.png", "/proj/..%2f..%2foutside.png",
                  "/proj/%00.png", "/proj\\..\\x.png", "//etc/passwd"):
            s, b, _ = self.req("GET", p)
            self.assertIn(s, (400, 403, 404), p)
            self.assertNotEqual(b, PNG, p)

    def test_dotfiles(self):
        for p in ("/proj/.hidden.png", "/.secret/s.png", "/.secret/", "/proj/%2ehidden.png"):
            self.assertEqual(self.req("GET", p)[0], 403, p)

    def test_symlink_escape(self):
        self.assertEqual(self.req("GET", "/proj/link.png")[0], 403)
        self.assertEqual(self.req("GET", "/linkdir/x.png")[0], 403)
        self.assertEqual(self.req("GET", "/linkdir/")[0], 403)

    def test_internal_symlink_ok(self):
        self.assertEqual(self.req("GET", "/proj/inner.png")[0], 200)

    def test_extension_allowlist(self):
        self.assertEqual(self.req("GET", "/proj/notes.txt")[0], 403)

    def test_index_does_not_list_escapes_or_hidden(self):
        _, b, _ = self.req("GET", "/api/index")
        names = {i["file"] for i in json.loads(b)}
        self.assertEqual(names, {"a.png", "inner.png"})

    def test_bad_host(self):
        for h in ("evil.example.com", "evil.example.com:%d" % self.port, "127.0.0.1:1", "localhost.evil.com"):
            self.assertEqual(self.req("GET", "/proj/a.png", host=h)[0], 403, h)
        self.assertEqual(self.req("GET", "/", host="evil.com")[0], 403)

    def test_good_host_variants(self):
        self.assertEqual(self.req("GET", "/proj/a.png", host="localhost:%d" % self.port)[0], 200)
        self.assertEqual(self.req("GET", "/proj/a.png", host="LOCALHOST")[0], 200)

    def test_missing_host(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.putrequest("GET", "/", skip_host=True)
        c.endheaders()
        self.assertEqual(c.getresponse().status, 403)


from tulda.netinfo import lan_ipv4
SECOND = lan_ipv4()


class MultiBind(unittest.TestCase):
    def setUp(self):
        from tulda.netinfo import lan_ipv4
        if lan_ipv4().startswith("127."):
            self.skipTest("no non-loopback IPv4 to use as a second address")
        self.root = Path(tempfile.mkdtemp()).resolve()
        (self.root / "p").mkdir()
        (self.root / "p" / "a.png").write_bytes(PNG)
        self.logs = []
        self.addCleanup(lambda: self.m.server_close())

    def get(self, ip, port, host):
        c = http.client.HTTPConnection(ip, port, timeout=5)
        c.putrequest("GET", "/p/a.png", skip_host=True, skip_accept_encoding=True)
        c.putheader("Host", host)
        c.endheaders()
        r = c.getresponse()
        r.read()
        c.close()
        return r.status

    def test_two_addresses_one_port_and_host_allowlist(self):
        self.m = MultiServer(self.root, ["127.0.0.1", SECOND], 0, ["myhost.local", "myhost.example.ts.net"],
                             retry_interval=60, log=self.logs.append)
        self.assertEqual(set(self.m.servers), {"127.0.0.1", SECOND})
        port = self.m.port
        self.assertTrue(all(s.server_address[1] == port for s in self.m.servers.values()))
        for ip in ("127.0.0.1", SECOND):
            for host in (f"{SECOND}:{port}", SECOND, "127.0.0.1", f"myhost.local:{port}",
                         "myhost.example.ts.net", "LOCALHOST"):
                self.assertEqual(self.get(ip, port, host), 200, (ip, host))
            for host in ("evil.com", f"{SECOND}:{port + 1}", "10.0.0.9", "myhost.example.ts.net.evil.com"):
                self.assertEqual(self.get(ip, port, host), 403, (ip, host))

    def test_failed_bind_is_retried(self):
        import socket
        blocker = socket.socket()
        blocker.bind((SECOND, 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        self.addCleanup(blocker.close)
        self.m = MultiServer(self.root, ["127.0.0.1", SECOND], port, retry_interval=60,
                             log=self.logs.append)
        self.assertEqual(set(self.m.servers), {"127.0.0.1"})
        self.assertEqual(self.m.pending, [SECOND])
        self.assertTrue(any(f"cannot bind {SECOND}" in m for m in self.logs))
        self.assertEqual(self.get("127.0.0.1", port, "127.0.0.1"), 200)
        self.assertEqual(self.get("127.0.0.1", port, SECOND), 403)  # not bound yet
        blocker.close()
        self.assertEqual(self.m.try_bind(), [SECOND])
        self.assertEqual(self.get(SECOND, port, SECOND), 200)


class ShortNameHosts(Base):
    ALIASES = host_aliases("MyHost", "myhost-2.lan", "myhost.tail1234.ts.net")

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.srv.allowed_hosts = set(cls.srv.allowed_hosts) | set(cls.ALIASES)

    def test_aliases_derived(self):
        self.assertEqual(set(self.ALIASES), {
            "myhost", "myhost.local", "myhost-2", "myhost-2.local",
            "myhost.tail1234.ts.net"})

    def test_accepted_forms(self):
        for n in ("myhost", "MyHost", "myhost.local", "MYHOST.LOCAL", "myhost-2",
                  "myhost.tail1234.ts.net", "MyHost.Tail1234.TS.net"):
            for h in (n, f"{n}:{self.port}"):
                self.assertEqual(self.req("GET", "/", host=h)[0], 200, h)

    def test_rejected(self):
        for n in ("evil.example", "myhost.evil.com", "myhost.local.evil.com", "evilmyhost",
                  "myhost.tail1234.ts.net.evil.com", "other.tail1234.ts.net", "tail1234.ts.net"):
            self.assertEqual(self.req("GET", "/", host=n)[0], 403, n)
        self.assertEqual(self.req("GET", "/", host="myhost:1")[0], 403)
        self.assertEqual(self.req("GET", "/", host="myhost:abc")[0], 403)

    def test_empty_inputs(self):
        self.assertEqual(host_aliases(None, "", None), [])


class SafeResolve(unittest.TestCase):
    def test_unit(self):
        root = Path(tempfile.mkdtemp()).resolve()
        (root / "p").mkdir()
        (root / "p" / "f.png").write_bytes(PNG)
        self.assertEqual(safe_resolve(root, "/p/f.png"), root / "p" / "f.png")
        self.assertEqual(safe_resolve(root, "/"), root)
        for bad, status in (("/p/../..", 403), ("/p/%2e%2e/f.png", 403), ("/p/x\x00", 400), ("/nope", 404)):
            with self.assertRaises(PathError) as cm:
                safe_resolve(root, bad)
            self.assertEqual(cm.exception.status, status, bad)


if __name__ == "__main__":
    unittest.main()
