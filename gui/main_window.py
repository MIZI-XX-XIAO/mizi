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
from PySide6.QtCore import QDateTime, QSettings, QThread, QTimer, Qt, Slot
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QCompleter, QDateTimeEdit, QFileDialog, QFormLayout, QFrame, QGridLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSpinBox, QStatusBar,
    QStackedWidget, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from src.analysis_service import (
    AnalysisRequest, AnalysisResult, AnalysisSelection, ExcelTargetAnalysisRequest,
    ProgressEvent, code_target_key, resolve_image_path,
)
from src.app_runtime import APP_VERSION, configure_logging, new_error_id, user_data_dir
from src.data_quality import DataQualityReport, validate_products
from src.defect_cause_analysis import select_product_exposure_details
from src.defect_relationships import analyze_defect_relationships
from src.result_views import ResultView, build_result_view, pattern_count
from src.nonlinear_relationships import build_unified_findings, enrich_findings
from .image_viewer import ImageReviewWidget
from .analysis_worker import AnalysisWorker
from .dataframe_table import DataFrameTableWidget
from .association_findings import AssociationFindingsWidget
from .relationship_worker import RelationshipWorker
from .result_dialogs import ResultDetailsWidget
from .parameter_dialog import ParameterDialog
from .workbench import ElidedLabel, LayoutProfile, WorkbenchShell, WorkbenchStack
from .image_download_dialog import ImageDownloadDialog
from .interaction_detail import InteractionDetailWidget
from .mes_download_panel import MesDownloadDialog
from src.image_download import ImageDownloadResult
from src.excel_analysis import excel_relationship_frame, load_excel_workbook
from src.station_sources import (
    build_image_product_index,
    load_station_catalog,
)
from src.station_workbook import (
    StationWorkbookData, enrich_products_with_station_truth, load_station_workbook,
    process_parameter_frame,
)
from src.defect_evidence import load_defect_catalog, normalize_defect_codes


