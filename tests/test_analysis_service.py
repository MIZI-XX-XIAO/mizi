"""本文件测试任务服务的进度回调、独立输出目录、安全取消和部分结果保留。"""

from pathlib import Path

import cv2
import numpy as np
import pandas as pd

from src.analysis_service import (
    AnalysisCallbacks, AnalysisRequest, CancellationToken, _map_order_list, resolve_image_path,
    run_analysis_task, validate_analysis_request,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_scope_order_lists_map_to_global_orders() -> None:
    mapping = {1: 2, 2: 4, 3: 7}
    assert _map_order_list("1;2;3", mapping) == "2;4;7"
    assert _map_order_list("", mapping) == ""
    assert _map_order_list("bad;2;99", mapping) == "4"


def _subset_products(tmp_path: Path, count: int = 5) -> Path:
    image_dir = tmp_path / "合成测试图片"
    image_dir.mkdir(exist_ok=True)
    a_image = np.full((96, 128), 80, dtype=np.uint8)
    e_image = cv2.cvtColor(a_image, cv2.COLOR_GRAY2BGR)
    cv2.rectangle(e_image, (30, 25), (42, 37), (0, 0, 255), thickness=-1)
    a_path, e_path = image_dir / "A.png", image_dir / "E.png"
    cv2.imencode(".png", a_image)[1].tofile(str(a_path))
    cv2.imencode(".png", e_image)[1].tofile(str(e_path))
    products = pd.DataFrame({
        "global_order": range(1, count + 1),
        "order_code": [f"TEST-{order:03d}" for order in range(1, count + 1)],
        "dmc_raw": [f"DMC-{order:03d}" for order in range(1, count + 1)],
        "camera": ["5S"] * count,
        "a_image_path": [str(a_path)] * count,
        "e_image_path": [str(e_path)] * count,
    })
    path = tmp_path / "products.csv"
    products.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def test_cancelled_task_keeps_partial_only(tmp_path: Path) -> None:
    token = CancellationToken()
    events = []

    def progress(event) -> None:
        events.append(event)
        if event.stage == "EXTRACTING" and event.processed == 1:
            token.cancel()

    result = run_analysis_task(
        AnalysisRequest(_subset_products(tmp_path), PROJECT_ROOT / "config/analysis_config.yaml",
                        tmp_path, "取消测试"),
        AnalysisCallbacks(on_progress=progress),
        token,
    )
    assert result.status == "cancelled"
    assert (result.output_dir / "partial_extracted_defects.csv").exists()
    assert not (result.output_dir / "discovered_patterns.csv").exists()
    assert any(event.stage == "EXTRACTING" for event in events)


def test_each_task_gets_a_new_directory(tmp_path: Path) -> None:
    request = AnalysisRequest(_subset_products(tmp_path, 2), PROJECT_ROOT / "config/analysis_config.yaml",
                              tmp_path, "独立任务")
    first = run_analysis_task(request)
    second = run_analysis_task(request)
    assert first.output_dir != second.output_dir
    assert first.output_dir.exists() and second.output_dir.exists()
    assert (first.output_dir / "task_manifest.json").exists()


def test_relative_image_path_is_resolved_from_products_csv_ancestors(tmp_path: Path) -> None:
    project = tmp_path / "公司项目"
    products_path = project / "data" / "dataset" / "products.csv"
    image_path = project / "data" / "dataset" / "A" / "sample.png"
    products_path.parent.mkdir(parents=True)
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"image")

    resolved = resolve_image_path(
        "data/dataset/A/sample.png",
        tmp_path / "_internal",
        products_path=products_path,
    )

    assert resolved == image_path.resolve()


def test_analysis_request_accepts_runtime_product_index(tmp_path: Path) -> None:
    a_path, e_path = tmp_path / "runtime_a.png", tmp_path / "runtime_e.png"
    image = np.zeros((32, 32), dtype=np.uint8)
    annotated = np.zeros((32, 32, 3), dtype=np.uint8)
    cv2.imencode(".png", image)[1].tofile(str(a_path))
    cv2.imencode(".png", annotated)[1].tofile(str(e_path))
    products = pd.DataFrame({
        "global_order": [1], "order_code": ["DMC-1"], "dmc_raw": ["DMC-1"],
        "camera": ["7X"], "a_image_path": [str(a_path)], "e_image_path": [str(e_path)],
    })
    request = AnalysisRequest(
        None, PROJECT_ROOT / "config/analysis_config.yaml", tmp_path, "运行时索引",
        products_frame=products, source_index_frame=products,
    )
    loaded, _config, _root, _legacy = validate_analysis_request(request)
    assert loaded.loc[0, "camera"] == "7X"
    assert Path(loaded.loc[0, "a_image_path"]) == a_path.resolve()


