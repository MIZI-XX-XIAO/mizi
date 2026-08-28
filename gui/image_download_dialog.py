"""本文件提供公司图片网站自动下载的深色任务窗口。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, QThread, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QDialog, QFileDialog, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QProgressBar, QPushButton, QRadioButton,
    QScrollArea, QSpinBox, QTextEdit, QVBoxLayout, QWidget,
)

from src.image_download import (
    ALL_IMAGE_CODES, AOI_DOWNLOAD_GROUPS, CODE_SCOPE, PRIMARY_IMAGE_CODES, ImageDownloadRequest,
    ImageDownloadResult, ProductIdSummary, default_image_download_dir,
    extract_product_ids,
)
from .image_download_worker import ImageDownloadWorker


class _ImageDialogHeader(QFrame):
    close_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("imageDialogHeader")
        self._drag_offset: QPoint | None = None
        badge = QLabel("IMG")
        badge.setObjectName("imageDialogBadge")
        title = QLabel("从公司网站下载检测图片")
        title.setObjectName("imageDialogTitle")
        subtitle = QLabel("读取 MES 产品号，自动分批下载并隔离缺图或坏图")
        subtitle.setObjectName("imageDialogSubtitle")
        text = QVBoxLayout(); text.setContentsMargins(0, 0, 0, 0); text.setSpacing(2)
        text.addWidget(title); text.addWidget(subtitle)
        close = QPushButton("×"); close.setObjectName("dialogCloseButton")
        close.clicked.connect(self.close_requested)
        layout = QHBoxLayout(self); layout.setContentsMargins(22, 15, 14, 15)
        layout.addWidget(badge); layout.addSpacing(4); layout.addLayout(text, 1)
        layout.addWidget(close, 0, Qt.AlignTop)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.window().frameGeometry().topLeft()
            event.accept(); return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept(); return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_offset = None
        super().mouseReleaseEvent(event)


class ImageDownloadDialog(QDialog):
    result_ready = Signal(object)

    def __init__(
        self,
        project_root: Path,
        workbook_path: Path | None,
        default_output: Path,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.project_root = project_root
        self.product_summary: ProductIdSummary | None = None
        self.retry_manifest: Path | None = None
        self.thread: QThread | None = None
        self.worker: ImageDownloadWorker | None = None
        self.setObjectName("imageDownloadDialog")
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumSize(900, 690)
        self.resize(960, 790)

        surface = QFrame(); surface.setObjectName("imageDialogSurface")
        surface_layout = QVBoxLayout(surface); surface_layout.setContentsMargins(0, 0, 0, 18)
        surface_layout.setSpacing(0)
        header = _ImageDialogHeader(); header.close_requested.connect(self._cancel_or_close)
        surface_layout.addWidget(header)
        content = QWidget(); content.setObjectName("imageDialogContent")
        body = QVBoxLayout(content); body.setContentsMargins(22, 16, 22, 0); body.setSpacing(12)

        source_group = QGroupBox("MES产品来源与保存位置")
        source_grid = QGridLayout(source_group); source_grid.setContentsMargins(16, 20, 16, 14)
        source_grid.setHorizontalSpacing(10); source_grid.setVerticalSpacing(8)
        self.workbook_edit = QLineEdit(str(workbook_path or ""))
        self.workbook_edit.setPlaceholderText("选择包含Ident No.的MES工作簿")
        workbook_button = QPushButton("选择工作簿"); workbook_button.clicked.connect(self._choose_workbook)
        inspect_button = QPushButton("读取产品号"); inspect_button.clicked.connect(self._inspect_workbook)
        self.output_edit = QLineEdit(str(default_output))
        output_button = QPushButton("选择目录"); output_button.clicked.connect(self._choose_output)
        source_grid.addWidget(QLabel("MES工作簿"), 0, 0)
        source_grid.addWidget(self.workbook_edit, 0, 1)
        source_grid.addWidget(workbook_button, 0, 2)
        source_grid.addWidget(inspect_button, 0, 3)
        source_grid.addWidget(QLabel("保存位置"), 1, 0)
        source_grid.addWidget(self.output_edit, 1, 1, 1, 2)
        source_grid.addWidget(output_button, 1, 3)
        self.product_stats = QLabel("尚未读取工作簿")
        self.product_stats.setObjectName("imageProductStats")
        self.product_stats.setWordWrap(True)
        source_grid.addWidget(self.product_stats, 2, 1, 1, 3)

        option_group = QGroupBox("需要下载的图片代码")
        option_layout = QVBoxLayout(option_group); option_layout.setContentsMargins(16, 20, 16, 12)
        option_layout.setSpacing(8)
        quick = QHBoxLayout()
        primary = QPushButton("检测主图对"); primary.clicked.connect(self._select_primary)
        select_all = QPushButton("全选"); select_all.clicked.connect(lambda: self._set_all_codes(True))
        clear = QPushButton("清空"); clear.clicked.connect(lambda: self._set_all_codes(False))
        quick.addWidget(primary); quick.addWidget(select_all); quick.addWidget(clear); quick.addStretch()
        option_layout.addLayout(quick)
        codes_grid = QGridLayout(); codes_grid.setHorizontalSpacing(18); codes_grid.setVerticalSpacing(5)
        self.code_checks: dict[str, QCheckBox] = {}
        grouped = {
            "5S · D系": ALL_IMAGE_CODES[0:6], "5X · E系": ALL_IMAGE_CODES[6:12],
            "7S · F系": ALL_IMAGE_CODES[12:18], "7X · G系": ALL_IMAGE_CODES[18:24],
        }
        for column, (caption, codes) in enumerate(grouped.items()):
            label = QLabel(caption); label.setObjectName("imageCodeGroupTitle")
            codes_grid.addWidget(label, 0, column)
            for row, code in enumerate(codes, 1):
                checkbox = QCheckBox(code); self.code_checks[code] = checkbox
                codes_grid.addWidget(checkbox, row, column)
        option_layout.addLayout(codes_grid)

        settings_group = QGroupBox("下载设置与临时登录")
        settings_grid = QGridLayout(settings_group); settings_grid.setContentsMargins(16, 20, 16, 14)
        settings_grid.setHorizontalSpacing(12); settings_grid.setVerticalSpacing(8)
        self.origin_radio = QRadioButton("原图 TIF")
        self.resize_radio = QRadioButton("压缩图 TIF")
        self.quality_group = QButtonGroup(self); self.quality_group.addButton(self.origin_radio)
        self.quality_group.addButton(self.resize_radio)
        quality_row = QHBoxLayout(); quality_row.addWidget(self.origin_radio); quality_row.addWidget(self.resize_radio)
        quality_row.addStretch()
        self.skip_rework = QCheckBox("去除7层返工站"); self.skip_rework.setChecked(True)
        self.batch_size = QSpinBox(); self.batch_size.setRange(10, 100); self.batch_size.setValue(80)
        self.batch_size.setSuffix(" 个产品/批")
        self.username_edit = QLineEdit(); self.username_edit.setPlaceholderText("LDAP账号（仅本次使用）")
        self.password_edit = QLineEdit(); self.password_edit.setEchoMode(QLineEdit.Password)
        self.password_edit.setPlaceholderText("LDAP密码（不会保存）")
        settings_grid.addWidget(QLabel("图片质量"), 0, 0); settings_grid.addLayout(quality_row, 0, 1)
        settings_grid.addWidget(self.skip_rework, 0, 2)
        settings_grid.addWidget(QLabel("批次大小"), 0, 3); settings_grid.addWidget(self.batch_size, 0, 4)
        settings_grid.addWidget(QLabel("LDAP账号"), 1, 0); settings_grid.addWidget(self.username_edit, 1, 1, 1, 2)
        settings_grid.addWidget(QLabel("LDAP密码"), 1, 3); settings_grid.addWidget(self.password_edit, 1, 4)

        self.login_hint = QLabel(
            "首次使用需在普通Edge中完成公司登录；认证成功后，软件专用配置会复用登录状态。"
            "Windows Security系统窗口需要手动输入。"
        )
        self.login_hint.setWordWrap(True)
        self.login_hint.setObjectName("imageLoginHint")
        settings_grid.addWidget(self.login_hint, 2, 0, 1, 5)

        self.notice = QLabel(); self.notice.setWordWrap(True); self.notice.setVisible(False)
        self.notice.setTextInteractionFlags(Qt.TextSelectableByMouse)
        run_card = QFrame(); run_card.setObjectName("imageRunCard")
        run_layout = QVBoxLayout(run_card); run_layout.setContentsMargins(15, 11, 15, 11)
        status_row = QHBoxLayout(); status_row.addWidget(QLabel("执行状态")); status_row.addStretch()
        self.status = QLabel("准备就绪"); self.status.setObjectName("imageStatusText")
        status_row.addWidget(self.status)
        self.progress = QProgressBar(); self.progress.setRange(0, 100)
        run_layout.addLayout(status_row); run_layout.addWidget(self.progress)
        self.log = QTextEdit(); self.log.setObjectName("imageDownloadLog"); self.log.setReadOnly(True)
        self.log.setMinimumHeight(120); self.log.setVisible(False)
        self.log_toggle = QPushButton("查看运行日志  ▾"); self.log_toggle.setObjectName("imageLogToggle")
        self.log_toggle.setCheckable(True); self.log_toggle.toggled.connect(self._toggle_log)
        self.retry_button = QPushButton("仅重试异常项…"); self.retry_button.clicked.connect(self._choose_retry_manifest)
        self.cancel_button = QPushButton("返回"); self.cancel_button.clicked.connect(self._cancel_or_close)
        self.start_button = QPushButton("开始下载"); self.start_button.setObjectName("primaryButton")
        self.start_button.clicked.connect(self._start)
        buttons = QHBoxLayout(); buttons.addWidget(self.log_toggle); buttons.addWidget(self.retry_button)
        buttons.addStretch(); buttons.addWidget(self.cancel_button); buttons.addWidget(self.start_button)

        body.addWidget(source_group); body.addWidget(option_group); body.addWidget(settings_group)
        body.addWidget(self.notice); body.addWidget(run_card); body.addWidget(self.log); body.addLayout(buttons)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(content)
        surface_layout.addWidget(scroll, 1)
        outer = QVBoxLayout(self); outer.setContentsMargins(14, 14, 14, 14); outer.addWidget(surface)
        self._input_widgets = (
            self.workbook_edit, workbook_button, inspect_button, self.output_edit, output_button,
            self.origin_radio, self.resize_radio, self.skip_rework, self.batch_size,
            self.username_edit, self.password_edit, self.retry_button, *self.code_checks.values(),
        )
        if workbook_path and workbook_path.is_file():
            self._inspect_workbook()

    def _show_notice(self, message: str, kind: str) -> None:
        self.notice.setObjectName(f"{kind}Banner"); self.notice.setText(message)
        self.notice.setVisible(True); self.notice.style().unpolish(self.notice); self.notice.style().polish(self.notice)

    def _toggle_log(self, visible: bool) -> None:
        self.log.setVisible(visible)
        self.log_toggle.setText("收起运行日志  ▴" if visible else "查看运行日志  ▾")

    def _choose_workbook(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择MES工作簿", self.workbook_edit.text(), "Excel工作簿 (*.xlsx *.xlsm)")
        if path:
            self.workbook_edit.setText(path); self.retry_manifest = None; self._inspect_workbook()

    def _choose_output(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择图片保存目录", self.output_edit.text())
        if path: self.output_edit.setText(path)

    def _inspect_workbook(self) -> None:
        try:
            self.product_summary = extract_product_ids(Path(self.workbook_edit.text().strip()))
            summary = self.product_summary
            group_lines = []
            for group in AOI_DOWNLOAD_GROUPS:
                family = group.family
                group_lines.append(
                    f"{group.scope}/{group.sheet_name}："
                    f"有效 {len(summary.products_by_family.get(family, ()))}，"
                    f"无效 {len(summary.invalid_ids_by_family.get(family, ()))}，"
                    f"重复 {summary.duplicate_counts_by_family.get(family, 0)}；"
                    f"来源 {summary.sources_by_family.get(family, '未找到')}"
                )
            self.product_stats.setText("\n".join(group_lines))
            self.product_stats.setObjectName("successBanner" if summary.valid_ids else "errorBanner")
            self.product_stats.style().unpolish(self.product_stats); self.product_stats.style().polish(self.product_stats)
        except Exception as exc:
            self.product_summary = None; self.product_stats.setText(f"工作簿读取失败：{exc}")
            self.product_stats.setObjectName("errorBanner")
            self.product_stats.style().unpolish(self.product_stats); self.product_stats.style().polish(self.product_stats)

    def _set_all_codes(self, checked: bool) -> None:
        for checkbox in self.code_checks.values(): checkbox.setChecked(checked)

    def _select_primary(self) -> None:
        for code, checkbox in self.code_checks.items(): checkbox.setChecked(code in PRIMARY_IMAGE_CODES)

    def _selected_codes(self) -> tuple[str, ...]:
        return tuple(code for code in ALL_IMAGE_CODES if self.code_checks[code].isChecked())

    def _quality(self) -> str:
        if self.origin_radio.isChecked(): return "origin"
        if self.resize_radio.isChecked(): return "resize"
        return ""

    def _choose_retry_manifest(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择图片下载任务清单", self.output_edit.text(), "任务清单 (image_download_manifest.json);;JSON (*.json)"
        )
        if not path: return
        try:
            import json
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            self.retry_manifest = Path(path)
            self.workbook_edit.setText(str(payload["workbook"]))
            self.output_edit.setText(str(self.retry_manifest.parent.parent))
            self._set_all_codes(False)
            for code in payload.get("image_codes", []):
                if code in self.code_checks: self.code_checks[code].setChecked(True)
            (self.origin_radio if payload.get("quality") == "origin" else self.resize_radio).setChecked(True)
            self.skip_rework.setChecked(bool(payload.get("skip_rework", True)))
            self.batch_size.setValue(int(payload.get("batch_size", 80)))
            self._inspect_workbook()
            self._show_notice("已加载异常任务；开始后只重试清单中的失败产品。", "warning")
        except Exception as exc:
            self._show_notice(f"任务清单读取失败：{exc}", "error")

    def _start(self) -> None:
        if self.product_summary is None:
            self._inspect_workbook()
        try:
            if self.product_summary is None or not self.product_summary.valid_ids:
                raise ValueError("工作簿中没有有效的25位产品号")
            if not self.output_edit.text().strip(): raise ValueError("请选择图片保存位置")
            codes = self._selected_codes()
            quality = self._quality()
            if not self.retry_manifest:
                missing_groups = [
                    group for group in AOI_DOWNLOAD_GROUPS
                    if any(code.startswith(group.family) for code in codes)
                    and not self.product_summary.products_by_family.get(group.family, ())
                ]
                if missing_groups:
                    details = "、".join(
                        f"{group.sheet_name}（{group.station_name}）" for group in missing_groups
                    )
                    raise ValueError(f"所选图片代码缺少对应AOI产品号：{details}")
            output = self.retry_manifest.parent if self.retry_manifest else default_image_download_dir(Path(self.output_edit.text().strip()))
            request = ImageDownloadRequest(
                Path(self.workbook_edit.text().strip()), output, codes, quality,
                self.skip_rework.isChecked(), self.username_edit.text().strip(),
                self.password_edit.text(), self.batch_size.value(), self.retry_manifest,
            )
            request.validate()
        except Exception as exc:
            self._show_notice(f"请检查任务设置：{exc}", "error"); return
        self.notice.setVisible(False); self.password_edit.clear(); self.log.clear(); self.progress.setValue(0)
        for widget in self._input_widgets: widget.setEnabled(False)
        self.start_button.setEnabled(False); self.cancel_button.setText("安全取消")
        self.status.setText("正在启动普通Edge；首次使用请完成公司登录")
        self.thread = QThread(self)
        self.worker = ImageDownloadWorker(self.project_root, request, self.product_summary)
        self.worker.moveToThread(self.thread); self.thread.started.connect(self.worker.run)
        self.worker.progress_changed.connect(self._on_progress); self.worker.log_message.connect(self.log.append)
        self.worker.completed.connect(self._on_completed); self.worker.failed.connect(self._on_failed)
        self.worker.cancelled.connect(self._on_cancelled); self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self._on_finished); self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()

    @Slot(int, str)
    def _on_progress(self, value: int, message: str) -> None:
        self.progress.setValue(value); self.status.setText(message)

    @Slot(object)
    def _on_completed(self, result: ImageDownloadResult) -> None:
        self.progress.setValue(100)
        failed = len([item for item in result.issues if item.status == "failed"])
        if result.status == "complete":
            self._show_notice(f"✓ 图片下载完成，已分类并回填检测目录\n{result.output_dir}", "success")
        else:
            self._show_notice(
                f"图片下载部分完成：正常项 {result.completed_item_count}，异常项 {failed}。"
                f"\n异常报告：{result.summary_path}", "warning",
            )
        self.status.setText("下载完成" if result.status == "complete" else "部分完成")
        self.result_ready.emit(result)

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        self.status.setText("任务失败"); self._show_notice(f"图片下载没有完成：{message}", "error")
        self.log_toggle.setChecked(True)

    @Slot()
    def _on_cancelled(self) -> None:
        self.status.setText("任务已取消"); self._show_notice("图片下载已安全取消，可从任务清单继续。", "warning")

    @Slot()
    def _on_finished(self) -> None:
        self.worker = None; self.thread = None
        for widget in self._input_widgets: widget.setEnabled(True)
        self.start_button.setEnabled(True); self.start_button.setText("再次下载")
        self.cancel_button.setEnabled(True); self.cancel_button.setText("完成并返回")

    def _cancel_or_close(self) -> None:
        if self.worker is not None:
            self.worker.cancel(); self.cancel_button.setEnabled(False); self.status.setText("正在安全取消…")
        else:
            self.accept()

    def closeEvent(self, event) -> None:
        if self.worker is not None:
            self._show_notice("下载仍在运行，请先安全取消。", "warning"); event.ignore(); return
        super().closeEvent(event)
