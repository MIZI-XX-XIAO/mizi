from __future__ import annotations

import logging
import queue
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps


MAX_SOURCE_PIXELS = 220_000_000
MAX_CACHE_BYTES = 256 * 1024 * 1024


@dataclass
class PreviewResult:
    token: int
    path: str
    image: Image.Image | None
    original_size: tuple[int, int] | None
    error: str = ""


class AsyncPreviewLoader:
    """Decode and resize away from Tk's main thread; Tk only consumes results."""

    def __init__(self, max_cache_bytes: int = MAX_CACHE_BYTES):
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="preview")
        self.results: queue.Queue[PreviewResult] = queue.Queue()
        self.cache: OrderedDict[tuple[str, int, int, int], tuple[Image.Image, tuple[int, int], int]] = OrderedDict()
        self.cache_bytes = 0
        self.max_cache_bytes = max_cache_bytes
        self.lock = threading.Lock()

    @staticmethod
    def _bucket(value: int) -> int:
        return max(128, ((max(1, value) + 127) // 128) * 128)

    def request(self, token: int, path: str, size: tuple[int, int], rotation: int = 0) -> None:
        width, height = self._bucket(size[0]), self._bucket(size[1])
        key = (str(Path(path)), width, height, rotation % 360)
        with self.lock:
            cached = self.cache.get(key)
            if cached:
                self.cache.move_to_end(key)
                self.results.put(PreviewResult(token, path, cached[0].copy(), cached[1]))
                return
        self.executor.submit(self._load, token, path, (width, height), rotation % 360, key)

    def _load(self, token: int, path: str, size: tuple[int, int], rotation: int, key: tuple[str, int, int, int]) -> None:
        try:
            with Image.open(path) as opened:
                original_size = opened.size
                if original_size[0] * original_size[1] > MAX_SOURCE_PIXELS:
                    raise ValueError(f"图片像素过大：{original_size[0]} × {original_size[1]}")
                if opened.format == "JPEG":
                    opened.draft("RGB", size)
                image = ImageOps.exif_transpose(opened).convert("RGB")
                if rotation:
                    image = image.rotate(-rotation, expand=True)
                image.thumbnail(size, Image.Resampling.LANCZOS, reducing_gap=3.0)
                image.load()
            memory = image.width * image.height * 3
            if memory <= self.max_cache_bytes:
                with self.lock:
                    self.cache[key] = (image.copy(), original_size, memory)
                    self.cache.move_to_end(key)
                    self.cache_bytes += memory
                    while self.cache and (self.cache_bytes > self.max_cache_bytes or len(self.cache) > 8):
                        _, (_, _, removed) = self.cache.popitem(last=False)
                        self.cache_bytes -= removed
            self.results.put(PreviewResult(token, path, image, original_size))
        except Exception as exc:
            logging.exception("Preview load failed: %s", path)
            self.results.put(PreviewResult(token, path, None, None, str(exc)))

    def poll(self) -> list[PreviewResult]:
        items = []
        while True:
            try:
                items.append(self.results.get_nowait())
            except queue.Empty:
                return items

    def clear(self) -> None:
        with self.lock:
            self.cache.clear()
            self.cache_bytes = 0

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)
