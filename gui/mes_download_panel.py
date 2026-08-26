"""本文件提供与主工作台一致的MES工作簿下载对话框。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QDateTime, QPoint, QSettings, QThread, Qt, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDateTimeEdit, QDialog, QFileDialog, QFrame, QGraphicsDropShadowEffect,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QProgressBar, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from src.mes_download import MesDownloadRequest
from .mes_download_worker import MesDownloadWorker, MesQuestion


class _DialogHeader(QFrame):
    close_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("mesDialogHeader")
        self._drag_offset: QPoint | None = None
        badge = QLabel("MES")
        badge.setObjectName("mesDialogBadge")
        title = QLabel("从 MES 获取工作簿")
        title.setObjectName("mesDialogTitle")
        subtitle = QLabel("按生产时间范围下载并整理 OIS 质量数据")
        subtitle.setObjectName("mesDialogSubtitle")
        title_stack = QVBoxLayout()
        title_stack.setContentsMargins(0, 0, 0, 0)
        title_stack.setSpacing(2)
        title_stack.addWidget(title)
        title_stack.addWidget(subtitle)
        close_button = QPushButton("×")
        close_button.setObjectName("dialogCloseButton")
        close_button.setToolTip("关闭")
        close_button.clicked.connect(self.close_requested)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(22, 16, 14, 16)
        layout.setSpacing(14)
        layout.addWidget(badge)
        layout.addLayout(title_stack, 1)
        layout.addWidget(close_button, 0, Qt.AlignTop)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.window().frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_offset = None
        super().mouseReleaseEvent(event)


class MesDownloadDialog(QDialog):
    workbook_ready = Signal(str)

    def __init__(self, project_root: Path, default_output: Path, parent=None) -> None:
        super().__init__(parent)
        self.project_root = project_root
        self.settings = QSettings()
        self.thread: QThread | None = None
        self.worker: MesDownloadWorker | None = None
        self.setObjectName("mesDownloadDialog")
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumSize(760, 620)
        self.resize(820, 700)

        surface = QFrame()
        surface.setObjectName("mesDialogSurface")
        shadow = QGraphicsDropShadowEffect(surface)
        shadow.setBlurRadius(30)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(0, 0, 0, 150))
        surface.setGraphicsEffect(shadow)
        surface_layout = QVBoxLayout(surface)
        surface_layout.setContentsMargins(0, 0, 0, 18)
        surface_layout.setSpacing(0)
        header = _DialogHeader()
        header.close_requested.connect(self._cancel_or_close)
        surface_layout.addWidget(header)

        content = QWidget()
        content.setObjectName("mesDialogContent")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(22, 18, 22, 0)
        content_layout.setSpacing(12)

        now = datetime.now().replace(second=0, microsecond=0)
        start = now.replace(hour=0, minute=0)
        self.begin_edit = QDateTimeEdit(QDateTime(start))
        self.end_edit = QDateTimeEdit(QDateTime(start + timedelta(days=1)))
        for editor in (self.begin_edit, self.end_edit):
            editor.setDisplayFormat("yyyy-MM-dd  HH:mm:ss")
            editor.setCalendarPopup(True)
        self.model_edit = QLineEdit()
        self.model_edit.setPlaceholderText("可选；10位型号，留空查询全部")
        range_group = QGroupBox("查询范围")
        range_grid = QGridLayout(range_group)
        range_grid.setContentsMargins(16, 20, 16, 14)
        range_grid.setHorizontalSpacing(14)
        range_grid.setVerticalSpacing(8)
        range_grid.addWidget(QLabel("开始时间"), 0, 0)
        range_grid.addWidget(QLabel("结束时间"), 0, 1)
        range_grid.addWidget(self.begin_edit, 1, 0)
        range_grid.addWidget(self.end_edit, 1, 1)
        range_grid.addWidget(QLabel("产品型号"), 2, 0, 1, 2)
        range_grid.addWidget(self.model_edit, 3, 0, 1, 2)

        self.domain_edit = QLineEdit(str(self.settings.value("mes/domain", "APAC")))
        self.username_edit = QLineEdit(str(self.settings.value("mes/username", "")))
        self.username_edit.setPlaceholderText("可选；也可在Firefox中手动登录")
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.password_edit.setPlaceholderText("仅本次使用，不会保存")
        saved_output = str(self.settings.value("mes/output_root", str(default_output)))
        self.output_edit = QLineEdit(saved_output)
        self.browse_button = QPushButton("选择目录")
        self.browse_button.clicked.connect(self._choose_output)
        output_row = QHBoxLayout()
        output_row.setContentsMargins(0, 0, 0, 0)
        output_row.setSpacing(8)
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(self.browse_button)
        source_group = QGroupBox("登录与文件保存")
        source_grid = QGridLayout(source_group)
        source_grid.setContentsMargins(16, 20, 16, 14)
        source_grid.setHorizontalSpacing(14)
        source_grid.setVerticalSpacing(8)
        source_grid.addWidget(QLabel("公司账号"), 0, 0)
        source_grid.addWidget(QLabel("密码"), 0, 1)
        source_grid.addWidget(self.username_edit, 1, 0)
        source_grid.addWidget(self.password_edit, 1, 1)
        source_grid.addWidget(QLabel("登录域"), 2, 0)
        source_grid.addWidget(QLabel("保存目录"), 2, 1)
        source_grid.addWidget(self.domain_edit, 3, 0)
        source_grid.addLayout(output_row, 3, 1)

        hint = QLabel("连接公司网络后将启动 Firefox。自动登录不可用时，可直接在浏览器中完成登录。")
        hint.setObjectName("mesPrivacyHint")
        hint.setWordWrap(True)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.setVisible(False)
        self.notice.setTextInteractionFlags(Qt.TextSelectableByMouse)

        run_card = QFrame()
        run_card.setObjectName("mesRunCard")
        run_layout = QVBoxLayout(run_card)
        run_layout.setContentsMargins(16, 12, 16, 12)
        run_layout.setSpacing(8)
        status_row = QHBoxLayout()
        caption = QLabel("执行状态")
        caption.setObjectName("mesStatusCaption")
        self.status = QLabel("准备就绪")
        self.status.setObjectName("mesStatusText")
        status_row.addWidget(caption)
        status_row.addStretch()
        status_row.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        run_layout.addLayout(status_row)
        run_layout.addWidget(self.progress)

        self.log_toggle = QPushButton("查看运行日志  ▾")
        self.log_toggle.setObjectName("mesLogToggle")
        self.log_toggle.setCheckable(True)
        self.log_toggle.toggled.connect(self._toggle_log)
        self.log = QTextEdit()
        self.log.setObjectName("mesDownloadLog")
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(110)
        self.log.setVisible(False)
        self.start_button = QPushButton("开始下载")
        self.start_button.setObjectName("primaryButton")
        self.start_button.clicked.connect(self._start)
        self.cancel_button = QPushButton("返回")
        self.cancel_button.clicked.connect(self._cancel_or_close)
        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addWidget(self.log_toggle)
        buttons.addStretch()
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.start_button)

        content_layout.addWidget(range_group)
        content_layout.addWidget(source_group)
        content_layout.addWidget(hint)
        content_layout.addWidget(self.notice)
        content_layout.addWidget(run_card)
        content_layout.addWidget(self.log)
        content_layout.addStretch()
        content_layout.addLayout(buttons)
        surface_layout.addWidget(content, 1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.addWidget(surface)
        self._input_widgets = (
            self.begin_edit, self.end_edit, self.model_edit, self.domain_edit,
            self.username_edit, self.password_edit, self.output_edit, self.browse_button,
        )

    def _toggle_log(self, visible: bool) -> None:
        self.log.setVisible(visible)
        self.log_toggle.setText("收起运行日志  ▴" if visible else "查看运行日志  ▾")

    def _show_notice(self, message: str, kind: str) -> None:
        self.notice.setObjectName(f"{kind}Banner")
        self.notice.setText(message)
        self.notice.setVisible(True)
        self.notice.style().unpolish(self.notice)
        self.notice.style().polish(self.notice)

    def _choose_output(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择MES数据保存目录", self.output_edit.text())
        if selected:
            self.output_edit.setText(selected)

    def _request(self) -> MesDownloadRequest:
        return MesDownloadRequest(
            self.begin_edit.dateTime().toPython(), self.end_edit.dateTime().toPython(),
            Path(self.output_edit.text().strip()), self.model_edit.text().strip(),
            self.username_edit.text().strip(), self.password_edit.text(),
            self.domain_edit.text().strip() or "APAC",
        )

    def _start(self) -> None:
        try:
            if not self.output_edit.text().strip():
                raise ValueError("请选择MES数据保存目录")
            request = self._request()
            request.validate()
        except Exception as exc:
            self._show_notice(f"请检查输入：{exc}", "error")
            return
        self.notice.setVisible(False)
        self.settings.setValue("mes/domain", request.domain)
        self.settings.setValue("mes/username", request.username)
        self.settings.setValue("mes/output_root", str(request.output_root))
        self.password_edit.clear()
        for widget in self._input_widgets:
            widget.setEnabled(False)
        self.start_button.setEnabled(False)
        self.cancel_button.setText("安全取消")
        self.log.clear()
        self.progress.setValue(0)
        self.status.setText("正在准备下载")
        self.thread = QThread(self)
        self.worker = MesDownloadWorker(self.project_root, request)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress_changed.connect(self._on_progress)
        self.worker.log_message.connect(self.log.append)
        self.worker.question_requested.connect(self._answer_question)
        self.worker.completed.connect(self._on_completed)
        self.worker.failed.connect(self._on_failed)
        self.worker.cancelled.connect(self._on_cancelled)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self._on_finished)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()

    @Slot(int, str)
    def _on_progress(self, value: int, message: str) -> None:
        self.progress.setValue(value)
        self.status.setText(message)

    @Slot(object)
    def _answer_question(self, prompt: MesQuestion) -> None:
        buttons = QMessageBox.Yes | QMessageBox.No
        default = QMessageBox.Yes if prompt.default else QMessageBox.No
        prompt.result = QMessageBox.question(
            self, prompt.title, prompt.message, buttons, default,
        ) == QMessageBox.Yes
        prompt.answered.set()

    @Slot(str)
    def _on_completed(self, path: str) -> None:
        self.status.setText("下载完成")
        self.progress.setValue(100)
        self._show_notice(f"✓ 工作簿已生成并自动填入新建任务\n{path}", "success")
        self.workbook_ready.emit(path)

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        self.status.setText("下载失败")
        self._show_notice(f"下载没有完成：{message}", "error")
        self.log_toggle.setChecked(True)

    @Slot()
    def _on_cancelled(self) -> None:
        self.status.setText("下载已取消")
        self._show_notice("下载已安全取消；临时内容不会作为正式工作簿使用。", "warning")

    @Slot()
    def _on_finished(self) -> None:
        self.thread = None
        self.worker = None
        for widget in self._input_widgets:
            widget.setEnabled(True)
        self.start_button.setEnabled(True)
        self.start_button.setText("再次下载")
        self.cancel_button.setEnabled(True)
        self.cancel_button.setText("完成并返回" if self.progress.value() == 100 else "返回")

    def _cancel_or_close(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            self.cancel_button.setEnabled(False)
            self.status.setText("正在安全取消…")
        else:
            self.accept()

    def closeEvent(self, event) -> None:
        if self.worker is not None:
            self._show_notice("下载仍在运行。请先点击“安全取消”，等待当前浏览器操作结束。", "warning")
            event.ignore()
            return
        super().closeEvent(event)
