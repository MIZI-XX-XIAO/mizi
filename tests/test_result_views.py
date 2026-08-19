"""Tests for the shared overview-card and result-dialog filtering model."""

import pandas as pd

from src.result_views import build_result_view, pattern_count


def _frames() -> dict[str, pd.DataFrame]:
    return {
        "patterns": pd.DataFrame([
            {"analysis_scope": "5S", "pattern_id": "P1", "pattern_type": "periodic", "cluster_id": "C1", "observed_orders": "1;2", "first_order": 1},
            {"analysis_scope": "5S", "pattern_id": "P2", "pattern_type": "fixed_position_repeat", "cluster_id": "C2", "observed_orders": "3", "first_order": 3},
        ]),
        "alerts": pd.DataFrame([
            {"analysis_scope": "5S", "cluster_id": "C1", "alert_at_order": 2, "severity": "warning"},
            {"analysis_scope": "5S", "cluster_id": "C2", "alert_at_order": 3, "severity": "notice"},
        ]),
        "trajectories": pd.DataFrame([
            {"analysis_scope": "5S", "trajectory_id": "T1", "task_orders": "1;2"},
            {"analysis_scope": "5S", "trajectory_id": "T2", "task_orders": "3"},
        ]),
        "code_patterns": pd.DataFrame([
            {"analysis_scope": "5S", "canonical_code": "5520", "source_type": "AOI_FAILURE", "evidence_task_orders": "1;2"},
            {"analysis_scope": "5S", "canonical_code": "7000", "source_type": "AOI_FAILURE", "evidence_task_orders": "3"},
        ]),
        "code_image_links": pd.DataFrame([
            {"analysis_scope": "5S", "global_order": 1, "canonical_code": "5520", "source_type": "AOI_FAILURE", "code_status": "defect"},
            {"analysis_scope": "5S", "global_order": 1, "canonical_code": "6000", "source_type": "AOI_FAILURE", "code_status": "defect"},
            {"analysis_scope": "5S", "global_order": 2, "canonical_code": "5520", "source_type": "AOI_FAILURE", "code_status": "defect"},
            {"analysis_scope": "5S", "global_order": 3, "canonical_code": "7000", "source_type": "AOI_FAILURE", "code_status": "defect"},
        ]),
        "code_space": pd.DataFrame([
            {"analysis_scope": "5S", "canonical_code": "5520", "source_type": "AOI_FAILURE", "spatial_type": "cluster", "spatial_id": "C1", "support_task_orders": "1;2"},
            {"analysis_scope": "5S", "canonical_code": "5520", "source_type": "AOI_FAILURE", "spatial_type": "trajectory", "spatial_id": "T1", "support_task_orders": "1;2"},
            {"analysis_scope": "5S", "canonical_code": "7000", "source_type": "AOI_FAILURE", "spatial_type": "cluster", "spatial_id": "C2", "support_task_orders": "3"},
        ]),
        "code_conflicts": pd.DataFrame(columns=["analysis_scope", "global_order"]),
    }


def test_selected_code_filters_every_result_category_through_explicit_links() -> None:
    products = pd.DataFrame({"analysis_scope": ["5S"] * 3, "global_order": [1, 2, 3]})
    extracted = pd.DataFrame({
        "analysis_scope": ["5S"] * 3, "global_order": [1, 2, 3],
        "cluster_id": ["C1", "C1", "C2"], "detection_type": ["local_defect"] * 3,
    })
    view = build_result_view(
        _frames(), products, extracted, {}, selected_codes={"5520"},
        code_source="AOI_FAILURE",
    )

    assert view.products.global_order.tolist() == [1, 2]
    assert view.extracted.global_order.tolist() == [1, 2]
    assert view.sections["periodic"].pattern_id.tolist() == ["P1"]
    assert view.sections["code"].canonical_code.tolist() == ["5520"]
    assert view.sections["trajectory"].trajectory_id.tolist() == ["T1"]
    assert len(view.alerts) == 1
    assert set(view.sections["cooccurrence"]["缺陷A"]) | set(view.sections["cooccurrence"]["缺陷B"]) >= {"5520"}
    assert pattern_count(view, "periodic") == 1
    assert pattern_count(view, "all") == sum(len(frame) for frame in view.sections.values())


def test_code_without_spatial_association_does_not_leak_spatial_results() -> None:
    frames = _frames()
    frames["code_space"] = frames["code_space"].iloc[0:0]
    products = pd.DataFrame({"analysis_scope": ["5S"] * 3, "global_order": [1, 2, 3]})
    extracted = pd.DataFrame({
        "analysis_scope": ["5S"] * 3, "global_order": [1, 2, 3],
        "cluster_id": ["C1", "C1", "C2"], "detection_type": ["local_defect"] * 3,
    })
    view = build_result_view(frames, products, extracted, {}, selected_codes={"5520"})

    assert view.sections["periodic"].empty
    assert view.sections["trajectory"].empty
    assert view.alerts.empty
    assert not view.sections["code"].empty
