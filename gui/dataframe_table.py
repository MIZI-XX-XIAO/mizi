"""本文件实现带搜索、排序、导出和列宽记忆的通用DataFrame结果表。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from PySide6.QtCore import QModelIndex, QSettings, QSortFilterProxyModel, Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QTableView, QVBoxLayout, QWidget,
)

from .result_models import DataFrameTableModel


class AllColumnsFilterProxy(QSortFilterProxyModel):
    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        pattern = self.filterRegularExpression()
        if pattern.pattern() == "":
            return True
        model = self.sourceModel()
        for column in range(model.columnCount()):
            value = model.data(model.index(source_row, column, source_parent), Qt.DisplayRole)
            if pattern.match(str(value or "")).hasMatch():
                return True
        return False


class DataFrameTableWidget(QWidget):
    row_activated = Signal(object)

    def __init__(self, settings_key: str, alert_colors: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.settings_key = settings_key
        self.model = DataFrameTableModel(alert_colors=alert_colors)
        self.proxy = AllColumnsFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setWordWrap(False)
        self.table.doubleClicked.connect(self._activated)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索全部列…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.proxy.setFilterWildcard)
        export_button = QPushButton("导出当前结果")
        export_button.clicked.connect(self._export)
        toolbar_widget = QWidget()
        toolbar_widget.setObjectName("tableToolbar")
        toolbar = QHBoxLayout(toolbar_widget)
        toolbar.setContentsMargins(0, 0, 0, 6)
        toolbar.addWidget(QLabel("筛选"))
        toolbar.addWidget(self.search, 1)
        toolbar.addWidget(export_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(toolbar_widget)
        self.empty_label = QLabel("暂无符合当前筛选条件的结果")
        self.empty_label.setObjectName("emptyResultLabel")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setVisible(False)
        layout.addWidget(self.empty_label)
        layout.addWidget(self.table)
        state = QSettings().value(f"tables/{settings_key}/header")
        if state is not None:
            self.table.horizontalHeader().restoreState(state)

    def set_frame(self, frame: pd.DataFrame) -> None:
        self.model.set_frame(frame)
        self.empty_label.setVisible(frame.empty)
        if frame.shape[1] <= 12:
            self.table.resizeColumnsToContents()

    def frame(self) -> pd.DataFrame:
        rows = [
            self.proxy.mapToSource(self.proxy.index(row, 0)).row()
            for row in range(self.proxy.rowCount())
        ]
        return self.model.frame.iloc[rows].copy() if rows else self.model.frame.iloc[0:0].copy()

    def _activated(self, proxy_index: QModelIndex) -> None:
        source = self.proxy.mapToSource(proxy_index)
        self.row_activated.emit(self.model.row_record(source.row()))

    def _export(self) -> None:
        filename, _ = QFileDialog.getSaveFileName(
            self, "导出结果", f"{self.settings_key}.csv", "CSV (*.csv)"
        )
        if not filename:
            return
        path = Path(filename)
        try:
            self.frame().to_csv(path, index=False, encoding="utf-8-sig")
            QMessageBox.information(self, "导出完成", str(path))
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", str(exc))

    def closeEvent(self, event) -> None:
        QSettings().setValue(
            f"tables/{self.settings_key}/header", self.table.horizontalHeader().saveState()
        )
        super().closeEvent(event)
