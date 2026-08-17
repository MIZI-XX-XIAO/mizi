"""本文件提供分析参数的类型化编辑、默认恢复、YAML加载和禁止覆盖式另存功能。"""

from pathlib import Path
from typing import Any

import yaml
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QMessageBox, QPushButton, QScrollArea, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from src.contour_extractor import DETECTION_PROFILE_NAMES, normalize_analysis_config


INTEGER_FIELDS = {
    "red_min": (0, 255), "red_dominance": (0, 255),
    "min_component_area": (1, 10_000_000), "max_component_area": (1, 100_000_000),
    "micro_max_component_area": (1, 10_000_000),
    "minimum_repeat_occurrences": (2, 10_000), "minimum_period": (1, 1_000_000),
    "maximum_period": (1, 1_000_000), "burst_minimum_length": (2, 100_000),
    "warning_lead_products": (0, 1_000_000),
    "morph_kernel_size": (1, 31), "period_order_tolerance": (0, 1000),
    "missing_order_tolerance": (0, 1000), "preview_count": (0, 1000),
    "trajectory_min_occurrences": (2, 10_000),
}
FLOAT_FIELDS = {
    "region_min_width_ratio": (0.01, 1.0, 3),
    "region_min_height_ratio": (0.01, 1.0, 3),
    "spatial_cluster_radius_norm": (0.000001, 1.0, 6),
    "minimum_period_precision": (0.0, 1.0, 4),
    "minimum_period_coverage": (0.0, 1.0, 4),
    "evaluation_center_tolerance_px": (0.0, 10000.0, 2),
    "registration_min_score": (0.0, 1.0, 3),
    "horizontal_y_tolerance_norm": (0.0001, 1.0, 4),
    "horizontal_min_x_span_norm": (0.0001, 1.0, 4),
    "linear_drift_min_abs_spearman": (0.0, 1.0, 3),
}

FIELD_INFO = {
    "red_min": ("红色最小值", "", "E图红色通道的最低阈值。", "detection"),
    "red_dominance": ("红色优势值", "", "红色通道相对其他通道的最小优势。", "detection"),
    "min_component_area": ("最小缺陷面积", "px²", "E图中保留连通域的最小面积。", "detection"),
    "max_component_area": ("最大局部缺陷面积", "px²", "过滤异常大的局部连通区域；区域异常不受此限制。", "detection"),
    "micro_max_component_area": ("微小缺陷面积上限", "px²", "不超过该面积的对象标记为微小缺陷。", "detection"),
    "region_min_width_ratio": ("区域异常最小宽度比例", "", "外接框宽度覆盖E图的最低比例。", "detection"),
    "region_min_height_ratio": ("区域异常最小高度比例", "", "外接框高度覆盖E图的最低比例。", "detection"),
    "spatial_cluster_radius_norm": ("空间聚类半径", "", "按图像宽高归一化后的空间距离。", "basic"),
    "minimum_repeat_occurrences": ("最少重复次数", "次", "达到该次数后才判断重复规律。", "basic"),
    "minimum_period": ("最小周期", "片", "周期搜索的下限。", "basic"),
    "maximum_period": ("最大周期", "片", "周期搜索的上限。", "basic"),
    "minimum_period_precision": ("最小周期精确率", "", "越高越少误报，但可能漏掉不稳定规律。", "basic"),
    "minimum_period_coverage": ("最小周期覆盖率", "", "期望周期位置中实际出现的最低比例。", "basic"),
    "burst_minimum_length": ("连续异常最小长度", "片", "连续多少片出现同位缺陷时触发异常。", "basic"),
    "warning_lead_products": ("提前预警片数", "片", "在预测缺陷前多少片发出预警。", "basic"),
    "morph_kernel_size": ("形态学核尺寸", "px", "Mask连接小断口使用的核尺寸，建议使用奇数。", "detection"),
    "period_order_tolerance": ("周期序号容差", "片", "周期点允许偏离的序号范围。", "advanced"),
    "missing_order_tolerance": ("缺失确认容差", "片", "超过预测点多少片后确认缺失。", "advanced"),
    "preview_count": ("预览图数量", "张", "任务结束后生成的抽查预览数量。", "advanced"),
    "evaluation_center_tolerance_px": ("评测中心容差", "px", "仅用于带真值评测。", "advanced"),
    "registration_min_score": ("图像配准最低分", "", "低于该值的轨迹仅作低可信证据。", "advanced"),
    "horizontal_y_tolerance_norm": ("水平轨迹纵向容差", "", "对齐后归入同一水平带的纵向范围。", "advanced"),
    "horizontal_min_x_span_norm": ("水平轨迹最小跨度", "", "横向移动达到该比例才判断为平移。", "advanced"),
    "trajectory_min_occurrences": ("轨迹最少重复次数", "次", "形成空间移动轨迹所需的最少产品数。", "advanced"),
    "linear_drift_min_abs_spearman": ("单向漂移相关阈值", "", "生产位次与横坐标的最小绝对秩相关。", "advanced"),
}

DETECTION_FIELDS = {
    "red_min", "red_dominance", "morph_kernel_size", "min_component_area",
    "max_component_area", "micro_max_component_area", "region_min_width_ratio",
    "region_min_height_ratio",
}


