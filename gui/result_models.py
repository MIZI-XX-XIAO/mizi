"""本文件把Pandas DataFrame适配为支持排序和告警配色的Qt只读表格模型。"""

from typing import Any

import pandas as pd
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor


class DataFrameTableModel(QAbstractTableModel):
    def __init__(self, frame: pd.DataFrame | None = None, alert_colors: bool = False) -> None:
        super().__init__()
        self.frame = frame.copy() if frame is not None else pd.DataFrame()
        self.alert_colors = alert_colors

    def set_frame(self, frame: pd.DataFrame) -> None:
        self.beginResetModel()
        self.frame = frame.copy()
        self.endResetModel()

    def rowCount(self, _parent: QModelIndex = QModelIndex()) -> int:
        return len(self.frame)

    def columnCount(self, _parent: QModelIndex = QModelIndex()) -> int:
        return len(self.frame.columns)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Any:
        if not index.isValid():
            return None
        value = self.frame.iat[index.row(), index.column()]
        if role == Qt.DisplayRole:
            return "" if pd.isna(value) else str(value)
        if role == Qt.BackgroundRole and self.alert_colors and "severity" in self.frame.columns:
            severity = str(self.frame.iloc[index.row()]["severity"])
            return {"critical": QColor("#ffd6d6"), "warning": QColor("#fff0bf"), "notice": QColor("#dceeff")}.get(severity)
        if role == Qt.TextAlignmentRole:
            return int(Qt.AlignLeft | Qt.AlignVCenter)
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole) -> Any:
        if role != Qt.DisplayRole:
            return None
        return str(self.frame.columns[section]) if orientation == Qt.Horizontal else str(section + 1)

    def row_record(self, row: int) -> pd.Series:
        return self.frame.iloc[row]

    def sort(self, column: int, order: Qt.SortOrder = Qt.AscendingOrder) -> None:
        if not 0 <= column < len(self.frame.columns):
            return
        name = self.frame.columns[column]
        self.layoutAboutToBeChanged.emit()
        self.frame = self.frame.sort_values(
            name, ascending=order == Qt.AscendingOrder, kind="stable", na_position="last"
        ).reset_index(drop=True)
        self.layoutChanged.emit()
