"""本文件测试统一数据质量报告的字段、重复值和工艺参数检查。"""

import pandas as pd

from src.data_quality import validate_process_parameters, validate_products


def test_product_quality_reports_duplicate_order() -> None:
    frame = pd.DataFrame({
        "global_order": [1, 1],
        "camera": ["5S", "5S"],
        "a_image_path": ["a.png", "b.png"],
        "e_image_path": ["ea.png", "eb.png"],
    })
    report = validate_products(frame)
    assert not report.is_valid
    assert any("重复" in message for message in report.errors)


def test_process_quality_requires_numeric_parameter() -> None:
    frame = pd.DataFrame({"global_order": [1, 2], "recipe": ["A", "B"]})
    report = validate_process_parameters(frame)
    assert not report.is_valid
    assert any("数值型" in message for message in report.errors)
