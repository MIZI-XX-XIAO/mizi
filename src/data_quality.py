"""本文件统一检查产品、工艺参数和缺陷事件表的数据质量。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd


@dataclass
class DataQualityReport:
    source: str
    row_count: int
    column_count: int
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return not self.errors

    def to_frame(self) -> pd.DataFrame:
        rows = [
            {"级别": "错误", "项目": f"错误 {index}", "结果": message}
            for index, message in enumerate(self.errors, 1)
        ]
        rows += [
            {"级别": "警告", "项目": f"警告 {index}", "结果": message}
            for index, message in enumerate(self.warnings, 1)
        ]
        rows += [
            {"级别": "信息", "项目": str(name), "结果": str(value)}
            for name, value in self.metrics.items()
        ]
        return pd.DataFrame(rows, columns=["级别", "项目", "结果"])


def validate_products(frame: pd.DataFrame) -> DataQualityReport:
    report = DataQualityReport("产品表", len(frame), len(frame.columns))
    required = {"global_order", "camera", "e_image_path"}
    if missing := required - set(frame.columns):
        report.errors.append(f"缺少必需字段：{', '.join(sorted(missing))}")
    if "a_image_path" not in frame and "v_image_path" not in frame:
        report.errors.append("缺少A图路径字段 a_image_path（兼容字段为 v_image_path）")
    if frame.empty:
        report.errors.append("产品表为空")
        return report
    if "global_order" in frame:
        numeric = pd.to_numeric(frame["global_order"], errors="coerce")
        invalid = int(numeric.isna().sum())
        duplicates = int(numeric.duplicated().sum())
        if invalid:
            report.errors.append(f"global_order 有 {invalid} 个非数字或空值")
        if duplicates:
            report.errors.append(f"global_order 有 {duplicates} 个重复值")
        if not numeric.dropna().is_monotonic_increasing:
            report.errors.append("global_order 必须严格递增")
        if not numeric.dropna().empty:
            report.metrics["序号范围"] = f"{int(numeric.min())}～{int(numeric.max())}"
    null_cells = int(frame.isna().sum().sum())
    report.metrics.update({
        "产品数量": len(frame),
        "字段数量": len(frame.columns),
        "空单元格": null_cells,
        "相机": ", ".join(sorted(frame["camera"].dropna().astype(str).unique())) if "camera" in frame else "-",
    })
    if null_cells:
        report.warnings.append(f"共发现 {null_cells} 个空单元格，请确认是否符合预期")
    return report


def validate_process_parameters(frame: pd.DataFrame) -> DataQualityReport:
    report = DataQualityReport("工艺参数表", len(frame), len(frame.columns))
    if frame.empty:
        report.errors.append("工艺参数表为空")
        return report
    exact_keys = [key for key in ("product_id", "order_code", "dmc_raw", "global_order") if key in frame]
    time_keys = [key for key in ("production_timestamp", "timestamp") if key in frame]
    if not exact_keys and not time_keys:
        report.errors.append(
            "必须包含 product_id、order_code、dmc_raw、global_order 或时间戳字段之一"
        )
    numeric = frame.select_dtypes(include="number").columns.tolist()
    numeric = [column for column in numeric if column != "global_order"]
    if not numeric:
        report.errors.append("没有可用于关联分析的数值型工艺参数")
    duplicate_key = exact_keys[0] if exact_keys else None
    if duplicate_key and frame[duplicate_key].duplicated().any():
        report.warnings.append(f"{duplicate_key} 存在重复值，分析时将按产品聚合数值参数")
    report.metrics.update({
        "记录数量": len(frame),
        "数值参数数量": len(numeric),
        "可用关联键": ", ".join(exact_keys + time_keys) or "无",
        "空单元格": int(frame.isna().sum().sum()),
    })
    return report


def validate_defects(frame: pd.DataFrame) -> DataQualityReport:
    report = DataQualityReport("缺陷事件表", len(frame), len(frame.columns))
    required = {"global_order", "center_x", "center_y", "component_area"}
    if missing := required - set(frame.columns):
        report.errors.append(f"缺少缺陷字段：{', '.join(sorted(missing))}")
    report.metrics.update({
        "缺陷事件数量": len(frame),
        "涉及产品数量": int(frame["global_order"].nunique()) if "global_order" in frame else 0,
    })
    return report
