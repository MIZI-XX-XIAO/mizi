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
from .defect_evidence import (
    analyze_code_spatial_associations, build_station_attribution,
    discover_code_patterns, discover_spatial_trajectories,
    load_defect_catalog, map_code_events_to_products, normalize_defect_codes,
)
from .pattern_analyzer import OnlinePatternEngine
from .process_relationships import analyze_process_relationships


STAGES = ("VALIDATING", "EXTRACTING", "ANALYZING", "WRITING", "VISUALIZING", "COMPLETE")

ANALYSIS_MODULES = (
    "code_patterns", "image_patterns", "code_image", "process_relationships",
)


@dataclass(frozen=True)
class AnalysisSelection:
    """User-selected analysis targets; empty tuples preserve legacy all-analysis behavior."""

    mode: str = "all"
    scopes: tuple[str, ...] = ()
    code_sources: tuple[str, ...] = ()
    defect_codes: tuple[str, ...] = ()
    modules: tuple[str, ...] = ()
    process_parameters: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "scopes": list(self.scopes),
            "code_sources": list(self.code_sources),
            "defect_codes": list(self.defect_codes),
            "modules": list(self.modules),
            "process_parameters": list(self.process_parameters),
        }

    def includes(self, module: str) -> bool:
        return not self.modules or module in self.modules


def code_target_key(source_type: Any, canonical_code: Any) -> str:
    return f"{str(source_type).strip()}:{str(canonical_code).strip()}"


def filter_selected_code_events(
    normalized_codes: pd.DataFrame, selection: AnalysisSelection,
) -> pd.DataFrame:
    """Filter target events without filtering the complete product/control population."""
    result = normalized_codes.copy()
    if result.empty:
        return result
    if selection.scopes:
        result = result[result["analysis_scope"].astype(str).isin(selection.scopes)]
    if selection.code_sources:
        result = result[result["source_type"].astype(str).isin(selection.code_sources)]
    if selection.mode in {"selected_codes", "process_only"} and selection.defect_codes:
        keys = result.apply(
            lambda row: code_target_key(row.get("source_type"), row.get("canonical_code")), axis=1
        )
        result = result[keys.isin(selection.defect_codes)]
    return result.reset_index(drop=True)


