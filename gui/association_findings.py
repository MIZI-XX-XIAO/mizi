"""本文件展示按证据强度排序的关联分析重点发现卡片。"""

from __future__ import annotations

import pandas as pd
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from .dataframe_table import DataFrameTableWidget


class AssociationFindingsWidget(QWidget):
    detail_requested = Signal(object)
    image_requested = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._findings = pd.DataFrame()
        filters = QHBoxLayout()
        self.scope_filter = QComboBox()
        self.target_filter = QComboBox()
        self.type_filter = QComboBox()
        self.level_filter = QComboBox()
        self.level_filter.addItems(["全部等级", "高", "中", "探索性"])
        for caption, widget in (
            ("范围", self.scope_filter), ("缺陷目标", self.target_filter),
            ("证据类型", self.type_filter), ("最低等级", self.level_filter),
        ):
            filters.addWidget(QLabel(caption))
            filters.addWidget(widget)
            widget.currentIndexChanged.connect(self._refresh)
        filters.addStretch(1)

        self.summary = QLabel("完成关联分析后，将在这里按证据强度展示前10条重点发现。")
        self.summary.setWordWrap(True)
        self.summary.setObjectName("warningBanner")
        self.cards_host = QWidget()
        self.cards_layout = QVBoxLayout(self.cards_host)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(8)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(270)
        scroll.setWidget(self.cards_host)
        self.all_findings = DataFrameTableWidget("association_findings")
        self.all_findings.setVisible(False)
        self.toggle = QPushButton("查看全部发现")
        self.toggle.clicked.connect(self._toggle_all)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(filters)
        layout.addWidget(self.summary)
        layout.addWidget(scroll)
        layout.addWidget(self.toggle)
        layout.addWidget(self.all_findings, 1)

    def set_findings(self, frame: pd.DataFrame) -> None:
        self._findings = frame.copy()
        self._populate_filter(self.scope_filter, "全部范围", "analysis_scope")
        self._populate_filter(self.target_filter, "全部目标", "target")
        self._populate_filter(self.type_filter, "全部类型", "finding_type")
        self._refresh()

    def _populate_filter(self, combo: QComboBox, all_text: str, column: str) -> None:
        previous = combo.currentText()
        combo.blockSignals(True)
        combo.clear(); combo.addItem(all_text, "")
        if column in self._findings:
            for value in self._findings[column].dropna().astype(str).loc[lambda s: s.str.strip().ne("")].drop_duplicates():
                combo.addItem(value, value)
        index = combo.findText(previous)
        combo.setCurrentIndex(max(0, index))
        combo.blockSignals(False)

    def filtered_findings(self) -> pd.DataFrame:
        frame = self._findings.copy()
        for combo, column in (
            (self.scope_filter, "analysis_scope"), (self.target_filter, "target"),
            (self.type_filter, "finding_type"),
        ):
            value = str(combo.currentData() or "")
            if value and column in frame:
                frame = frame[frame[column].astype(str).eq(value)]
        minimum = self.level_filter.currentText()
        if minimum != "全部等级" and "evidence_level" in frame:
            allowed = {"高": {"高"}, "中": {"高", "中"}, "探索性": {"高", "中", "探索性"}}[minimum]
            frame = frame[frame["evidence_level"].astype(str).isin(allowed)]
        return frame

    def _clear_cards(self) -> None:
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _refresh(self) -> None:
        self._clear_cards()
        frame = self.filtered_findings()
        self.all_findings.set_frame(frame)
        if frame.empty:
            self.summary.setText("当前筛选条件下没有关联发现。")
            self.cards_layout.addStretch(1)
            return
        high_count = int(frame.get("evidence_level", pd.Series(dtype=str)).astype(str).eq("高").sum())
        self.summary.setText(
            f"共发现{len(frame)}条关联线索，其中高证据{high_count}条；以下优先展示证据分最高的10条。"
            "证据分表示统计证据综合强度，不代表因果概率。"
        )
        for rank, (_, record) in enumerate(frame.head(10).iterrows(), 1):
            card = QFrame(); card.setObjectName("associationFindingCard")
            grid = QGridLayout(card)
            level = str(record.get("evidence_level", "探索性"))
            score = pd.to_numeric(pd.Series([record.get("evidence_score")]), errors="coerce").iloc[0]
            badge = QLabel(f"#{rank}  {level}  {score:.0f}/100" if pd.notna(score) else f"#{rank}  {level}")
            badge.setObjectName("associationFindingBadge")
            category = QLabel(str(record.get("finding_type", "关联发现")))
            category.setObjectName("associationFindingType")
            statement = QLabel(str(record.get("statement", "")))
            statement.setWordWrap(True)
            warning = QLabel(str(record.get("warning", "统计关联，尚未证明因果")))
            warning.setObjectName("associationFindingWarning")
            curve = QPushButton("查看曲线")
            curve.setEnabled(str(record.get("detail_type", "")) in {"nonlinear_effect", "interaction"})
            curve.clicked.connect(lambda _checked=False, row=record.copy(): self._request_detail(row, "curve"))
            detail = QPushButton("查看样本")
            detail.clicked.connect(lambda _checked=False, row=record.copy(): self._request_detail(row, "samples"))
            image = QPushButton("跳转图片")
            image.setEnabled(str(record.get("detail_type", "")) in {"trajectory", "code_space", "attribution"})
            image.clicked.connect(lambda _checked=False, row=record.copy(): self.image_requested.emit(row))
            grid.addWidget(badge, 0, 0)
            grid.addWidget(category, 0, 1)
            grid.addWidget(statement, 1, 0, 1, 4)
            grid.addWidget(warning, 2, 0)
            grid.addWidget(curve, 2, 1)
            grid.addWidget(detail, 2, 2)
            grid.addWidget(image, 2, 3)
            grid.setColumnStretch(1, 1)
            self.cards_layout.addWidget(card)
        self.cards_layout.addStretch(1)

    def _request_detail(self, record: pd.Series, action: str) -> None:
        selected = record.copy()
        selected["requested_action"] = action
        self.detail_requested.emit(selected)

    def _toggle_all(self) -> None:
        visible = not self.all_findings.isVisible()
        self.all_findings.setVisible(visible)
        self.toggle.setText("收起全部发现" if visible else "查看全部发现")
