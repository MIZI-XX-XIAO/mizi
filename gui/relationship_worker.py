"""本文件在后台线程执行耗时的工艺非线性关联分析。"""

from __future__ import annotations

from threading import Event

import pandas as pd
from PySide6.QtCore import QObject, Signal, Slot

from src.process_relationships import analyze_process_relationships


class RelationshipWorker(QObject):
    completed = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int, str)
    cancelled = Signal()
    finished = Signal()

    def __init__(self, jobs: list[tuple], parameters: pd.DataFrame,
                 tolerance_seconds: int, selected_parameters: tuple[str, ...]) -> None:
        super().__init__()
        self.jobs = jobs
        self.parameters = parameters
        self.tolerance_seconds = tolerance_seconds
        self.selected_parameters = selected_parameters
        self._cancelled = Event()

    @Slot()
    def run(self) -> None:
        results = []
        try:
            for index, (scope, target, source, code, products, defects) in enumerate(self.jobs, 1):
                if self._cancelled.is_set():
                    self.cancelled.emit()
                    return
                self.progress.emit(index - 1, len(self.jobs), target)
                result = analyze_process_relationships(
                    products, defects, self.parameters, self.tolerance_seconds,
                    selected_parameters=self.selected_parameters,
                )
                results.append((scope, target, source, code, result))
            self.completed.emit(results)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()

    @Slot()
    def cancel(self) -> None:
        self._cancelled.set()
