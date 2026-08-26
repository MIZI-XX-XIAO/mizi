"""本文件提供按时间范围下载MES工作簿的PySide6对话框。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QDateTime, QSettings, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QDateTimeEdit, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QProgressBar, QPushButton, QTextEdit, QVBoxLayout,
)

from src.mes_download import MesDownloadRequest
from .mes_download_worker import MesDownloadWorker, MesQuestion


class MesDownloadDialog(QDialog):
    workbook_ready = Signal(str)

    def __init__(self, project_root: Path, default_output: Path, parent=None) -> None:
        super().__init__(parent)
        self.project_root = project_root
        self.settings = QSettings()
        self.thread: QThread | None = None
        self.worker: MesDownloadWorker | None = None
        self.setWindowTitle("从MES下载Excel工作簿")
        self.setMinimumWidth(680)

        now = datetime.now().replace(second=0, microsecond=0)
        start = now.replace(hour=0, minute=0)
        self.begin_edit = QDateTimeEdit(QDateTime(start))
        self.end_edit = QDateTimeEdit(QDateTime(start + timedelta(days=1)))
        for editor in (self.begin_edit, self.end_edit):
            editor.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
            editor.setCalendarPopup(True)
        self.model_edit = QLineEdit()
        self.model_edit.setPlaceholderText("可选，通常为10位；留空查询全部型号")
        self.domain_edit = QLineEdit(str(self.settings.value("mes/domain", "APAC")))
        self.username_edit = QLineEdit(str(self.settings.value("mes/username", "")))
        self.username_edit.setPlaceholderText("可选；留空时可在Firefox中手动登录")
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.password_edit.setPlaceholderText("仅本次使用，不会保存")
        saved_output = str(self.settings.value("mes/output_root", str(default_output)))
        self.output_edit = QLineEdit(saved_output)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._choose_output)
        output_row = QHBoxLayout()
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(browse)

        form = QFormLayout()
        form.addRow("开始时间", self.begin_edit)
        form.addRow("结束时间", self.end_edit)
        form.addRow("型号", self.model_edit)
        form.addRow("域", self.domain_edit)
        form.addRow("公司账号", self.username_edit)
        form.addRow("密码", self.password_edit)
        form.addRow("保存目录", output_row)
        hint = QLabel("将启动Firefox访问公司OIS Portal；如自动登录不可用，请在浏览器中完成登录。")
        hint.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.status = QLabel("等待开始")
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(180)
        self.start_button = QPushButton("开始下载")
        self.start_button.setObjectName("primaryButton")
        self.start_button.clicked.connect(self._start)
        self.cancel_button = QPushButton("关闭")
        self.cancel_button.clicked.connect(self._cancel_or_close)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.start_button)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addWidget(self.progress)
        layout.addWidget(self.status)
        layout.addWidget(self.log, 1)
        layout.addLayout(buttons)

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
            QMessageBox.warning(self, "输入有误", str(exc))
            return
        self.settings.setValue("mes/domain", request.domain)
        self.settings.setValue("mes/username", request.username)
        self.settings.setValue("mes/output_root", str(request.output_root))
        self.password_edit.clear()
        self.start_button.setEnabled(False)
        self.cancel_button.setText("安全取消")
        self.log.clear()
        self.thread = QThread(self)
        self.worker = MesDownloadWorker(self.project_root, request)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress_changed.connect(self._on_progress)
        self.worker.log_message.connect(self.log.append)
        self.worker.question_requested.connect(self._answer_question)
        self.worker.completed.connect(self._on_completed)
        self.worker.failed.connect(self._on_failed)
        self.worker.cancelled.connect(lambda: self.status.setText("下载已取消"))
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
        prompt.result = QMessageBox.question(self, prompt.title, prompt.message, buttons, default) == QMessageBox.Yes
        prompt.answered.set()

    @Slot(str)
    def _on_completed(self, path: str) -> None:
        self.status.setText("下载完成，已回填Excel工作簿")
        self.workbook_ready.emit(path)
        QMessageBox.information(self, "MES下载完成", f"工作簿已生成并填入新建任务：\n{path}")

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        self.status.setText("下载失败")
        QMessageBox.critical(self, "MES下载失败", message)

    @Slot()
    def _on_finished(self) -> None:
        self.thread = None
        self.worker = None
        self.start_button.setEnabled(True)
        self.cancel_button.setEnabled(True)
        self.cancel_button.setText("关闭")

    def _cancel_or_close(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            self.cancel_button.setEnabled(False)
            self.status.setText("正在安全取消…")
        else:
            self.close()

    def closeEvent(self, event) -> None:
        if self.worker is not None:
            QMessageBox.information(self, "MES下载运行中", "请先点击“安全取消”，等待浏览器任务结束。")
            event.ignore()
            return
        super().closeEvent(event)
