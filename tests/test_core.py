import csv
import tempfile
import unittest
from pathlib import Path

from image_reviewer.core import Category, ReviewEngine, StateStore, scan_images, unique_destination


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "中文 源目录"
        self.source.mkdir()
        self.target = self.root / "目标"
        self.store = StateStore(self.root / "state.json")

    def tearDown(self):
        self.temp.cleanup()

    def image(self, name: str) -> Path:
        path = self.source / name
        path.write_bytes(b"test")
        return path

    def test_scan_formats_natural_order_and_no_recursion(self):
        for name in ("10.JPG", "2.png", "1.webp", "ignore.txt"):
            self.image(name)
        child = self.source / "child"; child.mkdir(); (child / "0.jpg").write_bytes(b"x")
        self.assertEqual([p.name for p in scan_images(self.source)], ["1.webp", "2.png", "10.JPG"])

    def test_move_and_undo(self):
        original = self.image("样品 01.jpg")
        engine = ReviewEngine.create(self.source, [Category("OK", str(self.target))], self.store)
        record = engine.classify(0)
        self.assertFalse(original.exists())
        self.assertTrue(Path(record.destination).exists())
        self.assertEqual(engine.completed_count, 1)
        engine.undo()
        self.assertTrue(original.exists())
        self.assertEqual(engine.completed_count, 0)

    def test_collision_never_overwrites_and_can_rename(self):
        original = self.image("same.jpg")
        self.target.mkdir(); existing = self.target / "same.jpg"; existing.write_bytes(b"existing")
        engine = ReviewEngine.create(self.source, [Category("NG", str(self.target))], self.store)
        with self.assertRaises(FileExistsError): engine.classify(0)
        self.assertEqual(existing.read_bytes(), b"existing")
        record = engine.classify(0, "rename")
        self.assertEqual(record.final_name, "same_1.jpg")
        self.assertTrue((self.target / "same_1.jpg").exists())
        self.assertFalse(original.exists())

    def test_collision_skip_keeps_file_and_advances(self):
        first = self.image("a.jpg"); second = self.image("b.jpg")
        self.target.mkdir(); (self.target / "a.jpg").write_bytes(b"existing")
        engine = ReviewEngine.create(self.source, [Category("NG", str(self.target))], self.store)
        self.assertIsNone(engine.classify(0, "skip"))
        self.assertTrue(first.exists())
        self.assertEqual(engine.current, second.resolve())
        self.assertIn(str(first.resolve()), engine.state.deferred)

    def test_defer_cycles_back(self):
        self.image("a.jpg")
        engine = ReviewEngine.create(self.source, [Category("OK", str(self.target))], self.store)
        engine.defer_current()
        self.assertIsNone(engine.current)
        self.assertTrue(engine.restore_deferred())
        self.assertEqual(engine.current.name, "a.jpg")

    def test_state_round_trip_and_reconcile_missing(self):
        original = self.image("a.jpg")
        ReviewEngine.create(self.source, [Category("OK", str(self.target))], self.store)
        original.unlink()
        restored = ReviewEngine(self.store.load(), self.store)
        self.assertIsNone(restored.current)

    def test_audit_csv_has_bom_and_rows(self):
        self.image("a.jpg")
        engine = ReviewEngine.create(self.source, [Category("OK", str(self.target))], self.store)
        engine.classify(0); engine.undo()
        with engine.audit.path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0][0], "操作时间")
        self.assertEqual([rows[1][5], rows[2][5]], ["移动", "撤销"])

    def test_unique_destination(self):
        self.target.mkdir(); (self.target / "a.jpg").write_bytes(b"x"); (self.target / "a_1.jpg").write_bytes(b"x")
        self.assertEqual(unique_destination(self.target / "a.jpg").name, "a_2.jpg")


if __name__ == "__main__":
    unittest.main()
