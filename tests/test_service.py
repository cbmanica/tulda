import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tulda import logs, service

SAMPLE = """gui/501/com.tulda.server = {
\tactive count = 1
\tpath = /Users/<you>/Library/LaunchAgents/com.tulda.server.plist
\ttype = LaunchAgent
\tstate = running

\tprogram = /Users/<you>/dev/tulda/.venv/bin/python
\targuments = {
\t\t/Users/<you>/dev/tulda/.venv/bin/python
\t\t-u
\t}
\tenvironment = {
\t\tPATH => /usr/bin
\t}
\truns = 3
\tpid = 12345
\timmediate reason = inferred program
\tlast exit code = 75

\tproperties = keepalive | inferred program
}
"""


def cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


class PlistTests(unittest.TestCase):
    def make(self, **kw):
        base = dict(python="/p/.venv/bin/python", project=Path("/p"), host=[], port=8765,
                    renders_root=None, log_dir=Path("/L"))
        base.update(kw)
        return service.build_plist(**base)

    def test_contents(self):
        d = self.make(renders_root="/r", host="10.0.0.2", port=8799)
        self.assertEqual(d["Label"], "com.tulda.server")
        a = d["ProgramArguments"]
        self.assertEqual(a[:5], ["/p/.venv/bin/python", "-u", "-m", "tulda", "serve"])
        self.assertEqual(service.args_option(a, "--port"), "8799")
        self.assertEqual(service.args_option(a, "--host"), "10.0.0.2")
        self.assertEqual(service.args_option(a, "--renders-root"), "/r")
        self.assertTrue(all(os.path.isabs(x) for x in (a[0], d["WorkingDirectory"],
                                                       d["StandardOutPath"], d["StandardErrorPath"])))
        env = d["EnvironmentVariables"]
        self.assertIn("/opt/homebrew/bin", env["PATH"].split(":"))
        self.assertEqual(env["PYTHONUNBUFFERED"], "1")
        self.assertEqual(d["KeepAlive"], {"SuccessfulExit": False})
        self.assertGreaterEqual(d["ThrottleInterval"], 5)
        self.assertEqual(d["StandardOutPath"], "/L/server.out.log")
        self.assertEqual(d["StandardErrorPath"], "/L/server.err.log")

    def test_multi_host_and_tailscale(self):
        a = self.make(host=["10.0.0.2", "10.0.0.3,10.0.0.2"], tailscale=True)["ProgramArguments"]
        self.assertEqual(service.args_options(a, "--host"), ["10.0.0.2", "10.0.0.3"])
        self.assertEqual(a.count("--host"), 2)
        self.assertIn("--tailscale", a)
        self.assertEqual(service.args_options(self.make(host="1.2.3.4")["ProgramArguments"], "--host"),
                         ["1.2.3.4"])
        self.assertNotIn("--tailscale", self.make()["ProgramArguments"])

    def test_defaults_and_at_login(self):
        d = self.make()
        self.assertNotIn("--host", d["ProgramArguments"])
        self.assertNotIn("--renders-root", d["ProgramArguments"])
        self.assertFalse(d["RunAtLoad"])
        self.assertTrue(self.make(at_login=True)["RunAtLoad"])

    def test_roundtrip(self):
        self.assertEqual(plistlib.loads(service.plist_bytes(self.make()))["Label"], service.LABEL)


