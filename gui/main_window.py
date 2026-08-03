"""本文件实现面向质量工程师的响应式主窗口和六步缺陷分析工作流。"""

from __future__ import annotations

from pathlib import Path
from time import perf_counter
from typing import Any
import json
import shutil

import pandas as pd
import psutil
import yaml
from PySide6.QtCore import QDateTime, QSettings, QThread, QTimer, Qt
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDateTimeEdit, QFileDialog, QFormLayout, QFrame, QGridLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSpinBox, QStatusBar,
    QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from src.analysis_service import (
    AnalysisRequest, AnalysisResult, ProgressEvent, resolve_image_path,
)
from src.app_runtime import APP_VERSION, configure_logging, new_error_id, user_data_dir
from src.data_quality import validate_products
from src.defect_relationships import analyze_defect_relationships
from src.process_relationships import analyze_process_relationships
from .image_viewer import ImageReviewWidget
from .analysis_worker import AnalysisWorker
from .dataframe_table import DataFrameTableWidget
from .parameter_dialog import ParameterDialog
from .workbench import ElidedLabel, LayoutProfile, WorkbenchShell, WorkbenchStack
from .excel_analysis_page import ExcelAnalysisPage
from src.excel_analysis import ExcelAnalysisResult, excel_relationship_frame, load_excel_workbook


