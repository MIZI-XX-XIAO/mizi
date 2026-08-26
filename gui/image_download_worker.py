"""本文件在Qt后台线程中执行图片网站下载任务。"""

from __future__ import annotations

from pathlib import Path
from threading import Event

from PySide6.QtCore import QObject, Signal, Slot

from src.image_download import (
    ImageDownloadOrchestrator, ImageDownloadRequest, ImageDownloadResult, ProductIdSummary,
)
from src.image_site_automation import EdgeImageSiteBackend


class ImageDownloadWorker(QObject):
    progress_changed = Signal(int, str)
    log_message = Signal(str)
    completed = Signal(object)
    failed = Signal(str)
    cancelled = Signal()
    finished = Signal()

    def __init__(
        self,
        project_root: Path,
        request: ImageDownloadRequest,
        product_summary: ProductIdSummary,
    ) -> None:
        super().__init__()
        self.project_root = project_root
        self.request = request
        self.product_summary = product_summary
        self.stop_event = Event()

    @Slot()
    def run(self) -> None:
        try:
            backend = EdgeImageSiteBackend(
                self.project_root,
                self.request.username,
                self.request.password,
                self.log_message.emit,
            )
            result: ImageDownloadResult = ImageDownloadOrchestrator(
                self.request,
                backend,
                self.stop_event,
                self.log_message.emit,
                self.progress_changed.emit,
            ).run(self.product_summary)
            self.completed.emit(result)
        except InterruptedError:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()

    def cancel(self) -> None:
        self.stop_event.set()