class ParameterDialog(QDialog):
    parameters_applied = Signal(object)

    def __init__(self, current: dict[str, Any], default_path: Path, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("分析参数")
        self.resize(680, 680)
        self.setMinimumSize(520, 460)
        self.default_path = default_path
        self.base_config = normalize_analysis_config(current)
        self.current_profile = DETECTION_PROFILE_NAMES[0]
        self.editors: dict[str, QSpinBox | QDoubleSpinBox] = {}
        self.profile_combo = QComboBox()
        self.profile_combo.addItems(DETECTION_PROFILE_NAMES)
        tabs = QTabWidget()
        forms: dict[str, QFormLayout] = {}
        for key, title in (("detection", "图像检测"), ("basic", "规律分析"), ("advanced", "高级参数")):
            content = QWidget()
            forms[key] = QFormLayout(content)
            forms[key].setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(content)
            tabs.addTab(scroll, title)
        for name, (minimum, maximum) in INTEGER_FIELDS.items():
            editor = QSpinBox(); editor.setRange(minimum, maximum)
            title, suffix, help_text, group = FIELD_INFO[name]
            if suffix: editor.setSuffix(f" {suffix}")
            editor.setToolTip(help_text)
            label = QLabel(title); label.setToolTip(f"{help_text}\n配置键：{name}")
            self.editors[name] = editor; forms[group].addRow(label, editor)
        for name, (minimum, maximum, decimals) in FLOAT_FIELDS.items():
            editor = QDoubleSpinBox(); editor.setRange(minimum, maximum); editor.setDecimals(decimals)
            editor.setSingleStep(0.01)
            title, suffix, help_text, group = FIELD_INFO[name]
            if suffix: editor.setSuffix(f" {suffix}")
            editor.setToolTip(help_text)
            label = QLabel(title); label.setToolTip(f"{help_text}\n配置键：{name}")
            self.editors[name] = editor; forms[group].addRow(label, editor)
        self._set_values(current)
        self.profile_combo.currentTextChanged.connect(self._profile_changed)
        load_button = QPushButton("加载YAML"); load_button.clicked.connect(self._load_yaml)
        save_button = QPushButton("另存为YAML"); save_button.clicked.connect(self._save_yaml)
        reset_button = QPushButton("恢复项目默认值"); reset_button.clicked.connect(self._restore_defaults)
        actions = QHBoxLayout(); actions.addWidget(load_button); actions.addWidget(save_button); actions.addWidget(reset_button)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept_checked); buttons.rejected.connect(self.reject)
        note = QLabel("将鼠标停留在参数名称上可查看说明。高级参数通常保持默认值。")
        note.setWordWrap(True)
        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("检测参数产品族"))
        profile_row.addWidget(self.profile_combo, 1)
        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addLayout(profile_row)
        layout.addWidget(tabs, 1)
        layout.addLayout(actions)
        layout.addWidget(buttons)

    def _set_values(self, config: dict[str, Any]) -> None:
        self.base_config = normalize_analysis_config(config)
        for name, editor in self.editors.items():
            if name not in DETECTION_FIELDS and name in self.base_config:
                editor.setValue(self.base_config[name])
        self._load_profile(self.profile_combo.currentText() or DETECTION_PROFILE_NAMES[0])

    def _store_profile(self) -> None:
        profile = self.base_config["detection_profiles"][self.current_profile]
        for name in DETECTION_FIELDS:
            profile[name] = self.editors[name].value()

    def _load_profile(self, name: str) -> None:
        self.current_profile = name
        profile = self.base_config["detection_profiles"][name]
        for field in DETECTION_FIELDS:
            self.editors[field].setValue(profile[field])

    def _profile_changed(self, name: str) -> None:
        if not name or name == self.current_profile:
            return
        self._store_profile()
        self._load_profile(name)

    def values(self) -> dict[str, Any]:
        self._store_profile()
        result = dict(self.base_config)
        result["detection_profiles"] = {
            name: dict(values) for name, values in self.base_config["detection_profiles"].items()
        }
        result.update({
            name: editor.value() for name, editor in self.editors.items()
            if name not in DETECTION_FIELDS
        })
        return result

    def _validate(self) -> str | None:
        values = self.values()
        for name, profile in values["detection_profiles"].items():
            if profile["min_component_area"] > profile["max_component_area"]:
                return f"{name}: min_component_area不能大于max_component_area"
            if profile["micro_max_component_area"] < profile["min_component_area"]:
                return f"{name}: 微小缺陷面积上限不能小于最小缺陷面积"
            if profile["morph_kernel_size"] % 2 == 0:
                return f"{name}: 形态学核尺寸必须为奇数"
        if values["minimum_period"] > values["maximum_period"]:
            return "minimum_period不能大于maximum_period"
        return None

    def _accept_checked(self) -> None:
        if error := self._validate():
            QMessageBox.warning(self, "参数错误", error); return
        self.parameters_applied.emit(self.values()); self.accept()

    def _load_yaml(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "加载分析配置", "", "YAML (*.yaml *.yml)")
        if not filename: return
        try:
            self._set_values(yaml.safe_load(Path(filename).read_text(encoding="utf-8")) or {})
        except Exception as exc:
            QMessageBox.critical(self, "加载失败", str(exc))

    def _save_yaml(self) -> None:
        if error := self._validate():
            QMessageBox.warning(self, "参数错误", error); return
        filename, _ = QFileDialog.getSaveFileName(self, "另存为分析配置", "", "YAML (*.yaml)")
        if not filename: return
        path = Path(filename)
        if path.exists():
            QMessageBox.warning(self, "禁止覆盖", "目标文件已经存在，请选择新的文件名。"); return
        path.write_text(yaml.safe_dump(self.values(), allow_unicode=True, sort_keys=False), encoding="utf-8")
        QMessageBox.information(self, "保存成功", str(path))

    def _restore_defaults(self) -> None:
        try:
            self._set_values(yaml.safe_load(self.default_path.read_text(encoding="utf-8")) or {})
        except Exception as exc:
            QMessageBox.critical(self, "恢复失败", str(exc))