class MainWindow(QMainWindow):
    def __init__(self, project_root: Path) -> None:
        super().__init__()
        self.project_root = project_root.resolve()
        self.settings = QSettings()
        self.logger, self.log_path = configure_logging()
        self.setWindowTitle("MEA 5S 缺陷规律分析")
        self.setMinimumSize(980, 620)
        self._set_adaptive_initial_size()
        self.config_snapshot = yaml.safe_load(
            (self.project_root / "config/analysis_config.yaml").read_text(encoding="utf-8")
        )
        self.config_modified = False
        self.thread: QThread | None = None
        self.worker: AnalysisWorker | None = None
        self.current_result: AnalysisResult | None = None
        self.current_excel_result: ExcelAnalysisResult | None = None
        self._result_config: dict[str, Any] = {}
        self.loaded_products = pd.DataFrame()
        self._close_after_cancel = False
        self._analysis_started = 0.0
        self._restore_maximized = False
        self._initial_show = True
        self.tabs = WorkbenchStack()
        self._build_setup_tab()
        self._build_quality_tab()
        self._build_progress_tab()
        self._build_result_tab()
        self._build_excel_tab()
        self._build_relationship_tab()
        self.review = ImageReviewWidget()
        self.tabs.addTab(self.review, "⑦ 图片复核")
        self.workbench = WorkbenchShell(self.tabs, APP_VERSION)
        self.workbench.navigation.exit_requested.connect(self.close)
        self.setCentralWidget(self.workbench)
        self.task_edit.textChanged.connect(self.workbench.header.set_task_name)
        self.workbench.header.set_task_name(self.task_edit.text())
        self._build_status_bar()
        self._restore_settings()
        style_files = (
            self.project_root / "resources/styles/app.qss",
            self.project_root / "resources/styles/workbench.qss",
        )
        self.setStyleSheet(
            "\n".join(
                path.read_text(encoding="utf-8") for path in style_files if path.exists()
            )
        )
        self.resource_timer = QTimer(self)
        self.resource_timer.timeout.connect(self._update_resource_status)
        self.resource_timer.start(2000)

    def _set_adaptive_initial_size(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            self.resize(1200, 760)
            return
        area = screen.availableGeometry()
        self.resize(
            min(area.width(), max(980, int(area.width() * 0.92))),
            min(area.height(), max(620, int(area.height() * 0.90))),
        )

    def _saved_path(self, key: str, default: Path | str, directory: bool = False) -> str:
        value = str(self.settings.value(key, str(default)))
        path = Path(value)
        valid = path.exists() or (directory and path.parent.exists())
        return value if valid else str(default)

    def _path_row(
        self, default: str, file_mode: bool, filter_text: str = ""
    ) -> tuple[QLineEdit, QWidget]:
        edit = QLineEdit(default)
        edit.setClearButtonEnabled(True)
        button = QPushButton("浏览…")
        if file_mode:
            button.clicked.connect(lambda: self._choose_file(edit, filter_text))
        else:
            button.clicked.connect(lambda: self._choose_directory(edit))
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit, 1)
        layout.addWidget(button)
        return edit, container

    def _build_setup_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        title = QLabel("新建分析任务")
        title.setObjectName("pageTitle")
        intro = QLabel("选择产品清单和图片位置。执行前先检查数据，避免任务运行到中途失败。")
        intro.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(intro)
        data_group = QGroupBox("数据源")
        form = QFormLayout(data_group)
        example_products = self.project_root / "data/dataset_realistic/products.csv"
        default_products = example_products if example_products.is_file() else ""
        self.products_edit, row = self._path_row(
            self._saved_path("paths/products", default_products), True, "CSV (*.csv)"
        )
        form.addRow("产品清单", row)
        saved_image_root = str(self.settings.value("paths/image_root", ""))
        self.image_root_edit, row = self._path_row(saved_image_root, False)
        self.image_root_edit.setPlaceholderText("CSV相对路径需要重新定位时填写")
        form.addRow("图片根目录（可选）", row)
        output_group = QGroupBox("任务与结果")
        form = QFormLayout(output_group)
        default_output = user_data_dir() / "analysis_tasks"
        default_output.mkdir(parents=True, exist_ok=True)
        self.output_edit, row = self._path_row(
            self._saved_path("paths/output", default_output, True), False
        )
        form.addRow("结果保存目录", row)
        self.task_edit = QLineEdit(str(self.settings.value("task/name", "5S分析")))
        form.addRow("任务名称", self.task_edit)
        advanced_group = QGroupBox("高级设置")
        advanced_group.setObjectName("advancedSettings")
        form = QFormLayout(advanced_group)
        self.config_edit, row = self._path_row(
            self._saved_path(
                "paths/config", self.project_root / "config/analysis_config.yaml"
            ), True, "YAML (*.yaml *.yml)"
        )
        form.addRow("分析配置", row)
        params = QPushButton("编辑分析参数…")
        params.clicked.connect(self._edit_parameters)
        form.addRow("", params)
        advanced_toggle = QPushButton("显示高级设置  ▾")
        advanced_toggle.setObjectName("advancedToggle")
        advanced_toggle.setCheckable(True)
        advanced_toggle.toggled.connect(advanced_group.setVisible)
        advanced_toggle.toggled.connect(
            lambda checked: advanced_toggle.setText(
                "收起高级设置  ▴" if checked else "显示高级设置  ▾"
            )
        )
        advanced_group.setVisible(False)
        buttons = QHBoxLayout()
        buttons.addStretch()
        inspect = QPushButton("1. 检查数据")
        inspect.clicked.connect(self._inspect_products)
        self.start_button = QPushButton("2. 开始分析")
        self.start_button.setObjectName("primaryButton")
        self.start_button.clicked.connect(self._start_analysis)
        buttons.addWidget(inspect)
        buttons.addWidget(self.start_button)
        layout.addWidget(data_group)
        layout.addWidget(output_group)
        layout.addWidget(advanced_toggle)
        layout.addWidget(advanced_group)
        layout.addStretch()
        layout.addLayout(buttons)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        self.tabs.addTab(scroll, "① 新建任务")

    def _build_quality_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        title = QLabel("数据质量检查")
        title.setObjectName("pageTitle")
        self.quality_summary = QLabel("尚未检查数据。")
        self.quality_summary.setWordWrap(True)
        self.quality_table = DataFrameTableWidget("data_quality")
        layout.addWidget(title)
        layout.addWidget(self.quality_summary)
        layout.addWidget(self.quality_table, 1)
        self.tabs.addTab(page, "② 数据检查")

    def _build_progress_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        title = QLabel("执行分析")
        title.setObjectName("pageTitle")
        self.stage_label = QLabel("等待任务")
        self.stage_label.setObjectName("stageLabel")
        self.stage_steps = QLabel("检查输入  ›  提取缺陷  ›  发现规律  ›  写入结果  ›  生成图表")
        self.stage_steps.setObjectName("stageStepper")
        self.stage_steps.setWordWrap(True)
        self.stage_steps.setMinimumWidth(0)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress_detail = QLabel("0/0")
        self.resource_label = QLabel("CPU：-　内存：-　预计剩余：-")
        self.live_alerts = QListWidget()
        self.run_log = QTextEdit()
        self.run_log.setReadOnly(True)
        details = QTabWidget()
        details.addTab(self.live_alerts, "实时告警")
        details.addTab(self.run_log, "运行日志")
        self.cancel_button = QPushButton("安全取消任务")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel)
        layout.addWidget(title)
        layout.addWidget(self.stage_label)
        layout.addWidget(self.stage_steps)
        layout.addWidget(self.progress)
        layout.addWidget(self.progress_detail)
        layout.addWidget(self.resource_label)
        layout.addWidget(details, 1)
        layout.addWidget(self.cancel_button, 0, Qt.AlignRight)
        self.tabs.addTab(page, "③ 执行分析")

    def _build_result_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        title = QLabel("分析结果概览")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        self.kpi_labels: dict[str, QLabel] = {}
        grid = QGridLayout()
        items = (
            ("analyzed_product_count", "产品总数"), ("extracted_defect_count", "提取缺陷"),
            ("spatial_cluster_count", "空间簇"), ("discovered_pattern_count", "发现规律"),
            ("periodic_pattern_count", "周期规律"), ("burst_pattern_count", "连续异常"),
            ("alert_count", "告警数量"), ("elapsed_seconds", "耗时（秒）"),
        )
        for index, (key, caption) in enumerate(items):
            card = QFrame()
            card.setObjectName("kpiCard")
            card_layout = QVBoxLayout(card)
            value = QLabel("-")
            value.setObjectName("kpiValue")
            value.setAlignment(Qt.AlignCenter)
            text = QLabel(caption)
            text.setAlignment(Qt.AlignCenter)
            self.kpi_labels[key] = value
            card_layout.addWidget(value)
            card_layout.addWidget(text)
            grid.addWidget(card, index // 4, index % 4)
        layout.addLayout(grid)
        filters = QGridLayout()
        self.camera_filter = QComboBox()
        self.camera_filter.addItem("全部相机")
        self.batch_filter = QComboBox()
        self.batch_filter.addItem("全部批次")
        self.defect_filter = QComboBox()
        self.defect_filter.addItem("全部缺陷类别")
        self.order_start = QSpinBox()
        self.order_start.setMaximum(2_000_000_000)
        self.order_end = QSpinBox()
        self.order_end.setMaximum(2_000_000_000)
        self.time_start = QDateTimeEdit()
        self.time_end = QDateTimeEdit()
        for editor in (self.time_start, self.time_end):
            editor.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
            editor.setCalendarPopup(True)
            editor.setVisible(False)
        self.camera_filter.currentTextChanged.connect(self._apply_global_filters)
        self.batch_filter.currentTextChanged.connect(self._apply_global_filters)
        self.defect_filter.currentTextChanged.connect(self._apply_global_filters)
        self.order_start.valueChanged.connect(self._apply_global_filters)
        self.order_end.valueChanged.connect(self._apply_global_filters)
        self.time_start.dateTimeChanged.connect(self._apply_global_filters)
        self.time_end.dateTimeChanged.connect(self._apply_global_filters)
        filters.addWidget(QLabel("全局筛选"), 0, 0)
        filters.addWidget(self.camera_filter, 0, 1)
        filters.addWidget(self.batch_filter, 0, 2)
        filters.addWidget(self.defect_filter, 0, 3)
        filters.addWidget(QLabel("产品序号"), 1, 0)
        filters.addWidget(self.order_start, 1, 1)
        filters.addWidget(self.order_end, 1, 2)
        filters.addWidget(self.time_start, 1, 3)
        filters.addWidget(self.time_end, 1, 4)
        filters.setColumnStretch(5, 1)
        layout.addLayout(filters)
        self.pattern_widget = DataFrameTableWidget("patterns")
        self.pattern_widget.row_activated.connect(self._jump_from_pattern)
        self.alert_widget = DataFrameTableWidget("alerts", alert_colors=True)
        self.alert_widget.row_activated.connect(self._jump_from_alert)
        self.cooccurrence_widget = DataFrameTableWidget("defect_cooccurrence")
        self.transition_widget = DataFrameTableWidget("defect_transitions")
        tables = QTabWidget()
        tables.addTab(self.pattern_widget, "规律")
        tables.addTab(self.alert_widget, "预警")
        tables.addTab(self.cooccurrence_widget, "缺陷共现")
        tables.addTab(self.transition_widget, "序列关系")
        layout.addWidget(tables, 1)
        self.tabs.addTab(page, "④ 结果概览")

    def _build_relationship_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        title = QLabel("工艺参数关联分析")
        title.setObjectName("pageTitle")
        warning = QLabel("统计关联不等于因果关系；结论需结合工艺机理和受控实验验证。")
        warning.setObjectName("warningBanner")
        warning.setWordWrap(True)
        controls = QGridLayout()
        self.process_edit, process_row = self._path_row(
            self._saved_path("paths/process", ""), True,
            "工艺参数 (*.csv *.xlsx *.xlsm);;CSV (*.csv);;Excel (*.xlsx *.xlsm)"
        )
        self.process_edit.setPlaceholderText("包含产品标识/时间戳和数值工艺参数的CSV或Excel")
        self.time_tolerance = QSpinBox()
        self.time_tolerance.setRange(0, 86400)
        self.time_tolerance.setValue(int(self.settings.value("process/tolerance_seconds", 60)))
        self.time_tolerance.setSuffix(" 秒")
        analyze = QPushButton("分析工艺关联")
        analyze.setObjectName("primaryButton")
        analyze.clicked.connect(self._analyze_process_parameters)
        self.use_current_excel = QCheckBox("使用当前Excel分析结果")
        self.use_current_excel.setEnabled(False)
        controls.addWidget(process_row, 0, 0, 1, 5)
        controls.addWidget(self.use_current_excel, 1, 0)
        controls.addWidget(QLabel("时间匹配容差"), 1, 1)
        controls.addWidget(self.time_tolerance, 1, 2)
        controls.setColumnStretch(3, 1)
        controls.addWidget(analyze, 1, 4)
        self.relationship_summary = QLabel("完成缺陷分析后，可加载工艺参数表进行关联分析。")
        self.relationship_summary.setWordWrap(True)
        self.relationship_metrics = DataFrameTableWidget("process_metrics")
        self.relationship_bins = DataFrameTableWidget("process_bins")
        self.relationship_model = DataFrameTableWidget("process_model")
        tables = QTabWidget()
        tables.addTab(self.relationship_metrics, "统计与效应量")
        tables.addTab(self.relationship_bins, "区间缺陷率")
        tables.addTab(self.relationship_model, "模型重要性")
        layout.addWidget(title)
        layout.addWidget(warning)
        layout.addLayout(controls)
        layout.addWidget(self.relationship_summary)
        layout.addWidget(tables, 1)
        self.tabs.addTab(page, "⑥ 关联分析")

    def _build_excel_tab(self) -> None:
        self.excel_page = ExcelAnalysisPage(self.project_root)
        self.excel_page.result_ready.connect(self._excel_result_ready)
        self.excel_page.status_message.connect(lambda message: self.statusBar().showMessage(message, 8000))
        self.tabs.addTab(self.excel_page, "⑤ Excel分析")

    def _excel_result_ready(self, result: ExcelAnalysisResult) -> None:
        self.current_excel_result = result
        self.use_current_excel.setEnabled(True)
        self.use_current_excel.setChecked(True)
        self.relationship_summary.setText(
            f"当前Excel结果已就绪：{result.summary['record_count']}条记录、"
            f"{result.summary['parameter_count']}个参数。可直接执行图片缺陷关联分析。"
        )

    def _build_status_bar(self) -> None:
        status = QStatusBar()
        status.setObjectName("applicationStatusBar")
        status.showMessage("就绪")
        self.version_status_label = QLabel(f"版本 {APP_VERSION}")
        self.log_status_label = ElidedLabel(f"日志：{self.log_path}")
        self.log_status_label.setMaximumWidth(320)
        status.addPermanentWidget(self.version_status_label)
        status.addPermanentWidget(self.log_status_label)
        self.setStatusBar(status)

    def _restore_settings(self) -> None:
        geometry = self.settings.value("window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
            self._clamp_window_to_screen()
        self.workbench.apply_responsive_layout(self.size(), force=True)
        saved_layout = str(self.settings.value("review/layout", "2×2"))
        index = self.review.layout_combo.findText(saved_layout)
        if index >= 0:
            self.review.layout_combo.setCurrentIndex(index)
        splitter_state = self.settings.value("window/workbench_splitter")
        if splitter_state is not None and self.workbench.profile is LayoutProfile.FULL:
            self.workbench.splitter.restoreState(splitter_state)
        assistant_visible = str(
            self.settings.value("window/assistant_visible", "true")
        ).lower() == "true"
        self.workbench.set_assistant_visible(
            assistant_visible if self.workbench.profile is LayoutProfile.FULL else False
        )
        self._restore_maximized = str(
            self.settings.value("window/maximized", "false")
        ).lower() == "true"

    def _clamp_window_to_screen(self) -> None:
        screen = QApplication.screenAt(self.frameGeometry().center()) or QApplication.primaryScreen()
        if screen is None:
            self._set_adaptive_initial_size()
            return
        area = screen.availableGeometry()
        frame = self.frameGeometry()
        width = min(frame.width(), area.width())
        height = min(frame.height(), area.height())
        x = min(max(frame.x(), area.left()), area.right() - width + 1)
        y = min(max(frame.y(), area.top()), area.bottom() - height + 1)
        self.setGeometry(x, y, width, height)

    def _choose_file(self, edit: QLineEdit, filter_text: str) -> None:
        start = str(Path(edit.text()).parent) if edit.text() else str(self.project_root)
        filename, _ = QFileDialog.getOpenFileName(self, "选择文件", start, filter_text)
        if filename:
            edit.setText(filename)

    def _choose_directory(self, edit: QLineEdit) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "选择目录", edit.text() or str(self.project_root)
        )
        if directory:
            edit.setText(directory)

    def _inspect_products(self) -> bool:
        try:
            path = Path(self.products_edit.text().strip())
            if not path.is_file():
                raise FileNotFoundError(f"产品清单不存在：{path}")
            frame = pd.read_csv(path)
            report = validate_products(frame)
            source = "a_image_path" if "a_image_path" in frame else "v_image_path"
            image_root = (
                Path(self.image_root_edit.text().strip())
                if self.image_root_edit.text().strip() else None
            )
            missing: list[str] = []
            if report.is_valid:
                for row in frame.itertuples(index=False):
                    for column in (source, "e_image_path"):
                        resolved = resolve_image_path(
                            str(getattr(row, column)), self.project_root, image_root, path
                        )
                        if not resolved.is_file():
                            missing.append(str(resolved))
                            if len(missing) >= 10:
                                break
                    if len(missing) >= 10:
                        break
            if missing:
                report.errors.append(
                    "图片路径不存在（最多列出10项）：" + "；".join(missing)
                )
            self.loaded_products = frame
            self.quality_table.set_frame(report.to_frame())
            self.quality_summary.setText(
                f"{'检查通过' if report.is_valid else '检查未通过'}："
                f"{report.row_count} 行、{report.column_count} 列；"
                f"{len(report.errors)} 个错误、{len(report.warnings)} 个警告。"
            )
            object_name = "successBanner" if report.is_valid else "errorBanner"
            self.quality_summary.setObjectName(object_name)
            self.quality_summary.style().unpolish(self.quality_summary)
            self.quality_summary.style().polish(self.quality_summary)
            self.tabs.setCurrentIndex(1)
            return report.is_valid
        except Exception as exc:
            self._show_error("数据检查失败", exc)
            return False

    def _edit_parameters(self) -> None:
        dialog = ParameterDialog(
            self.config_snapshot, self.project_root / "config/analysis_config.yaml", self
        )
        dialog.parameters_applied.connect(self._apply_parameters)
        dialog.exec()

    def _apply_parameters(self, values: dict[str, Any]) -> None:
        self.config_snapshot = dict(values)
        self.config_modified = True
        self.statusBar().showMessage("已应用本次任务的参数修改", 5000)

    def _start_analysis(self) -> None:
        if not self._inspect_products():
            return
        try:
            output = Path(self.output_edit.text().strip())
            output.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(output).free < 512 * 1024 * 1024:
                raise OSError("结果目录可用空间不足512 MB")
            config_path = Path(self.config_edit.text().strip())
            if not self.config_modified:
                self.config_snapshot = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            request = AnalysisRequest(
                Path(self.products_edit.text().strip()), config_path, output, self.task_edit.text(),
                Path(self.image_root_edit.text()) if self.image_root_edit.text().strip() else None,
                dict(self.config_snapshot),
            )
        except Exception as exc:
            self._show_error("无法启动分析", exc)
            return
        self.live_alerts.clear()
        self.run_log.clear()
        self.progress.setValue(0)
        self.start_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self._analysis_started = perf_counter()
        self.thread = QThread(self)
        self.worker = AnalysisWorker(request)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.stage_changed.connect(self._stage)
        self.worker.progress_changed.connect(self._progress)
        self.worker.alert_created.connect(self._live_alert)
        self.worker.completed.connect(self._completed)
        self.worker.cancelled.connect(self._cancelled)
        self.worker.failed.connect(self._failed)
        for signal in (self.worker.completed, self.worker.cancelled, self.worker.failed):
            signal.connect(self.thread.quit)
        self.thread.finished.connect(self._thread_finished)
        self.thread.start()
        self.workbench.header.set_run_state("分析运行中", "running")
        self.tabs.setCurrentIndex(2)

    def _stage(self, stage: str) -> None:
        names = {
            "VALIDATING": "检查输入", "EXTRACTING": "提取缺陷", "ANALYZING": "发现规律",
            "WRITING": "写入结果", "VISUALIZING": "生成图表", "COMPLETE": "完成",
        }
        text = names.get(stage, stage)
        self.stage_label.setText(text)
        self.run_log.append(f"[{pd.Timestamp.now().strftime('%H:%M:%S')}] {text}")

    def _progress(self, event: ProgressEvent) -> None:
        self.progress.setValue(event.percent)
        order = "-" if event.current_order is None else event.current_order
        self.progress_detail.setText(
            f"当前序号：{order}；已处理：{event.processed}/{event.total}；"
            f"已提取缺陷：{event.defect_count}"
        )
        self._update_resource_status(event.percent)

    def _update_resource_status(self, percent: int | None = None) -> None:
        process = psutil.Process()
        memory_mb = process.memory_info().rss / 1024 / 1024
        current = self.progress.value() if percent is None else percent
        remaining = "-"
        if self.thread and self.thread.isRunning() and current > 2:
            elapsed = perf_counter() - self._analysis_started
            seconds = max(0, elapsed * (100 - current) / current)
            remaining = f"约 {int(seconds // 60)}分{int(seconds % 60)}秒"
        self.resource_label.setText(
            f"进程CPU：{process.cpu_percent(interval=None):.0f}%　"
            f"内存：{memory_mb:.0f} MB　预计剩余：{remaining}"
        )

    def _live_alert(self, alert: dict[str, Any]) -> None:
        self.live_alerts.addItem(
            f"⚠ #{alert['alert_at_order']} {alert['alert_type']}：{alert['message']}"
        )

    def _completed(self, result: AnalysisResult) -> None:
        self.current_result = result
        for key, label in self.kpi_labels.items():
            label.setText(str(result.summary.get(key, "-")))
        config = yaml.safe_load(
            (result.output_dir / "analysis_config_snapshot.yaml").read_text(encoding="utf-8")
        )
        self._result_config = config
        cooccurrence, transitions = analyze_defect_relationships(
            result.frames["products"], result.frames["extracted"]
        )
        self.cooccurrence_widget.set_frame(cooccurrence)
        self.transition_widget.set_frame(transitions)
        cooccurrence.to_csv(
            result.output_dir / "defect_cooccurrence.csv", index=False, encoding="utf-8-sig"
        )
        transitions.to_csv(
            result.output_dir / "defect_transitions.csv", index=False, encoding="utf-8-sig"
        )
        self._populate_filters(result.frames["products"], result.frames["extracted"])
        self._apply_global_filters()
        self.statusBar().showMessage(f"分析完成：{result.output_dir}")
        self.workbench.header.set_run_state("分析完成", "success")
        QMessageBox.information(self, "分析完成", f"结果目录：\n{result.output_dir}")
        self.tabs.setCurrentIndex(3)

    def _populate_filters(self, products: pd.DataFrame, defects: pd.DataFrame) -> None:
        widgets = (
            self.camera_filter, self.batch_filter, self.defect_filter,
            self.order_start, self.order_end, self.time_start, self.time_end,
        )
        for widget in widgets:
            widget.blockSignals(True)
        self.camera_filter.clear()
        self.camera_filter.addItem("全部相机")
        if "camera" in products:
            self.camera_filter.addItems(sorted(products.camera.dropna().astype(str).unique()))
        self.batch_filter.clear()
        self.batch_filter.addItem("全部批次")
        batch = next((name for name in ("batch", "batch_id") if name in products), None)
        if batch:
            self.batch_filter.addItems(sorted(products[batch].dropna().astype(str).unique()))
        self.defect_filter.clear()
        self.defect_filter.addItem("全部缺陷类别")
        category = next((name for name in ("defect_type", "cluster_id") if name in defects), None)
        if category:
            self.defect_filter.addItems(sorted(defects[category].dropna().astype(str).unique()))
        minimum, maximum = int(products.global_order.min()), int(products.global_order.max())
        self.order_start.setRange(minimum, maximum)
        self.order_end.setRange(minimum, maximum)
        self.order_start.setValue(minimum)
        self.order_end.setValue(maximum)
        time_column = next(
            (name for name in ("production_timestamp", "timestamp") if name in products), None
        )
        show_time = time_column is not None
        self.time_start.setVisible(show_time)
        self.time_end.setVisible(show_time)
        if time_column:
            times = pd.to_datetime(products[time_column], errors="coerce").dropna()
            if not times.empty:
                self.time_start.setDateTime(QDateTime(times.min().to_pydatetime()))
                self.time_end.setDateTime(QDateTime(times.max().to_pydatetime()))
        for widget in widgets:
            widget.blockSignals(False)

    def _apply_global_filters(self, *_args) -> None:
        if self.current_result is None:
            return
        frames = self.current_result.frames
        products = frames["products"].copy()
        camera = self.camera_filter.currentText()
        if camera and camera != "全部相机" and "camera" in products:
            products = products[products.camera.astype(str) == camera]
        batch_name = next((name for name in ("batch", "batch_id") if name in products), None)
        batch = self.batch_filter.currentText()
        if batch_name and batch and batch != "全部批次":
            products = products[products[batch_name].astype(str) == batch]
        products = products[
            products.global_order.astype(int).between(
                self.order_start.value(), self.order_end.value()
            )
        ]
        time_column = next(
            (name for name in ("production_timestamp", "timestamp") if name in products), None
        )
        if time_column and self.time_start.isVisible():
            times = pd.to_datetime(products[time_column], errors="coerce")
            products = products[
                times.between(
                    pd.Timestamp(self.time_start.dateTime().toPython()),
                    pd.Timestamp(self.time_end.dateTime().toPython()),
                )
            ]
        orders = set(products.global_order.astype(int))
        extracted = frames["extracted"]
        extracted = extracted[extracted.global_order.astype(int).isin(orders)]
        category = next(
            (name for name in ("defect_type", "cluster_id") if name in extracted), None
        )
        selected_defect = self.defect_filter.currentText()
        if category and selected_defect and selected_defect != "全部缺陷类别":
            extracted = extracted[extracted[category].astype(str) == selected_defect]
            orders &= set(extracted.global_order.astype(int))
            products = products[products.global_order.astype(int).isin(orders)]
        patterns = frames["patterns"]
        if (
            selected_defect and selected_defect != "全部缺陷类别"
            and not patterns.empty and "cluster_id" in patterns
        ):
            patterns = patterns[patterns.cluster_id.astype(str) == selected_defect]
        if not patterns.empty and "first_order" in patterns:
            patterns = patterns[
                pd.to_numeric(patterns.first_order, errors="coerce").fillna(-1).astype(int).isin(orders)
            ]
        alerts = frames["alerts"]
        if (
            selected_defect and selected_defect != "全部缺陷类别"
            and not alerts.empty and "cluster_id" in alerts
        ):
            alerts = alerts[alerts.cluster_id.astype(str) == selected_defect]
        if not alerts.empty and "alert_at_order" in alerts:
            alerts = alerts[
                pd.to_numeric(alerts.alert_at_order, errors="coerce").fillna(-1).astype(int).isin(orders)
            ]
        self.pattern_widget.set_frame(patterns)
        self.alert_widget.set_frame(alerts)
        self.review.set_data(products, extracted, self._result_config)

    def _cancelled(self, result: AnalysisResult) -> None:
        self.workbench.header.set_run_state("任务已取消", "ready")
        QMessageBox.information(self, "任务已取消", f"部分诊断结果：\n{result.output_dir}")
        if self._close_after_cancel:
            self.close()

    def _failed(self, message: str) -> None:
        self.logger.error("分析失败：%s", message)
        self.workbench.header.set_run_state("分析失败", "error")
        QMessageBox.critical(
            self, "分析失败", f"{message}\n\n诊断日志：{self.log_path}"
        )

    def _thread_finished(self) -> None:
        self.start_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        if self.worker:
            self.worker.deleteLater()
        if self.thread:
            self.thread.deleteLater()
        self.worker = None
        self.thread = None

    def _cancel(self) -> None:
        if self.worker:
            self.worker.cancel()
            self.stage_label.setText("正在安全取消…")
            self.run_log.append("已请求安全取消，当前图片处理完成后停止。")

    def _jump_from_alert(self, record: pd.Series) -> None:
        self.review.jump_to(int(float(record["alert_at_order"])))
        self.tabs.setCurrentWidget(self.review)

    def _jump_from_pattern(self, record: pd.Series) -> None:
        self.review.jump_to(int(float(record["first_order"])))
        self.tabs.setCurrentWidget(self.review)

    def _analyze_process_parameters(self) -> None:
        if self.current_result is None:
            QMessageBox.warning(self, "尚无缺陷结果", "请先完成一次缺陷分析。")
            return
        try:
            if self.use_current_excel.isChecked():
                if self.current_excel_result is None:
                    raise ValueError("当前没有可用的Excel分析结果")
                parameters = excel_relationship_frame(
                    self.current_excel_result.workbook_data,
                    self.current_excel_result.frames["standardized"],
                )
            else:
                path = Path(self.process_edit.text().strip())
                if not path.is_file():
                    raise FileNotFoundError(f"工艺参数文件不存在：{path}")
                if path.suffix.lower() in {".xlsx", ".xlsm"}:
                    workbook_data = load_excel_workbook(path)
                    parameters = excel_relationship_frame(workbook_data)
                else:
                    parameters = pd.read_csv(path)
            result = analyze_process_relationships(
                self.current_result.frames["products"],
                self.current_result.frames["extracted"],
                parameters,
                self.time_tolerance.value(),
            )
            self.relationship_metrics.set_frame(result.parameter_metrics)
            self.relationship_bins.set_frame(result.binned_rates)
            self.relationship_model.set_frame(result.model_importance)
            summary = result.summary
            self.relationship_summary.setText(
                f"关联键：{summary['join_key']}；匹配：{summary['matched_count']}/"
                f"{summary['product_count']}（{summary['match_rate']:.1%}）；"
                f"参数：{summary['parameter_count']}；缺陷产品："
                f"{summary['defective_product_count']}；验证：{summary['validation_method']}；"
                f"AUC：{summary['validation_auc'] if summary['validation_auc'] is not None else '-'}"
            )
            output = self.current_result.output_dir
            result.parameter_metrics.to_csv(
                output / "process_parameter_metrics.csv", index=False, encoding="utf-8-sig"
            )
            result.binned_rates.to_csv(
                output / "process_parameter_binned_rates.csv", index=False, encoding="utf-8-sig"
            )
            result.model_importance.to_csv(
                output / "process_model_importance.csv", index=False, encoding="utf-8-sig"
            )
            (output / "process_relationship_summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self.review.set_process_data(result.joined)
            self.statusBar().showMessage("工艺参数关联分析完成", 5000)
        except Exception as exc:
            self._show_error("工艺参数关联分析失败", exc)

    def _show_error(self, title: str, exc: Exception) -> None:
        error_id = new_error_id()
        self.logger.exception("%s error_id=%s: %s", title, error_id, exc)
        QMessageBox.critical(
            self, title, f"{exc}\n\n错误编号：{error_id}\n诊断日志：{self.log_path}"
        )

    def _save_settings(self) -> None:
        for key, value in (
            ("paths/products", self.products_edit.text()), ("paths/config", self.config_edit.text()),
            ("paths/output", self.output_edit.text()), ("paths/image_root", self.image_root_edit.text()),
            ("paths/process", self.process_edit.text()), ("task/name", self.task_edit.text()),
            ("process/tolerance_seconds", self.time_tolerance.value()),
            ("review/layout", self.review.layout_combo.currentText()),
        ):
            self.settings.setValue(key, value)
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.setValue("window/maximized", self.isMaximized())
        self.settings.setValue(
            "window/workbench_splitter", self.workbench.splitter.saveState()
        )
        self.settings.setValue(
            "window/assistant_visible", self.workbench.assistant.isVisible()
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "workbench"):
            self.workbench.apply_responsive_layout(event.size())
            if hasattr(self, "log_status_label"):
                tight = self.workbench.profile is LayoutProfile.TIGHT
                self.log_status_label.setVisible(not tight)
                self.version_status_label.setVisible(not tight)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._initial_show:
            self._initial_show = False
            if self._restore_maximized:
                QTimer.singleShot(0, self.showMaximized)

    def closeEvent(self, event) -> None:
        self._save_settings()
        if self.excel_page.thread and self.excel_page.thread.isRunning():
            answer = QMessageBox.question(self, "Excel任务运行中", "先安全取消Excel分析再关闭？")
            if answer == QMessageBox.Yes:
                self.excel_page.cancel_analysis()
            event.ignore()
            return
        if self.thread and self.thread.isRunning():
            answer = QMessageBox.question(self, "任务运行中", "先安全取消任务再关闭？")
            if answer == QMessageBox.Yes:
                self._close_after_cancel = True
                self._cancel()
            event.ignore()
            return
        event.accept()
