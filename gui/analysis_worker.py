"""本文件在QThread中调用可取消分析服务，并把进度、告警和完成状态转换为Qt信号。"""

from PySide6.QtCore import QObject, Signal, Slot

from src.analysis_service import (
    AnalysisCallbacks,
    AnalysisRequest,
    AnalysisResult,
    CancellationToken,
    ProgressEvent,
    run_analysis_task,
)


class AnalysisWorker(QObject):
    stage_changed = Signal(str)
    progress_changed = Signal(object)
    alert_created = Signal(object)
    completed = Signal(object)
    cancelled = Signal(object)
    failed = Signal(str)

    def __init__(self, request: AnalysisRequest) -> None:
        super().__init__()
        self.request = request
        self.token = CancellationToken()

    @Slot()
    def run(self) -> None:
        callbacks = AnalysisCallbacks(
            on_stage=self.stage_changed.emit,
            on_progress=self.progress_changed.emit,
            on_alert=self.alert_created.emit,
        )
        try:
            result = run_analysis_task(self.request, callbacks, self.token)
            if result.status == "cancelled":
                self.cancelled.emit(result)
            else:
                self.completed.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))

    @Slot()
    def cancel(self) -> None:
        self.token.cancel()
