"""本文件把全部有效分析发现展示为可展开、可追溯样本的解释卡片。"""

from __future__ import annotations

import pandas as pd
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

class AssociationFindingsWidget(QWidget):
    detail_requested = Signal(object)
    image_requested = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._findings = pd.DataFrame()
        self._cause_summaries: list[dict] = []
        self._cards: list[QFrame] = []
        filters = QHBoxLayout()
        self.scope_filter = QComboBox()
        self.target_filter = QComboBox()
        self.type_filter = QComboBox()
        self.level_filter = QComboBox()
        for caption, widget in (
            ("范围", self.scope_filter), ("缺陷目标", self.target_filter),
            ("证据类型", self.type_filter), ("证据等级", self.level_filter),
        ):
            filters.addWidget(QLabel(caption))
            filters.addWidget(widget)
            if widget is self.scope_filter:
                widget.currentIndexChanged.connect(self._scope_changed)
            else:
                widget.currentIndexChanged.connect(self._refresh)
        filters.addStretch(1)

        self.summary = QLabel("完成分析后，将在这里展示全部有效发现及其判断依据。")
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
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(filters)
        layout.addWidget(self.summary)
        layout.addWidget(scroll, 1)

    @staticmethod
    def _target_key(scope: object, target: object) -> str:
        return f"{str(scope or '').strip()}|{str(target or '').strip()}"

    @staticmethod
    def _target_caption(scope: object, target: object, defect_name: object = "") -> str:
        raw = str(target or "").strip()
        source, separator, code = raw.partition(":")
        source_caption = {
            "VI_BLOCK": "VI", "VI_FAILURE": "VI", "AOI_FAILURE": "AOI",
            "IMAGE": "图片",
        }.get(source, source)
        name = "" if defect_name is None or pd.isna(defect_name) else str(defect_name).strip()
        defect = f"{name} · {code}" if name and separator else (name or (code if separator else raw))
        return f"{str(scope or '').strip()} · {defect}（{source_caption}）"

    def set_findings(
        self, frame: pd.DataFrame, *, cause_summaries: list[dict] | None = None,
        preferred_target: str = "",
    ) -> None:
        previous_target = str(self.target_filter.currentData() or "")
        self._findings = frame.copy()
        self._cause_summaries = list(cause_summaries or [])
        if "is_effective" in self._findings:
            explicit = self._findings["is_effective"]
            keep = explicit.isna() | explicit.map(
                lambda value: str(value).strip().lower() not in {"false", "0", "no"}
            )
            self._findings = self._findings.loc[keep].copy()
        if "positive_count" in self._findings:
            positives = pd.to_numeric(self._findings["positive_count"], errors="coerce")
            self._findings = self._findings.loc[positives.isna() | positives.gt(0)].copy()
        if "statement" in self._findings:
            self._findings = self._findings.loc[
                self._findings["statement"].fillna("").astype(str).str.strip().ne("")
            ].copy()
        if "evidence_score" in self._findings:
            self._findings["_score"] = pd.to_numeric(
                self._findings["evidence_score"], errors="coerce"
            ).fillna(0)
            self._findings = self._findings.sort_values(
                "_score", ascending=False, kind="stable"
            ).drop(columns="_score").reset_index(drop=True)
        if not self._findings.empty:
            scopes = self._findings.get(
                "analysis_scope", pd.Series("", index=self._findings.index)
            )
            targets = self._findings.get("target", pd.Series("", index=self._findings.index))
            self._findings["_target_key"] = [
                self._target_key(scope, target) for scope, target in zip(scopes, targets)
            ]
        self._populate_filter(self.scope_filter, "全部范围", "analysis_scope")
        self._populate_target_filter(previous_target, preferred_target)
        self._populate_filter(self.type_filter, "全部类型", "finding_type")
        self._populate_filter(self.level_filter, "全部等级", "evidence_level")
        self._refresh()

    def _populate_target_filter(self, previous: str, preferred: str) -> None:
        self.target_filter.blockSignals(True)
        self.target_filter.clear()
        if not self._findings.empty and "_target_key" in self._findings:
            available = self._findings
            selected_scope = str(self.scope_filter.currentData() or "")
            if selected_scope:
                available = available[available["analysis_scope"].astype(str).eq(selected_scope)]
            if "defect_name" not in available:
                available = available.assign(defect_name="")
            targets = available[["analysis_scope", "target", "defect_name", "_target_key"]].drop_duplicates(
                ["analysis_scope", "target", "_target_key"]
            )
            for item in targets.to_dict("records"):
                self.target_filter.addItem(
                    self._target_caption(item["analysis_scope"], item["target"], item["defect_name"]),
                    item["_target_key"],
                )
        selected = previous
        if not selected and preferred:
            preferred_matches = [
                str(self.target_filter.itemData(index))
                for index in range(self.target_filter.count())
                if str(self.target_filter.itemData(index)).endswith(f"|{preferred}")
            ]
            selected = preferred_matches[0] if preferred_matches else ""
        index = self.target_filter.findData(selected)
        self.target_filter.setCurrentIndex(index if index >= 0 else (0 if self.target_filter.count() else -1))
        self.target_filter.blockSignals(False)

    def _scope_changed(self) -> None:
        self._populate_target_filter(str(self.target_filter.currentData() or ""), "")
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
            (self.scope_filter, "analysis_scope"), (self.target_filter, "_target_key"),
            (self.type_filter, "finding_type"), (self.level_filter, "evidence_level"),
        ):
            value = str(combo.currentData() or "")
            if value and column in frame:
                frame = frame[frame[column].astype(str).eq(value)]
        return frame

    def _selected_cause_summary(self, frame: pd.DataFrame) -> dict | None:
        if frame.empty:
            return None
        first = frame.iloc[0]
        scope = str(first.get("analysis_scope", ""))
        target = str(first.get("target", ""))
        source, _, code = target.partition(":")
        return next((
            item for item in self._cause_summaries
            if str(item.get("analysis_scope", "")) == scope
            and str(item.get("source_type", "")) == source
            and str(item.get("canonical_code", "")) == code
        ), None)

    def _summary_text(self, frame: pd.DataFrame) -> str:
        selected = self._selected_cause_summary(frame)
        if selected is None:
            return f"当前缺陷共有{len(frame)}条有效发现；统计关联不代表因果关系。"
        population = int(selected.get("population_count", 0) or 0)
        traceable = int(selected.get(
            "current_window_traceable_count", selected.get("matched_route_count", 0)
        ) or 0)
        complete = int(selected.get("complete_route_count", 0) or 0)
        partial = int(selected.get("partial_route_count", max(0, traceable - complete)) or 0)
        rate = traceable / population if population else 0.0
        conclusion = str(selected.get("top_hypothesis", "数据不足"))
        code = str(selected.get("canonical_code", "目标缺陷"))
        stops = int(selected.get("downtime_event_count", 0) or 0)
        return (
            f"{code}：{conclusion}　｜　当前数据窗口可追溯 {traceable}/{population}（{rate:.1%}；"
            f"完整{complete}、部分{partial}）　｜　疑似停机{stops}次"
        )

    def _clear_cards(self) -> None:
        self._cards.clear()
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    @staticmethod
    def _text(record: pd.Series, key: str, fallback: str) -> str:
        value = record.get(key)
        if value is None or pd.isna(value) or not str(value).strip():
            return fallback
        return str(value).strip()

    def _explanation(self, record: pd.Series) -> str:
        statement = self._text(record, "statement", "当前记录没有摘要。")
        return "\n\n".join((
            f"判断问题\n{self._text(record, 'analysis_question', '这条现象是否与目标缺陷存在稳定关系？')}",
            f"怎么发现\n{self._text(record, 'discovery_method', statement)}",
            f"如何解读\n{self._text(record, 'interpretation', statement)}",
            f"证据限制\n{self._text(record, 'limitations', self._text(record, 'warning', '仍需现场验证。'))}",
            f"推荐验证\n{self._text(record, 'recommended_action', '结合原始记录和受控实验复核。')}",
        ))

    def _refresh(self) -> None:
        self._clear_cards()
        frame = self.filtered_findings()
        if frame.empty:
            self.summary.setText("当前筛选条件下没有有效分析发现。")
            self.cards_layout.addStretch(1)
            return
        self.summary.setText(self._summary_text(frame))
        for rank, (_, record) in enumerate(frame.iterrows(), 1):
            card = QFrame(); card.setObjectName("associationFindingCard")
            card.setProperty("finding_id", str(record.get("finding_id", "")))
            self._cards.append(card)
            grid = QGridLayout(card)
            level = str(record.get("evidence_level", "探索性线索"))
            score = pd.to_numeric(pd.Series([record.get("evidence_score")]), errors="coerce").iloc[0]
            badge = QLabel(f"#{rank}  {level}  {score:.0f}/100" if pd.notna(score) else f"#{rank}  {level}")
            badge.setObjectName("associationFindingBadge")
            category = QLabel(str(record.get("finding_type", "分析发现")))
            category.setObjectName("associationFindingType")
            statement = QLabel(str(record.get("statement", "")))
            statement.setWordWrap(True)
            warning = QLabel(str(record.get("warning", "统计关联，尚未证明因果")))
            warning.setObjectName("associationFindingWarning")
            explanation = QLabel(self._explanation(record))
            explanation.setObjectName("associationFindingExplanation")
            explanation.setWordWrap(True)
            explanation.setVisible(False)
            expand = QPushButton("展开分析依据")
            expand.setObjectName("associationFindingExpand")
            expand.setCheckable(True)

            def toggle_explanation(checked: bool, panel=explanation, button=expand) -> None:
                panel.setVisible(checked)
                button.setText("收起分析依据" if checked else "展开分析依据")

            expand.toggled.connect(toggle_explanation)
            actions = QHBoxLayout()
            actions.addWidget(expand)
            detail_type = str(record.get("detail_type", ""))
            if detail_type == "cause_evidence":
                detail = QPushButton("查看产品明细")
                detail.clicked.connect(lambda _checked=False, row=record.copy(): self._request_detail(row, "samples"))
                actions.addWidget(detail)
            elif detail_type == "cause_hypothesis":
                detail = QPushButton("查看支持证据")
                detail.clicked.connect(lambda _checked=False, row=record.copy(): self._request_detail(row, "evidence"))
                actions.addWidget(detail)
            elif detail_type in {"nonlinear_effect", "interaction"}:
                curve = QPushButton(
                    "查看风险热力图" if detail_type == "interaction" else "查看单参数曲线"
                )
                curve.clicked.connect(lambda _checked=False, row=record.copy(): self._request_detail(row, "curve"))
                actions.addWidget(curve)
                detail = QPushButton("查看样本")
                detail.clicked.connect(lambda _checked=False, row=record.copy(): self._request_detail(row, "samples"))
                actions.addWidget(detail)
            if detail_type in {"trajectory", "code_space", "attribution"}:
                image = QPushButton("跳转图片")
                image.clicked.connect(lambda _checked=False, row=record.copy(): self.image_requested.emit(row))
                actions.addWidget(image)
            actions.addStretch(1)
            grid.addWidget(badge, 0, 0)
            grid.addWidget(category, 0, 1)
            grid.addWidget(statement, 1, 0, 1, 2)
            grid.addWidget(warning, 2, 0)
            grid.addLayout(actions, 2, 1)
            grid.addWidget(explanation, 3, 0, 1, 2)
            grid.setColumnStretch(1, 1)
            self.cards_layout.addWidget(card)
        self.cards_layout.addStretch(1)

    def _request_detail(self, record: pd.Series, action: str) -> None:
        selected = record.copy()
        selected["requested_action"] = action
        self.detail_requested.emit(selected)