def discover_selected_code_patterns(
    normalized_codes: pd.DataFrame,
    selection: AnalysisSelection,
    config: dict[str, Any],
    image_links: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Analyze selected source/code targets separately while retaining normal sequence rows."""
    if not selection.defect_codes or selection.mode == "all":
        return discover_code_patterns(normalized_codes, config, image_links=image_links)
    parts: list[pd.DataFrame] = []
    for target in selection.defect_codes:
        source, separator, code = target.partition(":")
        if not separator or not code:
            continue
        population = normalized_codes[
            normalized_codes["source_type"].astype(str).eq(source)
        ]
        if selection.scopes:
            population = population[
                population["analysis_scope"].astype(str).isin(selection.scopes)
            ]
        links = image_links
        if links is not None and not links.empty and "source_type" in links:
            links = links[links["source_type"].astype(str).eq(source)]
        part = discover_code_patterns(
            population, config, selected_codes=(code,), image_links=links,
        )
        if not part.empty:
            parts.append(part)
    return (
        pd.concat(parts, ignore_index=True)
        if parts else discover_code_patterns(normalized_codes.iloc[0:0], config)
    )


def _map_order_list(value: Any, order_mapping: dict[int, int]) -> str:
    """Map a semicolon-delimited scope-order list to global product orders."""
    if value is None or pd.isna(value):
        return ""
    mapped: list[str] = []
    for part in str(value).split(";"):
        try:
            scope_order = int(float(part.strip()))
        except (TypeError, ValueError):
            continue
        if scope_order in order_mapping:
            mapped.append(str(int(order_mapping[scope_order])))
    return ";".join(mapped)


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
    station_events_frame: pd.DataFrame | None = None
    defect_catalog_path: Path | None = None
    selection: AnalysisSelection = field(default_factory=AnalysisSelection)


@dataclass(frozen=True)
class ExcelTargetAnalysisRequest:
    config_path: Path
    output_parent: Path
    task_name: str
    station_events_frame: pd.DataFrame
    process_parameters_frame: pd.DataFrame
    selection: AnalysisSelection
    config_snapshot: dict[str, Any] | None = None
    source_files: tuple[Path, ...] = ()
    defect_catalog_path: Path | None = None


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
    if "task_order" not in products:
        products["task_order"] = products["global_order"]
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


def _excel_display_products(codes: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "global_order", "task_order", "scope_order", "production_order", "order_code",
        "dmc_raw", "camera", "analysis_scope", "station_id", "production_timestamp",
        "a_image_path", "e_image_path", "has_primary_pair",
    ]
    if codes.empty:
        return pd.DataFrame(columns=columns)
    base = codes.sort_values("production_order", na_position="last", kind="stable").copy()
    base = base.drop_duplicates(["analysis_scope", "dmc_raw"], keep="last")
    base = base.reset_index(drop=True)
    base["global_order"] = range(1, len(base) + 1)
    base["task_order"] = base["global_order"]
    base["scope_order"] = base.groupby("analysis_scope").cumcount() + 1
    base["order_code"] = base["dmc_raw"].astype(str)
    base["camera"] = base["analysis_scope"].astype(str)
    base["production_timestamp"] = pd.to_datetime(base["test_date"], errors="coerce")
    base["a_image_path"] = ""
    base["e_image_path"] = ""
    base["has_primary_pair"] = False
    return base[columns]


def _target_relationship_population(
    all_codes: pd.DataFrame, source: str, code: str, scope: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    population = all_codes[
        all_codes["source_type"].astype(str).eq(source)
        & all_codes["analysis_scope"].astype(str).eq(scope)
    ].sort_values("production_order", na_position="last", kind="stable")
    population = population.drop_duplicates("dmc_raw", keep="last").reset_index(drop=True)
    products = pd.DataFrame({
        "global_order": range(1, len(population) + 1),
        "dmc_raw": population["dmc_raw"].astype(str),
        "order_code": population["dmc_raw"].astype(str),
        "camera": scope,
        "analysis_scope": scope,
        "production_timestamp": pd.to_datetime(population["test_date"], errors="coerce"),
    })
    target_dmcs = set(all_codes.loc[
        all_codes["source_type"].astype(str).eq(source)
        & all_codes["analysis_scope"].astype(str).eq(scope)
        & all_codes["canonical_code"].astype(str).eq(code)
        & all_codes["code_status"].isin(["defect", "state_code_conflict"]),
        "dmc_raw",
    ].astype(str))
    target_orders = products.loc[products["dmc_raw"].isin(target_dmcs), "global_order"]
    defects = pd.DataFrame({"global_order": target_orders, "component_area": 1})
    return products, defects


def run_excel_target_task(
    request: ExcelTargetAnalysisRequest,
    callbacks: AnalysisCallbacks | None = None,
    cancellation_token: CancellationToken | None = None,
) -> AnalysisResult:
    """Run code-pattern and process-relationship targets without reading image files."""
    callbacks = callbacks or AnalysisCallbacks()
    token = cancellation_token or CancellationToken()
    started = perf_counter()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base_name = f"{_safe_task_name(request.task_name)}_{timestamp}"
    work_dir = request.output_parent.resolve() / f".{base_name}.work"
    final_dir = request.output_parent.resolve() / base_name
    work_dir.mkdir(parents=False, exist_ok=False)
    try:
        callbacks.on_stage("VALIDATING")
        if request.station_events_frame.empty:
            raise ValueError("Excel中没有可分析的工站事件")
        config = (
            normalize_analysis_config(dict(request.config_snapshot))
            if request.config_snapshot is not None else load_analysis_config(request.config_path)
        )
        project_root = request.config_path.resolve().parent.parent
        catalog = load_defect_catalog(
            project_root / "config" / "defect_code_catalog.csv", request.defect_catalog_path,
        )
        all_codes = normalize_defect_codes(request.station_events_frame, catalog)
        if request.selection.scopes:
            all_codes = all_codes[
                all_codes["analysis_scope"].astype(str).isin(request.selection.scopes)
            ].reset_index(drop=True)
        selected_codes = filter_selected_code_events(all_codes, request.selection)
        products = _excel_display_products(all_codes)
        target_counts = {
            code_target_key(source, code): len(
                group[["analysis_scope", "dmc_raw"]].astype(str).drop_duplicates()
            )
            for (source, code), group in selected_codes[
                selected_codes["code_status"].isin(["defect", "state_code_conflict"])
            ].groupby(["source_type", "canonical_code"])
        } if not selected_codes.empty else {}
        selection_payload = {**request.selection.to_dict(), "target_product_counts": target_counts}
        input_files = {
            f"source_{index}": file_fingerprint(path.resolve())
            for index, path in enumerate(request.source_files, 1) if path.resolve().is_file()
        }
        manifest = {
            **runtime_metadata(), "task_name": request.task_name,
            "input_files": input_files, "analysis_selection": selection_payload,
            "input_summary": {
                "product_count": len(products), "code_event_count": len(all_codes),
                "parameter_count": max(0, len(request.process_parameters_frame.columns) - 1),
            },
        }
        write_json(work_dir / "task_manifest.json", manifest)
        write_json(work_dir / "analysis_selection.json", selection_payload)
        with (work_dir / "analysis_config_snapshot.yaml").open("w", encoding="utf-8") as handle:
            yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
        callbacks.on_progress(ProgressEvent("VALIDATING", None, 0, len(products), 15, 0))
        if token.is_cancelled:
            raise InterruptedError("用户取消")

        callbacks.on_stage("ANALYZING")
        all_code_links = map_code_events_to_products(products, all_codes)
        code_patterns = (
            discover_selected_code_patterns(
                all_codes, request.selection, config, image_links=all_code_links,
            )
            if request.selection.includes("code_patterns")
            else discover_code_patterns(all_codes.iloc[0:0], config)
        )
        metric_parts: list[pd.DataFrame] = []
        bin_parts: list[pd.DataFrame] = []
        model_parts: list[pd.DataFrame] = []
        joined_parts: list[pd.DataFrame] = []
        relationship_summaries: list[dict[str, Any]] = []
        if request.selection.includes("process_relationships"):
            defect_events = selected_codes[
                selected_codes["code_status"].isin(["defect", "state_code_conflict"])
            ]
            targets = defect_events[["analysis_scope", "source_type", "canonical_code"]].drop_duplicates()
            for target_index, target in enumerate(targets.to_dict("records"), 1):
                scope = str(target["analysis_scope"])
                source = str(target["source_type"])
                code = str(target["canonical_code"])
                target_name = code_target_key(source, code)
                target_products, target_defects = _target_relationship_population(
                    all_codes, source, code, scope,
                )
                result = analyze_process_relationships(
                    target_products, target_defects, request.process_parameters_frame,
                    selected_parameters=request.selection.process_parameters,
                )
                metadata = {
                    "analysis_scope": scope, "source_type": source,
                    "canonical_code": code, "target": target_name,
                }
                for destination, frame in (
                    (metric_parts, result.parameter_metrics),
                    (bin_parts, result.binned_rates),
                    (model_parts, result.model_importance),
                    (joined_parts, result.joined),
                ):
                    enriched = frame.copy()
                    for name, value in reversed(tuple(metadata.items())):
                        if name in enriched:
                            enriched[name] = value
                        else:
                            enriched.insert(0, name, value)
                    destination.append(enriched)
                relationship_summaries.append({**metadata, **result.summary})
                callbacks.on_progress(ProgressEvent(
                    "ANALYZING", None, target_index, len(targets),
                    20 + round(target_index / max(1, len(targets)) * 65), len(defect_events),
                ))
        if token.is_cancelled:
            raise InterruptedError("用户取消")

        callbacks.on_stage("WRITING")
        empty_extracted = pd.DataFrame(columns=EXTRACTED_COLUMNS)
        empty_result = pd.DataFrame()
        frames_to_write = {
            "normalized_defect_codes.csv": selected_codes,
            "code_patterns.csv": code_patterns,
            "process_parameter_metrics.csv": pd.concat(metric_parts, ignore_index=True) if metric_parts else pd.DataFrame(),
            "process_parameter_binned_rates.csv": pd.concat(bin_parts, ignore_index=True) if bin_parts else pd.DataFrame(),
            "process_model_importance.csv": pd.concat(model_parts, ignore_index=True) if model_parts else pd.DataFrame(),
            "process_joined.csv": pd.concat(joined_parts, ignore_index=True) if joined_parts else pd.DataFrame(),
            "extracted_defects.csv": empty_extracted,
            "spatial_clusters.csv": empty_result,
            "discovered_patterns.csv": empty_result,
            "alerts.csv": empty_result,
            "spatial_trajectories.csv": empty_result,
            "code_spatial_associations.csv": empty_result,
            "code_label_conflicts.csv": empty_result,
            "station_attribution.csv": empty_result,
        }
        products.to_csv(work_dir / "station_product_index.csv", index=False, encoding="utf-8-sig")
        catalog.frame.to_csv(work_dir / "defect_code_catalog_snapshot.csv", index=False, encoding="utf-8-sig")
        catalog.issues.to_csv(work_dir / "defect_code_catalog_issues.csv", index=False, encoding="utf-8-sig")
        for filename, frame in frames_to_write.items():
            frame.to_csv(work_dir / filename, index=False, encoding="utf-8-sig")
        write_json(work_dir / "process_relationship_summary.json", relationship_summaries)
        summary = {
            "task_name": request.task_name, "status": "complete",
            "analyzed_product_count": len(products), "extracted_defect_count": 0,
            "products_with_extracted_defects": 0, "micro_defect_count": 0,
            "local_defect_count": 0, "region_anomaly_count": 0,
            "spatial_cluster_count": 0, "discovered_pattern_count": len(code_patterns),
            "periodic_pattern_count": int(code_patterns.get("pattern_type", pd.Series(dtype=str)).eq("periodic").sum()),
            "burst_pattern_count": int(code_patterns.get("pattern_type", pd.Series(dtype=str)).eq("burst").sum()),
            "alert_count": 0, "normalized_code_event_count": len(selected_codes),
            "code_pattern_count": len(code_patterns), "spatial_trajectory_count": 0,
            "code_spatial_association_count": 0, "code_label_conflict_count": 0,
            "elapsed_seconds": round(perf_counter() - started, 3),
            "application_version": APP_VERSION, "algorithm_version": ALGORITHM_VERSION,
            "analysis_mode": "excel_only", "enabled_scopes": list(request.selection.scopes),
            "analysis_selection": selection_payload, "image_analysis_executed": False,
            "relationship_targets": relationship_summaries,
        }
        write_json(work_dir / "analysis_summary.json", summary)
        write_json(work_dir / "task_status.json", {"status": "complete", "message": ""})
        callbacks.on_stage("COMPLETE")
        callbacks.on_progress(ProgressEvent("COMPLETE", None, len(products), len(products), 100, 0))
        work_dir.rename(final_dir)
        empty = pd.DataFrame()
        selected_code_links = filter_selected_code_events(all_code_links, request.selection)
        return AnalysisResult("complete", final_dir, summary, {
            "products": products, "extracted": empty_extracted,
            "clusters": empty, "patterns": empty, "alerts": empty,
            "normalized_codes": selected_codes, "code_patterns": code_patterns,
            "trajectories": empty, "code_space": empty, "code_conflicts": empty,
            "station_attribution": empty, "defect_catalog": catalog.frame,
            "code_image_links": selected_code_links,
            "process_metrics": frames_to_write["process_parameter_metrics.csv"],
            "process_bins": frames_to_write["process_parameter_binned_rates.csv"],
            "process_models": frames_to_write["process_model_importance.csv"],
            "process_joined": frames_to_write["process_joined.csv"],
        })
    except InterruptedError:
        write_json(work_dir / "task_status.json", {"status": "cancelled", "message": "用户取消"})
        cancelled_dir = request.output_parent.resolve() / f"{base_name}_cancelled"
        work_dir.rename(cancelled_dir)
        return AnalysisResult("cancelled", cancelled_dir, {"status": "cancelled"})
    except Exception as exc:
        error_id = new_error_id()
        write_json(work_dir / "task_status.json", {
            "status": "failed", "message": str(exc), "error_id": error_id,
        })
        (work_dir / "traceback.txt").write_text(traceback.format_exc(), encoding="utf-8")
        failed_dir = request.output_parent.resolve() / f"{base_name}_failed"
        work_dir.rename(failed_dir)
        raise RuntimeError(
            f"纯Excel目标分析失败（错误编号 {error_id}），诊断文件位于：{failed_dir}"
        ) from exc


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
            "analysis_selection": request.selection.to_dict(),
        }
        if request.config_path.is_file():
            manifest["input_files"]["config"] = file_fingerprint(request.config_path.resolve())
        builtin_catalog = project_root / "config" / "defect_code_catalog.csv"
        if builtin_catalog.is_file():
            manifest["input_files"]["builtin_defect_catalog"] = file_fingerprint(builtin_catalog.resolve())
        if request.defect_catalog_path is not None and request.defect_catalog_path.is_file():
            manifest["input_files"]["custom_defect_catalog"] = file_fingerprint(
                request.defect_catalog_path.resolve()
            )
        write_json(work_dir / "task_manifest.json", manifest)
        write_json(work_dir / "analysis_selection.json", request.selection.to_dict())
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
        products["_analysis_station"] = (
            products["station_id"].fillna("").astype(str)
            if "station_id" in products else ""
        )
        analysis_groups = products.groupby(
            ["analysis_scope", "_analysis_station"], sort=False, dropna=False
        )
        for (scope, station_id), scope_products in analysis_groups:
            scope = str(scope)
            station_id = str(station_id)
            group_prefix = re.sub(r"[^A-Za-z0-9_-]+", "-", station_id or scope)
            scope_products = scope_products.copy()
            sequence_column = (
                "production_order" if "production_order" in scope_products else "scope_order"
            )
            sequence_values = pd.to_numeric(scope_products[sequence_column], errors="coerce")
            sequence_products = scope_products[sequence_values.notna()].copy()
            sequence_products[sequence_column] = pd.to_numeric(
                sequence_products[sequence_column], errors="raise"
            ).astype(int)
            order_to_global = dict(zip(
                sequence_products[sequence_column].astype(int), sequence_products["global_order"].astype(int)
            ))
            global_to_scope = {value: key for key, value in order_to_global.items()}
            engine_products = sequence_products.copy()
            engine_products["global_order"] = engine_products[sequence_column].astype(int)
            scope_detections = extracted[extracted["global_order"].isin(global_to_scope)].copy()
            scope_detections["global_order"] = scope_detections["global_order"].map(global_to_scope)

            def scoped_alert(
                alert: dict[str, Any], scope_name: str = scope,
                station_name: str = station_id, prefix: str = group_prefix,
            ) -> None:
                item = dict(alert)
                item["analysis_scope"] = scope_name
                item["station_id"] = station_name
                item["alert_id"] = f"{prefix}-{item['alert_id']}"
                item["cluster_id"] = f"{prefix}-{item['cluster_id']}"
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
            # Image evidence without a reliable MES event is retained for review,
            # but cannot contribute to temporal patterns.
            unsequenced = extracted[
                extracted["global_order"].isin(scope_products["global_order"])
                & ~extracted["global_order"].isin(global_to_scope)
            ].copy()
            scope_assigned["global_order"] = scope_assigned["global_order"].map(order_to_global)
            if not unsequenced.empty:
                scope_assigned = pd.concat([scope_assigned, unsequenced], ignore_index=True)
            scope_assigned["analysis_scope"] = scope
            scope_assigned["station_id"] = station_id
            if "cluster_id" in scope_assigned:
                mask = scope_assigned["cluster_id"].astype(str).str.strip().ne("")
                scope_assigned.loc[mask, "cluster_id"] = group_prefix + "-" + scope_assigned.loc[mask, "cluster_id"].astype(str)
            for frame in (scope_clusters, scope_patterns, scope_alerts):
                frame["analysis_scope"] = scope
                frame["station_id"] = station_id
                if not frame.empty and "cluster_id" in frame:
                    frame["cluster_id"] = group_prefix + "-" + frame["cluster_id"].astype(str)
            if not scope_patterns.empty:
                scope_patterns["pattern_id"] = group_prefix + "-" + scope_patterns["pattern_id"].astype(str)
                scope_patterns["phase_start_production_order"] = scope_patterns["phase_start"]
                scope_patterns["observed_production_orders"] = scope_patterns["observed_orders"]
                scope_patterns["outlier_production_orders"] = scope_patterns.get(
                    "outlier_orders", pd.Series("", index=scope_patterns.index)
                )
                scope_patterns["missing_production_orders"] = scope_patterns["inferred_missing_orders"]
                for column in ("first_order", "last_order", "confirmed_at_order", "next_expected_order"):
                    if column in scope_patterns:
                        scope_patterns[column] = scope_patterns[column].map(order_to_global)
                if "phase_start" in scope_patterns:
                    scope_patterns["phase_start"] = scope_patterns["phase_start"].map(order_to_global)
                for column in ("observed_orders", "outlier_orders", "inferred_missing_orders"):
                    if column in scope_patterns:
                        scope_patterns[column] = scope_patterns[column].map(
                            lambda value: _map_order_list(value, order_to_global)
                        )
            if not scope_alerts.empty:
                scope_alerts["alert_id"] = group_prefix + "-" + scope_alerts["alert_id"].astype(str)
                for column in ("alert_at_order", "predicted_order"):
                    if column in scope_alerts:
                        scope_alerts[column] = scope_alerts[column].map(order_to_global)
                if "evidence_orders" in scope_alerts:
                    scope_alerts["evidence_orders"] = scope_alerts["evidence_orders"].map(
                        lambda value: _map_order_list(value, order_to_global)
                    )
            assigned_parts.append(scope_assigned)
            cluster_parts.append(scope_clusters)
            pattern_parts.append(scope_patterns)
            alert_parts.append(scope_alerts)
        products = products.drop(columns="_analysis_station")
        assigned = pd.concat(assigned_parts, ignore_index=True) if assigned_parts else pd.DataFrame(columns=EXTRACTED_COLUMNS)
        clusters = pd.concat(cluster_parts, ignore_index=True) if cluster_parts else pd.DataFrame()
        patterns = pd.concat(pattern_parts, ignore_index=True) if pattern_parts else pd.DataFrame()
        alerts = pd.concat(alert_parts, ignore_index=True) if alert_parts else pd.DataFrame()
        catalog = load_defect_catalog(
            project_root / "config" / "defect_code_catalog.csv", request.defect_catalog_path
        )
        station_events = (
            request.station_events_frame.copy()
            if request.station_events_frame is not None else products.copy()
        )
        all_normalized_codes = normalize_defect_codes(station_events, catalog)
        if not all_normalized_codes.empty and scopes:
            all_normalized_codes = all_normalized_codes[
                all_normalized_codes["analysis_scope"].astype(str).isin(scopes)
            ].reset_index(drop=True)
        normalized_codes = filter_selected_code_events(all_normalized_codes, request.selection)
        target_counts = {
            code_target_key(source, code): len(
                group[["analysis_scope", "dmc_raw"]].astype(str).drop_duplicates()
            )
            for (source, code), group in normalized_codes[
                normalized_codes["code_status"].isin(["defect", "state_code_conflict"])
            ].groupby(["source_type", "canonical_code"])
        } if not normalized_codes.empty else {}
        selection_payload = {
            **request.selection.to_dict(), "target_product_counts": target_counts,
        }
        manifest["analysis_selection"] = selection_payload
        write_json(work_dir / "task_manifest.json", manifest)
        write_json(work_dir / "analysis_selection.json", selection_payload)
        code_image_links = map_code_events_to_products(products, normalized_codes)
        code_patterns = (
            discover_selected_code_patterns(
                all_normalized_codes, request.selection, config, image_links=code_image_links,
            )
            if request.selection.includes("code_patterns")
            else discover_code_patterns(all_normalized_codes.iloc[0:0], config)
        )
        assigned, trajectories = discover_spatial_trajectories(products, assigned, config)
        if request.selection.includes("code_image"):
            code_space, code_conflicts = analyze_code_spatial_associations(
                products, normalized_codes, assigned
            )
        else:
            code_space, code_conflicts = analyze_code_spatial_associations(
                products, normalized_codes.iloc[0:0], assigned,
            )
        station_attribution = build_station_attribution(
            code_patterns, trajectories, code_space, products, assigned
        )
        if not request.selection.includes("image_patterns"):
            clusters = clusters.iloc[0:0].copy()
            patterns = patterns.iloc[0:0].copy()
            alerts = alerts.iloc[0:0].copy()
            trajectories = trajectories.iloc[0:0].copy()
        if token.is_cancelled:
            raise InterruptedError("用户取消")
        callbacks.on_stage("WRITING")
        callbacks.on_progress(ProgressEvent("WRITING", None, total, total, 88, len(assigned)))
        assigned.to_csv(work_dir / "extracted_defects.csv", index=False, encoding="utf-8-sig")
        clusters.to_csv(work_dir / "spatial_clusters.csv", index=False, encoding="utf-8-sig")
        patterns.to_csv(work_dir / "discovered_patterns.csv", index=False, encoding="utf-8-sig")
        alerts.to_csv(work_dir / "alerts.csv", index=False, encoding="utf-8-sig")
        normalized_codes.to_csv(work_dir / "normalized_defect_codes.csv", index=False, encoding="utf-8-sig")
        catalog.frame.to_csv(work_dir / "defect_code_catalog_snapshot.csv", index=False, encoding="utf-8-sig")
        catalog.issues.to_csv(work_dir / "defect_code_catalog_issues.csv", index=False, encoding="utf-8-sig")
        code_patterns.to_csv(work_dir / "code_patterns.csv", index=False, encoding="utf-8-sig")
        trajectories.to_csv(work_dir / "spatial_trajectories.csv", index=False, encoding="utf-8-sig")
        code_space.to_csv(work_dir / "code_spatial_associations.csv", index=False, encoding="utf-8-sig")
        code_conflicts.to_csv(work_dir / "code_label_conflicts.csv", index=False, encoding="utf-8-sig")
        station_attribution.to_csv(work_dir / "station_attribution.csv", index=False, encoding="utf-8-sig")
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
                ("normalized_defect_codes.csv", normalized_codes), ("code_patterns.csv", code_patterns),
                ("spatial_trajectories.csv", trajectories),
                ("code_spatial_associations.csv", code_space),
                ("code_label_conflicts.csv", code_conflicts),
                ("station_attribution.csv", station_attribution),
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
            "normalized_code_event_count": len(normalized_codes),
            "code_pattern_count": len(code_patterns), "spatial_trajectory_count": len(trajectories),
            "code_spatial_association_count": len(code_space),
            "code_label_conflict_count": int(
                code_conflicts.get("comparison_status", pd.Series(dtype=str)).eq("label_conflict").sum()
            ),
            "truth_labels_used": False, "application_version": APP_VERSION,
            "algorithm_version": ALGORITHM_VERSION,
            "analysis_mode": request.analysis_mode,
            "analysis_selection": selection_payload,
            "image_analysis_executed": True,
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
            "patterns": patterns, "alerts": alerts, "normalized_codes": normalized_codes,
            "code_patterns": code_patterns, "trajectories": trajectories,
            "code_space": code_space, "code_conflicts": code_conflicts,
            "station_attribution": station_attribution, "defect_catalog": catalog.frame,
            "code_image_links": code_image_links,
        })
    except InterruptedError:
        for filename in (
            "extracted_defects.csv", "spatial_clusters.csv", "discovered_patterns.csv",
            "alerts.csv", "normalized_defect_codes.csv", "defect_code_catalog_snapshot.csv",
            "defect_code_catalog_issues.csv", "code_patterns.csv", "spatial_trajectories.csv",
            "code_spatial_associations.csv", "code_label_conflicts.csv",
            "station_attribution.csv", "analysis_summary.json",
        ):
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
