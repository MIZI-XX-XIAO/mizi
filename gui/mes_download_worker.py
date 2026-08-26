"""本文件在Qt后台线程中执行MES下载，并将交互请求安全转发给主线程。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import Event

from PySide6.QtCore import QObject, Signal, Slot

from src.mes_download import MesDownloadRequest, download_mes_workbook


@dataclass
class MesQuestion:
    title: str
    message: str
    default: bool
    answered: Event = field(default_factory=Event)
    result: bool = False


class MesDownloadWorker(QObject):
    progress_changed = Signal(int, str)
    log_message = Signal(str)
    question_requested = Signal(object)
    completed = Signal(str)
    failed = Signal(str)
    cancelled = Signal()
    finished = Signal()

    def __init__(self, project_root: Path, request: MesDownloadRequest) -> None:
        super().__init__()
        self.project_root = project_root
        self.request = request
        self.stop_event = Event()

    def _question(self, title: str, message: str, default: bool) -> bool:
        prompt = MesQuestion(title, message, default, result=default)
        self.question_requested.emit(prompt)
        while not prompt.answered.wait(0.2):
            if self.stop_event.is_set():
                return False
        return prompt.result

    @Slot()
    def run(self) -> None:
        try:
            path = download_mes_workbook(
                self.project_root, self.request, self.stop_event,
                self.log_message.emit, self.progress_changed.emit, self._question,
            )
            self.completed.emit(str(path))
        except InterruptedError:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()

    def cancel(self) -> None:
        self.stop_event.set()

