"""本文件在显式提供真实脱敏工作簿时验证Excel模板兼容性和完整分析输出。"""

import os
from pathlib import Path

import pytest

from src.excel_analysis import ExcelAnalysisRequest, analyze_excel_quality, load_excel_workbook


def test_real_excel_workbook_when_configured(tmp_path: Path) -> None:
    value = os.environ.get("MEA5S_REAL_EXCEL_PATH", "").strip()
    if not value:
        pytest.skip("未设置MEA5S_REAL_EXCEL_PATH")
    path = Path(value)
    if not path.is_file():
        pytest.fail(f"MEA5S_REAL_EXCEL_PATH不存在：{path}")
    loaded = load_excel_workbook(path)
    assert not loaded.data.empty
    assert loaded.parameter_specs
    result = analyze_excel_quality(ExcelAnalysisRequest(path, tmp_path, "真实Excel验收"))
    assert result.status == "complete"
    assert result.summary["record_count"] == len(loaded.data)
    assert (result.output_dir / "excel_analysis_summary.json").is_file()