class CommandTests(unittest.TestCase):
    def test_argv(self):
        u = os.getuid()
        self.assertEqual(service.argv_bootstrap("/x.plist"), ["bootstrap", f"gui/{u}", "/x.plist"])
        self.assertEqual(service.argv_bootout(), ["bootout", f"gui/{u}/com.tulda.server"])
        self.assertEqual(service.argv_kickstart(), ["kickstart", f"gui/{u}/com.tulda.server"])
        self.assertEqual(service.argv_kickstart(True), ["kickstart", "-k", f"gui/{u}/com.tulda.server"])
        self.assertEqual(service.argv_print(), ["print", f"gui/{u}/com.tulda.server"])
        self.assertEqual(service.argv_enable(False)[0], "disable")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(service, "plist_path", lambda: Path(self.tmp.name) / "x.plist")
        p.start()
        self.addCleanup(p.stop)
        self.calls = []

    def fake(self, loaded):
        def run(*a):
            self.calls.append(list(a))
            if a[0] == "print":
                return cp(0, SAMPLE) if loaded[0] else cp(113)
            if a[0] == "bootstrap":
                loaded[0] = True
            if a[0] == "bootout":
                loaded[0] = False
            return cp()
        return run

    def verbs(self):
        return [c[0] for c in self.calls]

    def test_install_not_at_login(self):
        loaded = [False]
        with mock.patch.object(service, "run_launchctl", self.fake(loaded)):
            service.install(host=[], port=8799, renders_root=None, at_login=False,
                            python="/p/python", project=Path("/p"), log_dir=Path(self.tmp.name) / "logs")
        self.assertEqual(self.verbs(), ["print", "enable", "bootstrap", "disable"])
        self.assertTrue((Path(self.tmp.name) / "logs").is_dir())
        self.assertTrue(service.read_installed())

    def test_install_at_login_replaces_loaded(self):
        loaded = [True]
        with mock.patch.object(service, "run_launchctl", self.fake(loaded)):
            service.install(host=[], port=8799, renders_root=None, at_login=True,
                            python="/p/python", project=Path("/p"), log_dir=Path(self.tmp.name) / "l")
        self.assertEqual(self.verbs(), ["print", "bootout", "enable", "bootstrap"])

    def test_start_stop_restart_uninstall(self):
        loaded = [False]
        with mock.patch.object(service, "run_launchctl", self.fake(loaded)):
            with self.assertRaises(Exception):
                service.start()  # not installed
            (Path(self.tmp.name) / "x.plist").write_bytes(service.plist_bytes(
                service.build_plist(python="/p", project=Path("/p"), host=[], port=1,
                                    renders_root=None, log_dir=Path("/L"))))
            service.start()
            self.assertEqual(self.verbs()[-4:], ["print", "enable", "bootstrap", "disable"])
            self.calls.clear()
            service.start()  # already loaded -> kickstart
            self.assertEqual(self.verbs(), ["print", "kickstart"])
            self.calls.clear()
            service.restart()
            self.assertEqual(self.calls[-1][:2], ["kickstart", "-k"])
            self.calls.clear()
            self.assertTrue(service.stop())
            self.assertEqual(self.verbs(), ["print", "bootout"])
            self.assertFalse(service.stop())
            service.start()
            self.calls.clear()
            self.assertTrue(service.uninstall())
            self.assertEqual(self.verbs(), ["print", "bootout", "enable"])
            self.assertFalse((Path(self.tmp.name) / "x.plist").exists())

    def test_bootstrap_failure(self):
        def run(*a):
            return cp(5, "", "Bootstrap failed") if a[0] == "bootstrap" else cp(113)
        with mock.patch.object(service, "run_launchctl", run), self.assertRaises(Exception) as cm:
            service.install(host=[], port=1, renders_root=None, at_login=False, python="/p",
                            project=Path("/p"), log_dir=Path(self.tmp.name) / "l")
        self.assertIn("Bootstrap failed", str(cm.exception))


