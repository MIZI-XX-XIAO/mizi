"""本文件实现结果概览页内部使用的分层结果明细浏览组件。"""

from __future__ import annotations

import pandas as pd
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QStackedWidget, QTabWidget, QVBoxLayout, QWidget,
)

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


class ResultDetailsWidget(QWidget):
    """Show one filtered result category without opening a separate window."""

    back_requested = Signal()
    record_activated = Signal(str, object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.current_key = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QHBoxLayout()
        self.back_button = QPushButton("‹ 返回结果概览")
        self.back_button.setObjectName("quietButton")
        self.back_button.clicked.connect(self.back_requested)
        self.title = QLabel("结果明细")
        self.title.setObjectName("pageTitle")
        self.count = QLabel("")
        self.count.setObjectName("sectionTitle")
        header.addWidget(self.back_button)
        header.addWidget(self.title)
        header.addStretch(1)
        header.addWidget(self.count)
        layout.addLayout(header)

        self.hint = QLabel("以下内容已应用结果概览页的全部筛选条件。")
        self.hint.setObjectName("resultDialogHint")
        layout.addWidget(self.hint)

        self.stack = QStackedWidget()
        self.table = DataFrameTableWidget("result_embedded_details")
        self.table.row_activated.connect(self._activate_table_record)
        self.stack.addWidget(self.table)

        self.tabs = QTabWidget()
        self.widgets: dict[str, DataFrameTableWidget] = {}
        for key, label in SECTION_LABELS.items():
            widget = DataFrameTableWidget(f"result_embedded_{key}")
            if key not in {"cooccurrence", "transition"}:
                widget.row_activated.connect(
                    lambda record, section=key: self.record_activated.emit(section, record)
                )
            self.widgets[key] = widget
            self.tabs.addTab(widget, label)
        self.stack.addWidget(self.tabs)
        layout.addWidget(self.stack, 1)

    def show_table(
        self, key: str, title: str, frame: pd.DataFrame, *, alert_colors: bool = False,
    ) -> None:
        if key != self.current_key:
            self.table.search.clear()
        self.current_key = key
        self.title.setText(title)
        self.count.setText(f"共 {len(frame)} 条")
        self.hint.setText(
            "以下内容已应用结果概览页的全部筛选条件。双击可进入图片复核。"
            if key != "spatial_cluster_count"
            else "以下空间簇已应用全部筛选条件。双击可复核该簇对应的产品图片。"
        )
        self.table.model.alert_colors = alert_colors
        self.table.set_frame(frame)
        self.stack.setCurrentWidget(self.table)

    def show_patterns(
        self, sections: dict[str, pd.DataFrame], selected: str = "all",
    ) -> None:
        self.current_key = "discovered_pattern_count"
        total = 0
        for index, (key, widget) in enumerate(self.widgets.items()):
            frame = sections.get(key, pd.DataFrame())
            total += len(frame)
            widget.set_frame(frame)
            self.tabs.setTabText(index, f"{SECTION_LABELS[key]} ({len(frame)})")
        if selected in self.widgets:
            self.tabs.setCurrentWidget(self.widgets[selected])
        self.title.setText("发现规律明细")
        self.count.setText(f"共 {total} 条")
        self.hint.setText("以下内容已应用结果概览页的全部筛选条件。双击可进入图片复核。")
        self.stack.setCurrentWidget(self.tabs)

    def _activate_table_record(self, record: pd.Series) -> None:
        self.record_activated.emit(self.current_key, record)
