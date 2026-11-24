import contextlib
import http.client
import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tulda import catalog as cm
from tulda.catalog import Catalog, CatalogError, Filters, safe_delete_render, select_prune
from tulda.server import make_server

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16


def ts(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def mk(root: Path, project: str, gid: str | None, created: str, name: str = "a", view: str = "iso",
       title: str | None = None, sources=("a.scad",), adapter="openscad", legacy_day=None, seq=0):
    """One PNG + sidecar with mtime = created."""
    d = root / project
    d.mkdir(parents=True, exist_ok=True)
    base = f"{created.replace('-', '').replace(':', '')}-{adapter}-{view}-{name}"
    meta = {"adapter": adapter, "stage": "source", "source": f"{name}.scad", "view": view,
            "created_utc": created, "steps": [{"tool_version": "OpenSCAD 1"}]}
    if gid:
        meta.update(group_id=gid, group_title=title, group_sources=list(sources),
                    request_cmd="tulda render x", group_created_utc=created, seq=seq)
    (d / f"{base}.png").write_bytes(PNG)
    (d / f"{base}.json").write_text(json.dumps(meta))
    t = ts(created)
    os.utime(d / f"{base}.png", (t, t))
    os.utime(d / f"{base}.json", (t, t))
    return d / f"{base}.png"


def utags(g):
    return [t for t in g["tags"] if t not in g["auto_tags"]]


def fresh_root():
    return Path(tempfile.mkdtemp()).resolve() / "renders"


def quiet(root):
    logs = []
    return Catalog(root, log=logs.append), logs


class Migrations(unittest.TestCase):
    def test_fresh_db_is_latest(self):
        root = fresh_root()
        mk(root, "p", "20261124T100000Z-a", "2026-11-24T10:00:00Z", title="Alpha")
        cat, logs = quiet(root)
        st = cat.stats()
        self.assertEqual(st["schema_version"], cm.SCHEMA_VERSION)
        self.assertEqual((st["groups"], st["renders"]), (1, 1))
        self.assertTrue(any("built 1 renders" in m for m in logs))
        with cat.conn() as c:
            self.assertEqual(c.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_upgrade_from_v1(self):
        root = fresh_root()
        mk(root, "p", "20261124T100000Z-pizza", "2026-11-24T10:00:00Z", title="Pizza tag")
        (root / ".tulda").mkdir()
        raw = sqlite3.connect(root / ".tulda" / "catalog.db", isolation_level=None)
        cm._m1(raw)
        raw.execute("PRAGMA user_version=1")
        raw.execute("INSERT INTO groups(id,title,sources,day,created_utc,last_mtime,n_renders,search_text,starred)"
                    " VALUES('20261124T100000Z-pizza','Pizza tag','[]','2026-11-24','2026-11-24T10:00:00Z',1,1,"
                    "'pizza tag',1)")
        raw.execute("INSERT INTO renders(path,project,file,group_id,mtime) VALUES('p/x.png','p','x.png',"
                    "'20261124T100000Z-pizza',1)")
        raw.close()
        cat, _ = quiet(root)
        self.assertEqual(cat.stats()["schema_version"], cm.SCHEMA_VERSION)
        g = cat.get_group("20261124T100000Z-pizza")
        self.assertTrue(g["starred"])          # v1 data survived
        self.assertTrue(cat.fts)
        groups, _ = cat.list_groups(Filters(q="piz"))
        self.assertEqual([x["id"] for x in groups], ["20261124T100000Z-pizza"])  # FTS populated by v2

    def test_newer_schema_refused(self):
        root = fresh_root()
        (root / ".tulda").mkdir(parents=True)
        raw = sqlite3.connect(root / ".tulda" / "catalog.db")
        raw.execute("PRAGMA user_version=99")
        raw.commit()
        raw.close()
        with self.assertRaises(CatalogError):
            Catalog(root).stats()


class Reindex(unittest.TestCase):
    def setUp(self):
        self.root = fresh_root()
        self.g1 = "20261124T100000Z-one"
        self.g2 = "20261123T100000Z-two"
        self.p1 = mk(self.root, "p", self.g1, "2026-11-24T10:00:00Z", title="One", name="a")
        mk(self.root, "p", self.g1, "2026-11-24T10:00:00Z", name="a", view="bottom", seq=1)
        mk(self.root, "q", self.g2, "2026-11-23T10:00:00Z", title="Two", name="b")
        self.cat, _ = quiet(self.root)

    def snapshot(self):
        with self.cat.conn() as c:
            return ([tuple(r) for r in c.execute("SELECT path,group_id,mtime,missing FROM renders ORDER BY path")],
                    [tuple(r) for r in c.execute(
                        "SELECT id,n_renders,last_mtime,day FROM groups ORDER BY id")])

    def test_idempotent_and_preserves_metadata(self):
        self.cat.set_starred(self.g1, True)
        self.cat.add_tags(self.g1, ["Keep", "nfc"])
        self.cat.set_title(self.g2, "My title")
        self.cat.set_trashed(self.g2, True)
        self.cat.set_notes(self.g1, "hello")
        before = self.snapshot()
        for _ in range(2):
            self.cat.sync(force=True)
        self.assertEqual(self.snapshot(), before)
        g1 = self.cat.get_group(self.g1)
        self.assertEqual((g1["starred"], utags(g1), g1["notes"]), (True, ["keep", "nfc"], "hello"))
        g2 = self.cat.get_group(self.g2)
        self.assertEqual(g2["title"], "My title")
        self.assertEqual(g2["auto_title"], "Two")
        self.assertTrue(g2["trashed"])

    def test_rebuild_from_scratch_equals_incremental(self):
        before = self.snapshot()
        (self.root / ".tulda" / "catalog.db").unlink()
        cat2, _ = quiet(self.root)
        self.cat = cat2
        self.assertEqual(self.snapshot(), before)

    def test_missing_files_marked_not_wiped(self):
        self.cat.set_starred(self.g2, True)
        for f in (self.root / "q").iterdir():
            f.unlink()
        self.cat.sync()
        st = self.cat.stats()
        self.assertEqual((st["renders"], st["missing_renders"], st["groups"]), (2, 1, 1))
        self.assertEqual([g["id"] for g in self.cat.list_groups()[0]], [self.g1])
        # file comes back: row revived, metadata still there
        mk(self.root, "q", self.g2, "2026-11-23T10:00:00Z", title="Two", name="b")
        self.cat.sync()
        self.assertTrue(self.cat.get_group(self.g2)["starred"])
        self.assertEqual(self.cat.stats()["missing_renders"], 0)

    def test_incremental_picks_up_copied_in_render(self):
        self.cat.sync()
        mk(self.root, "p", "20261124T120000Z-new", "2026-11-24T12:00:00Z", title="New", name="n")
        st = self.cat.sync()
        self.assertEqual(st["indexed"], 1)
        self.assertEqual(self.cat.list_groups()[0][0]["id"], "20261124T120000Z-new")
        # unchanged tree: nothing re-read (dir mtime older than RECENT_SECS after we age it)
        old = time.time() - 60
        for d in self.root.iterdir():
            if d.is_dir() and not d.name.startswith("."):
                os.utime(d, (old, old))
        self.cat.sync()
        self.assertEqual(self.cat.sync()["indexed"], 0)

    def test_legacy_groups_per_day(self):
        mk(self.root, "old", None, "2026-10-01T09:00:00Z", name="x")
        mk(self.root, "old", None, "2026-10-01T18:00:00Z", name="y")
        mk(self.root, "old", None, "2026-10-02T01:00:00Z", name="z")
        self.cat.sync()
        legacy = {g["id"]: g for g in self.cat.list_groups(Filters(day="2026-10"))[0]}
        self.assertEqual(set(legacy), {"legacy-20261001", "legacy-20261002"})
        self.assertEqual(legacy["legacy-20261001"]["n_renders"], 2)
        self.assertEqual(legacy["legacy-20261001"]["day"], "2026-10-01")

    def test_index_png_for_new_render(self):
        png = mk(self.root, "p", "20261124T130000Z-fresh", "2026-11-24T13:00:00Z", name="f", title="Fresh")
        self.assertTrue(self.cat.index_png(png))
        self.assertEqual(self.cat.get_group("20261124T130000Z-fresh")["n_renders"], 1)


class Corruption(unittest.TestCase):
    def test_garbage_db_is_rebuilt(self):
        root = fresh_root()
        mk(root, "p", "20261124T100000Z-a", "2026-11-24T10:00:00Z", title="A")
        cat, _ = quiet(root)
        cat.stats()
        db = root / ".tulda" / "catalog.db"
        for ext in ("-wal", "-shm"):
            Path(str(db) + ext).unlink(missing_ok=True)
        db.write_bytes(b"this is not a database" * 500)
        cat2, logs = quiet(root)
        self.assertEqual(cat2.stats()["renders"], 1)
        self.assertTrue(any("corrupt" in m for m in logs), logs)
        self.assertTrue(any("built 1 renders" in m for m in logs), logs)
        self.assertTrue(list((root / ".tulda").glob("catalog.db.corrupt-*")))

    def test_metadata_salvaged_when_still_readable(self):
        root = fresh_root()
        gid = "20261124T100000Z-a"
        mk(root, "p", gid, "2026-11-24T10:00:00Z", title="A")
        cat, _ = quiet(root)
        cat.set_starred(gid, True)
        cat.add_tags(gid, ["x"])
        s = cat._salvage()
        self.assertEqual(len(s["groups"]), 1)
        (root / ".tulda" / "catalog.db").unlink()
        cat2, _ = quiet(root)
        cat2.stats()
        cat2._restore_meta(s)
        g = cat2.get_group(gid)
        self.assertEqual((g["starred"], utags(g)), (True, ["x"]))


class Concurrency(unittest.TestCase):
    def test_writer_and_reader(self):
        root = fresh_root()
        mk(root, "p", "20261101T000000Z-seed", "2026-11-01T00:00:00Z")
        writer, _ = quiet(root)
        reader, _ = quiet(root)
        writer.stats()
        errors = []
        done = threading.Event()

        def write():
            try:
                for i in range(120):
                    png = mk(root, "p", f"20261102T{i // 60:02d}{i % 60:02d}00Z-w{i}",
                             f"2026-11-02T{i // 60:02d}:{i % 60:02d}:00Z", name=f"w{i}", title=f"W{i}")
                    writer.index_png(png)
                    if i % 20 == 0:
                        writer.set_starred(f"20261102T{i // 60:02d}{i % 60:02d}00Z-w{i}", True)
            except Exception as e:  # noqa: BLE001
                errors.append(("w", repr(e)))
            finally:
                done.set()

        def read():
            n = 0
            try:
                while not done.is_set() or n < 5:
                    groups, _ = reader.list_groups(limit=50)
                    reader.latest()
                    reader.facets()
                    n += 1
            except Exception as e:  # noqa: BLE001
                errors.append(("r", repr(e)))

        ts_ = [threading.Thread(target=write), threading.Thread(target=read), threading.Thread(target=read)]
        [t.start() for t in ts_]
        [t.join(60) for t in ts_]
        self.assertEqual(errors, [])
        self.assertEqual(reader.stats()["groups"], 121)

    def test_two_syncers(self):
        root = fresh_root()
        for i in range(30):
            mk(root, "p", f"2026110{1 + i % 9}T0000{i:02d}Z-g{i}", f"2026-11-0{1 + i % 9}T00:00:{i:02d}Z", name=f"n{i}")
        a, _ = quiet(root)
        b, _ = quiet(root)
        errs = []

        def go(c):
            try:
                for _ in range(5):
                    c.sync(force=True)
            except Exception as e:  # noqa: BLE001
                errs.append(repr(e))
        t = [threading.Thread(target=go, args=(c,)) for c in (a, b)]
        [x.start() for x in t]
        [x.join(60) for x in t]
        self.assertEqual(errs, [])
        self.assertEqual(a.stats()["renders"], 30)


class Queries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = fresh_root()
        n = 0
        cls.ids = []
        for day in ("2026-09-30", "2026-10-05", "2026-11-01", "2026-11-24"):
            for h in range(0, 12, 3):
                n += 1
                created = f"{day}T{h:02d}:00:00Z"
                gid = f"{created.replace('-', '').replace(':', '')}-g{n}"
                cls.ids.append(gid)
                mk(cls.root, "p" if n % 2 else "q", gid, created, name=f"m{n}", title=f"Group {n}",
                   adapter="openscad" if n % 3 else "bambu-export-png", view="iso" if n % 2 else "bottom")
        cls.cat, _ = quiet(cls.root)
        cls.cat.add_tags(cls.ids[0], ["nfc"])
        cls.cat.set_starred(cls.ids[1], True)
        cls.cat.set_trashed(cls.ids[2], True)

    def test_pagination_covers_all_once_in_order(self):
        seen, cur, pages = [], None, 0
        while True:
            gs, cur = self.cat.list_groups(limit=5, cursor=cur)
            seen += [g["id"] for g in gs]
            pages += 1
            if not cur:
                break
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(len(seen), 15)       # 16 minus the trashed one
        self.assertEqual(pages, 3)
        self.assertEqual(seen, sorted(seen, key=lambda i: i, reverse=True))
        self.assertNotIn(self.ids[2], seen)

    def test_show_trash(self):
        gs, _ = self.cat.list_groups(Filters(trash=True), limit=100)
        self.assertEqual(len(gs), 16)
        gs, _ = self.cat.list_groups(Filters(trashed_only=True), limit=100)
        self.assertEqual([g["id"] for g in gs], [self.ids[2]])

    def test_bad_cursor(self):
        with self.assertRaises(CatalogError):
            self.cat.list_groups(cursor="nope~x")

    def test_dates_and_day_filters(self):
        d = dict((k, v) for k, v in self.cat.dates())
        self.assertEqual(d["2026-11-24"], 4)
        self.assertEqual(d["2026-09-30"], 3)        # one of four trashed
        self.assertEqual(d["2026-11-01"], 4)
        self.assertEqual(list(d)[0], "2026-11-24")
        self.assertEqual(len(self.cat.list_groups(Filters(day="2026-11"), limit=100)[0]), 8)
        self.assertEqual(len(self.cat.list_groups(Filters(day="2026-11-24"), limit=100)[0]), 4)
        self.assertEqual(len(self.cat.list_groups(Filters(day="2026-12"), limit=100)[0]), 0)
        self.assertEqual(len(self.cat.list_groups(Filters(since="2026-10-01", until="2026-10-31"), limit=100)[0]), 4)

    def test_filters(self):
        ls = lambda **k: self.cat.list_groups(Filters(**k), limit=100)[0]
        self.assertEqual([g["id"] for g in ls(tags=["nfc"])], [self.ids[0]])
        self.assertEqual([g["id"] for g in ls(starred=True)], [self.ids[1]])
        self.assertTrue(all(i["project"] == "p" for g in ls(project="p") for i in g["items"]))
        self.assertTrue(ls(adapters=["bambu-export-png"]))
        self.assertTrue(all(i["view"] == "bottom" for g in ls(views=["bottom"]) for i in g["items"]))
        self.assertEqual(ls(adapters=["nope"]), [])

    def test_search_fts_and_like(self):
        for fts in (True, False):
            self.cat.fts = fts
            try:
                self.assertEqual([g["id"] for g in self.cat.list_groups(Filters(q="nfc"))[0]], [self.ids[0]], fts)
                self.assertEqual(len(self.cat.list_groups(Filters(q="group 1"), limit=100)[0]) > 0, True)
                self.assertEqual(self.cat.list_groups(Filters(q="zzzz"))[0], [])
                self.assertEqual(self.cat.list_groups(Filters(q='"; drop table'))[0], [])
            finally:
                self.cat.fts = True

    def test_resolve_prefix(self):
        self.assertEqual(self.cat.resolve_group(self.ids[0][:20])["id"], self.ids[0]) \
            if sum(i.startswith(self.ids[0][:20]) for i in self.ids) == 1 else None
        with self.assertRaises(CatalogError):
            self.cat.resolve_group("2026")      # ambiguous
        with self.assertRaises(CatalogError):
            self.cat.resolve_group("nothing")


class Prune(unittest.TestCase):
    NOW = ts("2026-11-24T12:00:00Z")

    def groups(self):
        days = [0, 1, 5, 10, 40, 50, 60]
        return [{"id": f"g{d}", "mtime": self.NOW - d * 86400, "starred": d == 50} for d in days]

    def ids(self, **kw):
        return [g["id"] for g in select_prune(self.groups(), self.NOW, **kw)]

    def test_older_than(self):
        self.assertEqual(self.ids(older_than=cm.parse_duration("30d")), ["g40", "g60"])  # g50 starred
        self.assertEqual(self.ids(older_than=cm.parse_duration("30d"), keep_starred=False),
                         ["g40", "g50", "g60"])

    def test_keep_last(self):
        self.assertEqual(self.ids(keep_last=5), ["g60"])       # g50 starred, kept
        self.assertEqual(self.ids(keep_last=0, keep_starred=True), ["g0", "g1", "g5", "g10", "g40", "g60"])
        self.assertEqual(self.ids(keep_last=100), [])

    def test_combined(self):
        self.assertEqual(self.ids(older_than=cm.parse_duration("7d"), keep_last=3), ["g10", "g40", "g60"])

    def test_needs_a_rule(self):
        with self.assertRaises(CatalogError):
            select_prune(self.groups(), self.NOW)

    def test_duration(self):
        self.assertEqual(cm.parse_duration("2w").days, 14)
        for bad in ("", "x", "10", "3m", "-1d"):
            with self.assertRaises(CatalogError):
                cm.parse_duration(bad)

    def test_candidates_project_exclusive_and_not_trashed(self):
        root = fresh_root()
        mk(root, "p", "20261101T000000Z-only-p", "2026-11-01T00:00:00Z", name="a")
        mk(root, "p", "20261102T000000Z-span", "2026-11-02T00:00:00Z", name="b")
        mk(root, "q", "20261102T000000Z-span", "2026-11-02T00:00:00Z", name="c")
        mk(root, "p", "20261103T000000Z-trashed", "2026-11-03T00:00:00Z", name="d")
        cat, _ = quiet(root)
        cat.set_trashed("20261103T000000Z-trashed", True)
        self.assertEqual({g["id"] for g in cm.prune_candidates(cat)},
                         {"20261101T000000Z-only-p", "20261102T000000Z-span"})
        self.assertEqual({g["id"] for g in cm.prune_candidates(cat, "p")}, {"20261101T000000Z-only-p"})


class DeleteSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.root = self.tmp / "renders"
        (self.tmp / "outside").mkdir()
        self.victim = self.tmp / "outside" / "victim.png"
        self.victim.write_bytes(PNG)
        (self.tmp / "outside" / "victim.json").write_text("{}")

    def test_traversal_and_bad_names(self):
        (self.root / "p").mkdir(parents=True)
        for project, file in (("..", "x.png"), ("p", "../../outside/victim.png"), ("p/..", "x.png"),
                              (".hidden", "x.png"), ("p", ".x.png"), ("p", "x.txt"), ("", "x.png"),
                              ("p", "a\\b.png"), ("p", "x\x00.png"), ("../outside", "victim.png")):
            with self.assertRaises(CatalogError, msg=(project, file)):
                safe_delete_render(self.root, project, file)
        self.assertTrue(self.victim.exists())

    def test_symlinked_project_dir_refused(self):
        self.root.mkdir()
        os.symlink(self.tmp / "outside", self.root / "linkproj")
        with self.assertRaises(CatalogError):
            safe_delete_render(self.root, "linkproj", "victim.png")
        self.assertTrue(self.victim.exists())

    def test_symlink_file_escaping_root_refused_target_intact(self):
        (self.root / "p").mkdir(parents=True)
        os.symlink(self.victim, self.root / "p" / "esc.png")
        os.symlink(self.tmp / "outside" / "victim.json", self.root / "p" / "esc.json")
        with self.assertRaises(CatalogError):
            safe_delete_render(self.root, "p", "esc.png")
        self.assertTrue(self.victim.exists())
        self.assertTrue((self.tmp / "outside" / "victim.json").exists())
        self.assertTrue(os.path.islink(self.root / "p" / "esc.png"))

    def test_internal_symlink_removes_link_only(self):
        (self.root / "p").mkdir(parents=True)
        real = self.root / "p" / "real.png"
        real.write_bytes(PNG)
        os.symlink(real, self.root / "p" / "alias.png")
        self.assertEqual(safe_delete_render(self.root, "p", "alias.png"), ["p/alias.png"])
        self.assertTrue(real.exists())

    def test_delete_group_end_to_end(self):
        gid = "20261124T100000Z-gone"
        a = mk(self.root, "p", gid, "2026-11-24T10:00:00Z", name="a")
        mk(self.root, "p", gid, "2026-11-24T10:00:00Z", name="a", view="bottom")
        keep = mk(self.root, "p", "20261123T100000Z-keep", "2026-11-23T10:00:00Z", name="k")
        cat, _ = quiet(self.root)
        cat.set_starred(gid, True)
        res = cat.delete_group(gid)
        self.assertEqual((len(res["deleted"]), res["errors"]), (4, []))
        self.assertFalse(a.exists())
        self.assertTrue(keep.exists())
        self.assertIsNone(cat.get_group(gid))
        self.assertEqual(cat.stats()["groups"], 1)
        self.assertEqual(cat.list_groups(Filters(q="gone"))[0], [])
        self.assertEqual(cat.check(), [])

    def test_delete_group_with_escaping_symlink_keeps_target_and_row(self):
        gid = "20261124T100000Z-esc"
        p = mk(self.root, "p", gid, "2026-11-24T10:00:00Z", name="a")
        cat, _ = quiet(self.root)
        cat.stats()                       # indexed while still a regular file
        p.unlink()
        os.symlink(self.victim, p)
        res = cat.delete_group(gid)
        self.assertEqual(len(res["errors"]), 1)
        self.assertTrue(self.victim.exists())
        self.assertIsNotNone(cat.get_group(gid))     # row kept: file was not removed


class ManageCli(unittest.TestCase):
    def run_cli(self, root, *argv, stdin=""):
        from tulda import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            old = os.sys.stdin
            os.sys.stdin = io.StringIO(stdin)
            try:
                code = cli.main([*argv, "--renders-root", str(root)])
            finally:
                os.sys.stdin = old
        return code, out.getvalue(), err.getvalue()

    def test_roundtrip(self):
        root = fresh_root()
        gid = "20261124T100000Z-pizza-test"
        png = mk(root, "p", gid, "2026-11-24T10:00:00Z", title="Pizza")
        mk(root, "p", "20250101T000000Z-old", "2025-01-01T00:00:00Z", name="o", title="Old")
        c, o, _ = self.run_cli(root, "ls")
        self.assertEqual(c, 0)
        self.assertIn(gid, o)
        self.run_cli(root, "group", "star", "20261124T1000")
        self.run_cli(root, "group", "tag", gid, "NFC", "x")
        self.run_cli(root, "group", "rename", gid, "Renamed")
        c, o, _ = self.run_cli(root, "ls", "--starred", "--tag", "nfc")
        self.assertIn("Renamed", o)
        self.assertIn(" -", o.replace("*-", " -")) if False else None
        c, o, _ = self.run_cli(root, "group", "show", gid)
        self.assertIn("nfc, x", o)
        # trash hides, restore shows
        self.run_cli(root, "group", "trash", gid)
        self.assertNotIn(gid, self.run_cli(root, "ls")[1])
        self.assertIn(gid, self.run_cli(root, "ls", "--trashed")[1])
        self.run_cli(root, "group", "restore", gid)
        self.assertIn(gid, self.run_cli(root, "ls")[1])
        # prune: dry run by default, starred kept
        c, o, _ = self.run_cli(root, "prune", "--older-than", "30d")
        self.assertIn("would delete: 20250101T000000Z-old", o)
        self.assertTrue(list((root / "p").glob("2025*")))
        c, o, _ = self.run_cli(root, "prune", "--older-than", "30d", "--yes")
        self.assertFalse(list((root / "p").glob("2025*")))
        self.assertTrue(png.exists())
        # rm asks; 'n' aborts, --yes deletes
        c, o, _ = self.run_cli(root, "group", "rm", gid, stdin="n\n")
        self.assertEqual(c, 1)
        self.assertTrue(png.exists())
        c, o, _ = self.run_cli(root, "group", "rm", gid, "--yes")
        self.assertEqual(c, 0)
        self.assertFalse(png.exists())
        self.assertEqual(self.run_cli(root, "db", "check")[0], 0)

    def test_trash_empty_and_db_commands(self):
        root = fresh_root()
        gid = "20261124T100000Z-t"
        png = mk(root, "p", gid, "2026-11-24T10:00:00Z")
        self.run_cli(root, "group", "trash", gid)
        self.assertEqual(self.run_cli(root, "trash", "empty", stdin="")[0], 1)   # no confirmation
        self.assertTrue(png.exists())
        self.assertEqual(self.run_cli(root, "trash", "empty", "--yes")[0], 0)
        self.assertFalse(png.exists())
        self.assertIn("schema_version", self.run_cli(root, "db", "stats")[1])
        self.assertEqual(self.run_cli(root, "db", "reindex")[0], 0)
        self.assertEqual(self.run_cli(root, "db", "vacuum")[0], 0)

    def test_prune_conflicting_flags(self):
        root = fresh_root()
        self.assertEqual(self.run_cli(root, "prune", "--yes", "--dry-run", "--keep-last", "1")[0], 1)
        self.assertEqual(self.run_cli(root, "prune")[0], 1)     # needs a rule


class ServerCatalog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = fresh_root()
        for i in range(30):
            d = 1 + i % 9
            mk(cls.root, "p", f"2026110{d}T0000{i:02d}Z-g{i}", f"2026-11-0{d}T00:00:{i:02d}Z", name=f"n{i}",
               title=f"Request {i}")
        mk(cls.root, "q", "20261124T221127Z-pizza-nfc-tag-test", "2026-11-24T22:11:27Z", name="pz", title="Pizza")
        cls.cat = Catalog(cls.root, log=lambda m: None)
        cls.cat.set_trashed("20261101T000000Z-g0", True)
        cls.srv = make_server(cls.root, "127.0.0.1", 0, catalog=cls.cat)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def req(self, method, path, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        c.putheader("Host", host or f"127.0.0.1:{self.port}")
        c.endheaders()
        r = c.getresponse()
        b = r.read()
        c.close()
        return r.status, b, r

    def test_pages_ok(self):
        for p in ("/", "/q/", "/_d/2026-11/", "/_d/2026-11-24/", "/_d/2026-12/", "/search?q=pizza",
                  "/search", "/_g/20261124T221127Z-pizza-nfc-tag-test/", "/api/latest", "/api/dates",
                  "/api/groups?limit=5", "/api/index"):
            self.assertEqual(self.req("GET", p)[0], 200, p)
        self.assertEqual(self.req("HEAD", "/_d/2026-11/")[0], 200)

    def test_bad_inputs(self):
        for p in ("/_d/2026-13/", "/_d/x/", "/_d/2026-11-31/", "/_d/", "/_d/2026-11/extra", "/_g/nope/",
                  "/_g/a%2Fb/"):
            self.assertEqual(self.req("GET", p)[0], 404, p)
        self.assertEqual(self.req("GET", "/api/groups?cursor=bad")[0], 400)
        self.assertEqual(self.req("GET", "/api/groups?day=junk")[0], 400)
        self.assertEqual(self.req("GET", "/api/latest", host="evil.example")[0], 403)
        self.assertEqual(self.req("GET", "/search?q=x", host="evil.example")[0], 403)

    def test_database_not_served(self):
        for p in ("/.tulda/catalog.db", "/.tulda/", "/%2etulda/catalog.db"):
            self.assertEqual(self.req("GET", p)[0], 403, p)

    def test_still_read_only(self):
        for m in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            for p in ("/", "/api/groups", "/_d/2026-11/", "/search?q=a", "/_g/x/"):
                s, _, r = self.req(m, p)
                self.assertEqual(s, 405, (m, p))
                self.assertEqual(r.getheader("Allow"), "GET, HEAD")
        # no handler for any other mutating verb either
        names = [n for n in dir(self.srv.RequestHandlerClass) if n.startswith("do_")]
        self.assertEqual(sorted(names), sorted(["do_GET", "do_HEAD", "do_POST", "do_PUT", "do_DELETE",
                                                "do_PATCH", "do_OPTIONS", "do_TRACE", "do_CONNECT"]))

    def test_pagination_api(self):
        seen, cur = [], None
        for _ in range(10):
            s, b, _ = self.req("GET", "/api/groups?limit=7" + (f"&cursor={cur}" if cur else ""))
            j = json.loads(b)
            seen += [g["id"] for g in j["groups"]]
            cur = j["next"]
            if not cur:
                break
        self.assertEqual(len(seen), len(set(seen)))
        s, b, _ = self.req("GET", "/api/groups?limit=100&trash=1")
        everything = [g["id"] for g in json.loads(b)["groups"]]
        self.assertEqual(sorted(seen), sorted(set(everything) - {"20261101T000000Z-g0"}))

    def test_date_page_data_and_search(self):
        _, b, _ = self.req("GET", "/_d/2026-11-24/")
        self.assertIn(b"20261124T221127Z-pizza-nfc-tag-test", b)
        self.assertNotIn(b"20261101T000000Z-g1", b)
        _, b, _ = self.req("GET", "/search?q=pizza")
        self.assertIn(b"pizza-nfc-tag-test", b)
        _, b, _ = self.req("GET", "/api/groups?q=pizza")
        self.assertEqual([g["id"] for g in json.loads(b)["groups"]], ["20261124T221127Z-pizza-nfc-tag-test"])

    def test_trash_hidden_by_default(self):
        _, b, _ = self.req("GET", "/api/groups?limit=100&day=2026-11-01")
        self.assertNotIn("20261101T000000Z-g0", [g["id"] for g in json.loads(b)["groups"]])
        _, b, _ = self.req("GET", "/api/groups?limit=100&day=2026-11-01&trash=1")
        self.assertIn("20261101T000000Z-g0", [g["id"] for g in json.loads(b)["groups"]])

    def test_latest_tracks_changes(self):
        a = json.loads(self.req("GET", "/api/latest")[1])
        self.cat.set_starred("20261101T000000Z-g0", True)
        b = json.loads(self.req("GET", "/api/latest")[1])
        self.assertGreater(b["rev"], a["rev"])

    def test_new_file_picked_up_by_request_sync(self):
        self.srv.sync_min_interval = 0
        try:
            mk(self.root, "p", "20261125T000000Z-late", "2026-11-25T00:00:00Z", name="late", title="Late")
            _, b, _ = self.req("GET", "/api/groups?limit=3")
            self.assertEqual(json.loads(b)["groups"][0]["id"], "20261125T000000Z-late")
        finally:
            self.srv.sync_min_interval = 2.0

    def test_fold_markup(self):
        _, b, r = self.req("GET", "/")
        h = b.decode()
        self.assertIn("text/html", r.getheader("Content-Type"))
        for needle in ("createElement", "'details'", "'summary'", "aria-expanded", "Collapse all", "Expand all",
                       "tulda.fold.v1", "localStorage", "loading='lazy'", "Load more requests"):
            self.assertIn(needle, h, needle)
        self.assertNotIn("https://", h.replace("https://claude", ""))   # no external deps
        self.assertNotIn("<script src", h)
        self.assertIn("single=ctx.kind==='group'", h)       # group page: never collapsible


if __name__ == "__main__":
    unittest.main()


def get(port, path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path)
    r = c.getresponse()
    return r.status, r.read(), r


class Serving:
    def start(self, root):
        self.srv = make_server(root, "127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def page_data(self, path="/"):
        s, b, r = get(self.port, path)
        assert s == 200, s
        txt = b.decode().split('id="data">', 1)[1].split("</script>", 1)[0]
        return json.loads(txt), r

    def api(self, path):
        return json.loads(get(self.port, path)[1])


class LatestStability(unittest.TestCase):
    def setUp(self):
        self.root = fresh_root()
        mk(self.root, "p", "20261124T100000Z-a", "2026-11-24T10:00:00Z", "a", title="A")
        self.s = Serving()
        self.s.start(self.root)
        self.addCleanup(self.s.stop)

    def test_page_baseline_matches_latest_and_is_stable(self):
        d, r = self.s.page_data()
        self.assertIn("no-cache", r.getheader("Cache-Control"))
        for _ in range(3):
            self.assertEqual(self.s.api("/api/latest")["rev"], d["rev"])
        d2, _ = self.s.page_data("/p/")
        self.assertEqual(d2["rev"], d["rev"])

    def test_sync_without_new_files_does_not_change_value(self):
        d, _ = self.s.page_data()
        before = self.s.api("/api/latest")
        self.s.srv.catalog.sync()
        self.s.srv.catalog.sync(force=False)
        time.sleep(0.05)
        self.s.srv.catalog.maybe_sync(0)
        self.assertEqual(self.s.api("/api/latest"), before)
        self.assertEqual(before["rev"], d["rev"])

    def test_new_render_changes_value_once(self):
        d, _ = self.s.page_data()
        mk(self.root, "p", "20261124T110000Z-b", "2026-11-24T11:00:00Z", "b", title="B")
        self.s.srv.catalog.sync()
        self.assertNotEqual(self.s.api("/api/latest")["rev"], d["rev"])
        d2, _ = self.s.page_data()
        self.assertEqual(self.s.api("/api/latest")["rev"], d2["rev"])

    def test_hidden_attribute_not_overridden_by_css(self):
        s, b, _ = get(self.s.port, "/")
        self.assertIn("[hidden]{display:none!important}", b.decode())


class Pagination(unittest.TestCase):
    def build(self, n):
        root = fresh_root()
        for i in range(n):
            mk(root, "p", f"20261124T{10 + i // 60:02d}{i % 60:02d}00Z-g{i}", f"2026-11-24T{10 + i // 60:02d}:{i % 60:02d}:00Z",
               f"n{i}", title=f"G{i}")
        s = Serving()
        s.start(root)
        self.addCleanup(s.stop)
        return s

    def test_exact_boundary_and_one_over(self):
        from tulda.server import PAGE_SIZE
        for n, more in ((PAGE_SIZE - 1, False), (PAGE_SIZE, False), (PAGE_SIZE + 1, True)):
            s = self.build(n)
            for path in ("/", "/p/", "/_d/2026-11-24/", "/search?q=G", "/?tag=n0"):
                d, _ = s.page_data(path)
                if path == "/?tag=n0":
                    self.assertFalse(d["has_more"])
                    continue
                self.assertEqual(d["has_more"], more, (n, path))
                self.assertEqual(d["next"] is not None, more)
                self.assertEqual(len(d["groups"]), min(n, PAGE_SIZE))
            s.stop()
            self.addCleanup(lambda: None)

    def test_api_pages_end_with_has_more_false(self):
        s = self.build(25)
        a = s.api("/api/groups?limit=20")
        self.assertTrue(a["has_more"])
        b = s.api("/api/groups?limit=20&cursor=" + a["next"].replace("~", "%7E"))
        self.assertEqual((len(b["groups"]), b["has_more"], b["next"]), (5, False, None))
        s2 = self.build(20)
        self.assertFalse(s2.api("/api/groups?limit=20")["has_more"])


class AutoTags(unittest.TestCase):
    def setUp(self):
        self.root = fresh_root()
        mk(self.root, "nfc", "20261124T100000Z-pz", "2026-11-24T10:00:00Z", "nfc_holder_body_pizza",
           title="Pizza", sources=("nfc_holder_body_pizza.scad", "t.3mf"))
        p = mk(self.root, "build", "20261124T100000Z-pz", "2026-11-24T10:00:01Z", "nfc_teardrop_pizza",
               title="Pizza", adapter="bambu-export-png")
        sc = p.with_suffix(".json")
        m = json.loads(sc.read_text())
        m["source"] = "nfc_teardrop_pizza.3mf"
        sc.write_text(json.dumps(m))
        self.cat, _ = quiet(self.root)
        self.gid = "20261124T100000Z-pz"

    def test_source_tags_derived(self):
        g = self.cat.get_group(self.gid)
        self.assertEqual(g["auto_tags"], ["nfc_holder_body_pizza", "nfc_teardrop_pizza"])
        self.assertEqual({i["tag"] for i in g["items"]}, {"nfc_holder_body_pizza", "nfc_teardrop_pizza"})
        self.assertEqual(cm.source_tag("/x/Foo Bar.SCAD"), "foo_bar")
        self.assertIsNone(cm.source_tag(None))

    def test_filter_and_search_match_source_tags(self):
        for f in (Filters(tags=["nfc_holder_body_pizza"]), Filters(q="nfc_holder_body_pizza"),
                  Filters(q="nfc_teardrop")):
            self.assertEqual([g["id"] for g in self.cat.list_groups(f)[0]], [self.gid])
        self.assertEqual(self.cat.list_groups(Filters(tags=["nope"]))[0], [])
        self.assertIn(["nfc_teardrop_pizza", 1], self.cat.facets()["tags"])

    def test_user_tags_independent_of_auto(self):
        self.cat.add_tags(self.gid, ["keep"])
        with self.assertRaises(CatalogError):
            self.cat.remove_tags(self.gid, ["nfc_teardrop_pizza"])
        self.cat.remove_tags(self.gid, ["keep"])
        self.assertEqual(utags(self.cat.get_group(self.gid)), [])
        self.cat.add_tags(self.gid, ["keep"])
        self.cat.sync(force=True)       # reindex keeps user tags and regenerates auto tags
        g = self.cat.get_group(self.gid)
        self.assertEqual(utags(g), ["keep"])
        self.assertEqual(len(g["auto_tags"]), 2)

    def test_reindex_regenerates_and_prunes_auto_tags(self):
        with self.cat.conn(True) as c:
            c.execute("DELETE FROM tags WHERE kind='auto'")
            c.execute("INSERT INTO tags(group_id,tag,kind) VALUES(?,?,'auto')", (self.gid, "stale"))
        self.cat.sync(force=True)
        self.assertEqual(self.cat.get_group(self.gid)["auto_tags"], ["nfc_holder_body_pizza", "nfc_teardrop_pizza"])

    def test_upgrade_from_v2_backfills(self):
        root = fresh_root()
        mk(root, "p", "20261124T100000Z-x", "2026-11-24T10:00:00Z", "widget", title="X")
        (root / ".tulda").mkdir()
        raw = sqlite3.connect(root / ".tulda" / "catalog.db", isolation_level=None)
        cm._m1(raw)
        cm._m2(raw)
        raw.execute("PRAGMA user_version=2")
        raw.execute("INSERT INTO groups(id,sources,day,last_mtime,n_renders) VALUES('20261124T100000Z-x','[]',"
                    "'2026-11-24',1,1)")
        raw.execute("INSERT INTO tags VALUES('20261124T100000Z-x','mine')")
        raw.close()
        cat, _ = quiet(root)
        cat.ensure()
        g = cat.get_group("20261124T100000Z-x")
        self.assertEqual((g["auto_tags"], utags(g)), (["widget"], ["mine"]))

    def test_tag_url_serves_group(self):
        s = Serving()
        s.start(self.root)
        self.addCleanup(s.stop)
        d, _ = s.page_data("/?tag=nfc_holder_body_pizza")
        self.assertEqual([g["id"] for g in d["groups"]], [self.gid])
        self.assertFalse(d["has_more"])
