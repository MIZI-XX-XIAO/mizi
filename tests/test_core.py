import csv
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from image_reviewer.core import (
    Category, GroupConflictError, GroupMoveError, ReviewEngine, SessionState,
    StateStore, parse_product_view, scan_images, scan_product_groups,
)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "中文 来源"
        self.source.mkdir()
        self.target = self.root / "OK"
        self.store = StateStore(self.root / "state.json")

    def tearDown(self):
        self.temp.cleanup()

    def image(self, name: str, folder: Path | None = None, content: bytes = b"image") -> Path:
        path = (folder or self.source) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def engine(self, view: str = "A") -> ReviewEngine:
        return ReviewEngine.create(self.source, [Category("OK", str(self.target))], view, self.store)

    def test_scan_formats_natural_order_and_no_recursion(self):
        for name in ("10A.JPG", "2A.png", "1A.webp", "ignore.txt"):
            self.image(name)
        self.image("0A.jpg", self.source / "child")
        self.assertEqual([p.name for p in scan_images(self.source)], ["1A.webp", "2A.png", "10A.JPG"])

    def test_parse_and_group_all_views(self):
        for name in ("产品2A.jpg", "产品2B.png", "产品2V.tif", "产品10A.jpg", "无视角.jpg"):
            self.image(name)
        result = scan_product_groups(self.source, "A")
        self.assertEqual([group.product_id for group in result.groups], ["产品2", "产品10"])
        self.assertEqual(set(result.groups[0].view_map), {"A", "B", "V"})
        self.assertEqual(len(result.ignored), 1)
        self.assertEqual(parse_product_view(Path("abcx.jpg")), ("abc", "X"))

    def test_selected_view_filters_products(self):
        self.image("p1A.jpg"); self.image("p1B.jpg"); self.image("p2B.jpg")
        result = scan_product_groups(self.source, "A")
        self.assertEqual([group.product_id for group in result.groups], ["p1"])
        self.assertEqual(result.missing_review_count, 1)

    def test_group_move_log_and_undo(self):
        originals = [self.image(f"货号001{view}.jpg") for view in "ABV"]
        engine = self.engine()
        record = engine.classify(0)
        self.assertEqual(len(record.files), 3)
        self.assertTrue(all(not path.exists() for path in originals))
        self.assertTrue(all((self.target / path.name).exists() for path in originals))
        log = self.target / "分类移动记录.csv"
        with log.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 3)
        self.assertEqual({row["产品编号"] for row in rows}, {"货号001"})
        self.assertEqual({row["原始来源文件夹"] for row in rows}, {str(self.source.resolve())})
        engine.undo()
        self.assertTrue(all(path.exists() for path in originals))
        with log.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([row["动作类型"] for row in rows[-3:]], ["撤销"] * 3)

    def test_any_collision_causes_zero_moves(self):
        originals = [self.image(f"p1{view}.jpg") for view in "AB"]
        self.target.mkdir(); self.image("p1B.jpg", self.target, b"existing")
        engine = self.engine()
        with self.assertRaises(GroupConflictError):
            engine.classify(0)
        self.assertTrue(all(path.exists() for path in originals))
        self.assertEqual((self.target / "p1B.jpg").read_bytes(), b"existing")

    def test_failure_rolls_back_prior_files(self):
        originals = [self.image(f"p1{view}.jpg") for view in "AB"]
        engine = self.engine()
        real_move = shutil.move
        calls = {"count": 0}
        def fail_second(source, target):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("simulated")
            return real_move(source, target)
        with patch("image_reviewer.core.shutil.move", side_effect=fail_second):
            with self.assertRaises(GroupMoveError):
                engine.classify(0)
        self.assertTrue(all(path.exists() for path in originals))
        self.assertEqual(engine.state.failure_count, 1)

    def test_state_round_trip_and_legacy_migration(self):
        self.image("p1A.jpg"); self.image("p1B.jpg")
        engine = self.engine()
        restored = ReviewEngine(self.store.load(), self.store)
        self.assertEqual(restored.current.product_id, "p1")
        legacy = {
            "version": 1, "source": str(self.source), "categories": [{"name": "OK", "destination": str(self.target)}],
            "pending": [str(self.source / "p1A.jpg")], "deferred": [], "history": [], "total": 1, "current_index": 0,
        }
        migrated = SessionState.from_dict(json.loads(json.dumps(legacy)))
        self.assertEqual(migrated.review_view, "")
        self.assertEqual(migrated.pending[0].members, [str(self.source / "p1A.jpg")])

    def test_category_log_appends_different_sources(self):
        source2 = self.root / "另一来源"; source2.mkdir()
        self.image("x1A.jpg")
        self.engine().classify(0)
        self.image("x2A.jpg", source2)
        ReviewEngine.create(source2, [Category("OK", str(self.target))], "A", StateStore(self.root / "state2.json")).classify(0)
        with (self.target / "分类移动记录.csv").open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual({row["原始来源文件夹"] for row in rows}, {str(self.source.resolve()), str(source2.resolve())})

    def test_deferred_group_cycles_back(self):
        self.image("p1A.jpg")
        engine = self.engine()
        engine.defer_current()
        self.assertIsNone(engine.current)
        self.assertTrue(engine.restore_deferred())
        self.assertEqual(engine.current.product_id, "p1")


if __name__ == "__main__":
    unittest.main()
