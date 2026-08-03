"""本文件实现响应式A/E图片复核，支持后台加载、同步操作、布局切换和缺陷导航。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pandas as pd
from PySide6.QtCore import QObject, QRectF, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QImage, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QGraphicsPixmapItem, QGraphicsRectItem, QGraphicsScene,
    QGraphicsView, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSpinBox, QSplitter, QVBoxLayout, QWidget,
)

from src.contour_extractor import build_failure_mask, read_image


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

    def apply_zoom(self, factor: float) -> None:
        self.scale(factor, factor)

    def wheelEvent(self, event) -> None:
        factor = 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
        self.apply_zoom(factor)
        self.zoom_requested.emit(factor)


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
            a_flag = cv2.IMREAD_GRAYSCALE if source_column == "a_image_path" else cv2.IMREAD_COLOR
            a_full = read_image(Path(self.product[source_column]), a_flag)
            e_full = read_image(Path(self.product.e_image_path), cv2.IMREAD_COLOR)
            if a_full.shape[:2] != e_full.shape[:2]:
                raise ValueError(f"A/E尺寸不一致：{a_full.shape[:2]} vs {e_full.shape[:2]}")
            height, width = a_full.shape[:2]
            if self.original_region:
                center_x = int(self.rows.center_x.iloc[0]) if not self.rows.empty else width // 2
                center_y = int(self.rows.center_y.iloc[0]) if not self.rows.empty else height // 2
                x1, y1 = max(0, center_x - 512), max(0, center_y - 512)
                x2, y2 = min(width, x1 + 1024), min(height, y1 + 1024)
                x1, y1 = max(0, x2 - 1024), max(0, y2 - 1024)
                a_image = a_full[y1:y2, x1:x2].copy()
                e_image = e_full[y1:y2, x1:x2].copy()
                boxes = [
                    (row.bbox_x1 - x1, row.bbox_y1 - y1, row.bbox_x2 - x1, row.bbox_y2 - y1)
                    for row in self.rows.itertuples(index=False)
                    if row.bbox_x2 >= x1 and row.bbox_x1 < x2
                    and row.bbox_y2 >= y1 and row.bbox_y1 < y2
                ]
                region_text = f"原图区域 x={x1}:{x2}, y={y1}:{y2}"
            else:
                scale = min(1.0, 2560 / width, 1440 / height)
                size = (max(1, round(width * scale)), max(1, round(height * scale)))
                a_image = cv2.resize(a_full, size, interpolation=cv2.INTER_AREA)
                e_image = cv2.resize(e_full, size, interpolation=cv2.INTER_AREA)
                boxes = [
                    (row.bbox_x1 * scale, row.bbox_y1 * scale,
                     row.bbox_x2 * scale, row.bbox_y2 * scale)
                    for row in self.rows.itertuples(index=False)
                ]
                region_text = f"预览缩放 {scale:.2f}"
            del a_full, e_full
            a_bgr = cv2.cvtColor(a_image, cv2.COLOR_GRAY2BGR) if a_image.ndim == 2 else a_image
            payload = {
                "a": a_image, "e": e_image,
                "diff": cv2.absdiff(e_image, a_bgr),
                "mask": build_failure_mask(a_image, e_image, self.config),
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
        self.config: dict[str, Any] = {}
        self.current_order: int | None = None
        self._request_id = 0
        self._payload: dict[str, Any] | None = None
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
            card_layout.addWidget(title)
            card_layout.addWidget(view, 1)
            self.cards[name] = card
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
        splitter = QSplitter(Qt.Horizontal)
        splitter.setObjectName("reviewSplitter")
        splitter.addWidget(view_container)
        splitter.addWidget(detail_scroll)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([1000, 260])

        layout = QVBoxLayout(self)
        layout.addLayout(controls)
        layout.addLayout(display_controls)
        layout.addWidget(self.info)
        layout.addWidget(splitter, 1)
        self._apply_layout()

    def set_data(
        self, products: pd.DataFrame, detections: pd.DataFrame, config: dict[str, Any]
    ) -> None:
        self.products, self.detections, self.config = products.copy(), detections.copy(), dict(config)
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

    def set_process_data(self, frame: pd.DataFrame) -> None:
        self.process_data = frame.copy()
        self._update_details()

    def jump_to(self, order: int) -> None:
        if self.products.empty:
            return
        orders = self.products.global_order.astype(int).tolist()
        if order not in orders:
            order = min(orders, key=lambda value: abs(value - order))
        self.order_spin.setValue(order)
        self.current_order = order
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
        self._render_payload()
        rows = self.detections[self.detections.global_order == self.current_order]
        areas = ",".join(str(int(value)) for value in rows.component_area.tolist()) if not rows.empty else "无"
        self.info.setText(
            f"#{self.current_order}　检测框：{len(rows)}　面积：{areas}　{payload['region_text']}"
        )

    def _load_failed(self, request_id: int, message: str) -> None:
        if request_id == self._request_id:
            self.info.setText(f"图片加载失败：{message}")

    def _render_payload(self) -> None:
        if not self._payload:
            return
        boxes = self._payload["boxes"] if self.overlay_check.isChecked() else []
        self.views["A图"].set_content(self._payload["a"])
        self.views["E图"].set_content(self._payload["e"], boxes)
        self.views["差异图"].set_content(self._payload["diff"])
        self.views["Mask"].set_content(self._payload["mask"], boxes)

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
            f"<b>产品序号：</b>{self.current_order}",
            f"<b>产品编码：</b>{product.get('order_code', '-')}",
            f"<b>相机：</b>{product.get('camera', '-')}",
            f"<b>批次：</b>{product.get('batch', product.get('batch_id', '-'))}",
            f"<b>缺陷数量：</b>{len(rows)}",
        ]
        for index, row in enumerate(rows.itertuples(index=False), 1):
            lines.append(
                f"<br><b>缺陷 {index}</b><br>"
                f"位置：({row.center_x:.1f}, {row.center_y:.1f})<br>"
                f"面积：{int(row.component_area)} px²<br>"
                f"空间簇：{getattr(row, 'cluster_id', '-')}"
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
        self.detail_label.setText("<br>".join(lines))

    def _fit_all(self) -> None:
        for view in self.views.values():
            view.fit_content()
