"""本文件从彩色E图整图提取AOI红色标记，并映射到灰度A图坐标。"""

from pathlib import Path
from typing import Any
import warnings

import cv2
import numpy as np
import pandas as pd


EXTRACTED_COLUMNS = [
    "detected_id", "global_order", "order_code", "dmc_raw", "camera", "center_x", "center_y",
    "center_x_norm", "center_y_norm", "bbox_x1", "bbox_y1", "bbox_x2", "bbox_y2",
    "width", "height", "component_area", "detection_type", "cluster_id",
]

DETECTION_PROFILE_NAMES = ("5S", "5X", "7S", "7X")
DETECTION_DEFAULTS: dict[str, Any] = {
    "red_min": 150,
    "red_dominance": 40,
    "morph_kernel_size": 3,
    "min_component_area": 1,
    "max_component_area": 5000,
    "micro_max_component_area": 7,
    "region_min_width_ratio": 0.4,
    "region_min_height_ratio": 0.4,
}
DETECTION_KEYS = tuple(DETECTION_DEFAULTS)


def normalize_analysis_config(config: dict[str, Any]) -> dict[str, Any]:
    """Normalize legacy flat detection settings into four product profiles."""
    normalized = dict(config)
    legacy = {key: normalized[key] for key in DETECTION_KEYS if key in normalized}
    profiles = normalized.get("detection_profiles") or {}
    normalized["detection_profiles"] = {
        name: {**DETECTION_DEFAULTS, **legacy, **dict(profiles.get(name) or {})}
        for name in DETECTION_PROFILE_NAMES
    }
    for key in (*DETECTION_KEYS, "tile_width", "tile_height", "tile_overlap"):
        normalized.pop(key, None)
    return normalized


def detection_profile(config: dict[str, Any], camera: str) -> dict[str, Any]:
    """Return the resolved detection parameters for one product family."""
    if "detection_profiles" not in config:
        return {**DETECTION_DEFAULTS, **{key: config[key] for key in DETECTION_KEYS if key in config}}
    name = str(camera).upper()
    if name not in DETECTION_PROFILE_NAMES:
        raise ValueError(f"Unsupported detection profile: {camera}")
    return {**DETECTION_DEFAULTS, **dict(config["detection_profiles"].get(name) or {})}


def image_scale_to_reference(a_image: np.ndarray, e_image: np.ndarray) -> tuple[float, float]:
    """Return E-to-A coordinate scale after validating a shared field of view."""
    if e_image.ndim != 3 or e_image.shape[2] != 3:
        raise ValueError(f"E image must be BGR, got shape {e_image.shape}")
    a_height, a_width = a_image.shape[:2]
    e_height, e_width = e_image.shape[:2]
    if not a_height or not a_width or not e_height or not e_width:
        raise ValueError("A/E images must not be empty")
    aspect_error = abs((a_width / a_height) / (e_width / e_height) - 1.0)
    if aspect_error > 0.02:
        raise ValueError(
            f"A/E aspect ratios differ too much for coordinate mapping: "
            f"{a_width}x{a_height} vs {e_width}x{e_height}"
        )
    return a_width / e_width, a_height / e_height


def read_image(path: Path, flag: int = cv2.IMREAD_COLOR) -> np.ndarray:
    """从兼容Windows中文路径的文件中按指定模式读取图片。"""
    image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), flag)
    if image is None:
        raise OSError(f"Cannot read image: {path}")
    return image


def _extract_region(a_region: np.ndarray, e_region: np.ndarray,
                    config: dict[str, Any]) -> list[dict[str, Any]]:
    """在完整E图中提取红色连通域并进行对象分类。"""
    mask = build_failure_mask(a_region, e_region, config)
    count, _, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    detections: list[dict[str, Any]] = []
    image_height, image_width = mask.shape
    for label in range(1, count):
        x, y, box_width, box_height, area = map(int, stats[label])
        if area < int(config["min_component_area"]):
            continue
        is_region_anomaly = (
            box_width / image_width >= float(config["region_min_width_ratio"])
            and box_height / image_height >= float(config["region_min_height_ratio"])
        )
        if not is_region_anomaly and area > int(config["max_component_area"]):
            continue
        detection_type = (
            "region_anomaly" if is_region_anomaly
            else "micro" if area <= int(config["micro_max_component_area"])
            else "local"
        )
        center_x, center_y = map(float, centroids[label])
        detections.append({
            "center_x": center_x, "center_y": center_y,
            "bbox_x1": x, "bbox_y1": y, "bbox_x2": x + box_width - 1, "bbox_y2": y + box_height - 1,
            "width": box_width, "height": box_height, "component_area": area,
            "detection_type": detection_type,
        })
    return sorted(detections, key=lambda item: (item["center_y"], item["center_x"]))


