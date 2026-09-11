"""本文件测试新建任务代码筛选及无图片目标分析的统计语义。"""

from pathlib import Path
import json

import pandas as pd

from src.analysis_service import (
    AnalysisSelection, ExcelTargetAnalysisRequest, code_target_key,
    filter_selected_code_events, run_excel_target_task,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _events() -> pd.DataFrame:
    rows = []
    for order in range(1, 13):
        code = "501100000000" if order <= 6 else "502200000000" if order <= 9 else "100000000000"
        rows.append({
            "event_id": f"A{order}", "dmc_raw": f"DMC-{order:03d}",
            "station_id": "35_5s_aoi", "test_date": f"2026-08-01 10:{order:02d}:00",
            "state": "OK" if order >= 10 else "NOK", "source_sheet": "AOI",
            "source_row": order + 1, "production_order": order,
            "Result.AOIFailureCode": code,
        })
    return pd.DataFrame(rows)


def test_source_and_code_key_filter_keeps_same_numeric_codes_independent() -> None:
    normalized = pd.DataFrame([
        {"analysis_scope": "5S", "source_type": "AOI_FAILURE", "canonical_code": "5050"},
        {"analysis_scope": "5S", "source_type": "VI_BLOCK", "canonical_code": "5050"},
    ])
    selection = AnalysisSelection(
        mode="selected_codes", scopes=("5S",), code_sources=("AOI_FAILURE",),
        defect_codes=(code_target_key("AOI_FAILURE", "5050"),),
    )

    result = filter_selected_code_events(normalized, selection)

    assert result[["source_type", "canonical_code"]].to_dict("records") == [
        {"source_type": "AOI_FAILURE", "canonical_code": "5050"}
    ]


def test_excel_only_multiple_codes_use_full_population_and_selected_parameters(tmp_path: Path) -> None:
    parameters = pd.DataFrame({
        "dmc_raw": [f"DMC-{order:03d}" for order in range(1, 13)],
        "temperature": range(101, 113),
        "pressure": [2.0 + order / 10 for order in range(1, 13)],
    })
    selection = AnalysisSelection(
        mode="selected_codes", scopes=("5S",), code_sources=("AOI_FAILURE",),
        defect_codes=(
            code_target_key("AOI_FAILURE", "5011"),
            code_target_key("AOI_FAILURE", "5022"),
        ),
        modules=("code_patterns", "process_relationships"),
        process_parameters=("temperature",),
    )

    result = run_excel_target_task(ExcelTargetAnalysisRequest(
        config_path=PROJECT_ROOT / "config" / "analysis_config.yaml",
        output_parent=tmp_path, task_name="纯Excel多代码",
        station_events_frame=_events(), process_parameters_frame=parameters,
        selection=selection,
    ))

    assert result.status == "complete"
    summaries = result.summary["relationship_targets"]
    assert {item["target"] for item in summaries} == {
        "AOI_FAILURE:5011", "AOI_FAILURE:5022",
    }
    assert {item["product_count"] for item in summaries} == {12}
    assert {item["defective_product_count"] for item in summaries} == {6, 3}
    assert set(result.frames["process_metrics"]["参数"]) == {"temperature"}
    assert set(result.frames["process_metrics"]["canonical_code"]) == {"5011", "5022"}
    assert not result.frames["code_patterns"].empty
    assert result.frames["code_patterns"]["evidence_task_orders"].str.strip().ne("").all()
    assert not result.frames["code_image_links"].empty
    assert result.summary["image_analysis_executed"] is False
    saved = json.loads((result.output_dir / "analysis_selection.json").read_text(encoding="utf-8"))
    assert saved["defect_codes"] == ["AOI_FAILURE:5011", "AOI_FAILURE:5022"]
    assert saved["target_product_counts"] == {
        "AOI_FAILURE:5011": 6, "AOI_FAILURE:5022": 3,
    }
    for filename in (
        "process_nonlinear_importance.csv", "process_nonlinear_effects.csv",
        "process_risk_curves.csv",
        "process_interactions.csv", "process_interaction_regions.csv",
        "process_model_validation.csv",
        "downtime_events.csv", "product_event_exposure.csv",
        "defect_cause_hypotheses.csv", "defect_cause_hypotheses.json",
        "cause_evidence.csv",
        "association_findings.csv", "association_findings.json",
    ):
        assert (result.output_dir / filename).is_file()
    assert "association_findings" in result.frames
