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
from .contour_extractor import (
    DETECTION_KEYS, DETECTION_PROFILE_NAMES, EXTRACTED_COLUMNS, extract_product,
    image_scale_to_reference, normalize_analysis_config, read_image,
)
from .pattern_analyzer import OnlinePatternEngine


STAGES = ("VALIDATING", "EXTRACTING", "ANALYZING", "WRITING", "VISUALIZING", "COMPLETE")


def load_analysis_config(path: Path) -> dict[str, Any]:
    """加载并校验生产缺陷分析所需的YAML参数。"""
    with path.open("r", encoding="utf-8") as handle:
        config = normalize_analysis_config(yaml.safe_load(handle) or {})
    required = {
        "spatial_cluster_radius_norm", "minimum_repeat_occurrences", "minimum_period",
        "maximum_period", "minimum_period_precision", "minimum_period_coverage",
        "burst_minimum_length", "warning_lead_products", "output_directory",
    }
    if missing := required - set(config):
        raise ValueError(f"分析配置缺少字段：{sorted(missing)}")
    for name in DETECTION_PROFILE_NAMES:
        if missing := set(DETECTION_KEYS) - set(config["detection_profiles"][name]):
            raise ValueError(f"检测配置 {name} 缺少字段：{sorted(missing)}")
    return config


