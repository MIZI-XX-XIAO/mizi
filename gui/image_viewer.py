"""本文件实现响应式A/E图片复核，支持后台加载、同步操作、布局切换和缺陷导航。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pandas as pd
from PySide6.QtCore import QObject, QRectF, QRunnable, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QBrush, QColor, QIcon, QImage, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QGraphicsPixmapItem, QGraphicsRectItem, QGraphicsScene,
    QGraphicsView, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListView, QListWidget,
    QListWidgetItem, QPushButton, QScrollArea,
    QSpinBox, QSplitter, QVBoxLayout, QWidget,
)

from src.contour_extractor import build_failure_mask, detection_profile, image_scale_to_reference, read_image


def _pixmap(image: np.ndarray) -> QPixmap:
    if image.ndim == 2:
        qimage = QImage(
            image.data, image.shape[1], image.shape[0], image.strides[0], QImage.Format_Grayscale8
        )
    else:
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        qimage = QImage(
            rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QImage.Format_RGB888
        )
    return QPixmap.fromImage(qimage.copy())


class ZoomGraphicsView(QGraphicsView):
    zoom_requested = Signal(float)
    fullscreen_requested = Signal()

    def __init__(self, title: str) -> None:
        super().__init__()
        self.title = title
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.scene = QGraphicsScene(self)
        self.setScene(self.scene)
        self.setMinimumSize(220, 160)

    def set_content(
        self, image: np.ndarray, boxes: list[tuple[float, float, float, float]] | None = None
    ) -> None:
        self.scene.clear()
        pixmap = _pixmap(image)
        self.scene.addItem(QGraphicsPixmapItem(pixmap))
        pen = QPen(Qt.green, 2)
        for x1, y1, x2, y2 in boxes or []:
            item = QGraphicsRectItem(QRectF(x1, y1, x2 - x1 + 1, y2 - y1 + 1))
            item.setPen(pen)
            self.scene.addItem(item)
        self.scene.setSceneRect(QRectF(pixmap.rect()))
        self.fit_content()

    def fit_content(self) -> None:
        if not self.scene.sceneRect().isEmpty():
            self.resetTransform()
            self.fitInView(self.scene.sceneRect(), Qt.KeepAspectRatio)

    def set_message(self, message: str) -> None:
        self.scene.clear()
        item = self.scene.addText(message)
        item.setDefaultTextColor(QColor("#dfe7ef"))
        self.scene.setSceneRect(item.boundingRect())
        self.fit_content()

    def apply_zoom(self, factor: float) -> None:
        self.scale(factor, factor)

    def wheelEvent(self, event) -> None:
        factor = 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
        self.apply_zoom(factor)
        self.zoom_requested.emit(factor)

    def mouseDoubleClickEvent(self, event) -> None:
        self.fullscreen_requested.emit()
        event.accept()


class FullscreenImageWindow(QWidget):
    """Borderless single-image viewer that preserves the review widget's state."""

    previous_requested = Signal()
    next_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent, Qt.Window | Qt.FramelessWindowHint)
        self.setObjectName("fullscreenImageWindow")
        self.image_type = "E图"
        self.view = ZoomGraphicsView("全屏图片")
        self.view.fullscreen_requested.connect(self.close)
        self.title_label = QLabel()
        self.title_label.setObjectName("fullscreenTitle")
        previous = QPushButton("上一证据")
        previous.clicked.connect(self.previous_requested)
        following = QPushButton("下一证据")
        following.clicked.connect(self.next_requested)
        close_button = QPushButton("退出全屏  Esc")
        close_button.clicked.connect(self.close)
        toolbar = QHBoxLayout()
        toolbar.addWidget(self.title_label, 1)
        toolbar.addWidget(previous)
        toolbar.addWidget(following)
        toolbar.addWidget(close_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addLayout(toolbar)
        layout.addWidget(self.view, 1)

    def set_content(
        self, image_type: str, order: int | None, image: np.ndarray,
        boxes: list[tuple[float, float, float, float]] | None = None,
    ) -> None:
        self.image_type = image_type
        order_text = "" if order is None else f"　产品 #{order}"
        self.title_label.setText(f"{image_type}{order_text}")
        self.view.set_content(image, boxes)

    def set_message(self, image_type: str, order: int | None, message: str) -> None:
        self.image_type = image_type
        order_text = "" if order is None else f"　产品 #{order}"
        self.title_label.setText(f"{image_type}{order_text}")
        self.view.set_message(message)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self.close()
            event.accept()
            return
        super().keyPressEvent(event)


class _LoadSignals(QObject):
    loaded = Signal(int, object)
    failed = Signal(int, str)


class _ImageLoadTask(QRunnable):
    def __init__(
        self, request_id: int, product: pd.Series, rows: pd.DataFrame,
        config: dict[str, Any], original_region: bool
    ) -> None:
        super().__init__()
        self.request_id = request_id
        self.product = product.copy()
        self.rows = rows.copy()
        self.config = dict(config)
        self.original_region = original_region
        self.signals = _LoadSignals()

    def run(self) -> None:
        try:
            source_column = "a_image_path" if "a_image_path" in self.product else "v_image_path"
            profile = detection_profile(self.config, str(self.product.get("camera", "")))
            a_flag = cv2.IMREAD_GRAYSCALE if source_column == "a_image_path" else cv2.IMREAD_COLOR
            a_full = read_image(Path(self.product[source_column]), a_flag)
            e_full = read_image(Path(self.product.e_image_path), cv2.IMREAD_COLOR)
            scale_x, scale_y = image_scale_to_reference(a_full, e_full)
            mask_full = build_failure_mask(e_full, e_full, profile)
            height, width = a_full.shape[:2]
            if self.original_region:
                center_x = int(self.rows.center_x.iloc[0]) if not self.rows.empty else width // 2
                center_y = int(self.rows.center_y.iloc[0]) if not self.rows.empty else height // 2
                x1, y1 = max(0, center_x - 512), max(0, center_y - 512)
                x2, y2 = min(width, x1 + 1024), min(height, y1 + 1024)
                x1, y1 = max(0, x2 - 1024), max(0, y2 - 1024)
                a_image = a_full[y1:y2, x1:x2].copy()
                e_aligned = cv2.resize(e_full, (width, height), interpolation=cv2.INTER_LINEAR)
                e_image = e_aligned[y1:y2, x1:x2].copy()
                mask_aligned = cv2.resize(mask_full, (width, height), interpolation=cv2.INTER_NEAREST)
                mask_image = mask_aligned[y1:y2, x1:x2].copy()
                boxes = [
                    (row.bbox_x1 - x1, row.bbox_y1 - y1, row.bbox_x2 - x1, row.bbox_y2 - y1)
                    for row in self.rows.itertuples(index=False)
                    if row.bbox_x2 >= x1 and row.bbox_x1 < x2
                    and row.bbox_y2 >= y1 and row.bbox_y1 < y2
                ]
                region_text = f"原图区域 x={x1}:{x2}, y={y1}:{y2}"
            else:
                a_image = cv2.resize(a_full, (e_full.shape[1], e_full.shape[0]), interpolation=cv2.INTER_AREA)
                e_image = e_full.copy()
                mask_image = mask_full
                boxes = [
                    (row.bbox_x1 / scale_x, row.bbox_y1 / scale_y,
                     row.bbox_x2 / scale_x, row.bbox_y2 / scale_y)
                    for row in self.rows.itertuples(index=False)
                ]
                region_text = f"E图检测尺度 {e_full.shape[1]}×{e_full.shape[0]}"
            del a_full, e_full
            a_bgr = cv2.cvtColor(a_image, cv2.COLOR_GRAY2BGR) if a_image.ndim == 2 else a_image
            payload = {
                "a": a_image, "e": e_image,
                "diff": cv2.absdiff(e_image, a_bgr),
                "mask": mask_image,
                "boxes": boxes, "region_text": region_text,
            }
            self.signals.loaded.emit(self.request_id, payload)
        except Exception as exc:
            self.signals.failed.emit(self.request_id, str(exc))


class ImageReviewWidget(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.products = pd.DataFrame()
        self.detections = pd.DataFrame()
        self.process_data = pd.DataFrame()
        self.station_events = pd.DataFrame()
        self.station_parameters = pd.DataFrame()
        self.package_data = pd.DataFrame()
        self.config: dict[str, Any] = {}
        self.current_order: int | None = None
        self.pattern_record: pd.Series | None = None
        self.pattern_orders: list[int] = []
        self._request_id = 0
        self._payload: dict[str, Any] | None = None
        self._payload_order: int | None = None
        self._fullscreen: FullscreenImageWindow | None = None
        self._fullscreen_after_load: str | None = None
        self._thread_pool = QThreadPool.globalInstance()
        self.views = {
            "A图": ZoomGraphicsView("A图"), "E图": ZoomGraphicsView("E图"),
            "差异图": ZoomGraphicsView("差异图"), "Mask": ZoomGraphicsView("Mask"),
        }
        self.cards: dict[str, QWidget] = {}
        for name, view in self.views.items():
            card = QWidget()
            card.setObjectName("imageCard")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(2, 2, 2, 2)
            title = QLabel(name)
            title.setObjectName("viewTitle")
            fullscreen = QPushButton("全屏")
            fullscreen.setObjectName("fullscreenButton")
            fullscreen.clicked.connect(lambda _checked=False, image_type=name: self.open_fullscreen(image_type))
            title_row = QHBoxLayout()
            title_row.addWidget(title)
            title_row.addStretch()
            title_row.addWidget(fullscreen)
            card_layout.addLayout(title_row)
            card_layout.addWidget(view, 1)
            self.cards[name] = card
            view.fullscreen_requested.connect(lambda image_type=name: self.open_fullscreen(image_type))
        for source_name, source_view in self.views.items():
            source_view.zoom_requested.connect(
                lambda factor, name=source_name: self._sync_zoom(name, factor)
            )
            source_view.horizontalScrollBar().valueChanged.connect(
                lambda value, name=source_name: self._sync_scroll(name, "horizontal", value)
            )
            source_view.verticalScrollBar().valueChanged.connect(
                lambda value, name=source_name: self._sync_scroll(name, "vertical", value)
            )

        self.info = QLabel("尚未加载分析结果")
        self.info.setWordWrap(True)
        self.search_edit = QLineEdit()
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setPlaceholderText("搜索完整或部分 Ident No.")
        self.search_edit.textChanged.connect(self._filter_product_list)
        self.product_list = QListWidget()
        self.product_list.setObjectName("productThumbnailList")
        self.product_list.setIconSize(QSize(96, 64))
        self.product_list.setMinimumWidth(210)
        self.product_list.currentItemChanged.connect(self._select_product_item)
        self.order_spin = QSpinBox()
        self.order_spin.setKeyboardTracking(False)
        self.layout_combo = QComboBox()
        self.layout_combo.addItems(["2×2", "A/E左右对比", "单图放大"])
        self.layout_combo.currentTextChanged.connect(self._apply_layout)
        self.single_combo = QComboBox()
        self.single_combo.addItems(self.views.keys())
        self.single_combo.currentTextChanged.connect(self._apply_layout)
        self.overlay_check = QCheckBox("检测框")
        self.overlay_check.setChecked(True)
        self.overlay_check.toggled.connect(self._render_payload)
        self.diff_check = QCheckBox("差异图")
        self.diff_check.setChecked(True)
        self.diff_check.toggled.connect(self._apply_layout)
        self.mask_check = QCheckBox("Mask")
        self.mask_check.setChecked(True)
        self.mask_check.toggled.connect(self._apply_layout)

        self.pattern_panel = QWidget()
        self.pattern_panel.setObjectName("patternEvidencePanel")
        self.pattern_summary = QLabel()
        self.pattern_summary.setObjectName("patternSummary")
        self.pattern_summary.setWordWrap(True)
        self.pattern_list = QListWidget()
        self.pattern_list.setObjectName("patternEvidenceList")
        self.pattern_list.setViewMode(QListView.IconMode)
        self.pattern_list.setFlow(QListView.LeftToRight)
        self.pattern_list.setWrapping(False)
        self.pattern_list.setResizeMode(QListView.Adjust)
        self.pattern_list.setMovement(QListView.Static)
        self.pattern_list.setIconSize(QSize(128, 82))
        self.pattern_list.setFixedHeight(132)
        self.pattern_list.currentItemChanged.connect(self._select_pattern_item)
        self.pattern_list.itemDoubleClicked.connect(self._open_pattern_item_fullscreen)
        previous_evidence = QPushButton("上一证据")
        previous_evidence.clicked.connect(lambda: self._jump_pattern_evidence(-1))
        next_evidence = QPushButton("下一证据")
        next_evidence.clicked.connect(lambda: self._jump_pattern_evidence(1))
        exit_pattern = QPushButton("退出规律复核")
        exit_pattern.clicked.connect(self.exit_pattern_review)
        pattern_toolbar = QHBoxLayout()
        pattern_toolbar.addWidget(self.pattern_summary, 1)
        pattern_toolbar.addWidget(previous_evidence)
        pattern_toolbar.addWidget(next_evidence)
        pattern_toolbar.addWidget(exit_pattern)
        pattern_layout = QVBoxLayout(self.pattern_panel)
        pattern_layout.setContentsMargins(8, 8, 8, 8)
        pattern_layout.addLayout(pattern_toolbar)
        pattern_layout.addWidget(self.pattern_list)
        self.pattern_panel.setVisible(False)

        controls = QHBoxLayout()
        for title, callback in (
            ("上一片", lambda: self.jump_to(self.order_spin.value() - 1)),
            ("下一片", lambda: self.jump_to(self.order_spin.value() + 1)),
            ("上一缺陷", lambda: self._jump_defect(-1)),
            ("下一缺陷", lambda: self._jump_defect(1)),
        ):
            button = QPushButton(title)
            button.clicked.connect(callback)
            controls.addWidget(button)
        controls.addWidget(QLabel("产品序号"))
        controls.addWidget(self.order_spin)
        jump = QPushButton("跳转")
        jump.clicked.connect(lambda: self.jump_to(self.order_spin.value()))
        controls.addWidget(jump)
        fit_button = QPushButton("适应窗口")
        fit_button.clicked.connect(self._fit_all)
        controls.addWidget(fit_button)
        original = QPushButton("加载原图局部")
        original.clicked.connect(lambda: self._load_current(True))
        controls.addWidget(original)
        controls.addStretch()

        display_controls = QHBoxLayout()
        display_controls.addWidget(QLabel("布局"))
        display_controls.addWidget(self.layout_combo)
        display_controls.addWidget(self.single_combo)
        display_controls.addWidget(self.overlay_check)
        display_controls.addWidget(self.diff_check)
        display_controls.addWidget(self.mask_check)
        display_controls.addStretch()
        self.view_grid = QGridLayout()
        self.view_grid.setContentsMargins(0, 0, 0, 0)
        view_container = QWidget()
        view_container.setLayout(self.view_grid)
        self.detail_label = QLabel("请选择分析结果中的产品。")
        self.detail_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.detail_label.setWordWrap(True)
        detail_scroll = QScrollArea()
        detail_scroll.setWidgetResizable(True)
        detail_widget = QWidget()
        detail_widget.setObjectName("imageDetailPanel")
        detail_layout = QVBoxLayout(detail_widget)
        detail_title = QLabel("产品、缺陷与工艺参数")
        detail_title.setObjectName("sectionTitle")
        detail_layout.addWidget(detail_title)
        detail_layout.addWidget(self.detail_label)
        detail_layout.addStretch()
        detail_scroll.setWidget(detail_widget)
        browser = QWidget()
        browser_layout = QVBoxLayout(browser)
        browser_layout.setContentsMargins(0, 0, 0, 0)
        browser_layout.addWidget(QLabel("按真实生产顺序浏览"))
        browser_layout.addWidget(self.search_edit)
        browser_layout.addWidget(self.product_list, 1)
        splitter = QSplitter(Qt.Horizontal)
        splitter.setObjectName("reviewSplitter")
        splitter.addWidget(browser)
        splitter.addWidget(view_container)
        splitter.addWidget(detail_scroll)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 5)
        splitter.setStretchFactor(2, 2)
        splitter.setSizes([230, 900, 330])

        layout = QVBoxLayout(self)
        layout.addLayout(controls)
        layout.addLayout(display_controls)
        layout.addWidget(self.pattern_panel)
        layout.addWidget(self.info)
        layout.addWidget(splitter, 1)
        self._apply_layout()

    def set_data(
        self, products: pd.DataFrame, detections: pd.DataFrame, config: dict[str, Any]
    ) -> None:
        self.exit_pattern_review()
        self._payload = None
        self._payload_order = None
        self.products, self.detections, self.config = products.copy(), detections.copy(), dict(config)
        self._populate_product_list()
        if self.products.empty:
            return
        self.order_spin.setRange(
            int(self.products.global_order.min()), int(self.products.global_order.max())
        )
        first = (
            int(self.detections.global_order.iloc[0])
            if not self.detections.empty else int(self.products.global_order.iloc[0])
        )
        self.jump_to(first)

    @staticmethod
    def _parse_orders(value: Any) -> list[int]:
        if value is None or pd.isna(value):
            return []
        orders: list[int] = []
        for part in str(value).split(";"):
            try:
                orders.append(int(float(part.strip())))
            except (TypeError, ValueError):
                continue
        return sorted(set(orders))

    def show_pattern(self, record: pd.Series) -> bool:
        """Enter pattern-review mode and select the first available evidence image."""
        self.exit_pattern_review()
        if self.products.empty:
            return False
        observed: list[int] = []
        for key in (
            "observed_orders", "evidence_task_orders", "task_orders",
            "support_task_orders", "global_order", "first_order",
        ):
            observed = self._parse_orders(record.get(key))
            if observed:
                break
        missing: list[int] = []
        for key in ("inferred_missing_orders", "missing_task_orders"):
            missing = self._parse_orders(record.get(key))
            if missing:
                break
        if not observed and not missing:
            return False
        self.pattern_record = record.copy()
        available = set(self.products.global_order.astype(int))
        self.pattern_list.blockSignals(True)
        for order in sorted(set(observed + missing)):
            status = "missing" if order in missing else "observed"
            matches = self.products[self.products.global_order.astype(int).eq(order)]
            product = matches.iloc[0] if not matches.empty else pd.Series(dtype=object)
            if str(product.get("evidence_status", "")) in {"unavailable", "ambiguous_event"}:
                status = "unavailable"
            production = product.get("production_order")
            production_text = "—" if pd.isna(production) else str(int(float(production)))
            status_text = {"missing": "周期缺失", "observed": "规律命中", "unavailable": "无法判定"}[status]
            caption = f"生产 #{production_text} / 任务 #{order}\n{status_text}"
            item = QListWidgetItem(caption)
            item.setData(Qt.UserRole, order)
            item.setData(Qt.UserRole + 1, status)
            item.setTextAlignment(Qt.AlignCenter)
            item.setSizeHint(QSize(154, 112))
            if status in {"missing", "unavailable"}:
                item.setBackground(QBrush(QColor("#e5e7eb")))
                item.setForeground(QBrush(QColor("#4b5563")))
                item.setToolTip(
                    "缺少有效图片或无法唯一匹配MES事件，不计为周期缺失"
                    if status == "unavailable" else "该周期位置图片有效，但算法未检出规律缺陷"
                )
            else:
                item.setBackground(QBrush(QColor("#eef6ff")))
            if not matches.empty:
                path = str(matches.iloc[0].get("e_image_path", ""))
                if path and Path(path).is_file():
                    item.setIcon(QIcon(path))
                else:
                    item.setToolTip("E图文件不存在，可点击查看其他可用视图")
            else:
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
                item.setToolTip("当前数据范围内没有该产品")
            self.pattern_list.addItem(item)
        self.pattern_list.blockSignals(False)
        self.pattern_orders = [
            int(self.pattern_list.item(index).data(Qt.UserRole))
            for index in range(self.pattern_list.count())
            if self.pattern_list.item(index).flags() & Qt.ItemIsEnabled
        ]
        pattern_type = str(record.get("pattern_type", "-"))
        period = record.get("period")
        period_text = "—" if pd.isna(period) else str(int(float(period)))
        confidence = record.get("confidence")
        confidence_text = "—" if pd.isna(confidence) else f"{float(confidence):.1%}"
        evidence_id = record.get(
            "pattern_id", record.get("trajectory_id", record.get("evidence_id", "-"))
        )
        spatial_id = record.get("cluster_id", record.get("spatial_id", "-"))
        code = record.get("canonical_code", "-")
        self.pattern_summary.setText(
            f"证据 {evidence_id}　类型：{pattern_type}　"
            f"代码：{code}　空间：{spatial_id}　周期：{period_text}　"
            f"置信度：{confidence_text}　命中：{len(observed)}　缺失：{len(missing)}"
        )
        self.pattern_panel.setVisible(True)
        target = next((order for order in observed if order in available), None)
        if target is None:
            target = next((order for order in missing if order in available), None)
        if target is None:
            self.exit_pattern_review()
            return False
        self._set_pattern_current(target)
        self.jump_to(target)
        return True

    def exit_pattern_review(self) -> None:
        self.pattern_record = None
        self.pattern_orders = []
        self.pattern_list.blockSignals(True)
        self.pattern_list.clear()
        self.pattern_list.blockSignals(False)
        self.pattern_panel.setVisible(False)

    def _set_pattern_current(self, order: int) -> None:
        for index in range(self.pattern_list.count()):
            item = self.pattern_list.item(index)
            if int(item.data(Qt.UserRole)) == order:
                self.pattern_list.blockSignals(True)
                self.pattern_list.setCurrentItem(item)
                self.pattern_list.scrollToItem(item)
                self.pattern_list.blockSignals(False)
                break

    def _select_pattern_item(self, current: QListWidgetItem | None, _previous=None) -> None:
        if current is not None and current.flags() & Qt.ItemIsEnabled:
            self.jump_to(int(current.data(Qt.UserRole)))

    def _open_pattern_item_fullscreen(self, item: QListWidgetItem) -> None:
        if not item.flags() & Qt.ItemIsEnabled:
            return
        self._fullscreen_after_load = "E图"
        self.jump_to(int(item.data(Qt.UserRole)))

    def _jump_pattern_evidence(self, direction: int) -> None:
        if not self.pattern_orders:
            return
        try:
            index = self.pattern_orders.index(int(self.current_order))
        except (ValueError, TypeError):
            index = 0 if direction > 0 else len(self.pattern_orders) - 1
        else:
            index = max(0, min(len(self.pattern_orders) - 1, index + direction))
        self.jump_to(self.pattern_orders[index])

    def set_process_data(self, frame: pd.DataFrame) -> None:
        self.process_data = frame.copy()
        self._update_details()

    def set_station_history(
        self, events: pd.DataFrame, parameters: pd.DataFrame,
        package: pd.DataFrame | None = None,
    ) -> None:
        self.station_events = events.copy()
        self.station_parameters = parameters.copy()
        self.package_data = package.copy() if package is not None else pd.DataFrame()
        self._update_details()

    def _populate_product_list(self) -> None:
        self.product_list.blockSignals(True)
        self.product_list.clear()
        if not self.products.empty:
            for row in self.products.sort_values("global_order").itertuples(index=False):
                dmc = str(getattr(row, "dmc_raw", getattr(row, "order_code", "")))
                scope = str(getattr(row, "analysis_scope", getattr(row, "camera", "")))
                state = str(getattr(row, "aoi_state", "") or "未匹配")
                timestamp = getattr(row, "aoi_test_date", None)
                time_text = "" if pd.isna(timestamp) else pd.Timestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
                item = QListWidgetItem(f"[{scope}] #{int(row.global_order)}  {state}\n{dmc}\n{time_text}")
                item.setData(Qt.UserRole, int(row.global_order))
                item.setData(Qt.UserRole + 1, dmc.lower())
                image_path = getattr(row, "e_image_path", "")
                if image_path and Path(str(image_path)).is_file():
                    item.setIcon(QIcon(str(image_path)))
                self.product_list.addItem(item)
        self.product_list.blockSignals(False)
        self._filter_product_list(self.search_edit.text())

    def _filter_product_list(self, text: str) -> None:
        query = text.strip().lower()
        first_visible = None
        for index in range(self.product_list.count()):
            item = self.product_list.item(index)
            visible = not query or query in str(item.data(Qt.UserRole + 1))
            item.setHidden(not visible)
            if visible and first_visible is None:
                first_visible = item
        if query and first_visible is not None:
            self.product_list.setCurrentItem(first_visible)

    def _select_product_item(self, current: QListWidgetItem | None, _previous=None) -> None:
        if current is not None:
            self.jump_to(int(current.data(Qt.UserRole)))

    def jump_to(self, order: int) -> None:
        if self.products.empty:
            return
        orders = self.products.global_order.astype(int).tolist()
        if order not in orders:
            order = min(orders, key=lambda value: abs(value - order))
        self.order_spin.setValue(order)
        self.current_order = order
        if self.pattern_record is not None:
            self._set_pattern_current(order)
        for index in range(self.product_list.count()):
            item = self.product_list.item(index)
            if int(item.data(Qt.UserRole)) == order:
                self.product_list.blockSignals(True)
                self.product_list.setCurrentItem(item)
                self.product_list.scrollToItem(item)
                self.product_list.blockSignals(False)
                break
        self._load_current(False)

    def _load_current(self, original_region: bool) -> None:
        if self.current_order is None or self.products.empty:
            return
        self._request_id += 1
        request_id = self._request_id
        product = self.products[self.products.global_order == self.current_order].iloc[0]
        rows = self.detections[self.detections.global_order == self.current_order]
        self.info.setText(f"正在后台加载 #{self.current_order}…")
        task = _ImageLoadTask(request_id, product, rows, self.config, original_region)
        task.signals.loaded.connect(self._loaded)
        task.signals.failed.connect(self._load_failed)
        self._thread_pool.start(task)
        self._update_details()

    def _loaded(self, request_id: int, payload: dict[str, Any]) -> None:
        if request_id != self._request_id:
            return
        self._payload = payload
        self._payload_order = self.current_order
        self._render_payload()
        rows = self.detections[self.detections.global_order == self.current_order]
        areas = ",".join(str(int(value)) for value in rows.component_area.tolist()) if not rows.empty else "无"
        self.info.setText(
            f"#{self.current_order}　检测框：{len(rows)}　E图面积：{areas}　{payload['region_text']}"
        )
        if self._fullscreen_after_load:
            image_type = self._fullscreen_after_load
            self._fullscreen_after_load = None
            self.open_fullscreen(image_type)
        elif self._fullscreen is not None and self._fullscreen.isVisible():
            self._update_fullscreen(self._fullscreen.image_type)

    def _load_failed(self, request_id: int, message: str) -> None:
        if request_id == self._request_id:
            self.info.setText(f"图片加载失败：{message}")
            image_type = self._fullscreen_after_load
            self._fullscreen_after_load = None
            if image_type:
                self._show_fullscreen_error(image_type, message)
            elif self._fullscreen is not None and self._fullscreen.isVisible():
                self._fullscreen.set_message(
                    self._fullscreen.image_type, self.current_order, f"图片加载失败\n{message}"
                )

    def _render_payload(self) -> None:
        if not self._payload:
            return
        boxes = self._payload["boxes"] if self.overlay_check.isChecked() else []
        self.views["A图"].set_content(self._payload["a"])
        self.views["E图"].set_content(self._payload["e"], boxes)
        self.views["差异图"].set_content(self._payload["diff"])
        self.views["Mask"].set_content(self._payload["mask"], boxes)

    def _update_fullscreen(self, image_type: str) -> None:
        if not self._payload or self._fullscreen is None:
            return
        payload_key = {"A图": "a", "E图": "e", "差异图": "diff", "Mask": "mask"}.get(image_type)
        if payload_key is None:
            return
        boxes = (
            self._payload["boxes"]
            if image_type in {"E图", "Mask"} and self.overlay_check.isChecked() else []
        )
        self._fullscreen.set_content(
            image_type, self.current_order, self._payload[payload_key], boxes
        )

    def open_fullscreen(self, image_type: str) -> None:
        """Open one rendered review image in a borderless full-screen window."""
        if image_type not in {"A图", "E图", "差异图", "Mask"}:
            return
        if not self._payload or self._payload_order != self.current_order:
            self._fullscreen_after_load = image_type
            self._load_current(False)
            return
        if self._fullscreen is None:
            self._fullscreen = FullscreenImageWindow(self)
            self._fullscreen.previous_requested.connect(lambda: self._jump_pattern_evidence(-1))
            self._fullscreen.next_requested.connect(lambda: self._jump_pattern_evidence(1))
        self._update_fullscreen(image_type)
        self._fullscreen.showFullScreen()
        self._fullscreen.raise_()
        self._fullscreen.activateWindow()

    def _show_fullscreen_error(self, image_type: str, message: str) -> None:
        if self._fullscreen is None:
            self._fullscreen = FullscreenImageWindow(self)
            self._fullscreen.previous_requested.connect(lambda: self._jump_pattern_evidence(-1))
            self._fullscreen.next_requested.connect(lambda: self._jump_pattern_evidence(1))
        self._fullscreen.set_message(image_type, self.current_order, f"图片加载失败\n{message}")
        self._fullscreen.showFullScreen()

    def _apply_layout(self, *_args) -> None:
        while self.view_grid.count():
            self.view_grid.takeAt(0)
        for card in self.cards.values():
            card.setVisible(False)
        mode = self.layout_combo.currentText()
        if mode == "A/E左右对比":
            names, positions = ["A图", "E图"], [(0, 0), (0, 1)]
        elif mode == "单图放大":
            names, positions = [self.single_combo.currentText()], [(0, 0)]
        else:
            names = ["A图", "E图"]
            if self.diff_check.isChecked():
                names.append("差异图")
            if self.mask_check.isChecked():
                names.append("Mask")
            positions = [(index // 2, index % 2) for index in range(len(names))]
        self.single_combo.setVisible(mode == "单图放大")
        for name, position in zip(names, positions):
            self.cards[name].setVisible(True)
            self.view_grid.addWidget(self.cards[name], *position)

    def _sync_zoom(self, source_name: str, factor: float) -> None:
        for name, view in self.views.items():
            if name != source_name and view.isVisible():
                view.apply_zoom(factor)

    def _sync_scroll(self, source_name: str, orientation: str, value: int) -> None:
        for name, view in self.views.items():
            if name == source_name or not view.isVisible():
                continue
            scrollbar = (
                view.horizontalScrollBar() if orientation == "horizontal"
                else view.verticalScrollBar()
            )
            scrollbar.setValue(value)

    def _jump_defect(self, direction: int) -> None:
        if self.current_order is None or self.detections.empty:
            return
        orders = sorted(self.detections.global_order.astype(int).unique())
        candidates = (
            [order for order in orders if order < self.current_order]
            if direction < 0 else [order for order in orders if order > self.current_order]
        )
        if candidates:
            self.jump_to(candidates[-1] if direction < 0 else candidates[0])

    def _update_details(self) -> None:
        if self.current_order is None or self.products.empty:
            return
        product = self.products[self.products.global_order == self.current_order].iloc[0]
        rows = self.detections[self.detections.global_order == self.current_order]
        lines = [
            f"<b>任务序号：</b>{product.get('task_order', self.current_order)}",
            f"<b>生产位次：</b>{product.get('production_order', '—')}",
            f"<b>分析范围：</b>{product.get('analysis_scope', product.get('camera', '-'))}",
            f"<b>Ident No.：</b>{product.get('dmc_raw', product.get('order_code', '-'))}",
            f"<b>产品编码：</b>{product.get('order_code', '-')}",
            f"<b>相机：</b>{product.get('camera', '-')}",
            f"<b>批次：</b>{product.get('batch', product.get('batch_id', '-'))}",
            f"<b>证据状态：</b>{product.get('evidence_status', 'evaluable')}",
            f"<b>AOI代码：</b>{product.get('aoi_failure_code', product.get('Result.AOIFailureCode', '-'))}",
            f"<b>VI代码：</b>{product.get('vi_defect_code', '-')}",
            f"<b>缺陷数量：</b>{len(rows)}",
        ]
        aoi_state = str(product.get("aoi_state", "") or "未匹配")
        evaluable_rows = (
            rows[rows["detection_type"].ne("region_anomaly")]
            if "detection_type" in rows else rows
        )
        algorithm = "检出" if not evaluable_rows.empty else "未检出"
        if aoi_state == "NOK": verdict = "命中" if algorithm == "检出" else "漏检候选"
        elif aoi_state == "OK": verdict = "误报候选" if algorithm == "检出" else "正确无缺陷"
        else: verdict = "不可评价"
        lines.extend([
            "<br><b>算法验真</b>",
            f"AOI当站结果：{aoi_state}",
            f"算法结果：{algorithm}",
            f"对比结论：{verdict}",
            f"真值匹配：{product.get('truth_match', '未提供')}",
        ])
        for index, row in enumerate(rows.itertuples(index=False), 1):
            type_name = {
                "micro": "微小缺陷", "local": "局部缺陷", "region_anomaly": "区域异常",
            }.get(getattr(row, "detection_type", "local"), "局部缺陷")
            cluster_id = getattr(row, "cluster_id", "") or "—"
            lines.append(
                f"<br><b>缺陷 {index}</b><br>"
                f"类型：{type_name}<br>"
                f"位置：({row.center_x:.1f}, {row.center_y:.1f})<br>"
                f"E图检测面积：{int(row.component_area)} px²<br>"
                f"空间簇：{cluster_id}"
            )
        if not self.process_data.empty and "global_order" in self.process_data:
            matches = self.process_data[self.process_data.global_order == self.current_order]
            if not matches.empty:
                lines.append("<br><b>工艺参数</b>")
                process_row = matches.iloc[0]
                excluded = {
                    "global_order", "a_image_path", "v_image_path", "e_image_path",
                    "has_detected_defect", "detected_defect_count",
                }
                for name, value in process_row.items():
                    if name not in excluded and pd.notna(value) and isinstance(
                        value, (int, float, np.integer, np.floating)
                    ):
                        lines.append(f"{name}：{value}")
        dmc = str(product.get("dmc_raw", product.get("order_code", "")))
        if not self.station_events.empty and "dmc_raw" in self.station_events:
            history = self.station_events[self.station_events.dmc_raw.astype(str).eq(dmc)].copy()
            if not history.empty:
                history = history.sort_values("test_date", na_position="last", kind="stable")
                lines.append("<br><b>跨工站真实时间线</b>")
                for _, event in history.iterrows():
                    stamp = event.get("test_date")
                    stamp_text = "时间未知" if pd.isna(stamp) else pd.Timestamp(stamp).strftime("%Y-%m-%d %H:%M:%S")
                    result_bits = []
                    for name, value in event.items():
                        normalized = "".join(character.lower() for character in str(name) if character.isalnum())
                        if any(token in normalized for token in ("failurecode", "failurescode", "blockcode")):
                            if pd.notna(value) and str(value).strip():
                                result_bits.append(f"{name}={value}")
                    suffix = "；" + "；".join(result_bits) if result_bits else ""
                    lines.append(
                        f"{stamp_text}　{event.get('station_id') or event.get('station_title', '')}　"
                        f"{event.get('state', 'UNKNOWN')}{suffix}"
                    )
                if not self.station_parameters.empty:
                    params = self.station_parameters[
                        self.station_parameters.dmc_raw.astype(str).eq(dmc)
                    ]
                    numeric = params.dropna(subset=["numeric_value"]) if "numeric_value" in params else pd.DataFrame()
                    if not numeric.empty:
                        lines.append("<br><b>最近工艺/检测参数（最多20项）</b>")
                        for param in numeric.sort_values("test_date").tail(20).itertuples(index=False):
                            lines.append(
                                f"{param.station_id}.{param.parameter_name}：{param.numeric_value}"
                                + (f"（规格 {param.tolerance_raw}）" if param.tolerance_raw else "")
                            )
        if not self.package_data.empty and "dmc_raw" in self.package_data:
            package = self.package_data[self.package_data.dmc_raw.astype(str).eq(dmc)]
            if not package.empty:
                lines.append("<br><b>包装：</b>已有包装记录")
        self.detail_label.setText("<br>".join(lines))

    def _fit_all(self) -> None:
        for view in self.views.values():
            view.fit_content()
