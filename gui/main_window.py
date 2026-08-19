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
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDateTimeEdit, QFileDialog, QFormLayout, QFrame, QGridLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSpinBox, QStatusBar,
    QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from src.analysis_service import (
    AnalysisRequest, AnalysisResult, ProgressEvent, resolve_image_path,
)
from src.app_runtime import APP_VERSION, configure_logging, new_error_id, user_data_dir
from src.data_quality import DataQualityReport, validate_products
from src.defect_relationships import analyze_defect_relationships
from src.result_views import ResultView, build_result_view, pattern_count
from src.process_relationships import analyze_process_relationships
from .image_viewer import ImageReviewWidget
from .analysis_worker import AnalysisWorker
from .dataframe_table import DataFrameTableWidget
from .result_dialogs import AlertResultsDialog, PatternResultsDialog
from .parameter_dialog import ParameterDialog
from .workbench import ElidedLabel, LayoutProfile, WorkbenchShell, WorkbenchStack
from .excel_analysis_page import ExcelAnalysisPage
from src.excel_analysis import ExcelAnalysisResult, excel_relationship_frame, load_excel_workbook
from src.station_sources import (
    build_image_product_index,
    load_station_catalog,
    validate_selected_station,
)
from src.station_workbook import (
    StationWorkbookData, enrich_products_with_station_truth, load_station_workbook,
    process_parameter_frame,
)


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
        self.current_result: AnalysisResult | None = None
        self.current_excel_result: ExcelAnalysisResult | None = None
        self._result_config: dict[str, Any] = {}
        self.loaded_products = pd.DataFrame()
        self.analysis_products = pd.DataFrame()
        self._station_issues = pd.DataFrame()
        self.station_workbook: StationWorkbookData | None = None
        self._auto_relationship_pending = False
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
        self.source_excel_edit.setPlaceholderText("可选；用于工艺/质量分析并提供Ident No.")
        form.addRow("Excel工作簿（可选）", row)
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
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setObjectName("resultOverviewScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        scroll.setWidget(content)
        page_layout.addWidget(scroll)
        title = QLabel("分析结果概览")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
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
        self.code_list = QListWidget()
        self.code_list.setObjectName("resultCodeFilter")
        self.code_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.code_list.setMaximumHeight(76)
        self.merge_selected_codes = QCheckBox("合并所选代码为缺陷族")
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
        self.code_list.itemSelectionChanged.connect(self._apply_global_filters)
        self.merge_selected_codes.toggled.connect(self._apply_global_filters)
        filter_title = QLabel("全局筛选")
        filter_title.setObjectName("sectionTitle")
        clear_codes = QPushButton("清除代码选择")
        clear_codes.setObjectName("quietButton")
        clear_codes.clicked.connect(self.code_list.clearSelection)
        filters.addWidget(filter_title, 0, 0)
        filters.addWidget(clear_codes, 0, 4, Qt.AlignRight)
        filters.addWidget(QLabel("缺陷代码（可多选）"), 1, 0, Qt.AlignTop)
        filters.addWidget(self.code_list, 1, 1, 1, 4)
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
        filters.addWidget(self.merge_selected_codes, 4, 3, 1, 2)
        for column in range(1, 5):
            filters.setColumnStretch(column, 1)
        layout.addLayout(filters)

        self.kpi_labels: dict[str, QLabel] = {}
        grid = QGridLayout()
        items = (
            ("analyzed_product_count", "产品总数", False),
            ("extracted_defect_count", "提取缺陷", False),
            ("micro_defect_count", "微小缺陷", False),
            ("local_defect_count", "局部缺陷", False),
            ("region_anomaly_count", "区域异常", False),
            ("spatial_cluster_count", "空间簇", False),
            ("code_label_conflict_count", "标签冲突", False),
            ("elapsed_seconds", "耗时（秒）", False),
            ("discovered_pattern_count", "发现规律  ›", True),
            ("alert_count", "预警  ›", True),
        )
        for index, (key, caption, clickable) in enumerate(items):
            if clickable:
                card = QPushButton()
                card.setObjectName("resultCard")
                card.setCursor(Qt.PointingHandCursor)
                accessible_caption = caption.replace("  ›", "")
                card.setAccessibleName(accessible_caption)
                card.setToolTip(f"点击查看{accessible_caption}明细")
            else:
                card = QFrame()
                card.setObjectName("kpiCard")
            card_layout = QVBoxLayout(card)
            value = QLabel("-")
            value.setObjectName("resultCardValue" if clickable else "kpiValue")
            value.setAttribute(Qt.WA_TransparentForMouseEvents)
            value.setAlignment(Qt.AlignCenter)
            text = QLabel(caption)
            text.setAttribute(Qt.WA_TransparentForMouseEvents)
            text.setAlignment(Qt.AlignCenter)
            self.kpi_labels[key] = value
            card_layout.addWidget(value)
            card_layout.addWidget(text)
            grid.addWidget(card, index // 4, index % 4)
            if key == "discovered_pattern_count":
                self.pattern_result_card = card
                card.clicked.connect(self._show_pattern_results)
            elif key == "alert_count":
                self.alert_result_card = card
                card.clicked.connect(self._show_alert_results)
        layout.addLayout(grid)
        layout.addStretch(1)

        self.pattern_dialog = PatternResultsDialog(self)
        self.pattern_dialog.pattern_activated.connect(self._jump_from_pattern)
        self.alert_dialog = AlertResultsDialog(self)
        self.alert_dialog.alert_activated.connect(self._jump_from_alert)
        self._current_result_view: ResultView | None = None
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
        tables.addTab(self.relationship_metrics, "统计与效应量")
        tables.addTab(self.relationship_bins, "区间缺陷率")
        tables.addTab(self.relationship_model, "模型重要性")
        tables.addTab(self.code_space_widget, "代码—空间关联")
        tables.addTab(self.code_conflict_widget, "AOI—VI一致性")
        tables.addTab(self.trajectory_widget, "水平轨迹")
        tables.addTab(self.attribution_widget, "工站归因证据")
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
        self._maybe_auto_relationship()

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
            legacy_text = self.products_edit.text().strip()
            if legacy_text:
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
                excel_data = None
                self.station_workbook = None
                station_warnings: list[str] = []
                if excel_text:
                    excel_path = Path(excel_text)
                    if not excel_path.is_file():
                        raise FileNotFoundError(f"Excel工作簿不存在：{excel_path}")
                    try:
                        station_book = load_station_workbook(excel_path, self.station_catalog)
                    except (ValueError, KeyError):
                        station_book = None
                    if station_book is not None:
                        self.station_workbook = station_book
                        station_warnings.extend(station_book.warnings)
                    else:
                        station = self.station_catalog.station(self._scope_station_id(scopes[0]))
                        excel_data = load_excel_workbook(excel_path)
                        station_warnings.extend(
                            validate_selected_station(station, excel_data.query_parameters, self.station_catalog)
                        )
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
                        if not selected_events.empty else (
                            excel_data.data["dmc_raw"].dropna().astype(str).str.strip().tolist()
                            if excel_data is not None and "dmc_raw" in excel_data.data else []
                        )
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
                if excel_data is not None:
                    report.warnings.extend(excel_data.quality_report.warnings)
                    report.warnings.extend(excel_data.quality_report.errors)
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
                    report.warnings.append("未找到完整主图对；可继续Excel分析，不运行图片算法")
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
            self.current_excel_result = None
            self.use_current_excel.setEnabled(False)
            self.use_current_excel.setChecked(False)
            excel_path = Path(self.source_excel_edit.text().strip()) if self.source_excel_edit.text().strip() else None
            if (
                excel_path is not None and self.station_workbook is None
                and not (self.excel_page.thread and self.excel_page.thread.isRunning())
            ):
                station = self.station_catalog.station(str(self.station_combo.currentData()))
                self.excel_page.excel_profile = station.excel_profile
                self.excel_page.workbook_edit.setText(str(excel_path))
                self.excel_page.output_edit.setText(str(output))
                self.excel_page.task_name.setText(self.task_edit.text())
                self.excel_page.start_analysis()
            if self.analysis_products.empty:
                if excel_path is not None:
                    if self.station_workbook is not None:
                        self.statusBar().showMessage(
                            "全流程Excel已完成解析；未提供有效图片目录，本次不运行图片规律分析", 10000
                        )
                        self.tabs.setCurrentIndex(1)
                    else:
                        self.statusBar().showMessage("未找到完整主图对，已启动Excel分析", 8000)
                        self.tabs.setCurrentIndex(4)
                    return
                raise ValueError("没有可运行图片算法的完整主图对")
            legacy_path = Path(self.products_edit.text().strip()) if self.products_edit.text().strip() else None
            source_files = (excel_path,) if excel_path is not None else ()
            request = AnalysisRequest(
                legacy_path, config_path, output, self.task_edit.text(),
                Path(self.image_root_edit.text()) if self.image_root_edit.text().strip() else None,
                dict(self.config_snapshot),
                products_frame=None if legacy_path is not None else self.analysis_products,
                source_files=source_files,
                source_index_frame=None if legacy_path is not None else self.loaded_products,
                source_issues_frame=None if self._station_issues.empty else self._station_issues,
                analysis_mode=str(self.task_mode_combo.currentData()),
                enabled_scopes=tuple(self.analysis_products["analysis_scope"].drop_duplicates())
                    if "analysis_scope" in self.analysis_products else (),
                image_roots={
                    scope: Path(edit.text().strip()) for scope, edit in self.scope_image_edits.items()
                    if edit.text().strip()
                },
                station_events_frame=(
                    self.station_workbook.events if self.station_workbook is not None else None
                ),
                defect_catalog_path=(
                    Path(self.catalog_edit.text().strip())
                    if hasattr(self, "catalog_edit") and self.catalog_edit.text().strip() else None
                ),
            )
            self._auto_relationship_pending = excel_path is not None
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
        self._cooccurrence_frame = cooccurrence
        self._transition_frame = transitions
        self.code_space_widget.set_frame(result.frames.get("code_space", pd.DataFrame()))
        self.code_conflict_widget.set_frame(result.frames.get("code_conflicts", pd.DataFrame()))
        self.trajectory_widget.set_frame(result.frames.get("trajectories", pd.DataFrame()))
        self.attribution_widget.set_frame(result.frames.get("station_attribution", pd.DataFrame()))
        self.code_list.blockSignals(True)
        self.code_list.clear()
        normalized_codes = result.frames.get("normalized_codes", pd.DataFrame())
        if not normalized_codes.empty:
            defect_codes = normalized_codes[
                normalized_codes["code_status"].isin(["defect", "state_code_conflict"])
            ][["canonical_code", "defect_name"]].drop_duplicates()
            for row in defect_codes.sort_values("canonical_code").itertuples(index=False):
                caption = str(row.canonical_code)
                if str(row.defect_name).strip():
                    caption += f"  {row.defect_name}"
                self.code_list.addItem(caption)
        self.code_list.blockSignals(False)
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
        QMessageBox.information(self, "分析完成", f"结果目录：\n{result.output_dir}")
        self.tabs.setCurrentIndex(3)
        self._maybe_auto_relationship()

    def _maybe_auto_relationship(self) -> None:
        """统一工站任务的Excel和图片都完成后自动关联。"""
        if self._auto_relationship_pending and self.current_result is not None and self.station_workbook is not None:
            self._auto_relationship_pending = False
            self._analyze_process_parameters()
            return
        if (
            not self._auto_relationship_pending
            or self.current_result is None
            or self.current_excel_result is None
        ):
            return
        if int(self.current_excel_result.summary.get("parameter_count", 0)) == 0:
            self._auto_relationship_pending = False
            self.relationship_summary.setText(
                "当前为无数值工艺参数的VI类工作簿；已完成分类失效统计，"
                "不执行数值参数与图片缺陷的相关模型。"
            )
            return
        self._auto_relationship_pending = False
        self.use_current_excel.setChecked(True)
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
        selected_codes = {
            item.text().split()[0] for item in self.code_list.selectedItems()
        } if hasattr(self, "code_list") else set()
        source = self.code_source_filter.currentData() if hasattr(self, "code_source_filter") else "all"
        view = build_result_view(
            frames, products, extracted, self._result_config,
            selected_codes=selected_codes,
            code_source=str(source or "all"),
            merge_selected_codes=self.merge_selected_codes.isChecked(),
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
        self.pattern_dialog.set_sections(view.sections, selected_section)
        self.alert_dialog.set_frame(view.alerts)

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
        self.review.set_data(view.products, view.extracted, self._result_config)
        if self.station_workbook is not None:
            self.review.set_station_history(
                self.station_workbook.events,
                self.station_workbook.parameters,
                self.station_workbook.package,
            )

    def _show_pattern_results(self) -> None:
        if self._current_result_view is None:
            return
        selected = str(self.evidence_mode.currentData() or "all")
        self.pattern_dialog.set_sections(self._current_result_view.sections, selected)
        self.pattern_dialog.show()
        self.pattern_dialog.raise_()
        self.pattern_dialog.activateWindow()

    def _show_alert_results(self) -> None:
        if self._current_result_view is None:
            return
        self.alert_dialog.set_frame(self._current_result_view.alerts)
        self.alert_dialog.show()
        self.alert_dialog.raise_()
        self.alert_dialog.activateWindow()

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
        if hasattr(self, "alert_dialog"):
            self.alert_dialog.hide()
        self.review.exit_pattern_review()
        self.review.jump_to(int(float(record["alert_at_order"])))
        self.tabs.setCurrentWidget(self.review)

    def _jump_from_pattern(self, record: pd.Series) -> None:
        if hasattr(self, "pattern_dialog"):
            self.pattern_dialog.hide()
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

    def _analyze_process_parameters(self) -> None:
        if self.current_result is None:
            QMessageBox.warning(self, "尚无缺陷结果", "请先完成一次缺陷分析。")
            return
        try:
            if self.station_workbook is not None:
                parameters = process_parameter_frame(self.station_workbook)
                if parameters.empty or len(parameters.columns) == 1:
                    raise ValueError("全工站工作簿中没有可用于关联的WP1-WP5数值工艺参数")
            elif self.use_current_excel.isChecked():
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
            products_frame = self.current_result.frames["products"]
            extracted_frame = self.current_result.frames["extracted"]
            relationship_results: list[tuple[str, str, Any]] = []
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
                targets: list[tuple[str, pd.DataFrame]] = [("图片算法检出", scope_defects)]
                normalized = self.current_result.frames.get("normalized_codes", pd.DataFrame())
                if not normalized.empty:
                    scope_codes = normalized[
                        normalized["analysis_scope"].astype(str).eq(str(scope))
                        & normalized["code_status"].isin(["defect", "state_code_conflict"])
                    ]
                    for (source_type, code), code_group in scope_codes.groupby(
                        ["source_type", "canonical_code"]
                    ):
                        dmcs = set(code_group["dmc_raw"].astype(str))
                        orders = scope_products.loc[
                            scope_products["dmc_raw"].astype(str).isin(dmcs), "global_order"
                        ]
                        if len(orders) >= 4:
                            targets.append((
                                f"{source_type}_{code}",
                                pd.DataFrame({"global_order": orders, "component_area": 1}),
                            ))
                if "trajectory_id" in scope_defects:
                    for trajectory_id, trajectory_group in scope_defects[
                        scope_defects["trajectory_id"].fillna("").astype(str).str.strip().ne("")
                    ].groupby("trajectory_id"):
                        orders = trajectory_group["global_order"].drop_duplicates()
                        if len(orders) >= 4:
                            targets.append((
                                f"TRAJECTORY_{trajectory_id}",
                                pd.DataFrame({"global_order": orders, "component_area": 1}),
                            ))
                for label, column, expected in (
                    ("AOI_NOK", "aoi_state", "NOK"), ("VI_NOK", "vi_state", "NOK"),
                ):
                    if column in scope_products:
                        orders = scope_products.loc[scope_products[column].astype(str).eq(expected), "global_order"]
                        targets.append((label, pd.DataFrame({"global_order": orders, "component_area": 1})))
                if "vi_defect_code" in scope_products:
                    codes = scope_products["vi_defect_code"].fillna("").astype(str).str.split(";").explode()
                    codes = codes[codes.str.strip().ne("")].str.strip()
                    for code, count in codes.value_counts().items():
                        if count < 5:
                            continue
                        has_code = scope_products["vi_defect_code"].fillna("").astype(str).str.split(";").map(
                            lambda values, target=code: target in values
                        )
                        orders = scope_products.loc[has_code, "global_order"]
                        targets.append((f"VI_CODE_{code}", pd.DataFrame({"global_order": orders, "component_area": 1})))
                for target_name, target_defects in targets:
                    relationship_results.append((scope, target_name, analyze_process_relationships(
                        scope_products, target_defects, parameters, self.time_tolerance.value(),
                    )))
            scope, target_name, result = relationship_results[0]
            metric_parts = []
            bin_parts = []
            model_parts = []
            summary_rows = []
            for scope_name, target, item in relationship_results:
                for parts, frame in ((metric_parts, item.parameter_metrics), (bin_parts, item.binned_rates), (model_parts, item.model_importance)):
                    enriched = frame.copy()
                    enriched.insert(0, "target", target)
                    enriched.insert(0, "analysis_scope", scope_name)
                    parts.append(enriched)
                summary_rows.append({"analysis_scope": scope_name, "target": target, **item.summary})
            combined_metrics = pd.concat(metric_parts, ignore_index=True)
            combined_bins = pd.concat(bin_parts, ignore_index=True)
            combined_models = pd.concat(model_parts, ignore_index=True)
            self.relationship_metrics.set_frame(combined_metrics)
            self.relationship_bins.set_frame(combined_bins)
            self.relationship_model.set_frame(combined_models)
            summary = result.summary
            self.relationship_summary.setText(
                f"关联键：{summary['join_key']}；匹配：{summary['matched_count']}/"
                f"{summary['product_count']}（{summary['match_rate']:.1%}）；"
                f"参数：{summary['parameter_count']}；缺陷产品："
                f"{summary['defective_product_count']}；验证：{summary['validation_method']}；"
                f"AUC：{summary['validation_auc'] if summary['validation_auc'] is not None else '-'}"
            )
            output = self.current_result.output_dir
            combined_metrics.to_csv(
                output / "process_parameter_metrics.csv", index=False, encoding="utf-8-sig"
            )
            combined_bins.to_csv(
                output / "process_parameter_binned_rates.csv", index=False, encoding="utf-8-sig"
            )
            combined_models.to_csv(
                output / "process_model_importance.csv", index=False, encoding="utf-8-sig"
            )
            (output / "process_relationship_summary.json").write_text(
                json.dumps(summary_rows, ensure_ascii=False, indent=2), encoding="utf-8"
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
            ("paths/defect_catalog", self.catalog_edit.text()),
            ("paths/source_excel", self.source_excel_edit.text()),
            ("paths/output", self.output_edit.text()), ("paths/image_root", self.image_root_edit.text()),
            ("paths/process", self.process_edit.text()), ("task/name", self.task_edit.text()),
            ("process/tolerance_seconds", self.time_tolerance.value()),
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