class MainWindow(QMainWindow):
    def __init__(self, project_root: Path) -> None:
        super().__init__()
        self.project_root = project_root.resolve()
        self.station_catalog = load_station_catalog(self.project_root / "config/stations.yaml")
        self.settings = QSettings()
        self.logger, self.log_path = configure_logging()
        self.setWindowTitle("MEA多工站缺陷规律分析")
        self.setMinimumSize(980, 620)
        self._set_adaptive_initial_size()
        self.config_snapshot = yaml.safe_load(
            (self.project_root / "config/analysis_config.yaml").read_text(encoding="utf-8")
        )
        self.config_modified = False
        self.thread: QThread | None = None
        self.worker: AnalysisWorker | None = None
        self.relationship_thread: QThread | None = None
        self.relationship_worker: RelationshipWorker | None = None
        self.current_result: AnalysisResult | None = None
        self._result_config: dict[str, Any] = {}
        self.loaded_products = pd.DataFrame()
        self.analysis_products = pd.DataFrame()
        self._station_issues = pd.DataFrame()
        self.station_workbook: StationWorkbookData | None = None
        self._inspected_codes = pd.DataFrame()
        self._inspected_all_codes = pd.DataFrame()
        self._inspected_parameters = pd.DataFrame()
        self._auto_relationship_pending = False
        self._close_after_cancel = False
        self._analysis_started = 0.0
        self._restore_maximized = False
        self._initial_show = True
        self._mes_dialog: MesDownloadDialog | None = None
        self._image_dialog: ImageDownloadDialog | None = None
        self.tabs = WorkbenchStack()
        self._build_setup_tab()
        self._build_quality_tab()
        self._build_progress_tab()
        self._build_result_tab()
        self._build_relationship_tab()
        self.review = ImageReviewWidget()
        self.tabs.addTab(self.review, "⑥ 图片复核")
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
        intro = QLabel("选择全流程Excel及AOI图片目录；程序会自动识别工站并按Ident No.建立产品履历。")
        intro.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(intro)
        data_group = QGroupBox("数据源")
        form = QFormLayout(data_group)
        self.task_mode_combo = QComboBox()
        self.task_mode_combo.addItem("全流程分析", "full_process")
        self.task_mode_combo.addItem("单AOI分析", "single_aoi")
        legacy_image_setting = str(self.settings.value("paths/image_root", ""))
        saved_mode = str(self.settings.value(
            "task/mode", "single_aoi" if legacy_image_setting else "full_process"
        ))
        self.task_mode_combo.setCurrentIndex(max(0, self.task_mode_combo.findData(saved_mode)))
        form.addRow("任务模式", self.task_mode_combo)
        self.scope_combo = QComboBox()
        for scope in ("5S", "5X", "7S", "7X"):
            self.scope_combo.addItem(scope, scope)
        legacy_station = str(self.settings.value("station/id", "35_5s_aoi"))
        legacy_scope = {
            "35_5s_aoi": "5S", "57_5x_aoi": "5X",
            "conveyor_7s_aoi": "7S", "conveyor_7x_aoi": "7X",
        }.get(legacy_station, "5S")
        self.scope_combo.setCurrentIndex(max(0, self.scope_combo.findData(str(
            self.settings.value("task/scope", legacy_scope)
        ))))
        self.scope_label = QLabel("单AOI范围")
        form.addRow(self.scope_label, self.scope_combo)
        self.station_combo = QComboBox()
        for station in self.station_catalog.stations:
            self.station_combo.addItem(station.display_name, station.id)
        saved_station = str(self.settings.value("station/id", "35_5s_aoi"))
        station_index = self.station_combo.findData(saved_station)
        self.station_combo.setCurrentIndex(station_index if station_index >= 0 else 0)
        self.station_label = QLabel("旧版工站")
        form.addRow(self.station_label, self.station_combo)
        self.source_excel_edit, row = self._path_row(
            self._saved_path("paths/source_excel", ""), True, "Excel工作簿 (*.xlsx *.xlsm)"
        )
        mes_button = QPushButton("从MES下载…")
        mes_button.setObjectName("mesDownloadButton")
        mes_button.setToolTip("输入时间范围，从OIS Portal下载并自动整理Excel")
        mes_button.clicked.connect(self._open_mes_download)
        row.layout().insertWidget(row.layout().count() - 1, mes_button)
        self.source_excel_edit.setPlaceholderText("可选；用于工艺/质量分析并提供Ident No.")
        form.addRow("Excel工作簿（可选）", row)
        image_download_button = QPushButton("从公司网站自动下载图片…")
        image_download_button.setObjectName("imageDownloadButton")
        image_download_button.setToolTip("读取MES工作簿中的产品号，自动分批下载并分类图片")
        image_download_button.clicked.connect(self._open_image_download)
        form.addRow("图片自动下载", image_download_button)
        self.scope_image_edits: dict[str, QLineEdit] = {}
        self.scope_image_rows: dict[str, QWidget] = {}
        self.scope_image_labels: dict[str, QLabel] = {}
        for scope in ("5S", "5X", "7S", "7X"):
            saved = str(self.settings.value(
                f"paths/image_root_{scope.lower()}",
                self.settings.value("paths/image_root", "") if scope == "5S" else "",
            ))
            edit, image_row = self._path_row(saved, False)
            edit.setPlaceholderText(f"可选；{scope} AOI图片根目录")
            self.scope_image_edits[scope] = edit
            self.scope_image_rows[scope] = image_row
            image_label = QLabel(f"{scope}图片目录")
            self.scope_image_labels[scope] = image_label
            form.addRow(image_label, image_row)
        self.image_root_edit = self.scope_image_edits["5S"]  # legacy compatibility
        self.task_mode_combo.currentIndexChanged.connect(self._update_task_mode_fields)
        self.scope_combo.currentIndexChanged.connect(self._update_task_mode_fields)
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
        target_group = self._build_analysis_target_group()
        advanced_group = QGroupBox("高级设置")
        advanced_group.setObjectName("advancedSettings")
        form = QFormLayout(advanced_group)
        self.config_edit, row = self._path_row(
            self._saved_path(
                "paths/config", self.project_root / "config/analysis_config.yaml"
            ), True, "YAML (*.yaml *.yml)"
        )
        form.addRow("分析配置", row)
        self.catalog_edit, row = self._path_row(
            self._saved_path(
                "paths/defect_catalog", self.project_root / "config/defect_code_catalog.csv"
            ), True, "缺陷字典 (*.csv *.xlsx *.xlsm)"
        )
        self.catalog_edit.setPlaceholderText("可选；按layer+code+region覆盖内置缺陷名称")
        form.addRow("缺陷代码字典", row)
        self.products_edit, row = self._path_row("", True, "CSV (*.csv)")
        self.products_edit.setPlaceholderText("仅用于旧数据集/开发验收，公司日常任务无需填写")
        form.addRow("旧版产品CSV（可选）", row)
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
        layout.addWidget(target_group)
        layout.addWidget(output_group)
        layout.addWidget(advanced_toggle)
        layout.addWidget(advanced_group)
        layout.addStretch()
        layout.addLayout(buttons)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        self.tabs.addTab(scroll, "① 新建任务")
        self._update_task_mode_fields()

    def _build_analysis_target_group(self) -> QGroupBox:
        group = QGroupBox("分析范围与目标")
        group.setObjectName("analysisTargetGroup")
        grid = QGridLayout(group)
        grid.setColumnStretch(1, 1)
        self.analysis_filter_mode = QComboBox()
        self.analysis_filter_mode.addItem("全部缺陷综合分析", "all")
        self.analysis_filter_mode.addItem("指定缺陷代码分析", "selected_codes")
        self.analysis_filter_mode.addItem("仅分析工艺参数关联", "process_only")
        grid.addWidget(QLabel("分析方式"), 0, 0)
        grid.addWidget(self.analysis_filter_mode, 0, 1, 1, 3)

        scope_box = QWidget()
        scope_layout = QHBoxLayout(scope_box); scope_layout.setContentsMargins(0, 0, 0, 0)
        self.analysis_scope_checks: dict[str, QCheckBox] = {}
        for scope in ("5S", "5X", "7S", "7X"):
            check = QCheckBox(scope); self.analysis_scope_checks[scope] = check
            scope_layout.addWidget(check)
        scope_layout.addStretch()
        grid.addWidget(QLabel("工站范围"), 1, 0); grid.addWidget(scope_box, 1, 1, 1, 3)

        source_box = QWidget()
        source_layout = QHBoxLayout(source_box); source_layout.setContentsMargins(0, 0, 0, 0)
        self.code_source_checks = {
            "AOI_FAILURE": QCheckBox("AOI"), "VI_BLOCK": QCheckBox("VI"),
        }
        for check in self.code_source_checks.values():
            check.setChecked(True); source_layout.addWidget(check)
        source_layout.addStretch()
        grid.addWidget(QLabel("代码来源"), 2, 0); grid.addWidget(source_box, 2, 1, 1, 3)

        self.code_search_edit = QLineEdit()
        self.code_search_edit.setPlaceholderText("输入代码或名称搜索")
        self.code_selection_list = QListWidget()
        self.code_selection_list.setMaximumHeight(130)
        grid.addWidget(QLabel("缺陷代码"), 3, 0)
        grid.addWidget(self.code_search_edit, 3, 1, 1, 3)
        grid.addWidget(self.code_selection_list, 4, 1, 1, 3)

        module_box = QWidget()
        module_layout = QHBoxLayout(module_box); module_layout.setContentsMargins(0, 0, 0, 0)
        self.analysis_module_checks: dict[str, QCheckBox] = {}
        for key, caption in (
            ("code_patterns", "代码时序规律"), ("image_patterns", "图片空间规律"),
            ("code_image", "代码与图片联合规律"),
            ("process_relationships", "与工艺参数关联"),
        ):
            check = QCheckBox(caption); check.setChecked(True)
            self.analysis_module_checks[key] = check; module_layout.addWidget(check)
        module_layout.addStretch()
        grid.addWidget(QLabel("分析内容"), 5, 0); grid.addWidget(module_box, 5, 1, 1, 3)

        self.parameter_mode_combo = QComboBox()
        self.parameter_mode_combo.addItem("自动分析全部数值参数", "all")
        self.parameter_mode_combo.addItem("仅分析勾选参数", "selected")
        self.parameter_selection_list = QListWidget()
        self.parameter_selection_list.setMaximumHeight(110)
        self.parameter_selection_list.setVisible(False)
        grid.addWidget(QLabel("工艺参数"), 6, 0)
        grid.addWidget(self.parameter_mode_combo, 6, 1, 1, 3)
        grid.addWidget(self.parameter_selection_list, 7, 1, 1, 3)
        self.analysis_selection_preview = QLabel("请先检查数据以载入可选缺陷代码和工艺参数。")
        self.analysis_selection_preview.setWordWrap(True)
        grid.addWidget(self.analysis_selection_preview, 8, 0, 1, 4)
        group.setEnabled(False)
        self.analysis_target_group = group
        self.code_search_edit.textChanged.connect(self._filter_target_code_list)
        self.code_selection_list.itemChanged.connect(self._update_analysis_selection_preview)
        self.parameter_selection_list.itemChanged.connect(self._update_analysis_selection_preview)
        self.parameter_mode_combo.currentIndexChanged.connect(
            lambda: self.parameter_selection_list.setVisible(
                self.parameter_mode_combo.currentData() == "selected"
            )
        )
        self.parameter_mode_combo.currentIndexChanged.connect(self._update_analysis_selection_preview)
        self.analysis_filter_mode.currentIndexChanged.connect(self._analysis_mode_changed)
        for check in (*self.analysis_scope_checks.values(), *self.code_source_checks.values(),
                      *self.analysis_module_checks.values()):
            check.toggled.connect(self._update_analysis_selection_preview)
        return group

    def _open_mes_download(self) -> None:
        if self._mes_dialog is not None and self._mes_dialog.isVisible():
            self._mes_dialog.raise_()
            self._mes_dialog.activateWindow()
            return
        default_output = Path(self.output_edit.text().strip() or str(user_data_dir())) / "MES_downloads"
        dialog = MesDownloadDialog(self.project_root, default_output, self)
        dialog.workbook_ready.connect(self._use_mes_workbook)
        dialog.continue_to_images.connect(self._continue_images_after_mes)
        dialog.finished.connect(lambda _result: setattr(self, "_mes_dialog", None))
        self._mes_dialog = dialog
        dialog.show()

    def _use_mes_workbook(self, path: str) -> None:
        self.source_excel_edit.setText(path)
        self.settings.setValue("paths/source_excel", path)
        self.statusBar().showMessage("MES工作簿已下载并填入新建任务", 8000)

    def _continue_images_after_mes(self, path: str) -> None:
        if self._mes_dialog is not None:
            self._mes_dialog.accept()
        QTimer.singleShot(0, lambda: self._open_image_download(path))

    def _open_image_download(self, workbook_path: str | None = None) -> None:
        if self._image_dialog is not None and self._image_dialog.isVisible():
            self._image_dialog.raise_()
            self._image_dialog.activateWindow()
            return
        selected = workbook_path or self.source_excel_edit.text().strip()
        workbook = Path(selected) if selected else None
        default_output = Path(self.output_edit.text().strip() or str(user_data_dir())) / "image_downloads"
        dialog = ImageDownloadDialog(self.project_root, workbook, default_output, self)
        dialog.result_ready.connect(self._use_downloaded_images)
        dialog.finished.connect(lambda _result: setattr(self, "_image_dialog", None))
        self._image_dialog = dialog
        dialog.show()

    @Slot(object)
    def _use_downloaded_images(self, result: ImageDownloadResult) -> None:
        if self._image_dialog is not None:
            workbook = self._image_dialog.workbook_edit.text().strip()
            if workbook:
                self.source_excel_edit.setText(workbook)
        for scope, path in result.image_roots.items():
            if scope in self.scope_image_edits:
                self.scope_image_edits[scope].setText(str(path))
                self.settings.setValue(f"paths/image_root_{scope.lower()}", str(path))
        self.statusBar().showMessage(
            "图片已下载并回填目录，正在检查数据" if result.status == "complete"
            else "图片部分下载完成，已回填正常图片并生成异常报告",
            12000,
        )
        self._inspect_products()

    def _update_task_mode_fields(self) -> None:
        full = self.task_mode_combo.currentData() == "full_process"
        self.scope_combo.setVisible(not full)
        self.scope_label.setVisible(not full)
        selected = str(self.scope_combo.currentData() or "5S")
        for scope, row in self.scope_image_rows.items():
            visible = full or scope == selected
            row.setVisible(visible)
            self.scope_image_labels[scope].setVisible(visible)
        # The 18-station selector is retained only for legacy single-workbook/CSV tasks.
        legacy = bool(self.products_edit.text().strip()) if hasattr(self, "products_edit") else False
        self.station_combo.setVisible(legacy)
        self.station_label.setVisible(legacy)

    @staticmethod
    def _scope_station_id(scope: str) -> str:
        return {
            "5S": "35_5s_aoi", "5X": "57_5x_aoi",
            "7S": "conveyor_7s_aoi", "7X": "conveyor_7x_aoi",
        }[scope]

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
        self.run_outcome = QLabel()
        self.run_outcome.setWordWrap(True)
        self.run_outcome.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.run_outcome.setVisible(False)
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
        layout.addWidget(self.run_outcome)
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
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        self.result_stack = QStackedWidget()
        scroll = QScrollArea()
        scroll.setObjectName("resultOverviewScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        scroll.setWidget(content)
        title = QLabel("分析结果概览")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        self.result_outcome = QLabel()
        self.result_outcome.setWordWrap(True)
        self.result_outcome.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.result_outcome.setVisible(False)
        layout.addWidget(self.result_outcome)
        filters = QGridLayout()
        filters.setHorizontalSpacing(10)
        filters.setVerticalSpacing(8)
        self.scope_filter = QComboBox()
        self.scope_filter.addItem("全部范围")
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
        self.evidence_mode = QComboBox()
        for caption, key in (
            ("全部规律", "all"), ("周期规律", "periodic"), ("连续异常", "burst"),
            ("代码规律", "code"), ("水平轨迹", "trajectory"),
            ("缺陷共现", "cooccurrence"), ("序列关系", "transition"),
            ("其他空间规律", "other"),
        ):
            self.evidence_mode.addItem(caption, key)
        self.code_source_filter = QComboBox()
        self.code_source_filter.addItem("AOI与VI对照", "all")
        self.code_source_filter.addItem("仅AOI", "AOI_FAILURE")
        self.code_source_filter.addItem("仅VI", "VI_BLOCK")
        self.defect_code_filter = QComboBox()
        self.defect_code_filter.setObjectName("resultCodeFilter")
        self.defect_code_filter.setEditable(True)
        self.defect_code_filter.setInsertPolicy(QComboBox.NoInsert)
        self.defect_code_filter.setMaxVisibleItems(20)
        self.defect_code_filter.addItem("全部缺陷代码", "")
        self.defect_code_filter.lineEdit().setPlaceholderText("输入代码/名称搜索，或点击右侧按钮选择")
        self.defect_code_filter.lineEdit().setClearButtonEnabled(True)
        self.defect_code_filter.completer().setCaseSensitivity(Qt.CaseInsensitive)
        self.defect_code_filter.completer().setFilterMode(Qt.MatchContains)
        self.defect_code_filter.completer().setCompletionMode(QCompleter.PopupCompletion)
        self.code_list_button = QPushButton("选择代码 ▼")
        self.code_list_button.setObjectName("codeListButton")
        self.code_list_button.setToolTip("展开当前分析结果中的全部缺陷代码")
        self.code_list_button.setEnabled(False)
        self.code_list_button.clicked.connect(self.defect_code_filter.showPopup)
        self.code_filter_timer = QTimer(self)
        self.code_filter_timer.setSingleShot(True)
        self.code_filter_timer.setInterval(250)
        self.code_filter_timer.timeout.connect(self._apply_global_filters)
        self.scope_filter.currentTextChanged.connect(self._apply_global_filters)
        self.camera_filter.currentTextChanged.connect(self._apply_global_filters)
        self.batch_filter.currentTextChanged.connect(self._apply_global_filters)
        self.defect_filter.currentTextChanged.connect(self._apply_global_filters)
        self.order_start.valueChanged.connect(self._apply_global_filters)
        self.order_end.valueChanged.connect(self._apply_global_filters)
        self.time_start.dateTimeChanged.connect(self._apply_global_filters)
        self.time_end.dateTimeChanged.connect(self._apply_global_filters)
        self.evidence_mode.currentIndexChanged.connect(self._apply_global_filters)
        self.code_source_filter.currentIndexChanged.connect(self._apply_global_filters)
        self.defect_code_filter.currentTextChanged.connect(
            lambda _text: self.code_filter_timer.start()
        )
        self.defect_code_filter.lineEdit().returnPressed.connect(self._apply_code_filter_now)
        filter_title = QLabel("全局筛选")
        filter_title.setObjectName("sectionTitle")
        clear_codes = QPushButton("清除代码选择")
        clear_codes.setObjectName("quietButton")
        clear_codes.clicked.connect(self._clear_code_filter)
        filters.addWidget(filter_title, 0, 0)
        filters.addWidget(clear_codes, 0, 4, Qt.AlignRight)
        filters.addWidget(QLabel("缺陷代码"), 1, 0)
        filters.addWidget(self.defect_code_filter, 1, 1, 1, 3)
        filters.addWidget(self.code_list_button, 1, 4)
        filters.addWidget(QLabel("范围 / 相机 / 批次 / 类别"), 2, 0)
        filters.addWidget(self.scope_filter, 2, 1)
        filters.addWidget(self.camera_filter, 2, 2)
        filters.addWidget(self.batch_filter, 2, 3)
        filters.addWidget(self.defect_filter, 2, 4)
        filters.addWidget(QLabel("产品序号 / 时间"), 3, 0)
        filters.addWidget(self.order_start, 3, 1)
        filters.addWidget(self.order_end, 3, 2)
        filters.addWidget(self.time_start, 3, 3)
        filters.addWidget(self.time_end, 3, 4)
        filters.addWidget(QLabel("规律类型 / 代码来源"), 4, 0)
        filters.addWidget(self.evidence_mode, 4, 1)
        filters.addWidget(self.code_source_filter, 4, 2)
        for column in range(1, 5):
            filters.setColumnStretch(column, 1)
        layout.addLayout(filters)

        self.kpi_labels: dict[str, QLabel] = {}
        grid = QGridLayout()
        items = (
            ("analyzed_product_count", "产品总数", True),
            ("extracted_defect_count", "提取缺陷", True),
            ("micro_defect_count", "微小缺陷", True),
            ("local_defect_count", "局部缺陷", True),
            ("region_anomaly_count", "区域异常", True),
            ("spatial_cluster_count", "空间簇", True),
            ("code_label_conflict_count", "标签冲突", True),
            ("elapsed_seconds", "耗时（秒）", False),
            ("discovered_pattern_count", "发现规律", True),
            ("alert_count", "预警", True),
        )
        self.result_cards: dict[str, QPushButton] = {}
        for index, (key, caption, clickable) in enumerate(items):
            if clickable:
                card = QPushButton()
                card.setObjectName("resultCard")
                card.setCursor(Qt.PointingHandCursor)
                card.setAccessibleName(caption)
                card.setToolTip(f"点击查看{caption}明细")
            else:
                card = QFrame()
                card.setObjectName("kpiCard")
            card_layout = QVBoxLayout(card)
            value = QLabel("-")
            value.setObjectName("resultCardValue" if clickable else "kpiValue")
            value.setAttribute(Qt.WA_TransparentForMouseEvents)
            value.setAlignment(Qt.AlignCenter)
            text = QLabel(f"{caption}  ›" if clickable else caption)
            text.setAttribute(Qt.WA_TransparentForMouseEvents)
            text.setAlignment(Qt.AlignCenter)
            self.kpi_labels[key] = value
            card_layout.addWidget(value)
            card_layout.addWidget(text)
            grid.addWidget(card, index // 4, index % 4)
            if clickable:
                self.result_cards[key] = card
                card.clicked.connect(
                    lambda _checked=False, detail_key=key: self._show_result_details(detail_key)
                )
            if key == "discovered_pattern_count":
                self.pattern_result_card = card
            elif key == "alert_count":
                self.alert_result_card = card
        layout.addLayout(grid)
        layout.addStretch(1)

        self.result_details = ResultDetailsWidget()
        self.result_details.back_requested.connect(
            lambda: self.result_stack.setCurrentWidget(scroll)
        )
        self.result_details.record_activated.connect(self._activate_result_record)
        self.result_overview = scroll
        self.result_stack.addWidget(scroll)
        self.result_stack.addWidget(self.result_details)
        page_layout.addWidget(self.result_stack)
        self._current_result_view: ResultView | None = None
        self.tabs.addTab(page, "④ 结果概览")

    def _build_relationship_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        title = QLabel("缺陷原因分析")
        title.setObjectName("pageTitle")
        warning = QLabel(
            "统计关联不等于因果关系。软件区分已观测事实、统计相关和候选机理；"
            "Excel推断的停机与原因均需现场验证。"
        )
        warning.setObjectName("warningBanner")
        warning.setWordWrap(True)
        controls = QGridLayout()
        self.process_edit, process_row = self._path_row(
            self._saved_path("paths/process", ""), True,
            "工艺参数 (*.csv *.xlsx *.xlsm);;CSV (*.csv);;Excel (*.xlsx *.xlsm)"
        )
        self.process_row = process_row
        self.process_edit.setPlaceholderText(
            "补充包含产品标识（DMC/product_id/order_code/global_order）和数值参数的CSV或Excel"
        )
        self.relationship_source_status = QLabel("请先在“新建任务”中完成一次缺陷分析。")
        self.relationship_source_status.setWordWrap(True)
        self.relationship_analyze = QPushButton("分析缺陷原因")
        self.relationship_analyze.setObjectName("primaryButton")
        self.relationship_analyze.setEnabled(False)
        self.relationship_analyze.clicked.connect(self._analyze_process_parameters)
        self.relationship_cancel = QPushButton("取消关联分析")
        self.relationship_cancel.setEnabled(False)
        self.relationship_cancel.clicked.connect(self._cancel_relationship_analysis)
        controls.addWidget(self.relationship_source_status, 0, 0, 1, 5)
        controls.addWidget(process_row, 1, 0, 1, 5)
        controls.setColumnStretch(3, 1)
        controls.addWidget(self.relationship_analyze, 2, 4)
        controls.addWidget(self.relationship_cancel, 2, 5)
        self.relationship_summary = QLabel("请选择一个缺陷目标查看分析结论。")
        self.relationship_summary.setWordWrap(True)
        self.association_findings = AssociationFindingsWidget()
        self.association_findings.detail_requested.connect(self._show_association_finding_detail)
        self.association_findings.image_requested.connect(self._jump_from_association_finding)
        self.relationship_metrics = DataFrameTableWidget("process_metrics")
        self.relationship_bins = DataFrameTableWidget("process_bins")
        self.relationship_model = DataFrameTableWidget("process_model")
        self.relationship_nonlinear_importance = DataFrameTableWidget("process_nonlinear_importance")
        self.relationship_nonlinear = DataFrameTableWidget("process_nonlinear_effects")
        self.relationship_curves = DataFrameTableWidget("process_risk_curves")
        self.relationship_interactions = DataFrameTableWidget("process_interactions")
        self.relationship_interaction_regions = pd.DataFrame()
        self.interaction_detail = InteractionDetailWidget()
        self.relationship_validation = DataFrameTableWidget("process_model_validation")
        self.relationship_samples = DataFrameTableWidget("process_joined")
        self.cause_hypotheses = DataFrameTableWidget("cause_hypotheses")
        self.downtime_events = DataFrameTableWidget("downtime_events")
        self.cause_evidence = DataFrameTableWidget("cause_evidence")
        self.product_event_exposure = DataFrameTableWidget("product_event_exposure")
        self.code_space_widget = DataFrameTableWidget("code_space_associations")
        self.code_conflict_widget = DataFrameTableWidget("code_label_conflicts")
        self.trajectory_widget = DataFrameTableWidget("spatial_trajectories")
        self.attribution_widget = DataFrameTableWidget("station_attribution")
        for evidence_table in (
            self.code_space_widget, self.code_conflict_widget,
            self.trajectory_widget, self.attribution_widget,
        ):
            evidence_table.row_activated.connect(self._jump_from_pattern)
        tables = QTabWidget()
        tables.addTab(self.cause_hypotheses, "原因假设")
        tables.addTab(self.downtime_events, "停机时间线")
        tables.addTab(self.cause_evidence, "原因证据")
        tables.addTab(self.product_event_exposure, "产品事件暴露")
        tables.addTab(self.relationship_metrics, "统计与效应量")
        tables.addTab(self.relationship_bins, "区间缺陷率")
        tables.addTab(self.relationship_model, "模型重要性")
        tables.addTab(self.relationship_nonlinear_importance, "非线性重要性")
        tables.addTab(self.relationship_nonlinear, "非线性阈值")
        tables.addTab(self.relationship_curves, "风险曲线")
        tables.addTab(self.relationship_interactions, "参数交互")
        tables.addTab(self.interaction_detail, "交互解释")
        tables.addTab(self.relationship_validation, "模型验证")
        tables.addTab(self.relationship_samples, "关联样本")
        tables.addTab(self.code_space_widget, "代码—空间关联")
        tables.addTab(self.code_conflict_widget, "AOI—VI一致性")
        tables.addTab(self.trajectory_widget, "水平轨迹")
        tables.addTab(self.attribution_widget, "工站归因证据")
        self.relationship_details = tables
        relationship_views = QTabWidget()
        relationship_views.addTab(self.association_findings, "分析发现")
        relationship_views.addTab(tables, "详细证据")
        self.relationship_views = relationship_views
        layout.addWidget(title)
        layout.addWidget(warning)
        layout.addLayout(controls)
        layout.addWidget(self.relationship_summary)
        layout.addWidget(relationship_views, 1)
        self.tabs.addTab(page, "⑤ 缺陷原因与关联分析")
        self._update_relationship_source_controls()

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

    def _analysis_mode_changed(self, *_args) -> None:
        process_only = self.analysis_filter_mode.currentData() == "process_only"
        for key, check in self.analysis_module_checks.items():
            if process_only:
                check.setChecked(key == "process_relationships")
                check.setEnabled(key == "process_relationships")
            else:
                check.setEnabled(
                    key != "process_relationships" or not self._inspected_parameters.empty
                )
        self.code_selection_list.setEnabled(
            self.analysis_filter_mode.currentData() != "all"
        )
        self._update_analysis_selection_preview()

    def _filter_target_code_list(self, text: str) -> None:
        needle = text.strip().lower()
        enabled_sources = {
            source for source, check in self.code_source_checks.items() if check.isChecked()
        }
        enabled_scopes = {
            scope for scope, check in self.analysis_scope_checks.items() if check.isChecked()
        }
        for index in range(self.code_selection_list.count()):
            item = self.code_selection_list.item(index)
            data = item.data(Qt.UserRole) or {}
            item_scopes = set(map(str, data.get("analysis_scopes", ())))
            item.setHidden(
                (needle and needle not in item.text().lower())
                or str(data.get("source_type")) not in enabled_sources
                or not (item_scopes & enabled_scopes)
            )

    def _selected_target_codes(self) -> tuple[str, ...]:
        values = []
        enabled_sources = {
            source for source, check in self.code_source_checks.items() if check.isChecked()
        }
        enabled_scopes = {
            scope for scope, check in self.analysis_scope_checks.items() if check.isChecked()
        }
        for index in range(self.code_selection_list.count()):
            item = self.code_selection_list.item(index)
            if item.checkState() == Qt.Checked:
                data = item.data(Qt.UserRole) or {}
                if (
                    str(data.get("source_type")) not in enabled_sources
                    or not (set(map(str, data.get("analysis_scopes", ()))) & enabled_scopes)
                ):
                    continue
                values.append(code_target_key(data.get("source_type"), data.get("canonical_code")))
        return tuple(dict.fromkeys(values))

    def _selected_process_parameters(self) -> tuple[str, ...]:
        if self.parameter_mode_combo.currentData() == "all":
            return ()
        return tuple(
            self.parameter_selection_list.item(index).text()
            for index in range(self.parameter_selection_list.count())
            if self.parameter_selection_list.item(index).checkState() == Qt.Checked
        )

    def _current_analysis_selection(self) -> AnalysisSelection:
        return AnalysisSelection(
            mode=str(self.analysis_filter_mode.currentData()),
            scopes=tuple(
                scope for scope, check in self.analysis_scope_checks.items() if check.isChecked()
            ),
            code_sources=tuple(
                source for source, check in self.code_source_checks.items() if check.isChecked()
            ),
            defect_codes=self._selected_target_codes(),
            modules=tuple(
                module for module, check in self.analysis_module_checks.items() if check.isChecked()
            ),
            process_parameters=self._selected_process_parameters(),
        )

    def _update_analysis_selection_preview(self, *_args) -> None:
        if not hasattr(self, "analysis_selection_preview"):
            return
        selection = self._current_analysis_selection()
        scope_products = self.loaded_products
        if not scope_products.empty and "analysis_scope" in scope_products:
            scope_products = scope_products[
                scope_products["analysis_scope"].astype(str).isin(selection.scopes)
            ]
        total = (
            len(scope_products)
            if not scope_products.empty and "dmc_raw" in scope_products else 0
        )
        if total == 0 and not self._inspected_all_codes.empty:
            population = self._inspected_all_codes.loc[
                self._inspected_all_codes["analysis_scope"].astype(str).isin(selection.scopes),
                ["analysis_scope", "dmc_raw"],
            ].astype(str).drop_duplicates()
            total = len(population)
        counts = []
        for target in selection.defect_codes:
            source, _, code = target.partition(":")
            rows = self._inspected_codes[
                self._inspected_codes.get("source_type", pd.Series(dtype=str)).astype(str).eq(source)
                & self._inspected_codes.get("canonical_code", pd.Series(dtype=str)).astype(str).eq(code)
                & self._inspected_codes.get("analysis_scope", pd.Series(dtype=str)).astype(str).isin(selection.scopes)
            ] if not self._inspected_codes.empty else pd.DataFrame()
            target_count = (
                len(rows[["analysis_scope", "dmc_raw"]].astype(str).drop_duplicates())
                if not rows.empty else 0
            )
            counts.append(f"{target}={target_count}件")
        parameter_count = (
            len(selection.process_parameters)
            if selection.process_parameters else max(0, len(self._inspected_parameters.columns) - 1)
        )
        needs_images = bool({"image_patterns", "code_image"} & set(selection.modules))
        detail = "、".join(counts) if counts else "全部可用缺陷"
        self.analysis_selection_preview.setText(
            f"总体约{total}个产品；目标：{detail}；工艺参数{parameter_count}个；"
            f"{'需要图片' if needs_images else '无需图片'}。"
        )
        self._filter_target_code_list(self.code_search_edit.text())

    def _populate_analysis_target_filters(self, valid: bool) -> None:
        preserve = self.analysis_target_group.isEnabled()
        previous_codes = set(self._selected_target_codes()) if preserve else set()
        previous_parameters = set(self._selected_process_parameters()) if preserve else set()
        previous_parameter_mode = self.parameter_mode_combo.currentData()
        previous_scopes = {
            scope for scope, check in self.analysis_scope_checks.items() if check.isChecked()
        }
        previous_sources = {
            source for source, check in self.code_source_checks.items() if check.isChecked()
        }
        previous_modules = {
            module for module, check in self.analysis_module_checks.items() if check.isChecked()
        }
        self._inspected_codes = pd.DataFrame()
        self._inspected_all_codes = pd.DataFrame()
        self._inspected_parameters = pd.DataFrame()
        if self.station_workbook is not None:
            catalog = load_defect_catalog(
                self.project_root / "config/defect_code_catalog.csv",
                Path(self.catalog_edit.text().strip()) if self.catalog_edit.text().strip() else None,
            )
            normalized = normalize_defect_codes(self.station_workbook.events, catalog)
            self._inspected_all_codes = normalized.copy()
            self._inspected_codes = normalized[
                normalized["code_status"].isin(["defect", "state_code_conflict"])
            ].copy()
            self._inspected_parameters = process_parameter_frame(self.station_workbook)
        mode_scopes = (
            ("5S", "5X", "7S", "7X") if self.task_mode_combo.currentData() == "full_process"
            else (str(self.scope_combo.currentData()),)
        )
        available_scopes = set(
            self._inspected_codes.get("analysis_scope", pd.Series(dtype=str)).dropna().astype(str)
        ) | set(
            self.loaded_products.get("analysis_scope", pd.Series(dtype=str)).dropna().astype(str)
        ) | set(
            self.loaded_products.get("camera", pd.Series(dtype=str)).dropna().astype(str).str.upper()
        )
        for scope, check in self.analysis_scope_checks.items():
            enabled = scope in mode_scopes and (not available_scopes or scope in available_scopes)
            check.setEnabled(enabled)
            check.setChecked(enabled and (not preserve or scope in previous_scopes))
        for source, check in self.code_source_checks.items():
            check.setChecked(not preserve or source in previous_sources)
        self.code_selection_list.blockSignals(True)
        self.code_selection_list.clear()
        if not self._inspected_codes.empty:
            grouped = self._inspected_codes.groupby(
                ["source_type", "canonical_code"], dropna=False,
            )
            for (source, code), rows in grouped:
                scopes = tuple(sorted(rows["analysis_scope"].dropna().astype(str).unique()))
                names = " / ".join(dict.fromkeys(
                    value for value in rows["defect_name"].dropna().astype(str) if value
                )) or "未命名缺陷"
                item = QListWidgetItem(
                    f"{code}｜{names}｜{'AOI' if source == 'AOI_FAILURE' else 'VI'}｜"
                    f"{'/'.join(scopes)}｜{rows['dmc_raw'].astype(str).nunique()}件"
                )
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.Checked if code_target_key(source, code) in previous_codes else Qt.Unchecked
                )
                item.setData(Qt.UserRole, {
                    "analysis_scopes": scopes, "source_type": str(source),
                    "canonical_code": str(code),
                })
                self.code_selection_list.addItem(item)
        self.code_selection_list.blockSignals(False)
        self.parameter_selection_list.blockSignals(True)
        self.parameter_selection_list.clear()
        for column in self._inspected_parameters.select_dtypes(include="number").columns:
            item = QListWidgetItem(str(column)); item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(
                Qt.Checked if not preserve or str(column) in previous_parameters else Qt.Unchecked
            )
            self.parameter_selection_list.addItem(item)
        self.parameter_selection_list.blockSignals(False)
        self.parameter_mode_combo.setCurrentIndex(
            max(0, self.parameter_mode_combo.findData(previous_parameter_mode))
        )
        has_parameters = self.parameter_selection_list.count() > 0
        self.analysis_module_checks["process_relationships"].setEnabled(has_parameters)
        self.analysis_module_checks["process_relationships"].setChecked(has_parameters)
        has_images = not self.analysis_products.empty
        for module in ("image_patterns", "code_image"):
            self.analysis_module_checks[module].setChecked(has_images)
            self.analysis_module_checks[module].setEnabled(has_images)
        self.analysis_module_checks["code_patterns"].setChecked(not self._inspected_codes.empty)
        self.analysis_module_checks["code_patterns"].setEnabled(not self._inspected_codes.empty)
        if preserve:
            for module, check in self.analysis_module_checks.items():
                if check.isEnabled():
                    check.setChecked(module in previous_modules)
        self.analysis_target_group.setEnabled(valid)
        self._analysis_mode_changed()

    def _inspect_products(self) -> bool:
        try:
            legacy_text = self.products_edit.text().strip()
            if legacy_text:
                self.station_workbook = None
                path = Path(legacy_text)
                if not path.is_file():
                    raise FileNotFoundError(f"旧版产品清单不存在：{path}")
                frame = pd.read_csv(path)
                report = validate_products(frame)
                source = "a_image_path" if "a_image_path" in frame else "v_image_path"
                image_root = Path(self.image_root_edit.text().strip()) if self.image_root_edit.text().strip() else None
                missing: list[str] = []
                if report.is_valid:
                    for row in frame.itertuples(index=False):
                        for column in (source, "e_image_path"):
                            resolved = resolve_image_path(str(getattr(row, column)), self.project_root, image_root, path)
                            if not resolved.is_file():
                                missing.append(str(resolved))
                                if len(missing) >= 10:
                                    break
                        if len(missing) >= 10:
                            break
                if missing:
                    report.errors.append("图片路径不存在（最多列出10项）：" + "；".join(missing))
                self.loaded_products = frame
                self.analysis_products = frame
                self._station_issues = pd.DataFrame()
            else:
                excel_text = self.source_excel_edit.text().strip()
                mode = str(self.task_mode_combo.currentData())
                scopes = (
                    ("5S", "5X", "7S", "7X") if mode == "full_process"
                    else (str(self.scope_combo.currentData()),)
                )
                image_texts = {scope: self.scope_image_edits[scope].text().strip() for scope in scopes}
                if not excel_text and not any(image_texts.values()):
                    raise ValueError("请至少选择Excel工作簿或图片根目录")
                self.station_workbook = None
                station_warnings: list[str] = []
                if excel_text:
                    excel_path = Path(excel_text)
                    if not excel_path.is_file():
                        raise FileNotFoundError(f"Excel工作簿不存在：{excel_path}")
                    try:
                        station_book = load_station_workbook(excel_path, self.station_catalog)
                    except (ValueError, KeyError) as exc:
                        raise ValueError(
                            "不支持该Excel格式，请导入包含各工站Ident No.、Test Date和工站字段的"
                            "完整MES工站工作簿。"
                        ) from exc
                    recognized = station_book.events.get(
                        "station_id", pd.Series(dtype=str)
                    ).fillna("").astype(str).str.strip().ne("")
                    if station_book.events.empty or not recognized.any():
                        raise ValueError(
                            "不支持该Excel格式，请导入包含各工站Ident No.、Test Date和工站字段的"
                            "完整MES工站工作簿。"
                        )
                    self.station_workbook = station_book
                    station_warnings.extend(station_book.warnings)
                product_parts: list[pd.DataFrame] = []
                issue_parts: list[pd.DataFrame] = []
                scanned_count = 0
                scope_metrics: dict[str, Any] = {}
                for scope in scopes:
                    station = self.station_catalog.station(self._scope_station_id(scope))
                    selected_events = (
                        self.station_workbook.station_events(station.id)
                        if self.station_workbook is not None else pd.DataFrame()
                    )
                    excel_dmcs = (
                        selected_events["dmc_raw"].dropna().astype(str).str.strip().tolist()
                        if not selected_events.empty else []
                    )
                    image_text = image_texts[scope]
                    if not image_text:
                        station_warnings.append(f"未提供{scope}图片目录；保留Excel履历并跳过该范围图片分析")
                        scope_metrics[f"{scope}图片"] = "未提供"
                        continue
                    indexed = build_image_product_index(
                        Path(image_text), station, self.station_catalog, excel_dmcs
                    )
                    frame = indexed.products
                    if self.station_workbook is not None:
                        frame = enrich_products_with_station_truth(frame, self.station_workbook, station.id)
                    frame["analysis_scope"] = scope
                    frame["scope_order"] = frame["global_order"]
                    product_parts.append(frame)
                    issue_parts.append(indexed.issues.assign(分析范围=scope))
                    scanned_count += indexed.scanned_file_count
                    scope_metrics[f"{scope}扫描图片"] = indexed.scanned_file_count
                    scope_metrics[f"{scope}有效图对"] = int(frame["has_primary_pair"].eq(True).sum())
                    scope_metrics[f"{scope}真值匹配"] = int(frame.get("truth_match", pd.Series(dtype=str)).eq("matched").sum())
                self.loaded_products = pd.concat(product_parts, ignore_index=True) if product_parts else pd.DataFrame()
                if not self.loaded_products.empty:
                    self.loaded_products["global_order"] = range(1, len(self.loaded_products) + 1)
                    self.loaded_products["task_order"] = self.loaded_products["global_order"]
                self._station_issues = pd.concat(issue_parts, ignore_index=True) if issue_parts else pd.DataFrame()
                self.analysis_products = self.loaded_products[
                    self.loaded_products.get("has_primary_pair", False).eq(True)
                ].copy() if not self.loaded_products.empty else pd.DataFrame()
                if not self.analysis_products.empty:
                    self.analysis_products["global_order"] = range(1, len(self.analysis_products) + 1)
                    self.analysis_products["task_order"] = self.analysis_products["global_order"]
                report = DataQualityReport("全流程任务", len(self.loaded_products), len(self.loaded_products.columns))
                report.warnings.extend(station_warnings)
                if self.station_workbook is not None:
                    report.metrics.update({
                        "全工站履历事件": len(self.station_workbook.events),
                        "工艺/检测参数值": len(self.station_workbook.parameters),
                        "当前工站真值匹配": int(
                            self.loaded_products.get("truth_match", pd.Series(dtype=str)).eq("matched").sum()
                        ),
                    })
                error_issues = int(self._station_issues.get("级别", pd.Series(dtype=str)).eq("错误").sum())
                report.metrics.update({
                    "任务模式": "全流程" if mode == "full_process" else f"单AOI {scopes[0]}",
                    "Excel Ident No.": len(self.station_workbook.products) if self.station_workbook is not None else 0,
                    "扫描图片文件": scanned_count,
                    "建立产品索引": len(self.loaded_products),
                    "可运行主图对": len(self.analysis_products),
                    "缺图/异常项": len(self._station_issues),
                    **scope_metrics,
                })
                if error_issues:
                    report.errors.append(f"发现 {error_issues} 个需人工解决的重复视图")
                if any(image_texts.values()) and self.analysis_products.empty:
                    report.warnings.append("未找到完整主图对；可继续缺陷代码分析，不运行图片算法")
            quality_frame = report.to_frame()
            if not self._station_issues.empty:
                issue_frame = pd.DataFrame({
                    "级别": self._station_issues["级别"],
                    "项目": "DMC " + self._station_issues["DMC"].fillna("").astype(str),
                    "结果": self._station_issues["问题"] + self._station_issues["文件"].map(lambda value: f"；{value}" if value else ""),
                })
                quality_frame = pd.concat([quality_frame, issue_frame], ignore_index=True)
            self.quality_table.set_frame(quality_frame)
            self.quality_summary.setText(
                f"{'检查通过' if report.is_valid else '检查未通过'}："
                f"{report.row_count} 行、{report.column_count} 列；"
                f"{len(report.errors)} 个错误、{len(report.warnings)} 个警告。"
            )
            object_name = "successBanner" if report.is_valid else "errorBanner"
            self.quality_summary.setObjectName(object_name)
            self.quality_summary.style().unpolish(self.quality_summary)
            self.quality_summary.style().polish(self.quality_summary)
            self._populate_analysis_target_filters(report.is_valid)
            self._update_relationship_source_controls()
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
            self.current_result = None
            self._update_relationship_source_controls()
            excel_path = Path(self.source_excel_edit.text().strip()) if self.source_excel_edit.text().strip() else None
            selection = self._current_analysis_selection()
            if not selection.scopes:
                raise ValueError("请至少选择一个工站范围")
            if not selection.code_sources:
                raise ValueError("请至少选择一个缺陷代码来源")
            if not selection.modules:
                raise ValueError("请至少选择一项分析内容")
            if selection.mode in {"selected_codes", "process_only"} and not selection.defect_codes:
                raise ValueError("当前分析方式要求至少选择一个缺陷代码")
            if (
                "process_relationships" in selection.modules
                and self._inspected_parameters.empty
            ):
                raise ValueError("当前Excel没有可用于关联分析的WP1-WP5数值工艺参数")
            if (
                "process_relationships" in selection.modules
                and self.parameter_mode_combo.currentData() == "selected"
                and not selection.process_parameters
            ):
                raise ValueError("请选择至少一个工艺参数，或改为自动分析全部数值参数")
            image_modules = {"image_patterns", "code_image"} & set(selection.modules)
            scoped_analysis_products = self.analysis_products
            if not scoped_analysis_products.empty and "analysis_scope" in scoped_analysis_products:
                scoped_analysis_products = scoped_analysis_products[
                    scoped_analysis_products["analysis_scope"].astype(str).isin(selection.scopes)
                ].copy()
            if image_modules:
                scope_column = (
                    "analysis_scope" if "analysis_scope" in scoped_analysis_products else "camera"
                )
                available = set(scoped_analysis_products[scope_column].astype(str).str.upper())
                missing_scopes = sorted(set(selection.scopes) - available)
                if missing_scopes:
                    raise ValueError(
                        "图片分析缺少有效主图对的工站：" + "、".join(missing_scopes)
                    )
            pure_excel = not image_modules
            legacy_path = Path(self.products_edit.text().strip()) if self.products_edit.text().strip() else None
            source_files = (excel_path,) if excel_path is not None else ()
            catalog_path = (
                Path(self.catalog_edit.text().strip())
                if hasattr(self, "catalog_edit") and self.catalog_edit.text().strip() else None
            )
            if pure_excel:
                if self.station_workbook is None or excel_path is None:
                    raise ValueError("纯Excel代码规律或工艺关联需要可识别的全工站Excel工作簿")
                request = ExcelTargetAnalysisRequest(
                    config_path=config_path, output_parent=output,
                    task_name=self.task_edit.text(),
                    station_events_frame=self.station_workbook.events,
                    process_parameters_frame=self._inspected_parameters,
                    selection=selection, source_files=source_files,
                    station_parameters_frame=self.station_workbook.parameters,
                    config_snapshot=dict(self.config_snapshot),
                    defect_catalog_path=catalog_path,
                )
                self._auto_relationship_pending = False
            else:
                request = AnalysisRequest(
                    legacy_path, config_path, output, self.task_edit.text(),
                    Path(self.image_root_edit.text()) if self.image_root_edit.text().strip() else None,
                    dict(self.config_snapshot),
                    products_frame=None if legacy_path is not None else scoped_analysis_products,
                    source_files=source_files,
                    source_index_frame=None if legacy_path is not None else self.loaded_products,
                    source_issues_frame=None if self._station_issues.empty else self._station_issues,
                    analysis_mode=str(self.task_mode_combo.currentData()),
                    enabled_scopes=selection.scopes,
                    image_roots={
                        scope: Path(edit.text().strip()) for scope, edit in self.scope_image_edits.items()
                        if edit.text().strip()
                    },
                    station_events_frame=(
                        self.station_workbook.events if self.station_workbook is not None else None
                    ),
                    defect_catalog_path=catalog_path,
                    selection=selection,
                )
                self._auto_relationship_pending = (
                    excel_path is not None and "process_relationships" in selection.modules
                )
        except Exception as exc:
            self._show_error("无法启动分析", exc)
            return
        self.live_alerts.clear()
        self.run_log.clear()
        self.run_outcome.setVisible(False)
        self.result_outcome.setVisible(False)
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
        self._cooccurrence_frame = cooccurrence
        self._transition_frame = transitions
        self.code_space_widget.set_frame(result.frames.get("code_space", pd.DataFrame()))
        self.code_conflict_widget.set_frame(result.frames.get("code_conflicts", pd.DataFrame()))
        self.trajectory_widget.set_frame(result.frames.get("trajectories", pd.DataFrame()))
        self.attribution_widget.set_frame(result.frames.get("station_attribution", pd.DataFrame()))
        normalized_codes = result.frames.get("normalized_codes", pd.DataFrame())
        self._populate_defect_code_filter(normalized_codes)
        selected_targets = result.summary.get("analysis_selection", {}).get("defect_codes", [])
        if selected_targets:
            first_code = str(selected_targets[0]).partition(":")[2]
            index = next((
                item for item in range(self.defect_code_filter.count())
                if str(self.defect_code_filter.itemData(item) or "") == first_code
            ), -1)
            if index >= 0:
                self.defect_code_filter.setCurrentIndex(index)
        cooccurrence.to_csv(
            result.output_dir / "defect_cooccurrence.csv", index=False, encoding="utf-8-sig"
        )
        transitions.to_csv(
            result.output_dir / "defect_transitions.csv", index=False, encoding="utf-8-sig"
        )
        for scope in result.summary.get("enabled_scopes", []):
            scope_dir = result.output_dir / "scopes" / str(scope)
            scope_dir.mkdir(parents=True, exist_ok=True)
            scoped_cooccurrence = (
                cooccurrence[cooccurrence["analysis_scope"].astype(str).eq(str(scope))]
                if "analysis_scope" in cooccurrence else cooccurrence
            )
            scoped_transitions = (
                transitions[transitions["analysis_scope"].astype(str).eq(str(scope))]
                if "analysis_scope" in transitions else transitions
            )
            scoped_cooccurrence.to_csv(
                scope_dir / "defect_cooccurrence.csv", index=False, encoding="utf-8-sig"
            )
            scoped_transitions.to_csv(
                scope_dir / "defect_transitions.csv", index=False, encoding="utf-8-sig"
            )
        self._populate_filters(result.frames["products"], result.frames["extracted"])
        self._apply_global_filters()
        self.statusBar().showMessage(f"分析完成：{result.output_dir}")
        self.workbench.header.set_run_state("分析完成", "success")
        self._set_outcome_banner(
            self.result_outcome,
            (
                f"✓ 纯Excel目标分析已完成（未运行图片分析）　结果已保存至：{result.output_dir}"
                if not result.summary.get("image_analysis_executed", True)
                else f"✓ 分析任务已完成　结果已保存至：{result.output_dir}"
            ),
            "success",
        )
        if not result.summary.get("image_analysis_executed", True):
            self._show_process_result_frames(result)
        self._refresh_unified_findings(result.frames.get("association_findings", pd.DataFrame()))
        self._update_relationship_source_controls()
        self.tabs.setCurrentIndex(3)
        self._maybe_auto_relationship()

    def _show_process_result_frames(self, result: AnalysisResult) -> None:
        self.cause_hypotheses.set_frame(result.frames.get("cause_hypotheses", pd.DataFrame()))
        self.downtime_events.set_frame(result.frames.get("downtime_events", pd.DataFrame()))
        self.cause_evidence.set_frame(result.frames.get("cause_evidence", pd.DataFrame()))
        self.product_event_exposure.set_frame(result.frames.get("product_event_exposure", pd.DataFrame()))
        self.relationship_metrics.set_frame(result.frames.get("process_metrics", pd.DataFrame()))
        self.relationship_bins.set_frame(result.frames.get("process_bins", pd.DataFrame()))
        self.relationship_model.set_frame(result.frames.get("process_models", pd.DataFrame()))
        self.relationship_nonlinear_importance.set_frame(result.frames.get("process_nonlinear_importance", pd.DataFrame()))
        self.relationship_nonlinear.set_frame(result.frames.get("process_nonlinear_effects", pd.DataFrame()))
        self.relationship_curves.set_frame(result.frames.get("process_risk_curves", pd.DataFrame()))
        self.relationship_interactions.set_frame(result.frames.get("process_interactions", pd.DataFrame()))
        self.relationship_interaction_regions = result.frames.get(
            "process_interaction_regions", pd.DataFrame()
        ).copy()
        self.relationship_validation.set_frame(result.frames.get("process_validation", pd.DataFrame()))
        self.relationship_samples.set_frame(result.frames.get("process_joined", pd.DataFrame()))
        cause_summaries = result.summary.get("cause_analysis_targets", [])
        summaries = result.summary.get("relationship_targets", [])
        if cause_summaries:
            self.relationship_summary.setText(
                f"已完成{len(cause_summaries)}个缺陷目标的原因初步分析；"
                "请选择一个缺陷查看结论和全部证据。"
            )
        elif summaries:
            self.relationship_summary.setText(
                f"已完成{len(summaries)}个缺陷目标的工艺参数关联；请逐个切换缺陷查看。"
            )
        else:
            self.relationship_summary.setText("本任务未生成工艺参数关联结果。")
        joined = result.frames.get("process_joined", pd.DataFrame())
        if not joined.empty:
            self.review.set_process_data(joined)

    def _refresh_unified_findings(self, process_findings: pd.DataFrame | None = None) -> None:
        if self.current_result is None:
            return
        frames = self.current_result.frames
        findings = build_unified_findings(
            process_findings=process_findings,
            code_space=frames.get("code_space", pd.DataFrame()),
            cooccurrence=getattr(self, "_cooccurrence_frame", pd.DataFrame()),
            transitions=getattr(self, "_transition_frame", pd.DataFrame()),
            trajectories=frames.get("trajectories", pd.DataFrame()),
            attribution=frames.get("station_attribution", pd.DataFrame()),
            conflicts=frames.get("code_conflicts", pd.DataFrame()),
            product_count=len(frames.get("products", pd.DataFrame())),
        )
        frames["association_findings"] = findings
        selection = self.current_result.summary.get("analysis_selection", {})
        preferred = next(iter(selection.get("defect_codes", []) or []), "")
        self.association_findings.set_findings(
            findings,
            cause_summaries=self.current_result.summary.get("cause_analysis_targets", []),
            preferred_target=str(preferred),
        )
        output = self.current_result.output_dir
        findings.to_csv(output / "association_findings.csv", index=False, encoding="utf-8-sig")
        records = findings.astype(object).where(pd.notna(findings), None).to_dict("records")
        (output / "association_findings.json").write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.current_result.summary["top_association_findings"] = records[:10]
        summary_path = output / "analysis_summary.json"
        if summary_path.is_file():
            summary_path.write_text(
                json.dumps(self.current_result.summary, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    def _show_association_finding_detail(self, finding: pd.Series) -> None:
        detail_type = str(finding.get("detail_type", ""))
        if detail_type == "cause_evidence" and self.current_result is not None:
            exposure = self.current_result.frames.get("product_event_exposure", pd.DataFrame())
            details = select_product_exposure_details(exposure, finding)
            if details.empty:
                self.statusBar().showMessage("该发现没有可定位的产品明细。", 8000)
                return
            code = str(finding.get("canonical_code", "目标"))
            columns = [
                "comparison_group", "matches_finding", "dmc_raw", "is_target_defect",
                "source_event_time", "target_time", "route_coverage_status",
                "route_match_reason", "downtime_ids", "downtime_segments",
                "longest_downtime_seconds", "nearest_restart_seconds",
                "nearest_restart_rank", "restart_bucket", "material_or_batch_change",
                "speed_anomaly", "tension_anomaly", "static_anomaly",
            ]
            display = details[[name for name in columns if name in details]].rename(columns={
                "comparison_group": "比较分组", "matches_finding": "符合当前发现条件",
                "dmc_raw": "DMC", "is_target_defect": f"是否{code}缺陷",
                "source_event_time": "缺陷检出时间", "target_time": "事件归因截止时间",
                "route_coverage_status": "当前窗口路径覆盖状态",
                "route_match_reason": "追溯状态说明", "downtime_ids": "疑似停机编号",
                "downtime_segments": "停机所在WP区间",
                "longest_downtime_seconds": "最长停机秒数",
                "nearest_restart_seconds": "距最近复产秒数",
                "nearest_restart_rank": "复产后产品序号", "restart_bucket": "复产窗口",
                "material_or_batch_change": "换料或换批", "speed_anomaly": "速度异常",
                "tension_anomaly": "张力异常", "static_anomaly": "静电异常",
            })
            self.product_event_exposure.set_frame(display)
            self.product_event_exposure.search.clear()
            self.relationship_views.setCurrentIndex(1)
            self.relationship_details.setCurrentWidget(self.product_event_exposure)
            exposed = int(details["matches_finding"].sum())
            self.statusBar().showMessage(
                f"已显示{len(details)}件当前数据窗口可追溯产品："
                f"暴露组{exposed}件，未满足条件组{len(details) - exposed}件。",
                8000,
            )
            return
        if detail_type == "cause_hypothesis" and self.current_result is not None:
            hypotheses = self.current_result.frames.get("cause_hypotheses", pd.DataFrame())
            evidence = self.current_result.frames.get("cause_evidence", pd.DataFrame())
            key = str(finding.get("detail_key", ""))
            selected = hypotheses[
                hypotheses.get("hypothesis_id", pd.Series(dtype=str)).astype(str).eq(key)
            ] if not hypotheses.empty else pd.DataFrame()
            identifiers = set()
            if not selected.empty:
                identifiers = {
                    value for value in str(selected.iloc[0].get("supporting_evidence_ids", "")).split(";")
                    if value
                }
            if identifiers and "evidence_id" in evidence:
                evidence = evidence[evidence["evidence_id"].astype(str).isin(identifiers)]
            self.cause_evidence.set_frame(evidence)
            self.cause_evidence.search.clear()
            self.relationship_views.setCurrentIndex(1)
            self.relationship_details.setCurrentWidget(self.cause_evidence)
            return
        if detail_type == "interaction":
            self.interaction_detail.set_data(
                finding,
                self.relationship_interaction_regions,
                self.relationship_samples.model.frame,
            )
            if finding.get("requested_action") == "samples":
                self.interaction_detail.show_samples()
            else:
                self.interaction_detail.show_heatmap()
            self.relationship_views.setCurrentIndex(1)
            self.relationship_details.setCurrentWidget(self.interaction_detail)
            return
        process_samples = finding.get("requested_action") == "samples" and detail_type in {
            "nonlinear_effect", "interaction"
        }
        widget = self.relationship_samples if process_samples else {
            "nonlinear_effect": self.relationship_curves,
            "interaction": self.relationship_interactions,
            "code_space": self.code_space_widget,
            "conflict": self.code_conflict_widget,
            "trajectory": self.trajectory_widget,
            "attribution": self.attribution_widget,
        }.get(detail_type, self.relationship_metrics)
        self.relationship_views.setCurrentIndex(1)
        self.relationship_details.setCurrentWidget(widget)
        key = str(finding.get("target", "")) if process_samples else str(finding.get("detail_key", ""))
        if key:
            widget.search.setText(key)

    def _jump_from_association_finding(self, finding: pd.Series) -> None:
        if self.current_result is None:
            return
        detail_type, key = str(finding.get("detail_type", "")), str(finding.get("detail_key", ""))
        frame, column = {
            "trajectory": (self.current_result.frames.get("trajectories", pd.DataFrame()), "trajectory_id"),
            "code_space": (self.current_result.frames.get("code_space", pd.DataFrame()), "spatial_id"),
            "attribution": (self.current_result.frames.get("station_attribution", pd.DataFrame()), "evidence_id"),
        }.get(detail_type, (pd.DataFrame(), ""))
        if not frame.empty and column in frame:
            match = frame[frame[column].astype(str).eq(key)]
            if not match.empty:
                self._jump_from_pattern(match.iloc[0])

    def _maybe_auto_relationship(self) -> None:
        """统一工站图片任务完成后，直接使用本任务的工艺数据关联。"""
        if self._auto_relationship_pending and self.current_result is not None and self.station_workbook is not None:
            self._auto_relationship_pending = False
            self._analyze_process_parameters()

    def _populate_filters(self, products: pd.DataFrame, defects: pd.DataFrame) -> None:
        widgets = (
            self.scope_filter, self.camera_filter, self.batch_filter, self.defect_filter,
            self.order_start, self.order_end, self.time_start, self.time_end,
        )
        for widget in widgets:
            widget.blockSignals(True)
        self.scope_filter.clear()
        self.scope_filter.addItem("全部范围")
        if "analysis_scope" in products:
            self.scope_filter.addItems(
                products["analysis_scope"].dropna().astype(str).drop_duplicates().tolist()
            )
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
            values = defects[category].dropna().astype(str)
            self.defect_filter.addItems(sorted(values[values.str.strip() != ""].unique()))
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

    def _selected_defect_codes(self) -> set[str]:
        if not hasattr(self, "defect_code_filter"):
            return set()
        text = self.defect_code_filter.currentText().strip()
        if not text or text == "全部缺陷代码":
            return set()
        index = self.defect_code_filter.currentIndex()
        if index >= 0 and self.defect_code_filter.itemText(index) == text:
            code = str(self.defect_code_filter.itemData(index) or "").strip()
        else:
            code = text.split()[0].strip()
        return {code} if code else set()

    def _populate_defect_code_filter(self, normalized_codes: pd.DataFrame) -> None:
        """Fill the searchable dropdown with unique defect codes and their names."""
        self.defect_code_filter.blockSignals(True)
        self.defect_code_filter.clear()
        self.defect_code_filter.addItem("全部缺陷代码", "")
        required = {"canonical_code", "defect_name", "code_status"}
        if not normalized_codes.empty and required.issubset(normalized_codes.columns):
            defect_codes = normalized_codes[
                normalized_codes["code_status"].isin(["defect", "state_code_conflict"])
            ].copy()
            defect_codes["canonical_code"] = defect_codes["canonical_code"].fillna("").astype(str).str.strip()
            defect_codes = defect_codes[defect_codes["canonical_code"].ne("")]
            for code, group in defect_codes.groupby("canonical_code", sort=True):
                names = [
                    name for name in group["defect_name"].fillna("").astype(str).str.strip().unique()
                    if name
                ]
                caption = str(code) + (f"  {' / '.join(names)}" if names else "")
                self.defect_code_filter.addItem(caption, str(code))
        self.defect_code_filter.setCurrentIndex(0)
        self.defect_code_filter.blockSignals(False)
        self.code_list_button.setEnabled(self.defect_code_filter.count() > 1)

    def _clear_code_filter(self) -> None:
        self.code_filter_timer.stop()
        self.defect_code_filter.blockSignals(True)
        self.defect_code_filter.setCurrentIndex(0)
        self.defect_code_filter.setEditText("全部缺陷代码")
        self.defect_code_filter.blockSignals(False)
        self._apply_global_filters()

    def _apply_code_filter_now(self) -> None:
        self.code_filter_timer.stop()
        self._apply_global_filters()

    def _apply_global_filters(self, *_args) -> None:
        if self.current_result is None:
            return
        frames = self.current_result.frames
        products = frames["products"].copy()
        scope = self.scope_filter.currentText()
        if scope and scope != "全部范围" and "analysis_scope" in products:
            products = products[products["analysis_scope"].astype(str).eq(scope)]
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
        selected_codes = self._selected_defect_codes()
        source = self.code_source_filter.currentData() if hasattr(self, "code_source_filter") else "all"
        view = build_result_view(
            frames, products, extracted, self._result_config,
            selected_codes=selected_codes,
            code_source=str(source or "all"),
            merge_selected_codes=False,
        )
        self._current_result_view = view
        selected_section = str(self.evidence_mode.currentData() or "all")
        counts = dict(view.counts)
        counts["discovered_pattern_count"] = pattern_count(view, selected_section)
        for key, value in counts.items():
            if key in self.kpi_labels:
                self.kpi_labels[key].setText(str(value))
        self.kpi_labels["elapsed_seconds"].setText(
            str(self.current_result.summary.get("elapsed_seconds", "-"))
        )
        if self.result_stack.currentWidget() is self.result_details:
            self._show_result_details(self.result_details.current_key)

        code_space = frames.get("code_space", pd.DataFrame()).copy()
        if selected_codes and not code_space.empty:
            code_space = code_space[code_space["canonical_code"].astype(str).isin(selected_codes)]
        if source != "all" and not code_space.empty:
            code_space = code_space[code_space["source_type"].astype(str).eq(str(source))]
        if scope and scope != "全部范围" and not code_space.empty:
            code_space = code_space[code_space["analysis_scope"].astype(str).eq(scope)]
        conflicts = frames.get("code_conflicts", pd.DataFrame()).copy()
        trajectories = view.sections["trajectory"].copy()
        attribution = frames.get("station_attribution", pd.DataFrame()).copy()
        if scope and scope != "全部范围":
            for frame_name, frame in (
                ("conflicts", conflicts), ("trajectories", trajectories),
                ("attribution", attribution),
            ):
                if not frame.empty and "analysis_scope" in frame:
                    filtered = frame[frame["analysis_scope"].astype(str).eq(scope)]
                    if frame_name == "conflicts":
                        conflicts = filtered
                    elif frame_name == "trajectories":
                        trajectories = filtered
                    else:
                        attribution = filtered
        self.code_space_widget.set_frame(code_space)
        self.code_conflict_widget.set_frame(conflicts)
        self.trajectory_widget.set_frame(trajectories)
        self.attribution_widget.set_frame(attribution)
        if self.current_result.summary.get("image_analysis_executed", True):
            self.review.set_data(view.products, view.extracted, self._result_config)
        if self.station_workbook is not None:
            self.review.set_station_history(
                self.station_workbook.events,
                self.station_workbook.parameters,
                self.station_workbook.package,
            )

    def _show_result_details(self, key: str) -> None:
        if self._current_result_view is None or not key:
            return
        titles = {
            "analyzed_product_count": "产品明细",
            "extracted_defect_count": "提取缺陷明细",
            "micro_defect_count": "微小缺陷明细",
            "local_defect_count": "局部缺陷明细",
            "region_anomaly_count": "区域异常明细",
            "spatial_cluster_count": "空间簇明细",
            "code_label_conflict_count": "标签冲突明细",
            "alert_count": "预警明细",
        }
        if key == "discovered_pattern_count":
            selected = str(self.evidence_mode.currentData() or "all")
            sections = self._current_result_view.sections
            if selected != "all":
                sections = {
                    name: frame if name == selected else frame.iloc[0:0].copy()
                    for name, frame in sections.items()
                }
            self.result_details.show_patterns(sections, selected)
        elif key == "alert_count":
            self.result_details.show_table(
                key, titles[key], self._current_result_view.alerts, alert_colors=True,
            )
        else:
            frame = self._current_result_view.details.get(key, pd.DataFrame())
            self.result_details.show_table(key, titles.get(key, "结果明细"), frame)
        self.result_stack.setCurrentWidget(self.result_details)

    def _show_pattern_results(self) -> None:
        """Compatibility entry point for opening embedded pattern details."""
        self._show_result_details("discovered_pattern_count")

    def _show_alert_results(self) -> None:
        """Compatibility entry point for opening embedded alert details."""
        self._show_result_details("alert_count")

    def _activate_result_record(self, key: str, record: pd.Series) -> None:
        if key == "alert_count":
            self._jump_from_alert(record)
            return
        if key in {
            "analyzed_product_count", "extracted_defect_count", "micro_defect_count",
            "local_defect_count", "region_anomaly_count", "code_label_conflict_count",
        }:
            try:
                order = int(float(record.get("global_order")))
            except (TypeError, ValueError):
                self.statusBar().showMessage("该结果没有可关联的任务图片。", 8000)
                return
            self.review.exit_pattern_review()
            self.review.jump_to(order)
            self.tabs.setCurrentWidget(self.review)
            return
        if key == "spatial_cluster_count" and self._current_result_view is not None:
            cluster_id = str(record.get("cluster_id", ""))
            detections = self._current_result_view.extracted
            matches = detections[
                detections.get("cluster_id", pd.Series(index=detections.index, dtype=str))
                .fillna("").astype(str).eq(cluster_id)
            ]
            orders = sorted(set(
                pd.to_numeric(matches.get("global_order"), errors="coerce").dropna().astype(int)
            ))
            if orders:
                linked = record.copy()
                linked["pattern_type"] = "cluster"
                linked["observed_orders"] = ";".join(map(str, orders))
                self._jump_from_pattern(linked)
            else:
                self.statusBar().showMessage("该空间簇没有当前筛选范围内的任务图片。", 8000)
            return
        self._jump_from_pattern(record)

    def _cancelled(self, result: AnalysisResult) -> None:
        self.workbench.header.set_run_state("任务已取消", "ready")
        self._set_outcome_banner(
            self.run_outcome,
            f"任务已安全取消。部分诊断结果保存在：{result.output_dir}",
            "warning",
        )
        self.tabs.setCurrentIndex(2)
        if self._close_after_cancel:
            self.close()

    def _failed(self, message: str) -> None:
        self.logger.error("分析失败：%s", message)
        self.workbench.header.set_run_state("分析失败", "error")
        self._set_outcome_banner(
            self.run_outcome,
            f"分析没有完成：{message}\n诊断日志：{self.log_path}",
            "error",
        )
        self.tabs.setCurrentIndex(2)

    @staticmethod
    def _set_outcome_banner(label: QLabel, message: str, kind: str) -> None:
        label.setObjectName(f"{kind}Banner")
        label.setText(message)
        label.setVisible(True)
        label.style().unpolish(label)
        label.style().polish(label)

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
        self.review.exit_pattern_review()
        self.review.jump_to(int(float(record["alert_at_order"])))
        self.tabs.setCurrentWidget(self.review)

    def _jump_from_pattern(self, record: pd.Series) -> None:
        if self.review.show_pattern(record):
            self.tabs.setCurrentWidget(self.review)
            return
        for key in ("global_order", "first_order", "first_task_order"):
            try:
                value = record.get(key)
                if value is not None and not pd.isna(value):
                    self.review.jump_to(int(float(value)))
                    self.tabs.setCurrentWidget(self.review)
                    return
            except (TypeError, ValueError):
                continue
        self.statusBar().showMessage("该结果没有可关联的任务图片，无法进入图片复核。", 8000)

    def _update_relationship_source_controls(self) -> None:
        """Show external parameter import only when the active task cannot supply it."""
        if not hasattr(self, "process_row"):
            return
        has_result = self.current_result is not None
        parameters = self._inspected_parameters
        if self.station_workbook is not None and parameters.empty:
            parameters = process_parameter_frame(self.station_workbook)
        parameter_columns = [
            column for column in parameters.columns
            if column != "dmc_raw" and pd.api.types.is_numeric_dtype(parameters[column])
        ] if not parameters.empty else []
        has_task_parameters = self.station_workbook is not None and bool(parameter_columns)
        needs_external = has_result and not has_task_parameters
        self.process_row.setVisible(needs_external)
        self.relationship_analyze.setEnabled(has_result and self.relationship_thread is None)
        if not has_result:
            message = "请先在“新建任务”中完成一次缺陷分析。"
        elif has_task_parameters:
            message = f"正在使用当前任务Excel中的工艺数据（{len(parameter_columns)}个数值参数）。"
        else:
            message = "当前任务没有工艺参数；如需参数关联，请补充外部CSV或Excel。"
        self.relationship_source_status.setText(message)

    def _analyze_process_parameters(self) -> None:
        if self.current_result is None:
            QMessageBox.warning(self, "尚无缺陷结果", "请先完成一次缺陷分析。")
            return
        try:
            parameters = pd.DataFrame()
            if self.station_workbook is not None:
                parameters = process_parameter_frame(self.station_workbook)
            if parameters.empty or len(parameters.columns) == 1:
                path = Path(self.process_edit.text().strip())
                if not path.is_file():
                    raise FileNotFoundError("当前任务缺少工艺参数，请选择需要补充的CSV或Excel文件")
                if path.suffix.lower() in {".xlsx", ".xlsm"}:
                    workbook_data = load_excel_workbook(path)
                    parameters = excel_relationship_frame(workbook_data)
                else:
                    parameters = pd.read_csv(path)
            products_frame = self.current_result.frames["products"]
            extracted_frame = self.current_result.frames["extracted"]
            selection_data = self.current_result.summary.get("analysis_selection", {})
            selection_mode = str(selection_data.get("mode", "all"))
            selected_target_keys = set(map(str, selection_data.get("defect_codes", [])))
            selected_parameters = tuple(map(str, selection_data.get("process_parameters", [])))
            relationship_jobs: list[tuple] = []
            scopes = (
                products_frame["analysis_scope"].dropna().astype(str).drop_duplicates().tolist()
                if "analysis_scope" in products_frame else ["全部"]
            )
            for scope in scopes:
                scope_products = (
                    products_frame[products_frame["analysis_scope"].astype(str).eq(scope)]
                    if "analysis_scope" in products_frame else products_frame
                )
                scope_defects = (
                    extracted_frame[extracted_frame["analysis_scope"].astype(str).eq(scope)]
                    if "analysis_scope" in extracted_frame else extracted_frame
                )
                targets: list[tuple[str, str, str, pd.DataFrame]] = (
                    [("图片算法检出", "IMAGE", "", scope_defects)]
                    if selection_mode == "all" else []
                )
                normalized = self.current_result.frames.get("normalized_codes", pd.DataFrame())
                if not normalized.empty:
                    scope_codes = normalized[
                        normalized["analysis_scope"].astype(str).eq(str(scope))
                        & normalized["code_status"].isin(["defect", "state_code_conflict"])
                    ]
                    for (source_type, code), code_group in scope_codes.groupby(
                        ["source_type", "canonical_code"]
                    ):
                        target_key = code_target_key(source_type, code)
                        if selected_target_keys and target_key not in selected_target_keys:
                            continue
                        dmcs = set(code_group["dmc_raw"].astype(str))
                        orders = scope_products.loc[
                            scope_products["dmc_raw"].astype(str).isin(dmcs), "global_order"
                        ]
                        if len(orders) >= 4 or selection_mode != "all":
                            targets.append((
                                target_key, str(source_type), str(code),
                                pd.DataFrame({"global_order": orders, "component_area": 1}),
                            ))
                if selection_mode == "all" and "trajectory_id" in scope_defects:
                    for trajectory_id, trajectory_group in scope_defects[
                        scope_defects["trajectory_id"].fillna("").astype(str).str.strip().ne("")
                    ].groupby("trajectory_id"):
                        orders = trajectory_group["global_order"].drop_duplicates()
                        if len(orders) >= 4:
                            targets.append((
                                f"TRAJECTORY_{trajectory_id}", "IMAGE", "",
                                pd.DataFrame({"global_order": orders, "component_area": 1}),
                            ))
                for label, column, expected in (
                    ("AOI_NOK", "aoi_state", "NOK"), ("VI_NOK", "vi_state", "NOK"),
                ):
                    if column in scope_products:
                        orders = scope_products.loc[scope_products[column].astype(str).eq(expected), "global_order"]
                        if selection_mode == "all":
                            targets.append((label, label.split("_")[0], "", pd.DataFrame({"global_order": orders, "component_area": 1})))
                if selection_mode == "all" and "vi_defect_code" in scope_products:
                    codes = scope_products["vi_defect_code"].fillna("").astype(str).str.split(";").explode()
                    codes = codes[codes.str.strip().ne("")].str.strip()
                    for code, count in codes.value_counts().items():
                        if count < 5:
                            continue
                        has_code = scope_products["vi_defect_code"].fillna("").astype(str).str.split(";").map(
                            lambda values, target=code: target in values
                        )
                        orders = scope_products.loc[has_code, "global_order"]
                        targets.append((f"VI_CODE_{code}", "VI_BLOCK", str(code), pd.DataFrame({"global_order": orders, "component_area": 1})))
                for target_name, source_type, canonical_code, target_defects in targets:
                    relationship_jobs.append((
                        scope, target_name, source_type, canonical_code,
                        scope_products, target_defects,
                    ))
            if not relationship_jobs:
                raise ValueError("所选缺陷代码在当前图片产品总体中没有足够的可关联样本")
            self._start_relationship_worker(relationship_jobs, parameters, selected_parameters)
            return
        except Exception as exc:
            self._show_error("工艺参数关联分析失败", exc)

    def _start_relationship_worker(self, jobs: list[tuple], parameters: pd.DataFrame,
                                   selected_parameters: tuple[str, ...]) -> None:
        if self.relationship_thread is not None:
            return
        self.relationship_thread = QThread(self)
        self.relationship_worker = RelationshipWorker(
            jobs, parameters, selected_parameters
        )
        self.relationship_worker.moveToThread(self.relationship_thread)
        self.relationship_thread.started.connect(self.relationship_worker.run)
        self.relationship_worker.completed.connect(self._apply_relationship_results)
        self.relationship_worker.progress.connect(
            lambda done, total, target: self.relationship_summary.setText(
                f"正在分析 {target}（{done + 1}/{total}）…可随时取消，旧结果会保留。"
            )
        )
        self.relationship_worker.failed.connect(
            lambda message: self._show_error("工艺参数关联分析失败", RuntimeError(message))
        )
        self.relationship_worker.cancelled.connect(
            lambda: self.relationship_summary.setText("关联分析已取消，原有结果未被覆盖。")
        )
        self.relationship_worker.finished.connect(self.relationship_thread.quit)
        self.relationship_thread.finished.connect(self._relationship_thread_finished)
        self.relationship_analyze.setEnabled(False)
        self.relationship_cancel.setEnabled(True)
        self.relationship_summary.setText("正在启动非线性关联分析…")
        self.relationship_thread.start()

    def _cancel_relationship_analysis(self) -> None:
        if self.relationship_worker is not None:
            self.relationship_worker.cancel()
            self.relationship_summary.setText("正在安全取消；当前目标分析完成后停止…")

    def _relationship_thread_finished(self) -> None:
        self.relationship_cancel.setEnabled(False)
        if self.relationship_worker is not None:
            self.relationship_worker.deleteLater()
        if self.relationship_thread is not None:
            self.relationship_thread.deleteLater()
        self.relationship_worker = None
        self.relationship_thread = None
        self._update_relationship_source_controls()

    @Slot(object)
    def _apply_relationship_results(self, relationship_results: list[tuple]) -> None:
        if not relationship_results or self.current_result is None:
            return
        groups = {
            "metrics": [], "bins": [], "models": [], "nonlinear_importance": [], "nonlinear": [],
            "curves": [], "interactions": [], "interaction_regions": [],
            "validation": [], "samples": [], "findings": [],
        }
        summary_rows = []
        first_result = relationship_results[0][4]
        for scope, target, source, code, result in relationship_results:
            defect_name = ""
            normalized = self.current_result.frames.get("normalized_codes", pd.DataFrame())
            if not normalized.empty and code:
                matching_names = normalized[
                    normalized.get("analysis_scope", pd.Series(index=normalized.index, dtype=str)).astype(str).eq(str(scope))
                    & normalized.get("source_type", pd.Series(index=normalized.index, dtype=str)).astype(str).eq(str(source))
                    & normalized.get("canonical_code", pd.Series(index=normalized.index, dtype=str)).astype(str).eq(str(code))
                ].get("defect_name", pd.Series(dtype=str)).dropna().astype(str)
                matching_names = matching_names[matching_names.str.strip().ne("")]
                defect_name = matching_names.iloc[0] if not matching_names.empty else ""
            metadata = {
                "analysis_scope": scope, "source_type": source,
                "canonical_code": code, "defect_name": defect_name, "target": target,
            }
            for key, frame in (
                ("metrics", result.parameter_metrics), ("bins", result.binned_rates),
                ("models", result.model_importance),
                ("nonlinear_importance", result.nonlinear_importance),
                ("nonlinear", result.nonlinear_effects),
                ("curves", result.risk_curves),
                ("interactions", result.interactions),
                ("interaction_regions", result.interaction_regions),
                ("validation", result.model_validation),
                ("samples", result.joined),
            ):
                enriched = frame.copy()
                for name, value in reversed(tuple(metadata.items())):
                    if name in enriched:
                        enriched[name] = value
                    else:
                        enriched.insert(0, name, value)
                groups[key].append(enriched)
            groups["findings"].append(enrich_findings(result.findings, **metadata))
            summary_rows.append({**metadata, **result.summary})
        combined = {
            key: pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
            for key, parts in groups.items()
        }
        self.relationship_metrics.set_frame(combined["metrics"])
        self.relationship_bins.set_frame(combined["bins"])
        self.relationship_model.set_frame(combined["models"])
        self.relationship_nonlinear_importance.set_frame(combined["nonlinear_importance"])
        self.relationship_nonlinear.set_frame(combined["nonlinear"])
        self.relationship_curves.set_frame(combined["curves"])
        self.relationship_interactions.set_frame(combined["interactions"])
        self.relationship_interaction_regions = combined["interaction_regions"].copy()
        self.relationship_validation.set_frame(combined["validation"])
        self.relationship_samples.set_frame(combined["samples"])
        self.relationship_summary.setText(
            f"已完成{len(summary_rows)}个缺陷目标的工艺参数关联；请逐个切换缺陷查看。"
        )
        output = self.current_result.output_dir
        filenames = {
            "metrics": "process_parameter_metrics.csv",
            "bins": "process_parameter_binned_rates.csv",
            "models": "process_model_importance.csv",
            "nonlinear_importance": "process_nonlinear_importance.csv",
            "nonlinear": "process_nonlinear_effects.csv",
            "curves": "process_risk_curves.csv",
            "interactions": "process_interactions.csv",
            "interaction_regions": "process_interaction_regions.csv",
            "validation": "process_model_validation.csv",
            "samples": "process_joined.csv",
        }
        for key, filename in filenames.items():
            combined[key].to_csv(output / filename, index=False, encoding="utf-8-sig")
        self.current_result.frames.update({
            "process_metrics": combined["metrics"],
            "process_bins": combined["bins"],
            "process_models": combined["models"],
            "process_nonlinear_importance": combined["nonlinear_importance"],
            "process_nonlinear_effects": combined["nonlinear"],
            "process_risk_curves": combined["curves"],
            "process_interactions": combined["interactions"],
            "process_interaction_regions": combined["interaction_regions"],
            "process_validation": combined["validation"],
            "process_joined": combined["samples"],
            "association_findings": combined["findings"],
        })
        (output / "process_relationship_summary.json").write_text(
            json.dumps(summary_rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.review.set_process_data(first_result.joined)
        self._refresh_unified_findings(combined["findings"])
        self.relationship_views.setCurrentIndex(0)
        self.statusBar().showMessage("工艺参数关联分析完成", 5000)

    def _show_error(self, title: str, exc: Exception) -> None:
        error_id = new_error_id()
        self.logger.exception("%s error_id=%s: %s", title, error_id, exc)
        QMessageBox.critical(
            self, title, f"{exc}\n\n错误编号：{error_id}\n诊断日志：{self.log_path}"
        )

    def _save_settings(self) -> None:
        for key, value in (
            ("paths/products", self.products_edit.text()), ("paths/config", self.config_edit.text()),
            ("paths/defect_catalog", self.catalog_edit.text()),
            ("paths/source_excel", self.source_excel_edit.text()),
            ("paths/output", self.output_edit.text()), ("paths/image_root", self.image_root_edit.text()),
            ("paths/process", self.process_edit.text()), ("task/name", self.task_edit.text()),
            ("review/layout", self.review.layout_combo.currentText()),
        ):
            self.settings.setValue(key, value)
        self.settings.setValue("station/id", self.station_combo.currentData())
        self.settings.setValue("task/mode", self.task_mode_combo.currentData())
        self.settings.setValue("task/scope", self.scope_combo.currentData())
        for scope, edit in self.scope_image_edits.items():
            self.settings.setValue(f"paths/image_root_{scope.lower()}", edit.text())
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
        if self._image_dialog is not None and self._image_dialog.worker is not None:
            answer = QMessageBox.question(self, "图片下载运行中", "先安全取消图片下载再关闭？")
            if answer == QMessageBox.Yes:
                self._image_dialog.worker.cancel()
            event.ignore()
            return
        if self._mes_dialog is not None and self._mes_dialog.worker is not None:
            answer = QMessageBox.question(self, "MES下载运行中", "先安全取消MES下载再关闭？")
            if answer == QMessageBox.Yes:
                self._mes_dialog.worker.cancel()
            event.ignore()
            return
        if self.thread and self.thread.isRunning():
            answer = QMessageBox.question(self, "任务运行中", "先安全取消任务再关闭？")
            if answer == QMessageBox.Yes:
                self._close_after_cancel = True
                self._cancel()
            event.ignore()
            return
        if self.relationship_thread and self.relationship_thread.isRunning():
            answer = QMessageBox.question(self, "关联分析运行中", "先安全取消关联分析再关闭？")
            if answer == QMessageBox.Yes:
                self._cancel_relationship_analysis()
            event.ignore()
            return
        event.accept()
