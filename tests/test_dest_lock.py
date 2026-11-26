"""The destination lock serialises importers and leaves no lock file behind."""

import shutil
import threading
import time
import unittest

from tulda import projects
from tulda.projects import Watcher, create_project, dest_lock

from tests.test_projects import MW, threemf_bytes, tmpdir, write_3mf


class DestLock(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir()
        self.addCleanup(shutil.rmtree, self.d)
        self.dest = self.d / "projects"
        self.dest.mkdir()

    def lock_path(self):
        return self.dest / projects.LOCK_NAME

    def test_file_exists_only_while_held(self):
        with dest_lock(self.dest):
            self.assertTrue(self.lock_path().exists())
        self.assertFalse(self.lock_path().exists())

    def test_stale_leftover_removed(self):
        self.lock_path().write_bytes(b"")                       # left by an older version / a kill
        with dest_lock(self.dest):
            pass
        self.assertFalse(self.lock_path().exists())

    def test_waiter_on_unlinked_inode_retries(self):
        entered = threading.Event()
        order = []

        def waiter():
            with dest_lock(self.dest):
                order.append("b")
                entered.set()

        with dest_lock(self.dest):
            t = threading.Thread(target=waiter)
            t.start()
            time.sleep(0.2)                                     # b has opened the inode and blocks
            self.assertFalse(entered.is_set())
            order.append("a")
        t.join(10)
        self.assertEqual(order, ["a", "b"])
        self.assertFalse(self.lock_path().exists())

    def test_concurrent_holders_serialised(self):
        inside, overlaps, count = [0], [0], [0]
        guard = threading.Lock()

        def worker():
            for _ in range(25):
                with dest_lock(self.dest):
                    with guard:
                        inside[0] += 1
                        if inside[0] > 1:
                            overlaps[0] += 1
                    time.sleep(0.0005)
                    with guard:
                        inside[0] -= 1
                        count[0] += 1

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual((overlaps[0], count[0]), (0, 200))
        self.assertFalse(self.lock_path().exists())

    def test_concurrent_creates_import_once(self):
        srcs = [write_3mf(self.d / f"copy{i}.3mf") for i in range(6)]   # same bytes, same sha256
        results = []
        barrier = threading.Barrier(len(srcs))

        def run(src):
            barrier.wait()
            results.append(create_project(src, self.dest)[0])

        threads = [threading.Thread(target=run, args=(s,)) for s in srcs]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(sorted(results), ["created"] + ["exists"] * 5)
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()), ["Example-Box-v2-final"])

    def test_no_lock_left_after_new_create_and_watch(self):
        projects.new_project("Box", self.dest)
        self.assertFalse(self.lock_path().exists())
        create_project(write_3mf(self.d / "a.3mf", {**MW, "Title": "Other"}), self.dest)
        self.assertFalse(self.lock_path().exists())
        inbox = self.d / "Downloads"
        inbox.mkdir()
        (inbox / "w.3mf").write_bytes(threemf_bytes({**MW, "Title": "Watched"}))
        now = time.time() + 100
        w = Watcher(inbox, self.dest, settle=5, log=lambda *_: None, clock=lambda: now)
        w.poll()
        self.assertEqual(w.poll()["created"], 1)
        self.assertFalse(self.lock_path().exists())
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()), ["Box", "Other", "Watched"])


if __name__ == "__main__":
    unittest.main()
