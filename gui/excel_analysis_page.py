"""本文件实现Excel工作簿配置、质量看板、结果表和后台任务交互页面。"""

from __future__ import annotations

from pathlib import Path
import json

from PySide6.QtCore import QSettings, QThread, Signal, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.app_runtime import configure_logging, new_error_id, user_data_dir
from src.excel_analysis import (
    CORE_ALIASES,
    ExcelAnalysisRequest,
    ExcelAnalysisResult,
    inspect_excel_workbook,
    preview_excel_sheet,
)
from .dataframe_table import DataFrameTableWidget
from .excel_analysis_worker import ExcelAnalysisWorker


class ExcelAnalysisPage(QWidget):
    result_ready = Signal(object)
    status_message = Signal(str)

    def __init__(self, project_root: Path, parent=None) -> None:
        super().__init__(parent)
        self.project_root = project_root
        self.settings = QSettings()
        self.logger, self.log_path = configure_logging()
        self.thread: QThread | None = None
        self.worker: ExcelAnalysisWorker | None = None
        self.current_result: ExcelAnalysisResult | None = None
        self._headers_by_sheet: dict[str, list[str]] = {}

        title = QLabel("Excel质量数据分析")
        title.setObjectName("pageTitle")
        intro = QLabel(
            "读取Data工作表，自动识别产品字段、Result参数及其Tolerance；"
            "原始State和系统重算结果会并列保留。"
        )
        intro.setWordWrap(True)

        source_group = QGroupBox("工作簿与任务")
        source_form = QFormLayout(source_group)
        self.workbook_edit = QLineEdit(str(self.settings.value("excel/workbook", "")))
        self.workbook_edit.setClearButtonEnabled(True)
        workbook_row = QWidget()
        workbook_layout = QHBoxLayout(workbook_row)
        workbook_layout.setContentsMargins(0, 0, 0, 0)
        workbook_layout.addWidget(self.workbook_edit, 1)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._choose_workbook)
        workbook_layout.addWidget(browse)
        inspect = QPushButton("检查与预览")
        inspect.clicked.connect(self.inspect_workbook)
        workbook_layout.addWidget(inspect)
        source_form.addRow("Excel文件", workbook_row)

        self.data_sheet = QComboBox()
        self.data_sheet.setEditable(True)
        self.data_sheet.addItem("Data")
        self.data_sheet.currentTextChanged.connect(lambda _text: self._populate_mapping())
        self.query_sheet = QComboBox()
        self.query_sheet.setEditable(True)
        self.query_sheet.addItem("Query parameter")
        sheets = QWidget()
        sheets_layout = QHBoxLayout(sheets)
        sheets_layout.setContentsMargins(0, 0, 0, 0)
        sheets_layout.addWidget(QLabel("数据"))
        sheets_layout.addWidget(self.data_sheet, 1)
        sheets_layout.addWidget(QLabel("查询参数"))
        sheets_layout.addWidget(self.query_sheet, 1)
        source_form.addRow("工作表", sheets)

        self.output_edit = QLineEdit(str(
            self.settings.value("excel/output", user_data_dir() / "excel_analysis_tasks")
        ))
        output_row = QWidget()
        output_layout = QHBoxLayout(output_row)
        output_layout.setContentsMargins(0, 0, 0, 0)
        output_layout.addWidget(self.output_edit, 1)
        output_browse = QPushButton("浏览…")
        output_browse.clicked.connect(self._choose_output)
        output_layout.addWidget(output_browse)
        source_form.addRow("结果目录", output_row)
        self.task_name = QLineEdit(str(self.settings.value("excel/task_name", "Excel质量分析")))
        source_form.addRow("任务名称", self.task_name)

        mapping_group = QGroupBox("核心字段映射（自动识别后可调整）")
        mapping_layout = QGridLayout(mapping_group)
        self.mapping_combos: dict[str, QComboBox] = {}
        labels = {
            "dmc_raw": "Ident No.", "state": "State", "test_date": "Test Date",
            "result_timestamp": "Result.TimeStamping", "variant": "Variant", "batch": "Batch",
            "line": "Line", "ttnr": "TTNR",
        }
        for index, (canonical, caption) in enumerate(labels.items()):
            combo = QComboBox(); combo.addItem("自动/未映射", "")
            self.mapping_combos[canonical] = combo
            mapping_layout.addWidget(QLabel(caption), index // 4 * 2, index % 4)
            mapping_layout.addWidget(combo, index // 4 * 2 + 1, index % 4)

        actions = QHBoxLayout()
        self.start_button = QPushButton("开始Excel分析")
        self.start_button.setObjectName("primaryButton")
        self.start_button.clicked.connect(self.start_analysis)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_analysis)
        actions.addStretch(); actions.addWidget(self.cancel_button); actions.addWidget(self.start_button)
        self.progress = QProgressBar(); self.progress.setRange(0, 100)
        self.stage = QLabel("等待选择工作簿")
        self.stage.setObjectName("stageLabel")

        self.kpi_labels: dict[str, QLabel] = {}
        kpi_grid = QGridLayout()
        kpis = (
            ("record_count", "记录数"), ("parameter_count", "参数数"),
            ("state_nok_count", "原始NOK"), ("state_nok_rate", "原始NOK率"),
            ("tolerance_nok_count", "超差记录"), ("tolerance_nok_rate", "超差率"),
            ("violation_event_count", "超差事件"), ("judgement_conflict_count", "判定冲突"),
        )
        for index, (key, caption) in enumerate(kpis):
            card = QFrame(); card.setObjectName("kpiCard")
            card_layout = QVBoxLayout(card)
            value = QLabel("-"); value.setObjectName("kpiValue"); value.setAlignment(Qt.AlignCenter)
            label = QLabel(caption); label.setAlignment(Qt.AlignCenter)
            card_layout.addWidget(value); card_layout.addWidget(label)
            self.kpi_labels[key] = value
            kpi_grid.addWidget(card, index // 4, index % 4)

        self.summary = QLabel("尚未执行Excel分析。")
        self.summary.setObjectName("warningBanner")
        self.summary.setWordWrap(True)
        self.tables: dict[str, DataFrameTableWidget] = {
            "parameter_stats": DataFrameTableWidget("excel_parameter_stats"),
            "violations": DataFrameTableWidget("excel_violations", alert_colors=True),
            "conflicts": DataFrameTableWidget("excel_conflicts", alert_colors=True),
            "group_stats": DataFrameTableWidget("excel_group_stats"),
            "trend": DataFrameTableWidget("excel_trend"),
            "quality": DataFrameTableWidget("excel_quality", alert_colors=True),
            "standardized": DataFrameTableWidget("excel_standardized"),
            "preview": DataFrameTableWidget("excel_preview"),
        }
        chart_page = QWidget()
        chart_layout = QVBoxLayout(chart_page)
        self.trend_chart = QLabel("分析完成后显示质量趋势图")
        self.violation_chart = QLabel("分析完成后显示参数超差排名")
        for chart in (self.trend_chart, self.violation_chart):
            chart.setAlignment(Qt.AlignCenter); chart.setMinimumHeight(220)
            chart.setObjectName("excelChart")
            chart_layout.addWidget(chart)
        result_tabs = QTabWidget()
        result_tabs.addTab(self.tables["preview"], "导入预览")
        result_tabs.addTab(chart_page, "质量图表")
        for key, caption in (
            ("parameter_stats", "参数统计"), ("violations", "超差明细"),
            ("conflicts", "判定冲突"), ("group_stats", "分组对比"),
            ("trend", "质量趋势"), ("quality", "数据质量"), ("standardized", "标准化数据"),
        ):
            result_tabs.addTab(self.tables[key], caption)

        configuration = QWidget()
        configuration_layout = QVBoxLayout(configuration)
        configuration_layout.setContentsMargins(0, 0, 0, 0)
        configuration_layout.addWidget(source_group)
        configuration_layout.addWidget(mapping_group)
        config_scroll = QScrollArea(); config_scroll.setWidgetResizable(True); config_scroll.setWidget(configuration)
        config_scroll.setMinimumHeight(250); config_scroll.setMaximumHeight(340)

        layout = QVBoxLayout(self)
        layout.addWidget(title); layout.addWidget(intro); layout.addWidget(config_scroll, 0)
        layout.addWidget(self.stage); layout.addWidget(self.progress); layout.addLayout(actions)
        layout.addLayout(kpi_grid); layout.addWidget(self.summary); layout.addWidget(result_tabs, 1)

    def _choose_workbook(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self, "选择Excel工作簿", self.workbook_edit.text() or str(self.project_root),
            "Excel工作簿 (*.xlsx *.xlsm)",
        )
        if filename:
            self.workbook_edit.setText(filename)
            self.inspect_workbook()

    def _choose_output(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "选择结果目录", self.output_edit.text() or str(user_data_dir())
        )
        if directory:
            self.output_edit.setText(directory)

    def inspect_workbook(self) -> None:
        try:
            path = Path(self.workbook_edit.text().strip())
            self._headers_by_sheet = inspect_excel_workbook(path)
            names = list(self._headers_by_sheet)
            for combo, preferred in ((self.data_sheet, "Data"), (self.query_sheet, "Query parameter")):
                combo.clear(); combo.addItems(names)
                index = combo.findText(preferred)
                if index >= 0: combo.setCurrentIndex(index)
            self._populate_mapping()
            preview = preview_excel_sheet(path, self.data_sheet.currentText(), 50)
            self.tables["preview"].set_frame(preview)
            self.stage.setText(f"已识别 {len(names)} 个工作表；请确认字段映射")
            self.status_message.emit("Excel工作簿识别完成")
        except Exception as exc:
            QMessageBox.critical(self, "工作簿识别失败", str(exc))

    def _populate_mapping(self) -> None:
        headers = self._headers_by_sheet.get(self.data_sheet.currentText(), [])
        saved = self._saved_mapping()
        normalized = {self._normalize(value): value for value in headers if value}
        for canonical, combo in self.mapping_combos.items():
            combo.blockSignals(True); combo.clear(); combo.addItem("自动/未映射", "")
            for header in headers: combo.addItem(header, header)
            candidate = saved.get(canonical, "")
            if not candidate:
                for alias in CORE_ALIASES[canonical]:
                    if self._normalize(alias) in normalized:
                        candidate = normalized[self._normalize(alias)]; break
            index = combo.findData(candidate)
            combo.setCurrentIndex(index if index >= 0 else 0); combo.blockSignals(False)

    @staticmethod
    def _normalize(value: str) -> str:
        return "".join(character for character in value.lower() if character.isalnum())

    def _saved_mapping(self) -> dict[str, str]:
        try:
            return json.loads(str(self.settings.value("excel/column_mapping", "{}")))
        except json.JSONDecodeError:
            return {}

    def _mapping(self) -> dict[str, str]:
        return {
            key: str(combo.currentData()) for key, combo in self.mapping_combos.items()
            if combo.currentData()
        }

    def start_analysis(self) -> None:
        if self.thread and self.thread.isRunning():
            return
        try:
            workbook_path = Path(self.workbook_edit.text().strip())
            if not workbook_path.is_file(): raise FileNotFoundError(f"Excel工作簿不存在：{workbook_path}")
            output = Path(self.output_edit.text().strip()); output.mkdir(parents=True, exist_ok=True)
            request = ExcelAnalysisRequest(
                workbook_path=workbook_path, output_parent=output, task_name=self.task_name.text(),
                data_sheet=self.data_sheet.currentText().strip() or "Data",
                query_sheet=self.query_sheet.currentText().strip() or "Query parameter",
                column_mapping=self._mapping(),
            )
        except Exception as exc:
            QMessageBox.critical(self, "无法启动Excel分析", str(exc)); return
        self.settings.setValue("excel/workbook", str(workbook_path))
        self.settings.setValue("excel/output", str(output))
        self.settings.setValue("excel/task_name", self.task_name.text())
        self.settings.setValue("excel/column_mapping", json.dumps(self._mapping(), ensure_ascii=False))
        self.progress.setValue(0); self.start_button.setEnabled(False); self.cancel_button.setEnabled(True)
        self.thread = QThread(self); self.worker = ExcelAnalysisWorker(request); self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress_changed.connect(self._progress)
        self.worker.completed.connect(self._completed)
        self.worker.cancelled.connect(self._cancelled)
        self.worker.failed.connect(self._failed)
        for signal in (self.worker.completed, self.worker.cancelled, self.worker.failed): signal.connect(self.thread.quit)
        self.thread.finished.connect(self._thread_finished); self.thread.start()

    def _progress(self, event) -> None:
        self.progress.setValue(event.percent); self.stage.setText(event.message)

    def _completed(self, result: ExcelAnalysisResult) -> None:
        self.current_result = result
        for key, label in self.kpi_labels.items():
            value = result.summary.get(key)
            label.setText(f"{value:.1%}" if key.endswith("_rate") and value is not None else str(value if value is not None else "-"))
        for key, table in self.tables.items():
            if key in result.frames:
                table.set_frame(result.frames[key])
        for label, filename in (
            (self.trend_chart, "quality_trend.png"),
            (self.violation_chart, "top_tolerance_violations.png"),
        ):
            pixmap = QPixmap(str(result.output_dir / "visualizations" / filename))
            label.setPixmap(pixmap.scaled(760, 320, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.summary.setObjectName("successBanner")
        self.summary.setText(
            f"分析完成：{result.summary['record_count']}条记录，"
            f"{result.summary['violation_event_count']}个超差事件，"
            f"{result.summary['judgement_conflict_count']}条判定冲突。结果：{result.output_dir}"
        )
        self.summary.style().unpolish(self.summary); self.summary.style().polish(self.summary)
        self.result_ready.emit(result); self.status_message.emit(f"Excel分析完成：{result.output_dir}")

    def _cancelled(self, _result) -> None:
        self.stage.setText("Excel分析已取消"); self.status_message.emit("Excel分析已取消")

    def _failed(self, message: str) -> None:
        error_id = new_error_id()
        self.logger.error("excel_analysis_failed error_id=%s message=%s", error_id, message)
        self.stage.setText("Excel分析失败")
        QMessageBox.critical(
            self, "Excel分析失败", f"{message}\n\n错误编号：{error_id}\n诊断日志：{self.log_path}"
        )

    def _thread_finished(self) -> None:
        self.start_button.setEnabled(True); self.cancel_button.setEnabled(False)
        if self.worker: self.worker.deleteLater()
        if self.thread: self.thread.deleteLater()
        self.worker = None; self.thread = None

    def cancel_analysis(self) -> None:
        if self.worker:
            self.worker.cancel(); self.stage.setText("正在安全取消…")
