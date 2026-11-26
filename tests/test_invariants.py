"""Repo invariants: read-only server, no wildcard bind, vendored files intact, licence files."""
import ast
import hashlib
import http.client
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "tulda"
VENDOR = PKG / "static" / "vendor"
WILDCARDS = {"0.0.0.0", "::", "0:0:0:0:0:0:0:0", ""}


def _py_files():
    return [p for p in PKG.rglob("*.py") if "__pycache__" not in p.parts]


class ReadOnlyServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tulda.server import make_server
        cls.tmp = tempfile.TemporaryDirectory()
        cls.srv = make_server(Path(cls.tmp.name) / "renders", "127.0.0.1", 0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def test_every_non_get_method_is_refused(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "CONNECT"):
            for path in ("/", "/anything.png"):
                c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
                c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
                c.putheader("Host", f"127.0.0.1:{self.port}")
                c.putheader("Content-Length", "0")
                c.endheaders()
                r = c.getresponse()
                r.read()
                c.close()
                self.assertEqual(r.status, 405, (method, path))

    def test_server_module_has_no_write_or_service_paths(self):
        tree = ast.parse((PKG / "server.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level:
                self.assertNotIn((node.module or "").split(".")[0], {"manage", "service", "runner", "projects", "watchd", "watchconfig"})
            if isinstance(node, ast.Import):
                self.assertFalse({a.name for a in node.names} & {"subprocess"})
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
                mode = node.args[1] if len(node.args) > 1 else None
                if isinstance(mode, ast.Constant):
                    self.assertFalse(set(str(mode.value)) & set("wax+"), "server.py opens a file for writing")
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, {"write_text", "write_bytes", "unlink", "rmtree", "rmdir"},
                                 "server.py must not modify the filesystem")


class NoWildcardBind(unittest.TestCase):
    def test_no_wildcard_literals_in_package(self):
        for p in _py_files():
            tree = ast.parse(p.read_text())
            comparisons = {id(c) for n in ast.walk(tree) if isinstance(n, ast.Compare)
                           for c in [*n.comparators, n.left]}
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value in WILDCARDS and isinstance(node.value, str):
                    if node.value == "":
                        continue  # empty strings are everywhere; wildcard-ness is covered below
                    # a wildcard may only appear as the operand of a comparison (exclusion check)
                    self.assertIn(id(node), comparisons, f"{p.relative_to(ROOT)}:{node.lineno}: wildcard literal")

    def test_no_empty_host_passed_to_servers(self):
        for p in _py_files():
            for node in ast.walk(ast.parse(p.read_text())):
                if isinstance(node, ast.Call):
                    name = getattr(node.func, "id", getattr(node.func, "attr", ""))
                    if name in ("make_server", "MultiServer", "Server"):
                        for a in node.args:
                            self.assertFalse(isinstance(a, ast.Constant) and a.value in WILDCARDS,
                                             f"{p.name}:{node.lineno}")

    def test_default_hosts_are_never_wildcards(self):
        from tulda import cli
        with mock.patch.object(cli, "lan_ipv4", return_value="192.0.2.10"):
            hosts, _ = cli.resolve_hosts([], False)
        self.assertEqual(hosts, ["192.0.2.10", "127.0.0.1"])
        self.assertFalse(set(hosts) & WILDCARDS)


class WildcardRejected(unittest.TestCase):
    def test_every_wildcard_spelling_is_refused_by_cli(self):
        from tulda import cli
        from tulda.errors import TuldaError
        for w in sorted(WILDCARDS) + ["0", "[::]", "::0"]:
            with self.subTest(host=w), self.assertRaises(TuldaError):
                cli.resolve_hosts([w], False)
            with self.subTest(host=w, via="main"):
                with mock.patch("sys.stderr"):
                    self.assertNotEqual(cli.main(["serve", "--host", w]), 0)


class VendorIntegrity(unittest.TestCase):
    def test_sha256sums_match_exactly(self):
        sums = VENDOR / "SHA256SUMS"
        self.assertTrue(sums.exists(), "tulda/static/vendor/SHA256SUMS is missing")
        want = {}
        for line in sums.read_text().splitlines():
            if line.strip():
                digest, _, name = line.partition("  ")
                want[name.strip()] = digest.strip()
        have = {p.relative_to(VENDOR).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in VENDOR.rglob("*") if p.is_file() and p.name != "SHA256SUMS"}
        self.assertEqual(have, want, "vendored files differ from SHA256SUMS "
                         "(deliberate upgrade: python3 scripts/guard.py vendor-sums)")

    def test_vendored_three_has_licence(self):
        self.assertTrue(list(VENDOR.glob("three-*/LICENSE")))


class LicenceFiles(unittest.TestCase):
    def test_present_and_apache(self):
        lic = (ROOT / "LICENSE").read_text()
        self.assertIn("Apache License", lic)
        self.assertIn("Version 2.0", lic)
        notice = (ROOT / "NOTICE").read_text()
        self.assertIn("three", notice.lower())
        self.assertIn("Apache", notice)


if __name__ == "__main__":
    unittest.main()