class StatusTests(unittest.TestCase):
    def test_parse(self):
        d = service.parse_print(SAMPLE)
        self.assertEqual(d["state"], "running")
        self.assertEqual(d["pid"], 12345)
        self.assertEqual(d["runs"], 3)
        self.assertEqual(d["last exit code"], 75)

    def test_never_exited(self):
        d = service.parse_print("x = {\n\tstate = waiting\n\tlast exit code = (never exited)\n}\n")
        self.assertIsNone(d["last exit code"])
        self.assertNotIn("pid", d)

    def test_not_loaded(self):
        with mock.patch.object(service, "run_launchctl", lambda *a: cp(113, "", "Could not find")):
            self.assertEqual(service.is_loaded(), (False, {}))

    def test_parse_hosts(self):
        from tulda.netinfo import in_cgnat, parse_hosts
        self.assertEqual(parse_hosts(["a, b", "a", "c"]), ["a", "b", "c"])
        self.assertTrue(in_cgnat("100.64.0.1"))
        self.assertFalse(in_cgnat("100.128.0.1"))
        self.assertFalse(in_cgnat("192.168.0.1"))

    def test_parse_hosts_rejects_wildcards(self):
        from tulda.errors import TuldaError
        from tulda.netinfo import parse_hosts
        for bad in ("0.0.0.0", "::", "0:0:0:0:0:0:0:0", "[::]", "0", "", "  ", "0.0.0.0,a",
                    "a,", "::0"):
            with self.subTest(bad=bad), self.assertRaises(TuldaError):
                parse_hosts([bad])
        self.assertEqual(parse_hosts(["::1", "192.0.2.5"]), ["::1", "192.0.2.5"])

    def test_resolve_and_install_reject_wildcards(self):
        from tulda import cli
        from tulda.errors import TuldaError
        with self.assertRaises(TuldaError):
            cli.resolve_hosts(["0.0.0.0"], False)
        with self.assertRaises(TuldaError):
            service.build_plist(python="p", project=Path("/x"), host="::", port=1,
                                renders_root=None, log_dir=Path("/x"))

    def test_resolve_hosts(self):
        import tulda.cli as c
        with mock.patch.object(c, "lan_ipv4", lambda: "192.168.0.5"), \
                mock.patch.object(c, "tailscale_ipv4", lambda: "100.1.1.1"):
            self.assertEqual(c.resolve_hosts([], False), (["192.168.0.5", "127.0.0.1"], {"lan": "192.168.0.5"}))
            self.assertEqual(c.resolve_hosts(["10.0.0.1,10.0.0.2"], True),
                             (["10.0.0.1", "10.0.0.2", "100.1.1.1", "127.0.0.1"], {"tailscale": "100.1.1.1"}))
            self.assertEqual(c.resolve_hosts([], True)[0], ["192.168.0.5", "100.1.1.1", "127.0.0.1"])
            # loopback is never auto-detected, so the IP watcher cannot restart on it
            self.assertNotIn("127.0.0.1", c.resolve_hosts([], True)[1].values())
        with mock.patch.object(c, "tailscale_ipv4", lambda: None):
            self.assertEqual(c.resolve_hosts(["10.0.0.1"], True), (["10.0.0.1", "127.0.0.1"], {"tailscale": None}))
        # an explicit loopback is not duplicated; a LAN fallback of 127.0.0.1 is not either
        with mock.patch.object(c, "lan_ipv4", lambda: "127.0.0.1"), \
                mock.patch.object(c, "tailscale_ipv4", lambda: None):
            self.assertEqual(c.resolve_hosts([], False), (["127.0.0.1"], {"lan": "127.0.0.1"}))
        self.assertEqual(c.resolve_hosts(["127.0.0.1", "10.0.0.1"], False)[0], ["127.0.0.1", "10.0.0.1"])

    def test_probe_hosts_include_loopback(self):
        with mock.patch("tulda.netinfo.lan_ipv4", lambda: "192.168.0.5"), \
                mock.patch("tulda.netinfo.tailscale_ipv4", lambda: "100.1.1.1"):
            self.assertEqual(service._probe_hosts([], True), ["192.168.0.5", "100.1.1.1", "127.0.0.1"])
            self.assertEqual(service._probe_hosts(["10.0.0.2"], False), ["10.0.0.2", "127.0.0.1"])
            self.assertEqual(service._probe_hosts(["127.0.0.1"], False), ["127.0.0.1"])

    def test_status_per_address(self):
        import argparse, io, contextlib
        s = {"installed": True, "plist": "/x", "loaded": True, "state": "running", "pid": 1,
             "last_exit": None, "runs": 1, "at_login": False, "port": 8765, "log_dir": None}
        cfg = {"ProgramArguments": ["py", "--host", "10.0.0.2", "--tailscale"]}
        with mock.patch.object(service, "status_info", lambda: s), \
                mock.patch.object(service, "read_installed", lambda: cfg), \
                mock.patch("tulda.netinfo.tailscale_ipv4", lambda: "100.1.1.1"), \
                mock.patch("tulda.netinfo.tailscale_dnsname", lambda: "myhost.example.ts.net"), \
                mock.patch.object(service, "port_listening", lambda h, p, timeout=1.0: h == "10.0.0.2"):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = service.cmd_status(argparse.Namespace())
        out = buf.getvalue()
        self.assertEqual(rc, 1)
        self.assertIn("listening : yes  (10.0.0.2:8765)", out)
        self.assertIn("listening : no  (100.1.1.1:8765)", out)
        self.assertIn("listening : no  (127.0.0.1:8765)", out)
        self.assertIn("http://100.1.1.1:8765/", out)
        self.assertIn("tailscale : http://myhost.example.ts.net:8765/", out)

    def test_port_listening(self):
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        self.assertTrue(service.port_listening("127.0.0.1", port))
        s.close()
        self.assertFalse(service.port_listening("127.0.0.1", port))


class LogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_tail(self):
        p = self.dir / "a.log"
        p.write_text("".join(f"line{i}\n" for i in range(1000)))
        self.assertEqual(logs.tail_lines(p, 3), ["line997", "line998", "line999"])
        self.assertEqual(len(logs.tail_lines(p, 5000)), 1000)
        self.assertEqual(logs.tail_lines(p, 0), [])

    def test_tail_no_trailing_newline(self):
        p = self.dir / "a.log"
        p.write_text("a\nb\nc")
        self.assertEqual(logs.tail_lines(p, 2), ["b", "c"])

    def test_rotate(self):
        p = self.dir / "s.log"
        p.write_text("x" * 10)
        self.assertFalse(logs.rotate(p, max_bytes=100))
        for gen in ("one", "two", "three"):
            p.write_text(gen * 100)
            self.assertTrue(logs.rotate(p, max_bytes=100, keep=2))
        self.assertFalse(p.exists())
        self.assertTrue((self.dir / "s.log.1").read_text().startswith("three"))
        self.assertTrue((self.dir / "s.log.2").read_text().startswith("two"))
        self.assertFalse((self.dir / "s.log.3").exists())

    def test_rotate_missing(self):
        self.assertFalse(logs.rotate(self.dir / "nope.log"))

    def test_cmd_logs_uses_err(self):
        (self.dir / "server.err.log").write_text("e1\ne2\n")
        (self.dir / "server.out.log").write_text("o1\n")
        import argparse, io, contextlib
        with mock.patch.object(service, "read_installed",
                               lambda: {"StandardOutPath": str(self.dir / "server.out.log")}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                service.cmd_logs(argparse.Namespace(n=1, follow=False, err=True))
            self.assertEqual(buf.getvalue(), "e2\n")


if __name__ == "__main__":
    unittest.main()


class IpWatchTest(unittest.TestCase):
    def test_exit_75_on_ip_change(self):
        code = ("import tulda.cli as c, itertools\n"
                "ips = itertools.chain(['127.0.0.1'], itertools.repeat('10.9.9.9'))\n"
                "c.lan_ipv4 = lambda: next(ips)\n"
                "import sys; sys.exit(c.main(['serve','--port','0','--watch-interval','0.2',"
                "'--renders-root', sys.argv[1]]))\n")
        with tempfile.TemporaryDirectory() as d:
            p = subprocess.run([__import__('sys').executable, "-c", code, d],
                               capture_output=True, text=True, timeout=20)
        self.assertEqual(p.returncode, 75, p.stderr)
        self.assertIn("LAN address changed", p.stdout)
