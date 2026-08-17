"""本文件验证缺陷代码、真实生产位次、空间轨迹和多证据关联规则。"""

from pathlib import Path

import cv2
import numpy as np
import pandas as pd

from src.defect_evidence import (
    DefectCatalog, align_detection_coordinates, analyze_code_spatial_associations,
    assign_production_order, build_station_attribution, discover_code_patterns,
    discover_spatial_trajectories,
    load_defect_catalog, normalize_defect_codes, parse_aoi_code, parse_vi_block_code,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _catalog() -> DefectCatalog:
    return load_defect_catalog(PROJECT_ROOT / "config" / "defect_code_catalog.csv")


def _config() -> dict:
    return {
        "minimum_repeat_occurrences": 4, "minimum_period": 2, "maximum_period": 30,
        "period_order_tolerance": 0, "minimum_period_precision": 0.75,
        "minimum_period_coverage": 0.65, "burst_minimum_length": 4,
        "registration_min_score": 0.8, "horizontal_y_tolerance_norm": 0.02,
        "horizontal_min_x_span_norm": 0.05, "trajectory_min_occurrences": 4,
        "linear_drift_min_abs_spearman": 0.8,
    }


def test_business_code_parsers() -> None:
    assert parse_aoi_code("506100000000xxx") == "5061"
    assert parse_aoi_code(501100000000) == "5011"
    assert parse_aoi_code("bad") == ""
    assert parse_vi_block_code("175520_") == "5520"
    assert parse_vi_block_code("17-55-20") == "5520"


def test_normalization_keeps_aoi_and_vi_as_independent_evidence() -> None:
    events = pd.DataFrame([
        {"event_id": "a1", "dmc_raw": "D1", "station_id": "35_5s_aoi",
         "test_date": "2026-06-10 10:00", "state": "NOK", "source_sheet": "AOI",
         "source_row": 2, "Result.AOIFailureCode": "501100000000"},
        {"event_id": "a2", "dmc_raw": "D2", "station_id": "35_5s_aoi",
         "test_date": "2026-06-10 10:01", "state": "OK", "source_sheet": "AOI",
         "source_row": 3, "Result.AOIFailureCode": "100000000000"},
        {"event_id": "a3", "dmc_raw": "D3", "station_id": "35_5s_aoi",
         "test_date": "2026-06-10 10:02", "state": "SCRAPPED", "source_sheet": "AOI",
         "source_row": 4, "Result.AOIFailureCode": None},
        {"event_id": "v1", "dmc_raw": "D1", "station_id": "35_5s_vi",
         "test_date": "2026-06-10 10:05", "state": "NOK", "source_sheet": "MS0335all",
         "source_row": 2, "BlockCode": "175520_"},
        {"event_id": "v2", "dmc_raw": "D2", "station_id": "35_5s_vi",
         "test_date": "2026-06-10 10:06", "state": "NOK", "source_sheet": "Other",
         "source_row": 3, "BlockCode": "175520_"},
    ])
    normalized = normalize_defect_codes(assign_production_order(events), _catalog())
    assert set(normalized.source_type) == {"AOI_FAILURE", "VI_BLOCK"}
    assert normalized.loc[normalized.event_id.eq("a1"), "canonical_code"].iloc[0] == "5011"
    assert normalized.loc[normalized.event_id.eq("a2"), "code_status"].iloc[0] == "normal"
    assert normalized.loc[normalized.event_id.eq("a3"), "code_status"].iloc[0] == "scrapped"
    assert normalized.loc[normalized.event_id.eq("v1"), "canonical_code"].iloc[0] == "5520"
    assert "v2" not in set(normalized.event_id)


def test_event_order_preserves_repeated_dmc_passes() -> None:
    events = pd.DataFrame({
        "event_id": ["later", "first"], "dmc_raw": ["D1", "D1"],
        "station_id": ["35_5s_aoi", "35_5s_aoi"],
        "test_date": ["2026-01-01 10:02", "2026-01-01 10:01"],
        "source_sheet": ["S", "S"], "source_row": [3, 2],
    })
    ordered = assign_production_order(events)
    assert ordered.event_id.tolist() == ["first", "later"]
    assert ordered.production_order.tolist() == [1, 2]


def test_selected_codes_can_be_analyzed_separately_or_as_a_family() -> None:
    events = pd.DataFrame({
        "analysis_scope": ["5S"] * 8, "station_id": ["35_5s_aoi"] * 8,
        "event_id": [f"E{i}" for i in range(8)], "dmc_raw": [f"D{i}" for i in range(8)],
        "production_order": [1, 3, 5, 7, 2, 4, 6, 8], "test_date": [pd.NaT] * 8,
        "batch": ["B"] * 8, "source_type": ["AOI_FAILURE"] * 8,
        "source_sheet": ["AOI"] * 8, "raw_code": [""] * 8,
        "canonical_code": ["5011"] * 4 + ["5020"] * 4,
        "defect_name": [""] * 8, "state": ["NOK"] * 8, "code_status": ["defect"] * 8,
    })
    separate = discover_code_patterns(events, _config(), ["5011", "5020"])
    merged = discover_code_patterns(events, _config(), ["5011", "5020"], merge_selected=True)
    assert set(separate.canonical_code) == {"5011", "5020"}
    assert merged.canonical_code.tolist() == ["5011+5020"]
    assert merged.iloc[0].pattern_type == "burst"


def _write_image(path: Path, image: np.ndarray) -> str:
    assert cv2.imencode(".png", image)[1].tofile(str(path)) is None
    return str(path)


def test_registration_removes_whole_product_shift(tmp_path: Path) -> None:
    rng = np.random.default_rng(7)
    base = rng.integers(0, 255, (96, 128), dtype=np.uint8)
    product_rows, detection_rows = [], []
    for index, shift in enumerate((0, 4, 8, 12), 1):
        matrix = np.float32([[1, 0, shift], [0, 1, 0]])
        image = cv2.warpAffine(base, matrix, (128, 96))
        path = _write_image(tmp_path / f"{index}.png", image)
        product_rows.append({"global_order": index, "production_order": index,
                             "analysis_scope": "5S", "a_image_path": path})
        detection_rows.append({"global_order": index, "analysis_scope": "5S",
                               "center_x_norm": (40 + shift) / 128, "center_y_norm": 0.5,
                               "detection_type": "local", "cluster_id": "C1"})
    aligned = align_detection_coordinates(pd.DataFrame(product_rows), pd.DataFrame(detection_rows), 0.5)
    assert aligned.aligned_x_norm.max() - aligned.aligned_x_norm.min() < 0.02


def test_real_horizontal_drift_creates_trajectory(tmp_path: Path) -> None:
    rng = np.random.default_rng(9)
    base = rng.integers(0, 255, (96, 128), dtype=np.uint8)
    path = _write_image(tmp_path / "reference.png", base)
    products, detections = [], []
    for order, x in enumerate((20, 32, 44, 56), 1):
        products.append({"global_order": order, "production_order": order,
                         "analysis_scope": "5S", "a_image_path": path})
        detections.append({"global_order": order, "analysis_scope": "5S",
                           "center_x_norm": x / 128, "center_y_norm": 0.5,
                           "detection_type": "local", "cluster_id": f"C{order}"})
    aligned, trajectories = discover_spatial_trajectories(
        pd.DataFrame(products), pd.DataFrame(detections), _config()
    )
    assert trajectories.iloc[0].pattern_type == "linear_drift"
    assert trajectories.iloc[0].registration_quality == "high"
    assert aligned.trajectory_id.str.strip().ne("").all()


def test_horizontal_trajectory_does_not_mix_stations(tmp_path: Path) -> None:
    rng = np.random.default_rng(11)
    base = rng.integers(0, 255, (96, 128), dtype=np.uint8)
    path = _write_image(tmp_path / "station_reference.png", base)
    products, detections = [], []
    for order, (station, x) in enumerate(
        (("station_a", 20), ("station_a", 60), ("station_b", 25), ("station_b", 65)), 1
    ):
        products.append({"global_order": order, "production_order": order,
                         "analysis_scope": "5S", "station_id": station, "a_image_path": path})
        detections.append({"global_order": order, "analysis_scope": "5S", "station_id": station,
                           "center_x_norm": x / 128, "center_y_norm": 0.5,
                           "detection_type": "local", "cluster_id": "C1"})
    _, trajectories = discover_spatial_trajectories(
        pd.DataFrame(products), pd.DataFrame(detections), _config()
    )
    assert trajectories.empty


def test_code_space_association_and_label_conflict_are_not_overwritten() -> None:
    products = pd.DataFrame({"analysis_scope": ["5S"] * 10,
                             "dmc_raw": [f"D{i}" for i in range(10)], "global_order": range(1, 11)})
    codes = []
    for index in range(5):
        codes.append({"analysis_scope": "5S", "dmc_raw": f"D{index}", "source_type": "AOI_FAILURE",
                      "canonical_code": "5011", "defect_name": "折皱", "code_status": "defect"})
        codes.append({"analysis_scope": "5S", "dmc_raw": f"D{index}", "source_type": "VI_BLOCK",
                      "canonical_code": "5011" if index < 4 else "5520", "defect_name": "", "code_status": "defect"})
    detections = pd.DataFrame({"analysis_scope": ["5S"] * 5, "global_order": range(1, 6),
                               "cluster_id": ["C1"] * 5})
    associations, conflicts = analyze_code_spatial_associations(products, pd.DataFrame(codes), detections)
    assert associations.loc[associations.canonical_code.eq("5011"), "association_strength"].iloc[0] == "strong"
    assert conflicts.loc[conflicts.dmc_raw.eq("D4"), "comparison_status"].iloc[0] == "label_conflict"


def test_station_attribution_requires_same_dmc_upstream_evidence() -> None:
    products = []
    target_detections = []
    for index in range(5):
        dmc = f"D{index}"
        products.extend([
            {"global_order": index + 1, "dmc_raw": dmc, "station_id": "upstream",
             "aoi_test_date": f"2026-01-01 08:0{index}:00", "evidence_status": "evaluable"},
            {"global_order": index + 6, "dmc_raw": dmc, "station_id": "target",
             "aoi_test_date": f"2026-01-01 09:0{index}:00", "evidence_status": "evaluable"},
        ])
        target_detections.append({
            "global_order": index + 6, "dmc_raw": dmc, "station_id": "target",
            "trajectory_id": "target-T001", "aligned_y_norm": 0.5,
        })
    trajectories = pd.DataFrame([{
        "trajectory_id": "target-T001", "analysis_scope": "5S", "station_id": "target",
        "pattern_type": "linear_drift", "registration_quality": "high",
    }])
    attribution = build_station_attribution(
        pd.DataFrame(), trajectories, pd.DataFrame(),
        pd.DataFrame(products), pd.DataFrame(target_detections),
    )
    assert attribution.iloc[0].association_level == "较强工站关联"
    assert attribution.iloc[0].equipment_conclusion == "设备原因待确认"


def test_repeated_dmc_code_space_mapping_uses_exact_event_id() -> None:
    products = pd.DataFrame([
        {"analysis_scope": "5S", "dmc_raw": "D1", "global_order": 1,
         "production_order": 10, "aoi_event_id": "A1"},
        {"analysis_scope": "5S", "dmc_raw": "D1", "global_order": 2,
         "production_order": 20, "aoi_event_id": "A2"},
    ])
    codes = pd.DataFrame([
        {"analysis_scope": "5S", "dmc_raw": "D1", "event_id": "A1",
         "source_type": "AOI_FAILURE", "canonical_code": "5011",
         "defect_name": "折皱", "code_status": "defect"},
        {"analysis_scope": "5S", "dmc_raw": "D1", "event_id": "A2",
         "source_type": "AOI_FAILURE", "canonical_code": "5520",
         "defect_name": "贴合", "code_status": "defect"},
    ])
    detections = pd.DataFrame([
        {"analysis_scope": "5S", "global_order": 1, "cluster_id": "C1"},
        {"analysis_scope": "5S", "global_order": 2, "cluster_id": "C2"},
    ])
    associations, conflicts = analyze_code_spatial_associations(products, codes, detections)
    mapped = set(zip(associations["canonical_code"], associations["spatial_id"]))
    assert mapped == {("5011", "C1"), ("5520", "C2")}
    assert set(conflicts["production_order"].astype(int)) == {10, 20}
