"""本文件测试灰度A/彩色E差分能提取AOI红线并正确处理跨分块轮廓。"""

import cv2
import numpy as np
import pandas as pd
import yaml
from pathlib import Path
import warnings

from src.contour_extractor import extract_dataset, extract_pair


CONFIG = Path(__file__).resolve().parents[1] / "config" / "analysis_config.yaml"


def test_a_e_difference_finds_added_outline() -> None:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    a_image = np.full((128, 192), 110, dtype=np.uint8)
    cv2.circle(a_image, (40, 50), 10, 170, -1)
    e_image = cv2.cvtColor(a_image, cv2.COLOR_GRAY2BGR)
    cv2.ellipse(e_image, (145, 35), (12, 8), 0, 0, 360, (0, 0, 255), 3, cv2.LINE_AA)
    detections = extract_pair(a_image, e_image, config)
    assert len(detections) == 1
    assert abs(detections[0]["center_x"] - 145) < 1
    assert abs(detections[0]["center_y"] - 35) < 1


def test_identical_pair_has_no_detection() -> None:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    a_image = np.full((128, 192), 110, dtype=np.uint8)
    e_image = cv2.cvtColor(a_image, cv2.COLOR_GRAY2BGR)
    assert extract_pair(a_image, e_image, config) == []


def test_brightness_shift_does_not_block_red_aoi_detection() -> None:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    a_image = np.full((128, 192), 60, dtype=np.uint8)
    e_image = np.full((128, 192, 3), 150, dtype=np.uint8)
    cv2.rectangle(e_image, (70, 40), (92, 60), (0, 0, 255), 2)
    detections = extract_pair(a_image, e_image, config)
    assert len(detections) == 1
    assert abs(detections[0]["center_x"] - 81) < 2
    assert abs(detections[0]["center_y"] - 50) < 2


def test_downsampled_e_aoi_is_mapped_back_to_a_coordinates() -> None:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    a_image = np.full((1280, 1920), 80, dtype=np.uint8)
    e_image = np.full((80, 120, 3), 150, dtype=np.uint8)
    cv2.rectangle(e_image, (30, 20), (42, 32), (0, 0, 255), 2)
    detections = extract_pair(a_image, e_image, config)
    assert len(detections) == 1
    assert abs(detections[0]["center_x"] - 576) < 24
    assert abs(detections[0]["center_y"] - 416) < 24
    assert detections[0]["bbox_x1"] >= 0
    assert detections[0]["bbox_x2"] < 1920


def test_outline_crossing_tile_boundary_is_returned_once() -> None:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    config.update(tile_width=96, tile_height=64, tile_overlap=20)
    a_image = np.full((128, 192), 100, dtype=np.uint8)
    e_image = cv2.cvtColor(a_image, cv2.COLOR_GRAY2BGR)
    cv2.circle(e_image, (96, 64), 12, (0, 0, 255), 3, cv2.LINE_AA)
    detections = extract_pair(a_image, e_image, config)
    assert len(detections) == 1
    assert abs(detections[0]["center_x"] - 96) < 1
    assert abs(detections[0]["center_y"] - 64) < 1


def test_legacy_v_image_path_remains_readable(tmp_path: Path) -> None:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    legacy_v = np.full((80, 120, 3), 100, dtype=np.uint8)
    e_image = legacy_v.copy()
    cv2.circle(e_image, (60, 40), 8, (0, 0, 255), 3, cv2.LINE_AA)
    cv2.imencode(".png", legacy_v)[1].tofile(str(tmp_path / "legacy_v.png"))
    cv2.imencode(".png", e_image)[1].tofile(str(tmp_path / "e.png"))
    products = pd.DataFrame([{
        "global_order": 1, "order_code": "0001", "dmc_raw": "DMC0001", "camera": "5S",
        "v_image_path": "legacy_v.png", "e_image_path": "e.png",
    }])
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        extracted = extract_dataset(products, tmp_path, config)
    assert len(extracted) == 1
    assert any("v_image_path" in str(item.message) for item in captured)