def build_failure_mask(a_region: np.ndarray, e_region: np.ndarray,
                       config: dict[str, Any]) -> np.ndarray:
    """Extract E's red AOI annotation; A is retained only for API compatibility."""
    if e_region.ndim != 3 or e_region.shape[2] != 3:
        raise ValueError("E image must be BGR")
    red = e_region[:, :, 2].astype(np.int16)
    green = e_region[:, :, 1].astype(np.int16)
    blue = e_region[:, :, 0].astype(np.int16)
    mask = (
        (red >= int(config["red_min"]))
        & (red - green >= int(config["red_dominance"]))
        & (red - blue >= int(config["red_dominance"]))
    ).astype(np.uint8) * 255
    kernel_size = int(config["morph_kernel_size"])
    if kernel_size > 1:
        kernel = np.ones((kernel_size, kernel_size), dtype=np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    return mask


def extract_pair(a_image: np.ndarray, e_image: np.ndarray, config: dict[str, Any]) -> list[dict[str, Any]]:
    """整图提取E图AOI轮廓，并返回A原图坐标。"""
    if a_image.dtype != np.uint8 or e_image.dtype != np.uint8:
        raise ValueError("A/E images must be 8-bit uint8")
    scale_x, scale_y = image_scale_to_reference(a_image, e_image)
    detections: list[dict[str, Any]] = []
    for local in _extract_region(e_image, e_image, config):
        e_center_x, e_center_y = float(local["center_x"]), float(local["center_y"])
        local.update({
            "center_x": round((e_center_x + 0.5) * scale_x - 0.5, 3),
            "center_y": round((e_center_y + 0.5) * scale_y - 0.5, 3),
            "center_x_norm": round((e_center_x + 0.5) / e_image.shape[1], 7),
            "center_y_norm": round((e_center_y + 0.5) / e_image.shape[0], 7),
            "bbox_x1": int(np.floor(int(local["bbox_x1"]) * scale_x)),
            "bbox_y1": int(np.floor(int(local["bbox_y1"]) * scale_y)),
            "bbox_x2": min(a_image.shape[1] - 1, int(np.ceil((int(local["bbox_x2"]) + 1) * scale_x) - 1)),
            "bbox_y2": min(a_image.shape[0] - 1, int(np.ceil((int(local["bbox_y2"]) + 1) * scale_y) - 1)),
        })
        detections.append(local)
    return sorted(detections, key=lambda item: (item["center_y"], item["center_x"]))


def extract_dataset(products: pd.DataFrame, project_root: Path, config: dict[str, Any]) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    detection_number = 1
    use_legacy_path = "a_image_path" not in products.columns and "v_image_path" in products.columns
    if use_legacy_path:
        warnings.warn("v_image_path已弃用，请迁移为a_image_path；本次仍按旧彩色图兼容读取。", DeprecationWarning)
    for product in products.sort_values("global_order").itertuples(index=False):
        source_path = getattr(product, "v_image_path") if use_legacy_path else getattr(product, "a_image_path")
        a_flag = cv2.IMREAD_COLOR if use_legacy_path else cv2.IMREAD_GRAYSCALE
        a_image = read_image(project_root / Path(source_path), a_flag)
        e_image = read_image(project_root / Path(product.e_image_path), cv2.IMREAD_COLOR)
        profile = detection_profile(config, str(product.camera))
        for detection in extract_pair(a_image, e_image, profile):
            detection.update({
                "detected_id": f"X{detection_number:04d}", "global_order": int(product.global_order),
                "order_code": str(product.order_code), "dmc_raw": str(product.dmc_raw),
                "camera": str(product.camera), "cluster_id": "",
            })
            records.append(detection)
            detection_number += 1
    return pd.DataFrame(records, columns=EXTRACTED_COLUMNS)


def extract_product(product: Any, project_root: Path, config: dict[str, Any],
                    use_legacy_path: bool = False, image_root: Path | None = None,
                    detection_start: int = 1) -> list[dict[str, Any]]:
    """提取单片产品并返回带产品信息的记录，供可取消任务服务逐片调用。"""
    source_path = getattr(product, "v_image_path") if use_legacy_path else getattr(product, "a_image_path")

    def resolve(value: str) -> Path:
        path = Path(value)
        if path.is_absolute():
            return path
        candidates = []
        if image_root is not None:
            candidates.extend([image_root / path, image_root / path.parent.name / path.name, image_root / path.name])
        candidates.append(project_root / path)
        return next((candidate for candidate in candidates if candidate.exists()), candidates[0])

    a_flag = cv2.IMREAD_COLOR if use_legacy_path else cv2.IMREAD_GRAYSCALE
    a_image = read_image(resolve(str(source_path)), a_flag)
    e_image = read_image(resolve(str(product.e_image_path)), cv2.IMREAD_COLOR)
    records: list[dict[str, Any]] = []
    profile = detection_profile(config, str(product.camera))
    for offset, detection in enumerate(extract_pair(a_image, e_image, profile)):
        detection.update({
            "detected_id": f"X{detection_start + offset:04d}", "global_order": int(product.global_order),
            "order_code": str(product.order_code), "dmc_raw": str(product.dmc_raw),
            "camera": str(product.camera), "cluster_id": "",
        })
        records.append(detection)
    return records
