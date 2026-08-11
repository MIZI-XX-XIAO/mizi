"""本文件测试E图整图红色AOI提取、分类、配置选择和A坐标映射。"""

import cv2
import numpy as np
import pandas as pd
import yaml
from pathlib import Path
import warnings

from src.contour_extractor import (
    detection_profile, extract_dataset, extract_pair, normalize_analysis_config,
)


CONFIG = Path(__file__).resolve().parents[1] / "config" / "analysis_config.yaml"


def _config(profile: str = "5S") -> dict:
    return detection_profile(yaml.safe_load(CONFIG.read_text(encoding="utf-8")), profile)


def test_a_e_difference_finds_added_outline() -> None:
    config = _config()
    a_image = np.full((128, 192), 110, dtype=np.uint8)
    cv2.circle(a_image, (40, 50), 10, 170, -1)
    e_image = cv2.cvtColor(a_image, cv2.COLOR_GRAY2BGR)
    cv2.ellipse(e_image, (145, 35), (12, 8), 0, 0, 360, (0, 0, 255), 3, cv2.LINE_AA)
    detections = extract_pair(a_image, e_image, config)
    assert len(detections) == 1
    assert abs(detections[0]["center_x"] - 145) < 1
    assert abs(detections[0]["center_y"] - 35) < 1


def test_identical_pair_has_no_detection() -> None:
    config = _config()
    a_image = np.full((128, 192), 110, dtype=np.uint8)
    e_image = cv2.cvtColor(a_image, cv2.COLOR_GRAY2BGR)
    assert extract_pair(a_image, e_image, config) == []


def test_brightness_shift_does_not_block_red_aoi_detection() -> None:
    config = _config()
    a_image = np.full((128, 192), 60, dtype=np.uint8)
    e_image = np.full((128, 192, 3), 150, dtype=np.uint8)
    cv2.rectangle(e_image, (70, 40), (92, 60), (0, 0, 255), 2)
    detections = extract_pair(a_image, e_image, config)
    assert len(detections) == 1
    assert abs(detections[0]["center_x"] - 81) < 2
    assert abs(detections[0]["center_y"] - 50) < 2


def test_downsampled_e_aoi_is_mapped_back_to_a_coordinates() -> None:
    config = _config()
    a_image = np.full((1280, 1920), 80, dtype=np.uint8)
    e_image = np.full((80, 120, 3), 150, dtype=np.uint8)
    cv2.rectangle(e_image, (30, 20), (42, 32), (0, 0, 255), 2)
    detections = extract_pair(a_image, e_image, config)
    assert len(detections) == 1
    assert abs(detections[0]["center_x"] - 576) < 24
    assert abs(detections[0]["center_y"] - 416) < 24
    assert detections[0]["bbox_x1"] >= 0
    assert detections[0]["bbox_x2"] < 1920


def test_long_outline_is_returned_once_by_full_image_detection() -> None:
    config = _config()
    a_image = np.full((128, 192), 100, dtype=np.uint8)
    e_image = cv2.cvtColor(a_image, cv2.COLOR_GRAY2BGR)
    cv2.circle(e_image, (96, 64), 12, (0, 0, 255), 3, cv2.LINE_AA)
    detections = extract_pair(a_image, e_image, config)
    assert len(detections) == 1
    assert abs(detections[0]["center_x"] - 96) < 1
    assert abs(detections[0]["center_y"] - 64) < 1


def test_single_red_pixel_is_a_micro_detection() -> None:
    config = _config("5X")
    a_image = np.full((80, 120), 80, dtype=np.uint8)
    e_image = np.full((80, 120, 3), 80, dtype=np.uint8)
    e_image[30, 40] = (0, 0, 255)
    detections = extract_pair(a_image, e_image, config)
    assert len(detections) == 1
    assert detections[0]["component_area"] == 1
    assert detections[0]["detection_type"] == "micro"


def test_large_enclosing_outline_is_one_region_anomaly() -> None:
    config = _config()
    a_image = np.full((100, 200), 80, dtype=np.uint8)
    e_image = np.full((100, 200, 3), 80, dtype=np.uint8)
    cv2.rectangle(e_image, (20, 15), (180, 85), (0, 0, 255), 2)
    detections = extract_pair(a_image, e_image, config)
    assert len(detections) == 1
    assert detections[0]["detection_type"] == "region_anomaly"


def test_legacy_flat_config_is_expanded_to_all_profiles() -> None:
    normalized = normalize_analysis_config({"red_min": 170, "min_component_area": 3})
    assert set(normalized["detection_profiles"]) == {"5S", "5X", "7S", "7X"}
    assert all(profile["red_min"] == 170 for profile in normalized["detection_profiles"].values())
    assert all(profile["min_component_area"] == 3 for profile in normalized["detection_profiles"].values())


def test_product_profiles_resolve_independently() -> None:
    config = normalize_analysis_config({})
    config["detection_profiles"]["5S"]["red_min"] = 120
    config["detection_profiles"]["5X"]["red_min"] = 220
    assert detection_profile(config, "5S")["red_min"] == 120
    assert detection_profile(config, "5X")["red_min"] == 220
    assert detection_profile(config, "7S")["red_min"] == 150


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
