"""Popup result browsers for discovered patterns and alerts."""

from __future__ import annotations

import pandas as pd
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QDialog, QLabel, QTabWidget, QVBoxLayout

from .dataframe_table import DataFrameTableWidget


SECTION_LABELS = {
    "periodic": "周期规律",
    "burst": "连续异常",
    "code": "代码规律",
    "trajectory": "水平轨迹",
    "cooccurrence": "缺陷共现",
    "transition": "序列关系",
    "other": "其他空间规律",
}


class PatternResultsDialog(QDialog):
    pattern_activated = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("发现规律明细")
        self.setObjectName("resultDetailsDialog")
        self.setModal(False)
        self.resize(1180, 720)
        layout = QVBoxLayout(self)
        hint = QLabel("以下内容已应用结果概览页的全部筛选条件。双击可进入图片复核。")
        hint.setObjectName("resultDialogHint")
        layout.addWidget(hint)
        self.tabs = QTabWidget()
        self.widgets: dict[str, DataFrameTableWidget] = {}
        for key, label in SECTION_LABELS.items():
            widget = DataFrameTableWidget(f"result_dialog_{key}")
            if key not in {"cooccurrence", "transition"}:
                widget.row_activated.connect(self.pattern_activated)
            self.widgets[key] = widget
            self.tabs.addTab(widget, label)
        layout.addWidget(self.tabs, 1)

    def set_sections(self, sections: dict[str, pd.DataFrame], selected: str = "all") -> None:
        for index, (key, widget) in enumerate(self.widgets.items()):
            frame = sections.get(key, pd.DataFrame())
            widget.set_frame(frame)
            self.tabs.setTabText(index, f"{SECTION_LABELS[key]} ({len(frame)})")
        if selected in self.widgets:
            self.tabs.setCurrentWidget(self.widgets[selected])


class AlertResultsDialog(QDialog):
    alert_activated = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("预警明细")
        self.setObjectName("resultDetailsDialog")
        self.setModal(False)
        self.resize(1080, 680)
        layout = QVBoxLayout(self)
        hint = QLabel("以下预警已应用结果概览页的全部筛选条件。双击可跳转到对应产品。")
        hint.setObjectName("resultDialogHint")
        layout.addWidget(hint)
        self.table = DataFrameTableWidget("result_dialog_alerts", alert_colors=True)
        self.table.row_activated.connect(self.alert_activated)
        layout.addWidget(self.table, 1)

    def set_frame(self, frame: pd.DataFrame) -> None:
        self.table.set_frame(frame)
        self.setWindowTitle(f"预警明细（{len(frame)}）")