@dataclass(frozen=True)
class AnalysisRequest:
    products_path: Path | None
    config_path: Path
    output_parent: Path
    task_name: str
    image_root: Path | None = None
    config_snapshot: dict[str, Any] | None = None
    products_frame: pd.DataFrame | None = None
    source_files: tuple[Path, ...] = ()
    source_index_frame: pd.DataFrame | None = None
    source_issues_frame: pd.DataFrame | None = None
    analysis_mode: str = "single_aoi"
    enabled_scopes: tuple[str, ...] = ()
    image_roots: dict[str, Path] = field(default_factory=dict)


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
    products_path = request.products_path.resolve() if request.products_path is not None else None
    config_path = request.config_path.resolve()
    if request.products_frame is None and (products_path is None or not products_path.is_file()):
        raise FileNotFoundError(f"products.csv不存在：{products_path}")
    if request.config_snapshot is None and not config_path.is_file():
        raise FileNotFoundError(f"分析配置不存在：{config_path}")
    if not request.output_parent.exists() or not request.output_parent.is_dir():
        raise FileNotFoundError(f"输出父目录不存在：{request.output_parent}")
    _safe_task_name(request.task_name)
    config = (
        normalize_analysis_config(dict(request.config_snapshot))
        if request.config_snapshot is not None else load_analysis_config(config_path)
    )
    products = (
        request.products_frame.copy()
        if request.products_frame is not None
        else pd.read_csv(products_path)
    )
    required = {"global_order", "order_code", "dmc_raw", "camera", "e_image_path"}
    if missing := required - set(products.columns):
        raise ValueError(f"products.csv缺少字段：{sorted(missing)}")
    use_legacy = "a_image_path" not in products.columns and "v_image_path" in products.columns
    if "a_image_path" not in products.columns and not use_legacy:
        raise ValueError("products.csv必须包含a_image_path（或兼容字段v_image_path）")
    if products.empty or products["global_order"].duplicated().any() or not products["global_order"].is_monotonic_increasing:
        raise ValueError("global_order必须非空、唯一且严格递增")
    supported_cameras = {"5S", "5X", "7S", "7X"}
    unknown_cameras = set(products["camera"].astype(str)) - supported_cameras
    if unknown_cameras:
        raise ValueError(f"存在未配置的图片产品族：{sorted(unknown_cameras)}")
    if "analysis_scope" not in products:
        products["analysis_scope"] = products["camera"].astype(str).str.upper()
    if "scope_order" not in products:
        products["scope_order"] = products.groupby("analysis_scope").cumcount() + 1
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
    image_scale_to_reference(a_image, e_image)
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
        input_files: dict[str, Any] = {}
        if request.products_path is not None:
            input_files["products"] = file_fingerprint(request.products_path.resolve())
        for index, source in enumerate(request.source_files, 1):
            resolved_source = source.resolve()
            if resolved_source.is_file():
                input_files[f"source_{index}"] = file_fingerprint(resolved_source)
        manifest = {
            **runtime_metadata(),
            "task_name": request.task_name,
            "input_files": input_files,
            "input_summary": {
                "product_count": len(products),
                "source_index_count": (
                    len(request.source_index_frame)
                    if request.source_index_frame is not None else len(products)
                ),
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
        source_index = request.source_index_frame if request.source_index_frame is not None else products
        source_index.to_csv(work_dir / "station_product_index.csv", index=False, encoding="utf-8-sig")
        source_issues = (
            request.source_issues_frame
            if request.source_issues_frame is not None
            else pd.DataFrame(columns=["级别", "DMC", "文件", "问题"])
        )
        source_issues.to_csv(
            work_dir / "station_source_issues.csv", index=False, encoding="utf-8-sig"
        )
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
        assigned_parts: list[pd.DataFrame] = []
        cluster_parts: list[pd.DataFrame] = []
        pattern_parts: list[pd.DataFrame] = []
        alert_parts: list[pd.DataFrame] = []
        analyzed_count = 0
        scopes = products["analysis_scope"].drop_duplicates().astype(str).tolist()
        for scope in scopes:
            scope_products = products[products["analysis_scope"].astype(str).eq(scope)].copy()
            order_to_global = dict(zip(scope_products["scope_order"].astype(int), scope_products["global_order"].astype(int)))
            global_to_scope = {value: key for key, value in order_to_global.items()}
            engine_products = scope_products.copy()
            engine_products["global_order"] = engine_products["scope_order"].astype(int)
            scope_detections = extracted[extracted["global_order"].isin(global_to_scope)].copy()
            scope_detections["global_order"] = scope_detections["global_order"].map(global_to_scope)

            def scoped_alert(alert: dict[str, Any], scope_name: str = scope) -> None:
                item = dict(alert)
                item["analysis_scope"] = scope_name
                item["alert_id"] = f"{scope_name}-{item['alert_id']}"
                item["cluster_id"] = f"{scope_name}-{item['cluster_id']}"
                callbacks.on_alert(item)

            engine = OnlinePatternEngine(config, alert_callback=scoped_alert)

            def analysis_progress(order: int, processed: int, analysis_total: int) -> None:
                completed = analyzed_count + processed
                callbacks.on_progress(ProgressEvent(
                    "ANALYZING", order_to_global.get(order), completed, total,
                    70 + round(completed / total * 15), len(extracted),
                ))

            scope_assigned, scope_clusters, scope_patterns, scope_alerts = engine.process(
                engine_products, scope_detections, progress_callback=analysis_progress,
                cancel_check=lambda: token.is_cancelled,
            )
            analyzed_count += len(scope_products)
            scope_assigned["global_order"] = scope_assigned["global_order"].map(order_to_global)
            scope_assigned["analysis_scope"] = scope
            if "cluster_id" in scope_assigned:
                mask = scope_assigned["cluster_id"].astype(str).str.strip().ne("")
                scope_assigned.loc[mask, "cluster_id"] = scope + "-" + scope_assigned.loc[mask, "cluster_id"].astype(str)
            for frame in (scope_clusters, scope_patterns, scope_alerts):
                frame["analysis_scope"] = scope
                if not frame.empty and "cluster_id" in frame:
                    frame["cluster_id"] = scope + "-" + frame["cluster_id"].astype(str)
            if not scope_patterns.empty:
                scope_patterns["pattern_id"] = scope + "-" + scope_patterns["pattern_id"].astype(str)
                for column in ("first_order", "last_order", "confirmed_at_order", "next_expected_order"):
                    if column in scope_patterns:
                        scope_patterns[column] = scope_patterns[column].map(order_to_global)
            if not scope_alerts.empty:
                scope_alerts["alert_id"] = scope + "-" + scope_alerts["alert_id"].astype(str)
                for column in ("alert_at_order", "predicted_order"):
                    if column in scope_alerts:
                        scope_alerts[column] = scope_alerts[column].map(order_to_global)
            assigned_parts.append(scope_assigned)
            cluster_parts.append(scope_clusters)
            pattern_parts.append(scope_patterns)
            alert_parts.append(scope_alerts)
        assigned = pd.concat(assigned_parts, ignore_index=True) if assigned_parts else pd.DataFrame(columns=EXTRACTED_COLUMNS)
        clusters = pd.concat(cluster_parts, ignore_index=True) if cluster_parts else pd.DataFrame()
        patterns = pd.concat(pattern_parts, ignore_index=True) if pattern_parts else pd.DataFrame()
        alerts = pd.concat(alert_parts, ignore_index=True) if alert_parts else pd.DataFrame()
        if token.is_cancelled:
            raise InterruptedError("用户取消")
        callbacks.on_stage("WRITING")
        callbacks.on_progress(ProgressEvent("WRITING", None, total, total, 88, len(assigned)))
        assigned.to_csv(work_dir / "extracted_defects.csv", index=False, encoding="utf-8-sig")
        clusters.to_csv(work_dir / "spatial_clusters.csv", index=False, encoding="utf-8-sig")
        patterns.to_csv(work_dir / "discovered_patterns.csv", index=False, encoding="utf-8-sig")
        alerts.to_csv(work_dir / "alerts.csv", index=False, encoding="utf-8-sig")
        scopes_root = work_dir / "scopes"
        for scope in scopes:
            scope_dir = scopes_root / scope
            scope_dir.mkdir(parents=True, exist_ok=True)
            products[products["analysis_scope"].astype(str).eq(scope)].to_csv(
                scope_dir / "products.csv", index=False, encoding="utf-8-sig"
            )
            for filename, frame in (
                ("extracted_defects.csv", assigned), ("spatial_clusters.csv", clusters),
                ("discovered_patterns.csv", patterns), ("alerts.csv", alerts),
            ):
                scoped = (
                    frame[frame["analysis_scope"].astype(str).eq(scope)]
                    if not frame.empty and "analysis_scope" in frame else frame.iloc[0:0]
                )
                scoped.to_csv(scope_dir / filename, index=False, encoding="utf-8-sig")
        summary = {
            "task_name": request.task_name, "status": "complete", "analyzed_product_count": total,
            "extracted_defect_count": len(assigned), "products_with_extracted_defects": int(assigned.global_order.nunique()),
            "micro_defect_count": int((assigned.detection_type == "micro").sum()),
            "local_defect_count": int((assigned.detection_type == "local").sum()),
            "region_anomaly_count": int((assigned.detection_type == "region_anomaly").sum()),
            "spatial_cluster_count": len(clusters), "discovered_pattern_count": len(patterns),
            "periodic_pattern_count": int((patterns.pattern_type == "periodic").sum()) if not patterns.empty else 0,
            "burst_pattern_count": int((patterns.pattern_type == "burst").sum()) if not patterns.empty else 0,
            "alert_count": len(alerts), "elapsed_seconds": round(perf_counter() - started, 3),
            "truth_labels_used": False, "application_version": APP_VERSION,
            "algorithm_version": ALGORITHM_VERSION,
            "analysis_mode": request.analysis_mode,
            "enabled_scopes": scopes,
            "scope_summary": {
                scope: {
                    "product_count": int(products["analysis_scope"].astype(str).eq(scope).sum()),
                    "defect_count": int(assigned["analysis_scope"].astype(str).eq(scope).sum()),
                    "pattern_count": int(patterns["analysis_scope"].astype(str).eq(scope).sum()) if not patterns.empty else 0,
                    "alert_count": int(alerts["analysis_scope"].astype(str).eq(scope).sum()) if not alerts.empty else 0,
                } for scope in scopes
            },
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
        for scope in scopes:
            scope_products = display_products[display_products["analysis_scope"].astype(str).eq(scope)]
            scope_assigned = assigned[assigned["analysis_scope"].astype(str).eq(scope)]
            scope_clusters = clusters[clusters["analysis_scope"].astype(str).eq(scope)] if not clusters.empty else clusters
            scope_patterns = patterns[patterns["analysis_scope"].astype(str).eq(scope)] if not patterns.empty else patterns
            scope_alerts = alerts[alerts["analysis_scope"].astype(str).eq(scope)] if not alerts.empty else alerts
            create_analysis_visualizations(
                scope_products, scope_assigned, scope_clusters, scope_patterns, scope_alerts,
                Path("."), work_dir / "scopes" / scope, config,
            )
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
