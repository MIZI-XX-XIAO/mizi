"""本文件提供分析参数的类型化编辑、默认恢复、YAML加载和禁止覆盖式另存功能。"""

from pathlib import Path
from typing import Any

import yaml
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QMessageBox, QPushButton, QScrollArea, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)


INTEGER_FIELDS = {
    "red_min": (0, 255), "red_dominance": (0, 255),
    "min_component_area": (1, 10_000_000), "max_component_area": (1, 100_000_000),
    "minimum_repeat_occurrences": (2, 10_000), "minimum_period": (1, 1_000_000),
    "maximum_period": (1, 1_000_000), "burst_minimum_length": (2, 100_000),
    "warning_lead_products": (0, 1_000_000),
    "morph_kernel_size": (1, 31), "tile_width": (256, 32768), "tile_height": (256, 32768),
    "tile_overlap": (0, 2048), "period_order_tolerance": (0, 1000),
    "missing_order_tolerance": (0, 1000), "preview_count": (0, 1000),
}
FLOAT_FIELDS = {
    "spatial_cluster_radius_norm": (0.000001, 1.0, 6),
    "minimum_period_precision": (0.0, 1.0, 4),
    "minimum_period_coverage": (0.0, 1.0, 4),
    "evaluation_center_tolerance_px": (0.0, 10000.0, 2),
}

FIELD_INFO = {
    "red_min": ("红色最小值", "", "E图红色通道的最低阈值。", "basic"),
    "red_dominance": ("红色优势值", "", "红色通道相对其他通道的最小优势。", "basic"),
    "min_component_area": ("最小缺陷面积", "px²", "过滤面积过小的噪点。", "basic"),
    "max_component_area": ("最大缺陷面积", "px²", "过滤异常大的连通区域。", "basic"),
    "spatial_cluster_radius_norm": ("空间聚类半径", "", "按图像宽高归一化后的空间距离。", "basic"),
    "minimum_repeat_occurrences": ("最少重复次数", "次", "达到该次数后才判断重复规律。", "basic"),
    "minimum_period": ("最小周期", "片", "周期搜索的下限。", "basic"),
    "maximum_period": ("最大周期", "片", "周期搜索的上限。", "basic"),
    "minimum_period_precision": ("最小周期精确率", "", "越高越少误报，但可能漏掉不稳定规律。", "basic"),
    "minimum_period_coverage": ("最小周期覆盖率", "", "期望周期位置中实际出现的最低比例。", "basic"),
    "burst_minimum_length": ("连续异常最小长度", "片", "连续多少片出现同位缺陷时触发异常。", "basic"),
    "warning_lead_products": ("提前预警片数", "片", "在预测缺陷前多少片发出预警。", "basic"),
    "morph_kernel_size": ("形态学核尺寸", "px", "Mask去噪使用的核尺寸，建议使用奇数。", "advanced"),
    "tile_width": ("分块宽度", "px", "大图处理块宽；越大越快但更占内存。", "advanced"),
    "tile_height": ("分块高度", "px", "大图处理块高；越大越快但更占内存。", "advanced"),
    "tile_overlap": ("分块重叠", "px", "避免跨分块缺陷被切断。", "advanced"),
    "period_order_tolerance": ("周期序号容差", "片", "周期点允许偏离的序号范围。", "advanced"),
    "missing_order_tolerance": ("缺失确认容差", "片", "超过预测点多少片后确认缺失。", "advanced"),
    "preview_count": ("预览图数量", "张", "任务结束后生成的抽查预览数量。", "advanced"),
    "evaluation_center_tolerance_px": ("评测中心容差", "px", "仅用于带真值评测。", "advanced"),
}


class ParameterDialog(QDialog):
    parameters_applied = Signal(object)

    def __init__(self, current: dict[str, Any], default_path: Path, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("分析参数")
        self.resize(680, 680)
        self.setMinimumSize(520, 460)
        self.default_path = default_path
        self.base_config = dict(current)
        self.editors: dict[str, QSpinBox | QDoubleSpinBox] = {}
        tabs = QTabWidget()
        forms: dict[str, QFormLayout] = {}
        for key, title in (("basic", "基础参数"), ("advanced", "高级参数")):
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
        load_button = QPushButton("加载YAML"); load_button.clicked.connect(self._load_yaml)
        save_button = QPushButton("另存为YAML"); save_button.clicked.connect(self._save_yaml)
        reset_button = QPushButton("恢复项目默认值"); reset_button.clicked.connect(self._restore_defaults)
        actions = QHBoxLayout(); actions.addWidget(load_button); actions.addWidget(save_button); actions.addWidget(reset_button)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept_checked); buttons.rejected.connect(self.reject)
        note = QLabel("将鼠标停留在参数名称上可查看说明。高级参数通常保持默认值。")
        note.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addWidget(tabs, 1)
        layout.addLayout(actions)
        layout.addWidget(buttons)

    def _set_values(self, config: dict[str, Any]) -> None:
        for name, editor in self.editors.items():
            if name in config:
                editor.setValue(config[name])
        self.base_config.update(config)

    def values(self) -> dict[str, Any]:
        result = dict(self.base_config)
        result.update({name: editor.value() for name, editor in self.editors.items()})
        return result

    def _validate(self) -> str | None:
        values = self.values()
        if values["min_component_area"] > values["max_component_area"]:
            return "min_component_area不能大于max_component_area"
        if values["minimum_period"] > values["maximum_period"]:
            return "minimum_period不能大于maximum_period"
        if values["morph_kernel_size"] % 2 == 0:
            return "形态学核尺寸必须为奇数"
        if values["tile_overlap"] * 2 >= min(values["tile_width"], values["tile_height"]):
            return "分块重叠必须小于分块宽度和高度的一半"
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
