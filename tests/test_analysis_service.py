"""本文件测试任务服务的进度回调、独立输出目录、安全取消和部分结果保留。"""

from pathlib import Path

import cv2
import numpy as np
import pandas as pd

from src.analysis_service import (
    AnalysisCallbacks, AnalysisRequest, CancellationToken, resolve_image_path, run_analysis_task,
    validate_analysis_request,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


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
