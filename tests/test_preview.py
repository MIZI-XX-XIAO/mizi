import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image

from image_reviewer.preview import AsyncPreviewLoader


class PreviewTests(unittest.TestCase):
    def test_background_preview_is_bounded_and_cached(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "largeA.jpg"
            Image.new("RGB", (4000, 3000), "navy").save(path, quality=85)
            loader = AsyncPreviewLoader(max_cache_bytes=16 * 1024 * 1024)
            try:
                loader.request(1, str(path), (800, 600))
                result = None
                deadline = time.time() + 8
                while time.time() < deadline and result is None:
                    items = loader.poll()
                    result = items[-1] if items else None
                    time.sleep(0.02)
                self.assertIsNotNone(result)
                self.assertFalse(result.error)
                self.assertLessEqual(result.image.width, 896)
                self.assertLessEqual(result.image.height, 640)
                loader.request(2, str(path), (800, 600))
                cached = loader.poll()[0]
                self.assertEqual(cached.token, 2)
            finally:
                loader.close()


if __name__ == "__main__":
    unittest.main()
