"""本文件通过成对灰度A/彩色E图分块差分提取AOI红色轮廓，不读取缺陷真值。"""

from pathlib import Path
from typing import Any
import warnings

import cv2
import numpy as np
import pandas as pd


EXTRACTED_COLUMNS = [
    "detected_id", "global_order", "order_code", "dmc_raw", "camera", "center_x", "center_y",
    "center_x_norm", "center_y_norm", "bbox_x1", "bbox_y1", "bbox_x2", "bbox_y2",
    "width", "height", "component_area", "cluster_id",
]


def read_image(path: Path, flag: int = cv2.IMREAD_COLOR) -> np.ndarray:
    """从兼容Windows中文路径的文件中按指定模式读取图片。"""
    image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), flag)
    if image is None:
        raise OSError(f"Cannot read image: {path}")
    return image


def _extract_region(a_region: np.ndarray, e_region: np.ndarray,
                    config: dict[str, Any]) -> list[dict[str, Any]]:
    """在一个含重叠边缘的局部区域中提取连通域。"""
    mask = build_failure_mask(a_region, e_region, config)
    count, _, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    detections: list[dict[str, Any]] = []
    for label in range(1, count):
        x, y, box_width, box_height, area = map(int, stats[label])
        if not int(config["min_component_area"]) <= area <= int(config["max_component_area"]):
            continue
        center_x, center_y = map(float, centroids[label])
        detections.append({
            "center_x": center_x, "center_y": center_y,
            "bbox_x1": x, "bbox_y1": y, "bbox_x2": x + box_width - 1, "bbox_y2": y + box_height - 1,
            "width": box_width, "height": box_height, "component_area": area,
        })
    return sorted(detections, key=lambda item: (item["center_y"], item["center_x"]))


def build_failure_mask(a_region: np.ndarray, e_region: np.ndarray,
                       config: dict[str, Any]) -> np.ndarray:
    """构建与主提取算法完全一致的二值failure mask，供Qt调试界面复用。"""
    if a_region.shape[:2] != e_region.shape[:2] or e_region.ndim != 3:
        raise ValueError("A/E region sizes must match and E must be BGR")
    if a_region.ndim == 2:
        # A保持单通道，仅在当前小分块中计算三个E通道相对灰度A的变化。
        difference = np.max(np.abs(e_region.astype(np.int16) - a_region[:, :, None].astype(np.int16)), axis=2)
    elif a_region.ndim == 3 and a_region.shape[2] == 3:
        # 兼容旧版彩色V图数据。
        difference = cv2.absdiff(e_region, a_region).max(axis=2)
    else:
        raise ValueError(f"A image must be uint8 grayscale or legacy BGR, got shape {a_region.shape}")
    red = e_region[:, :, 2].astype(np.int16)
    green = e_region[:, :, 1].astype(np.int16)
    blue = e_region[:, :, 0].astype(np.int16)
    mask = (
        (difference >= int(config["diff_threshold"]))
        & (red >= int(config["red_min"]))
        & (red - green >= int(config["red_dominance"]))
        & (red - blue >= int(config["red_dominance"]))
    ).astype(np.uint8) * 255
    kernel_size = int(config["morph_kernel_size"])
    if kernel_size > 1:
        kernel = np.ones((kernel_size, kernel_size), dtype=np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    return mask


def extract_pair(a_image: np.ndarray, e_image: np.ndarray, config: dict[str, Any]) -> list[dict[str, Any]]:
    """以重叠分块方式提取一对A/E图中的AOI轮廓，并返回全图坐标。"""
    if a_image.dtype != np.uint8 or e_image.dtype != np.uint8:
        raise ValueError("A/E images must be 8-bit uint8")
    if e_image.ndim != 3 or e_image.shape[2] != 3:
        raise ValueError(f"E image must be BGR, got shape {e_image.shape}")
    if a_image.shape[:2] != e_image.shape[:2]:
        raise ValueError(f"A/E image sizes differ: {a_image.shape[:2]} vs {e_image.shape[:2]}")
    image_height, image_width = a_image.shape[:2]
    tile_width = max(1, int(config.get("tile_width", image_width)))
    tile_height = max(1, int(config.get("tile_height", image_height)))
    overlap = max(0, int(config.get("tile_overlap", 0)))
    detections: list[dict[str, Any]] = []
    for core_y1 in range(0, image_height, tile_height):
        core_y2 = min(core_y1 + tile_height, image_height)
        for core_x1 in range(0, image_width, tile_width):
            core_x2 = min(core_x1 + tile_width, image_width)
            read_x1, read_y1 = max(0, core_x1 - overlap), max(0, core_y1 - overlap)
            read_x2, read_y2 = min(image_width, core_x2 + overlap), min(image_height, core_y2 + overlap)
            a_region = a_image[read_y1:read_y2, read_x1:read_x2]
            e_region = e_image[read_y1:read_y2, read_x1:read_x2]
            for local in _extract_region(a_region, e_region, config):
                global_x = float(local["center_x"]) + read_x1
                global_y = float(local["center_y"]) + read_y1
                # 每个质心仅由其所在核心分块接收，重叠区域不会产生重复结果。
                if not (core_x1 <= global_x < core_x2 and core_y1 <= global_y < core_y2):
                    continue
                local.update({
                    "center_x": round(global_x, 3), "center_y": round(global_y, 3),
                    "center_x_norm": round(global_x / image_width, 7),
                    "center_y_norm": round(global_y / image_height, 7),
                    "bbox_x1": int(local["bbox_x1"]) + read_x1,
                    "bbox_y1": int(local["bbox_y1"]) + read_y1,
                    "bbox_x2": int(local["bbox_x2"]) + read_x1,
                    "bbox_y2": int(local["bbox_y2"]) + read_y1,
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
        for detection in extract_pair(a_image, e_image, config):
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
    for offset, detection in enumerate(extract_pair(a_image, e_image, config)):
        detection.update({
            "detected_id": f"X{detection_start + offset:04d}", "global_order": int(product.global_order),
            "order_code": str(product.order_code), "dmc_raw": str(product.dmc_raw),
            "camera": str(product.camera), "cluster_id": "",
        })
        records.append(detection)
    return records