def test_downsampled_e_image_generates_visualization_preview(tmp_path: Path) -> None:
    a_path, e_path = tmp_path / "large_a.png", tmp_path / "small_e.png"
    a_image = np.full((1280, 1920), 70, dtype=np.uint8)
    e_image = np.full((80, 120, 3), 150, dtype=np.uint8)
    cv2.rectangle(e_image, (30, 20), (42, 32), (0, 0, 255), 2)
    cv2.imencode(".png", a_image)[1].tofile(str(a_path))
    cv2.imencode(".png", e_image)[1].tofile(str(e_path))
    products = pd.DataFrame({
        "global_order": [1], "order_code": ["TEST-001"], "dmc_raw": ["DMC-1"],
        "camera": ["5S"], "a_image_path": [str(a_path)], "e_image_path": [str(e_path)],
    })
    products_path = tmp_path / "products.csv"
    products.to_csv(products_path, index=False, encoding="utf-8-sig")

    result = run_analysis_task(
        AnalysisRequest(products_path, PROJECT_ROOT / "config/analysis_config.yaml", tmp_path, "缩小E图预览")
    )

    assert result.status == "complete"
    assert (result.output_dir / "visualizations" / "extraction_preview.png").is_file()
    assert result.summary["local_defect_count"] == 1
    assert result.summary["micro_defect_count"] == 0
    assert result.summary["region_anomaly_count"] == 0
    assert "detection_type" in result.frames["extracted"]


def test_multi_scope_analysis_is_isolated_and_writes_scope_outputs(tmp_path: Path) -> None:
    image = np.full((64, 64), 70, dtype=np.uint8)
    annotated = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    cv2.rectangle(annotated, (20, 20), (28, 28), (0, 0, 255), thickness=-1)
    a_path, e_path = tmp_path / "a.png", tmp_path / "e.png"
    cv2.imencode(".png", image)[1].tofile(str(a_path))
    cv2.imencode(".png", annotated)[1].tofile(str(e_path))
    products = pd.DataFrame({
        "global_order": [1, 2, 3, 4], "scope_order": [1, 2, 1, 2],
        "analysis_scope": ["5S", "5S", "5X", "5X"],
        "order_code": ["A1", "A2", "B1", "B2"],
        "dmc_raw": ["A1", "A2", "B1", "B2"],
        "camera": ["5S", "5S", "5X", "5X"],
        "a_image_path": [str(a_path)] * 4, "e_image_path": [str(e_path)] * 4,
    })
    result = run_analysis_task(AnalysisRequest(
        None, PROJECT_ROOT / "config/analysis_config.yaml", tmp_path, "多范围",
        products_frame=products, source_index_frame=products,
        analysis_mode="full_process", enabled_scopes=("5S", "5X"),
    ))
    assert result.summary["enabled_scopes"] == ["5S", "5X"]
    assert set(result.frames["extracted"].analysis_scope) == {"5S", "5X"}
    assert set(result.frames["extracted"].cluster_id) == {"5S-C001", "5X-C001"}
    for scope in ("5S", "5X"):
        assert (result.output_dir / "scopes" / scope / "products.csv").is_file()
        assert (result.output_dir / "scopes" / scope / "extracted_defects.csv").is_file()


def test_task_writes_code_and_spatial_evidence_outputs(tmp_path: Path) -> None:
    products_path = _subset_products(tmp_path, 5)
    events = pd.DataFrame([
        {"event_id": f"A{order}", "dmc_raw": f"DMC-{order:03d}",
         "station_id": "35_5s_aoi", "test_date": f"2026-06-10 10:0{order}:00",
         "state": "NOK", "source_sheet": "AOI", "source_row": order + 1,
         "production_order": order, "Result.AOIFailureCode": "501100000000"}
        for order in range(1, 6)
    ] + [
        {"event_id": f"V{order}", "dmc_raw": f"DMC-{order:03d}",
         "station_id": "35_5s_vi", "test_date": f"2026-06-10 11:0{order}:00",
         "state": "NOK", "source_sheet": "MS0335all", "source_row": order + 1,
         "production_order": order, "BlockCode": "175011_" if order < 5 else "175520_"}
        for order in range(1, 6)
    ])
    result = run_analysis_task(AnalysisRequest(
        products_path, PROJECT_ROOT / "config/analysis_config.yaml", tmp_path, "证据融合",
        station_events_frame=events,
    ))
    for filename in (
        "normalized_defect_codes.csv", "defect_code_catalog_snapshot.csv", "code_patterns.csv",
        "spatial_trajectories.csv", "code_spatial_associations.csv", "code_label_conflicts.csv",
        "station_attribution.csv",
    ):
        assert (result.output_dir / filename).is_file()
    assert result.summary["normalized_code_event_count"] == 10
    assert not result.frames["code_patterns"].empty
    conflict = result.frames["code_conflicts"]
    assert conflict.loc[conflict.dmc_raw.eq("DMC-005"), "comparison_status"].iloc[0] == "label_conflict"
