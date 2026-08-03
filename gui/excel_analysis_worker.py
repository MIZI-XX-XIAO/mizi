"""本文件在后台线程执行可取消的Excel质量分析并转换为Qt信号。"""

from PySide6.QtCore import QObject, Signal, Slot

from src.analysis_service import CancellationToken
from src.excel_analysis import (
    ExcelAnalysisCallbacks,
    ExcelAnalysisRequest,
    ExcelAnalysisResult,
    analyze_excel_quality,
)


class ExcelAnalysisWorker(QObject):
    progress_changed = Signal(object)
    completed = Signal(object)
    cancelled = Signal(object)
    failed = Signal(str)

    def __init__(self, request: ExcelAnalysisRequest) -> None:
        super().__init__()
        self.request = request
        self.token = CancellationToken()

    @Slot()
    def run(self) -> None:
        try:
            result = analyze_excel_quality(
                self.request,
                ExcelAnalysisCallbacks(on_progress=self.progress_changed.emit),
                self.token,
            )
            if result.status == "cancelled":
                self.cancelled.emit(result)
            else:
                self.completed.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))

    @Slot()
    def cancel(self) -> None:
        self.token.cancel()
