"""本文件以风险热力图和样本对照解释单个工艺参数交互。"""

from __future__ import annotations

import numpy as np
import pandas as pd
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QHeaderView, QLabel, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget,
)

from .dataframe_table import DataFrameTableWidget


class InteractionDetailWidget(QWidget):
    """Present an interaction as an annotated risk matrix and readable samples."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.title = QLabel("请选择一条参数交互发现。")
        self.title.setObjectName("pageTitle")
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setObjectName("resultDialogHint")
        self.tabs = QTabWidget()

        heatmap_page = QWidget()
        heatmap_layout = QVBoxLayout(heatmap_page)
        self.heatmap_hint = QLabel("颜色越深表示相对总体缺陷风险越高；★表示稳定高风险区域。")
        self.heatmap_hint.setWordWrap(True)
        self.heatmap = QTableWidget()
        self.heatmap.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.heatmap.setSelectionMode(QAbstractItemView.NoSelection)
        self.heatmap.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.heatmap.verticalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        heatmap_layout.addWidget(self.heatmap_hint)
        heatmap_layout.addWidget(self.heatmap, 1)
        self.tabs.addTab(heatmap_page, "风险热力图")

        samples_page = QWidget()
        samples_layout = QVBoxLayout(samples_page)
        self.sample_hint = QLabel("")
        self.sample_hint.setWordWrap(True)
        self.sample_tabs = QTabWidget()
        self.hit_samples = DataFrameTableWidget("interaction_hit_samples")
        self.control_samples = DataFrameTableWidget("interaction_control_samples")
        self.sample_tabs.addTab(self.hit_samples, "命中高风险区间")
        self.sample_tabs.addTab(self.control_samples, "区间外对照")
        samples_layout.addWidget(self.sample_hint)
        samples_layout.addWidget(self.sample_tabs, 1)
        self.tabs.addTab(samples_page, "样本对照")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.title)
        layout.addWidget(self.summary)
        layout.addWidget(self.tabs, 1)

    @staticmethod
    def _same_target(frame: pd.DataFrame, finding: pd.Series) -> pd.Series:
        keep = pd.Series(True, index=frame.index)
        for column in ("analysis_scope", "target"):
            value = str(finding.get(column, "")).strip()
            if value and column in frame:
                keep &= frame[column].astype(str).eq(value)
        return keep

    @staticmethod
    def _boolean_flags(values: pd.Series) -> pd.Series:
        return values.map(
            lambda value: bool(value) if isinstance(value, (bool, np.bool_))
            else str(value).strip().lower() in {"true", "1", "yes"}
        )

    @staticmethod
    def _target_caption(finding: pd.Series) -> str:
        raw_name = finding.get("defect_name", "")
        name = (
            "" if raw_name is None or pd.isna(raw_name) else str(raw_name).strip()
        ) or "目标缺陷"
        source = {
            "VI_BLOCK": "VI", "VI_FAILURE": "VI", "AOI_FAILURE": "AOI", "IMAGE": "图片",
        }.get(str(finding.get("source_type", "")), str(finding.get("source_type", "")))
        raw_code = finding.get("canonical_code", "")
        code = "" if raw_code is None or pd.isna(raw_code) else str(raw_code).strip()
        suffix = " ".join(part for part in (source, code) if part)
        return f"{name}（{suffix}）" if suffix else name

    def set_data(
        self,
        finding: pd.Series,
        region_frame: pd.DataFrame,
        sample_frame: pd.DataFrame,
    ) -> None:
        left = str(finding.get("subject", ""))
        right = str(finding.get("related_subject", ""))
        regions = region_frame.copy()
        if not regions.empty:
            regions = regions.loc[self._same_target(regions, finding)]
            regions = regions[
                regions.get("参数A", pd.Series(index=regions.index, dtype=str)).astype(str).eq(left)
                & regions.get("参数B", pd.Series(index=regions.index, dtype=str)).astype(str).eq(right)
            ].copy()
        samples = sample_frame.copy()
        if not samples.empty:
            samples = samples.loc[self._same_target(samples, finding)].copy()

        target = self._target_caption(finding)
        self.title.setText(f"{target}：{left} × {right}")
        self._fill_heatmap(regions, left, right)
        self._fill_samples(samples, regions, left, right)

    def show_heatmap(self) -> None:
        self.tabs.setCurrentIndex(0)

    def show_samples(self) -> None:
        self.tabs.setCurrentIndex(1)

    def _fill_heatmap(self, regions: pd.DataFrame, left: str, right: str) -> None:
        self.heatmap.clear()
        if regions.empty:
            self.summary.setText("旧结果或当前结果没有可用的二维区域数据，请重新运行关联分析。")
            self.heatmap.setRowCount(0)
            self.heatmap.setColumnCount(0)
            return
        left_bins = regions.sort_values("参数A下界")["参数A区间"].drop_duplicates().tolist()
        right_bins = regions.sort_values("参数B下界")["参数B区间"].drop_duplicates().tolist()
        self.heatmap.setRowCount(len(right_bins))
        self.heatmap.setColumnCount(len(left_bins))
        self.heatmap.setHorizontalHeaderLabels([f"{left}\n{value}" for value in left_bins])
        self.heatmap.setVerticalHeaderLabels([f"{right}  {value}" for value in right_bins])
        for row_index, right_bin in enumerate(right_bins):
            for column_index, left_bin in enumerate(left_bins):
                match = regions[
                    regions["参数A区间"].astype(str).eq(str(left_bin))
                    & regions["参数B区间"].astype(str).eq(str(right_bin))
                ]
                if match.empty:
                    item = QTableWidgetItem("无样本")
                else:
                    region = match.iloc[0]
                    ratio = pd.to_numeric(pd.Series([region.get("风险比")]), errors="coerce").iloc[0]
                    high = bool(self._boolean_flags(
                        pd.Series([region.get("是否高风险", False)])
                    ).iloc[0])
                    prefix = "★ " if high else ""
                    item = QTableWidgetItem(
                        f"{prefix}{float(region['缺陷率']):.1%}\n"
                        f"{int(region['缺陷数量'])}/{int(region['样本量'])}\n"
                        f"{float(ratio):.2f}倍" if pd.notna(ratio) else
                        f"{prefix}{float(region['缺陷率']):.1%}\n"
                        f"{int(region['缺陷数量'])}/{int(region['样本量'])}"
                    )
                    if pd.notna(ratio):
                        if float(ratio) >= 1:
                            strength = float(np.clip((float(ratio) - 1) / 3, 0, 1))
                            color = QColor(
                                255, int(235 - 125 * strength), int(220 - 135 * strength)
                            )
                        else:
                            strength = float(np.clip(1 - float(ratio), 0, 1))
                            color = QColor(
                                int(225 - 55 * strength), int(240 - 20 * strength), 255
                            )
                        item.setBackground(color)
                        item.setForeground(QColor(25, 30, 38))
                    if high:
                        font = QFont(item.font())
                        font.setBold(True)
                        item.setFont(font)
                item.setTextAlignment(Qt.AlignCenter)
                self.heatmap.setItem(row_index, column_index, item)
        high_count = int(self._boolean_flags(regions["是否高风险"]).sum())
        base_rate = float(regions.iloc[0]["总体缺陷率"])
        self.summary.setText(
            f"二维区域共{len(regions)}格，总体缺陷率{base_rate:.1%}；"
            f"其中{high_count}格达到稳定高风险条件。单元格依次显示缺陷率、缺陷数/样本数和风险倍数。"
        )

    @staticmethod
    def _hit_mask(samples: pd.DataFrame, regions: pd.DataFrame, left: str, right: str) -> tuple[pd.Series, pd.Series]:
        hit = pd.Series(False, index=samples.index)
        labels = pd.Series("", index=samples.index, dtype=object)
        if left not in samples or right not in samples:
            return hit, labels
        left_values = pd.to_numeric(samples[left], errors="coerce")
        right_values = pd.to_numeric(samples[right], errors="coerce")
        high_regions = regions[
            InteractionDetailWidget._boolean_flags(regions["是否高风险"])
        ].sort_values("高风险排名").head(3)
        for _, region in high_regions.iterrows():
            current = (
                left_values.gt(float(region["参数A下界"]))
                & left_values.le(float(region["参数A上界"]))
                & right_values.gt(float(region["参数B下界"]))
                & right_values.le(float(region["参数B上界"]))
            )
            rank = int(region["高风险排名"])
            labels.loc[current & labels.eq("")] = f"高风险区域#{rank}"
            hit |= current
        return hit, labels

    @staticmethod
    def _display_columns(frame: pd.DataFrame, left: str, right: str) -> pd.DataFrame:
        preferred = [
            "样本分组", "命中风险区域", "是否缺陷", "dmc_raw", "product_id",
            "order_code", "global_order", "production_timestamp", "timestamp",
            left, right, "canonical_code", "defect_name", "analysis_scope", "target",
        ]
        columns = [column for column in preferred if column in frame]
        remaining = [column for column in frame.columns if column not in columns]
        return frame[columns + remaining]

    def _fill_samples(self, samples: pd.DataFrame, regions: pd.DataFrame, left: str, right: str) -> None:
        if samples.empty or regions.empty:
            empty = samples.iloc[0:0].copy()
            self.hit_samples.set_frame(empty)
            self.control_samples.set_frame(empty)
            self.sample_hint.setText("当前没有可解释的高风险区域样本。")
            return
        if not self._boolean_flags(regions["是否高风险"]).any():
            empty = samples.iloc[0:0].copy()
            self.hit_samples.set_frame(empty)
            self.control_samples.set_frame(empty)
            self.sample_hint.setText(
                "模型发现两参数联合后区分能力提高，但当前样本不足以定位可靠高风险区间。"
            )
            return
        hit_mask, labels = self._hit_mask(samples, regions, left, right)
        hits = samples.loc[hit_mask].copy()
        hits.insert(0, "命中风险区域", labels.loc[hit_mask])
        hits.insert(0, "样本分组", "命中高风险区间")
        outside = samples.loc[~hit_mask].copy()
        control_count = min(len(hits), len(outside))
        order_column = next((name for name in ("global_order", "production_timestamp", "timestamp") if name in samples), None)
        if control_count and order_column == "global_order" and not hits.empty:
            hit_orders = pd.to_numeric(hits[order_column], errors="coerce").dropna().to_numpy()
            outside_orders = pd.to_numeric(outside[order_column], errors="coerce")
            if len(hit_orders):
                outside["_control_distance"] = outside_orders.map(
                    lambda value: min(abs(value - hit_orders)) if pd.notna(value) else np.inf
                )
                controls = outside.sort_values(["_control_distance", order_column]).head(control_count).drop(columns="_control_distance")
            else:
                controls = outside.head(control_count)
        else:
            controls = outside.head(control_count)
        controls.insert(0, "命中风险区域", "区间外")
        controls.insert(0, "样本分组", "区间外附近对照")
        defect_column = "has_detected_defect"
        for frame in (hits, controls):
            if defect_column in frame:
                frame.insert(2, "是否缺陷", frame[defect_column].map({1: "缺陷", 0: "正常"}).fillna("未知"))
        self.hit_samples.set_frame(self._display_columns(hits, left, right))
        self.control_samples.set_frame(self._display_columns(controls, left, right))
        hit_defects = int(hits.get(defect_column, pd.Series(dtype=int)).sum())
        control_defects = int(controls.get(defect_column, pd.Series(dtype=int)).sum())
        self.sample_tabs.setTabText(0, f"命中高风险区间 ({len(hits)})")
        self.sample_tabs.setTabText(1, f"区间外对照 ({len(controls)})")
        self.sample_hint.setText(
            f"命中高风险区间：{hit_defects}/{len(hits)}件缺陷；"
            f"按生产顺序就近选择等量区间外对照：{control_defects}/{len(controls)}件缺陷。"
        )
