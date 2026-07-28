"""本文件提供Qt界面使用的可进度、可取消、可追溯缺陷分析任务服务。"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Event
from time import perf_counter
from typing import Any, Callable
import json
import re
import shutil
import traceback

import cv2
import pandas as pd
import yaml

from .analysis_visualization import create_analysis_visualizations
from .app_runtime import (
    ALGORITHM_VERSION, APP_VERSION, configure_logging, file_fingerprint,
    new_error_id, runtime_metadata, write_json,
)
from .contour_extractor import EXTRACTED_COLUMNS, extract_product, read_image
from .pattern_analyzer import OnlinePatternEngine


STAGES = ("VALIDATING", "EXTRACTING", "ANALYZING", "WRITING", "VISUALIZING", "COMPLETE")


def load_analysis_config(path: Path) -> dict[str, Any]:
    """加载并校验生产缺陷分析所需的YAML参数。"""
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    required = {
        "diff_threshold", "red_min", "red_dominance", "min_component_area",
        "spatial_cluster_radius_norm", "minimum_repeat_occurrences", "minimum_period",
        "maximum_period", "minimum_period_precision", "minimum_period_coverage",
        "burst_minimum_length", "warning_lead_products", "output_directory",
    }
    if missing := required - set(config):
        raise ValueError(f"分析配置缺少字段：{sorted(missing)}")
    return config


@dataclass(frozen=True)
class AnalysisRequest:
    products_path: Path
    config_path: Path
    output_parent: Path
    task_name: str
    image_root: Path | None = None
    config_snapshot: dict[str, Any] | None = None


@dataclass(frozen=True)
class ProgressEvent:
    stage: str
    current_order: int | None
    processed: int
    total: int
    percent: int
    defect_count: int


@dataclass
class AnalysisCallbacks:
    on_stage: Callable[[str], None] = lambda _stage: None
    on_progress: Callable[[ProgressEvent], None] = lambda _event: None
    on_alert: Callable[[dict[str, Any]], None] = lambda _alert: None


@dataclass
class AnalysisResult:
    status: str
    output_dir: Path
    summary: dict[str, Any]
    frames: dict[str, pd.DataFrame] = field(default_factory=dict)


class CancellationToken:
    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()


def _safe_task_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value.strip())
    cleaned = cleaned.rstrip(". ")
    if not cleaned:
        raise ValueError("任务名称不能为空")
    return cleaned[:80]


def resolve_image_path(
    value: str,
    project_root: Path,
    image_root: Path | None = None,
    products_path: Path | None = None,
) -> Path:
    """按图片根目录、产品CSV位置和项目目录依次解析相对图片路径。"""
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    candidates: list[Path] = []
    if image_root is not None:
        candidates.extend((image_root / path, image_root / path.parent.name / path.name, image_root / path.name))
    if products_path is not None:
        products_path = products_path.resolve()
        candidates.extend(parent / path for parent in products_path.parents[:6])
    candidates.append(project_root / path)
    unique_candidates = list(dict.fromkeys(item.resolve() for item in candidates))
    return next((item for item in unique_candidates if item.exists()), unique_candidates[0])


def validate_analysis_request(request: AnalysisRequest) -> tuple[pd.DataFrame, dict[str, Any], Path, bool]:
    products_path = request.products_path.resolve()
    config_path = request.config_path.resolve()
    if not products_path.is_file():
        raise FileNotFoundError(f"products.csv不存在：{products_path}")
    if request.config_snapshot is None and not config_path.is_file():
        raise FileNotFoundError(f"分析配置不存在：{config_path}")
    if not request.output_parent.exists() or not request.output_parent.is_dir():
        raise FileNotFoundError(f"输出父目录不存在：{request.output_parent}")
    _safe_task_name(request.task_name)
    config = dict(request.config_snapshot) if request.config_snapshot is not None else load_analysis_config(config_path)
    products = pd.read_csv(products_path)
    required = {"global_order", "order_code", "dmc_raw", "camera", "e_image_path"}
    if missing := required - set(products.columns):
        raise ValueError(f"products.csv缺少字段：{sorted(missing)}")
    use_legacy = "a_image_path" not in products.columns and "v_image_path" in products.columns
    if "a_image_path" not in products.columns and not use_legacy:
        raise ValueError("products.csv必须包含a_image_path（或兼容字段v_image_path）")
    if products.empty or products["global_order"].duplicated().any() or not products["global_order"].is_monotonic_increasing:
        raise ValueError("global_order必须非空、唯一且严格递增")
    if set(products["camera"].astype(str)) != {"5S"}:
        raise ValueError("第一阶段只允许camera=5S")
    project_root = config_path.parent.parent
    source_column = "v_image_path" if use_legacy else "a_image_path"
    products = products.copy()
    for column in (source_column, "e_image_path"):
        products[column] = products[column].map(
            lambda value: str(resolve_image_path(
                str(value), project_root, request.image_root, products_path
            ))
        )
    missing_paths: list[str] = []
    for row in products.itertuples(index=False):
        for column in (source_column, "e_image_path"):
            resolved = Path(str(getattr(row, column)))
            if not resolved.is_file():
                missing_paths.append(str(resolved))
                if len(missing_paths) >= 10:
                    break
        if len(missing_paths) >= 10:
            break
    if missing_paths:
        raise FileNotFoundError("发现不存在的A/E路径（最多显示10项）：\n" + "\n".join(missing_paths))
    first = products.iloc[0]
    a_path = Path(str(first[source_column]))
    e_path = Path(str(first["e_image_path"]))
    a_flag = cv2.IMREAD_COLOR if use_legacy else cv2.IMREAD_GRAYSCALE
    a_image, e_image = read_image(a_path, a_flag), read_image(e_path, cv2.IMREAD_COLOR)
    if a_image.dtype.name != "uint8" or e_image.dtype.name != "uint8":
        raise ValueError("A/E必须为8位图像")
    if a_image.shape[:2] != e_image.shape[:2]:
        raise ValueError(f"首对A/E尺寸不一致：{a_image.shape[:2]} vs {e_image.shape[:2]}")
    return products, config, project_root, use_legacy


def run_analysis_task(request: AnalysisRequest, callbacks: AnalysisCallbacks | None = None,
                      cancellation_token: CancellationToken | None = None) -> AnalysisResult:
    callbacks = callbacks or AnalysisCallbacks()
    token = cancellation_token or CancellationToken()
    started = perf_counter()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base_name = f"{_safe_task_name(request.task_name)}_{timestamp}"
    work_dir = request.output_parent.resolve() / f".{base_name}.work"
    final_dir = request.output_parent.resolve() / base_name
    work_dir.mkdir(parents=False, exist_ok=False)
    extracted_records: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    logger, _log_path = configure_logging()

    def status_file(status: str, message: str = "", error_id: str = "") -> None:
        payload = {"status": status, "message": message, "updated_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        if error_id:
            payload["error_id"] = error_id
        (work_dir / "task_status.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        callbacks.on_stage("VALIDATING")
        products, config, project_root, use_legacy = validate_analysis_request(request)
        manifest = {
            **runtime_metadata(),
            "task_name": request.task_name,
            "input_files": {"products": file_fingerprint(request.products_path.resolve())},
            "input_summary": {
                "product_count": len(products),
                "columns": list(products.columns),
                "order_min": int(products.global_order.min()),
                "order_max": int(products.global_order.max()),
                "cameras": sorted(products.camera.astype(str).unique().tolist()),
            },
        }
        if request.config_path.is_file():
            manifest["input_files"]["config"] = file_fingerprint(request.config_path.resolve())
        write_json(work_dir / "task_manifest.json", manifest)
        with (work_dir / "analysis_config_snapshot.yaml").open("w", encoding="utf-8") as handle:
            yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
        total = len(products)
        callbacks.on_progress(ProgressEvent("VALIDATING", None, 0, total, 2, 0))
        if token.is_cancelled:
            raise InterruptedError("用户取消")

        callbacks.on_stage("EXTRACTING")
        detection_number = 1
        for processed, product in enumerate(products.itertuples(index=False), 1):
            if token.is_cancelled:
                raise InterruptedError("用户取消")
            records = extract_product(product, project_root, config, use_legacy, request.image_root, detection_number)
            extracted_records.extend(records)
            detection_number += len(records)
            percent = 5 + round(processed / total * 65)
            callbacks.on_progress(ProgressEvent("EXTRACTING", int(product.global_order), processed, total,
                                                percent, len(extracted_records)))
        extracted = pd.DataFrame(extracted_records, columns=EXTRACTED_COLUMNS)

        callbacks.on_stage("ANALYZING")
        engine = OnlinePatternEngine(config, alert_callback=callbacks.on_alert)

        def analysis_progress(order: int, processed: int, analysis_total: int) -> None:
            callbacks.on_progress(ProgressEvent("ANALYZING", order, processed, analysis_total,
                                                70 + round(processed / analysis_total * 15), len(extracted)))

        assigned, clusters, patterns, alerts = engine.process(
            products, extracted, progress_callback=analysis_progress,
            cancel_check=lambda: token.is_cancelled,
        )
        if token.is_cancelled:
            raise InterruptedError("用户取消")
        callbacks.on_stage("WRITING")
        callbacks.on_progress(ProgressEvent("WRITING", None, total, total, 88, len(assigned)))
        assigned.to_csv(work_dir / "extracted_defects.csv", index=False, encoding="utf-8-sig")
        clusters.to_csv(work_dir / "spatial_clusters.csv", index=False, encoding="utf-8-sig")
        patterns.to_csv(work_dir / "discovered_patterns.csv", index=False, encoding="utf-8-sig")
        alerts.to_csv(work_dir / "alerts.csv", index=False, encoding="utf-8-sig")
        summary = {
            "task_name": request.task_name, "status": "complete", "analyzed_product_count": total,
            "extracted_defect_count": len(assigned), "products_with_extracted_defects": int(assigned.global_order.nunique()),
            "spatial_cluster_count": len(clusters), "discovered_pattern_count": len(patterns),
            "periodic_pattern_count": int((patterns.pattern_type == "periodic").sum()) if not patterns.empty else 0,
            "burst_pattern_count": int((patterns.pattern_type == "burst").sum()) if not patterns.empty else 0,
            "alert_count": len(alerts), "elapsed_seconds": round(perf_counter() - started, 3),
            "truth_labels_used": False, "application_version": APP_VERSION,
            "algorithm_version": ALGORITHM_VERSION,
        }
        (work_dir / "analysis_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

        callbacks.on_stage("VISUALIZING")
        if token.is_cancelled:
            raise InterruptedError("用户取消")
        callbacks.on_progress(ProgressEvent("VISUALIZING", None, total, total, 94, len(assigned)))
        display_products = products.copy()
        source_column = "v_image_path" if use_legacy else "a_image_path"
        display_products[source_column] = display_products[source_column].map(
            lambda value: str(resolve_image_path(
                str(value), project_root, request.image_root, request.products_path
            ))
        )
        display_products["e_image_path"] = display_products["e_image_path"].map(
            lambda value: str(resolve_image_path(
                str(value), project_root, request.image_root, request.products_path
            ))
        )
        create_analysis_visualizations(display_products, assigned, clusters, patterns, alerts, Path("."), work_dir, config)
        if token.is_cancelled:
            raise InterruptedError("用户取消")
        status_file("complete")
        work_dir.rename(final_dir)
        callbacks.on_stage("COMPLETE")
        callbacks.on_progress(ProgressEvent("COMPLETE", None, total, total, 100, len(assigned)))
        return AnalysisResult("complete", final_dir, summary, {
            "products": display_products, "extracted": assigned, "clusters": clusters,
            "patterns": patterns, "alerts": alerts,
        })
    except InterruptedError:
        for filename in ("extracted_defects.csv", "spatial_clusters.csv", "discovered_patterns.csv",
                         "alerts.csv", "analysis_summary.json"):
            (work_dir / filename).unlink(missing_ok=True)
        if (work_dir / "visualizations").exists():
            shutil.rmtree(work_dir / "visualizations")
        partial = pd.DataFrame(extracted_records, columns=EXTRACTED_COLUMNS)
        partial.to_csv(work_dir / "partial_extracted_defects.csv", index=False, encoding="utf-8-sig")
        status_file("cancelled", "用户取消")
        cancelled_dir = request.output_parent.resolve() / f"{base_name}_cancelled"
        work_dir.rename(cancelled_dir)
        return AnalysisResult("cancelled", cancelled_dir, {"status": "cancelled"})
    except Exception as exc:
        error_id = new_error_id()
        status_file("failed", str(exc), error_id)
        (work_dir / "traceback.txt").write_text(traceback.format_exc(), encoding="utf-8")
        failed_dir = request.output_parent.resolve() / f"{base_name}_failed"
        work_dir.rename(failed_dir)
        logger.exception("analysis_failed error_id=%s output=%s", error_id, failed_dir)
        raise RuntimeError(
            f"分析失败（错误编号 {error_id}），诊断文件位于：{failed_dir}"
        ) from exc
