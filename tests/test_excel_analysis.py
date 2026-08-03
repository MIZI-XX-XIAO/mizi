"""本文件测试Excel重复容差列、质量判定、输出文件和图片结果关联。"""

from pathlib import Path

import pandas as pd
from openpyxl import Workbook

from src.analysis_service import CancellationToken
from src.excel_analysis import (
    ExcelAnalysisRequest,
    analyze_excel_quality,
    excel_relationship_frame,
    inspect_excel_workbook,
    load_excel_workbook,
)
from src.process_relationships import analyze_process_relationships


def _workbook(path: Path) -> Path:
    workbook = Workbook()
    data = workbook.active
    data.title = "Data"
    data.append([
        "Ident No.", "TTNR", "Variant", "Batch", "State", "Line", "Test Date",
        "Result.BottomSGTensionForce", "Tolerance",
        "Result.RollingSpeed", "Tolerance",
        "Result.StaticElectricity", "Tolerance",
        "Result.TimeStamping", "Tolerance",
    ])
    rows = [
        ["DMC-001", "P1", "01", "B1", "OK", "3003", "2026-07-03 10:00:00", 30, "25 ... 70", 3.4, "0 ~ 6", -195, "-1500 … 1500", "2026-07-03 10:00:01", None],
        ["DMC-002", "P1", "01", "B1", "NOK", "3003", "2026-07-03 10:01:00", 80, "25 ... 70", 3.4, "0 ~ 6", 5000, "-1500 … 1500", "2026-07-03 10:01:01", None],
        ["DMC-003", "P2", "02", "B2", "OK", "3004", "2026-07-04 11:00:00", 75, "25 ... 70", 3.4, "0 ~ 6", 100, "-1500 … 1500", "2026-07-04 11:00:01", None],
        ["DMC-004", "P2", "02", "B2", "NOK", "3004", "2026-07-04 11:01:00", 40, "25 ... 70", 3.4, "bad", 100, "-1500 … 1500", "2026-07-04 11:01:01", None],
    ]
    for row in rows:
        data.append(row)
    query = workbook.create_sheet("Query parameter")
    query.append(["Query parameter", None])
    query.append(["Location(s)", "3003.10.1.1.1"])
    query.append(["Test Date Begin", "7/1/2026 12:00:00 AM"])
    query.append(["Characteristics", "Result.BottomSGTensionForce;Result.RollingSpeed"])
    workbook.save(path)
    return path


def test_load_excel_pairs_duplicate_tolerance_columns(tmp_path: Path) -> None:
    path = _workbook(tmp_path / "quality.xlsx")
    inspected = inspect_excel_workbook(path)
    assert set(inspected) == {"Data", "Query parameter"}
    loaded = load_excel_workbook(path)
    assert loaded.column_mapping["dmc_raw"] == "Ident No."
    assert loaded.column_mapping["state"] == "State"
    assert loaded.data["timestamp"].notna().all()
    specs = {spec.name: spec for spec in loaded.parameter_specs}
    assert specs["Result.BottomSGTensionForce"].tolerance_column == "Tolerance"
    assert specs["Result.RollingSpeed"].tolerance_column == "Tolerance__2"
    assert specs["Result.StaticElectricity"].lower == -1500
    assert loaded.query_parameters["Location(s)"] == "3003.10.1.1.1"
    assert any("无法解析" in warning for warning in loaded.quality_report.warnings)


def test_excel_quality_outputs_state_tolerance_conflicts(tmp_path: Path) -> None:
    path = _workbook(tmp_path / "quality.xlsx")
    result = analyze_excel_quality(ExcelAnalysisRequest(path, tmp_path, "Excel诊断"))
    assert result.status == "complete"
    assert result.summary["record_count"] == 4
    assert result.summary["state_nok_count"] == 2
    assert result.summary["tolerance_nok_count"] == 2
    assert result.summary["judgement_conflict_count"] == 2
    assert len(result.frames["violations"]) == 3
    assert set(result.frames["group_stats"]["维度"]) == {"variant", "batch", "line", "ttnr"}
    for filename in (
        "excel_analysis_summary.json", "excel_standardized_data.csv",
        "excel_parameter_statistics.csv", "excel_tolerance_violations.csv",
        "excel_judgement_conflicts.csv", "excel_data_quality.csv",
        "visualizations/quality_trend.png", "visualizations/top_tolerance_violations.png",
    ):
        assert (result.output_dir / filename).is_file()


def test_excel_standardized_data_can_join_image_products(tmp_path: Path) -> None:
    path = _workbook(tmp_path / "quality.xlsx")
    excel = analyze_excel_quality(ExcelAnalysisRequest(path, tmp_path, "Excel诊断"))
    products = pd.DataFrame({
        "global_order": [1, 2, 3, 4], "camera": ["5S"] * 4,
        "a_image_path": ["a.png"] * 4, "e_image_path": ["e.png"] * 4,
        "dmc_raw": ["DMC-001", "DMC-002", "DMC-003", "DMC-004"],
    })
    defects = pd.DataFrame({"global_order": [2, 3], "component_area": [4, 5]})
    parameters = excel_relationship_frame(excel.workbook_data, excel.frames["standardized"])
    assert "source_row" not in parameters
    relationship = analyze_process_relationships(products, defects, parameters)
    assert relationship.summary["join_key"] == "dmc_raw"
    assert relationship.summary["matched_count"] == 4
    assert relationship.summary["defective_product_count"] == 2


def test_excel_relationship_falls_back_to_nearest_timestamp(tmp_path: Path) -> None:
    path = _workbook(tmp_path / "quality.xlsx")
    loaded = load_excel_workbook(path)
    parameters = excel_relationship_frame(loaded).drop(columns="dmc_raw")
    products = pd.DataFrame({
        "global_order": [1, 2, 3, 4], "camera": ["5S"] * 4,
        "a_image_path": ["a.png"] * 4, "e_image_path": ["e.png"] * 4,
        "production_timestamp": pd.to_datetime([
            "2026-07-03 10:00:02", "2026-07-03 10:01:02",
            "2026-07-04 11:00:02", "2026-07-04 11:01:02",
        ]),
    })
    defects = pd.DataFrame({"global_order": [2], "component_area": [4]})
    relationship = analyze_process_relationships(products, defects, parameters, tolerance_seconds=5)
    assert relationship.summary["join_key"] == "production_timestamp≈timestamp"
    assert relationship.summary["matched_count"] == 4


def test_excel_analysis_honors_pre_cancelled_token(tmp_path: Path) -> None:
    path = _workbook(tmp_path / "quality.xlsx")
    token = CancellationToken(); token.cancel()
    result = analyze_excel_quality(ExcelAnalysisRequest(path, tmp_path, "取消测试"), token=token)
    assert result.status == "cancelled"
    assert not list(tmp_path.glob("取消测试_*"))
