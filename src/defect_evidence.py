"""本文件实现缺陷代码标准化、事件规律、空间轨迹和多证据关联分析。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
import re

import cv2
import numpy as np
import pandas as pd

from .contour_extractor import read_image
from .pattern_analyzer import fit_period, longest_consecutive_run


CATALOG_COLUMNS = ["layer", "code", "name", "region", "description", "enabled", "version"]
CODE_COLUMNS = [
    "analysis_scope", "station_id", "event_id", "dmc_raw", "production_order",
    "test_date", "batch", "source_type", "source_sheet", "raw_code",
    "canonical_code", "defect_name", "state", "code_status",
]
CODE_PATTERN_COLUMNS = [
    "pattern_id", "evidence_source", "analysis_scope", "station_id", "source_type",
    "canonical_code", "defect_name", "pattern_type", "occurrence_count",
    "first_production_order", "last_production_order", "observed_production_orders",
    "period", "phase_start_production_order", "precision", "coverage", "confidence",
    "missing_production_orders",
]
TRAJECTORY_COLUMNS = [
    "trajectory_id", "analysis_scope", "station_id", "pattern_type", "occurrence_count",
    "first_production_order", "last_production_order", "observed_production_orders",
    "task_orders", "mean_y_norm", "x_span_norm", "y_span_norm", "spearman_order_x",
    "direction", "registration_quality", "minimum_registration_score", "related_cluster_ids",
]
ASSOCIATION_COLUMNS = [
    "analysis_scope", "station_id", "source_type", "canonical_code", "defect_name",
    "spatial_type", "spatial_id", "support_products", "code_products", "spatial_products",
    "p_spatial_given_code", "p_code_given_spatial", "lift", "association_strength",
]
CONFLICT_COLUMNS = [
    "analysis_scope", "global_order", "production_order", "dmc_raw", "aoi_codes", "vi_codes",
    "comparison_status", "review_note",
]
ATTRIBUTION_COLUMNS = [
    "evidence_id", "analysis_scope", "station_id", "evidence_source", "pattern_type",
    "association_level", "equipment_conclusion", "reason",
]


def norm_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def text(value: Any) -> str:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def scope_for_station(station_id: Any) -> str:
    value = str(station_id or "").lower()
    return next((scope for token, scope in (
        ("_5s_", "5S"), ("_5x_", "5X"), ("_7s_", "7S"), ("_7x_", "7X")
    ) if token in value), "")


def parse_aoi_code(value: Any) -> str:
    """AOI uses the first four digits of its first numeric token."""
    match = re.search(r"\d+", text(value))
    return match.group(0)[:4] if match and len(match.group(0)) >= 4 else ""


def parse_vi_block_code(value: Any) -> str:
    """MS0335all BlockCode uses the last four digits after punctuation removal."""
    digits = "".join(re.findall(r"\d", text(value)))
    return digits[-4:] if len(digits) >= 4 else ""


@dataclass(frozen=True)
class DefectCatalog:
    frame: pd.DataFrame
    issues: pd.DataFrame

    def name_for(self, scope: Any, code: str) -> str:
        layer_match = re.match(r"\s*([57])", str(scope or ""))
        layer = layer_match.group(1) if layer_match else ""
        rows = self.frame[
            self.frame["layer"].astype(str).eq(layer)
            & self.frame["code"].astype(str).eq(str(code))
            & self.frame["enabled"].eq(True)
        ]
        return " / ".join(dict.fromkeys(
            value for value in rows["name"].dropna().astype(str).str.strip() if value
        ))


def _read_catalog(path: Path) -> pd.DataFrame:
    return (
        pd.read_excel(path, dtype=str)
        if path.suffix.lower() in {".xlsx", ".xlsm"}
        else pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    )


def load_defect_catalog(builtin: Path, override: Path | None = None) -> DefectCatalog:
    frames: list[tuple[str, pd.DataFrame]] = []
    if builtin.is_file():
        frames.append((str(builtin), _read_catalog(builtin)))
    if override is not None and override.resolve() != builtin.resolve():
        if not override.is_file():
            raise FileNotFoundError(f"缺陷代码字典不存在：{override}")
        frames.append((str(override), _read_catalog(override)))
    rows: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for source, frame in frames:
        columns = {norm_key(column): column for column in frame.columns}
        if any(name not in columns for name in ("layer", "code", "name")):
            issues.append({"source": source, "row": 0, "issue": "缺少layer/code/name字段"})
            continue
        for index, item in frame.iterrows():
            layer = re.search(r"[57]", text(item[columns["layer"]]))
            code = re.search(r"\d{4}", text(item[columns["code"]]))
            name = text(item[columns["name"]])
            if not layer or not code or not name:
                issues.append({"source": source, "row": int(index) + 2, "issue": "layer/code/name无效"})
                continue
            enabled_raw = text(item.get(columns.get("enabled", ""), "true")).lower()
            rows.append({
                "layer": layer.group(0), "code": code.group(0), "name": name,
                "region": text(item.get(columns.get("region", ""), "")),
                "description": text(item.get(columns.get("description", ""), "")),
                "enabled": enabled_raw not in {"0", "false", "no", "off", "否"},
                "version": text(item.get(columns.get("version", ""), "")),
                "source": source,
            })
    catalog = pd.DataFrame(rows, columns=[*CATALOG_COLUMNS, "source"])
    if not catalog.empty:
        catalog = catalog.drop_duplicates(["layer", "code", "region"], keep="last").reset_index(drop=True)
    return DefectCatalog(catalog, pd.DataFrame(issues, columns=["source", "row", "issue"]))


def assign_production_order(events: pd.DataFrame) -> pd.DataFrame:
    """Preserve every station pass and rank it by the real MES timestamp."""
    result = events.copy()
    if result.empty:
        result["production_order"] = pd.Series(dtype="Int64")
        return result
    if "station_id" not in result:
        result["production_order"] = pd.Series(pd.NA, index=result.index, dtype="Int64")
        return result
    result["_time"] = pd.to_datetime(result.get("test_date"), errors="coerce")
    sort_columns = [column for column in ("station_id", "_time", "source_sheet", "source_row", "event_id") if column in result]
    result = result.sort_values(sort_columns, na_position="last", kind="stable").reset_index(drop=True)
    result["production_order"] = result.groupby("station_id").cumcount() + 1
    result.loc[result["_time"].isna(), "production_order"] = pd.NA
    return result.drop(columns="_time")


def normalize_defect_codes(events: pd.DataFrame, catalog: DefectCatalog) -> pd.DataFrame:
    """Build independent AOI and VI evidence without treating VI as ground truth."""
    if events.empty or "station_id" not in events:
        return pd.DataFrame(columns=CODE_COLUMNS)
    ordered = events if "production_order" in events else assign_production_order(events)
    aoi_fields = [column for column in ordered if norm_key(column) in {"aoifailurecode", "resultaoifailurecode"}]
    block_fields = [column for column in ordered if norm_key(column) == "blockcode"]
    rows: list[dict[str, Any]] = []
    for event in ordered.to_dict("records"):
        station_id = text(event.get("station_id"))
        scope = scope_for_station(station_id)
        state = text(event.get("state")).upper() or "UNKNOWN"
        common = {
            "analysis_scope": scope, "station_id": station_id,
            "event_id": text(event.get("event_id")), "dmc_raw": text(event.get("dmc_raw")),
            "production_order": event.get("production_order"), "test_date": event.get("test_date"),
            "batch": event.get("batch"), "source_sheet": text(event.get("source_sheet")), "state": state,
        }
        if station_id.endswith("_aoi"):
            raw = next((text(event.get(field)) for field in aoi_fields if text(event.get(field))), "")
            code = parse_aoi_code(raw)
            status = (
                "normal" if code == "1000" and state == "OK"
                else "scrapped" if not code and state == "SCRAPPED"
                else "defect" if code and code != "1000" and state != "OK"
                else "state_code_conflict" if code
                else "unknown"
            )
            rows.append({**common, "source_type": "AOI_FAILURE", "raw_code": raw,
                         "canonical_code": code, "defect_name": catalog.name_for(scope, code),
                         "code_status": status})
        if station_id.endswith("_vi") and norm_key(common["source_sheet"]) == "ms0335all":
            raw_values = [text(event.get(field)) for field in block_fields if text(event.get(field))]
            if not raw_values:
                raw_values = [""]
            for raw in raw_values:
                code = parse_vi_block_code(raw)
                rows.append({**common, "source_type": "VI_BLOCK", "raw_code": raw,
                             "canonical_code": code, "defect_name": catalog.name_for(scope, code),
                             "code_status": "defect" if code else ("scrapped" if state == "SCRAPPED" else "unknown")})
    return pd.DataFrame(rows, columns=CODE_COLUMNS)


def discover_code_patterns(events: pd.DataFrame, config: dict[str, Any],
                           selected_codes: Iterable[str] | None = None,
                           merge_selected: bool = False) -> pd.DataFrame:
    defects = events[events["code_status"].isin(["defect", "state_code_conflict"])].copy()
    selected = {str(value) for value in (selected_codes or []) if str(value)}
    if selected:
        defects = defects[defects["canonical_code"].astype(str).isin(selected)]
    if defects.empty:
        return pd.DataFrame(columns=CODE_PATTERN_COLUMNS)
    defects["pattern_code"] = (
        "+".join(sorted(selected)) if merge_selected and selected
        else defects["canonical_code"].astype(str)
    )
    rows: list[dict[str, Any]] = []
    groups = ["analysis_scope", "station_id", "source_type", "pattern_code"]
    for keys, group in defects.groupby(groups, dropna=False):
        orders = sorted(set(pd.to_numeric(group["production_order"], errors="coerce").dropna().astype(int)))
        if len(orders) < int(config["minimum_repeat_occurrences"]):
            continue
        eligible_orders = sorted(set(pd.to_numeric(
            events[
                events["analysis_scope"].astype(str).eq(str(keys[0]))
                & events["station_id"].astype(str).eq(str(keys[1]))
                & events["source_type"].astype(str).eq(str(keys[2]))
            ]["production_order"], errors="coerce"
        ).dropna().astype(int)))
        fit, run = fit_period(orders, config, eligible_orders), longest_consecutive_run(orders)
        if fit:
            kind = "periodic"
            eligible = set(eligible_orders)
            expected = [order for order in range(
                int(fit["phase_start"]), max(orders) + 1, int(fit["period"])
            ) if order in eligible]
            metrics = {
                "period": int(fit["period"]), "phase_start_production_order": int(fit["phase_start"]),
                "precision": round(float(fit["precision"]), 4), "coverage": round(float(fit["coverage"]), 4),
                "confidence": round(float(fit["confidence"]), 4),
                "missing_production_orders": ";".join(map(str, sorted(set(expected) - set(fit["matched_orders"])))),
            }
        elif len(run) >= int(config["burst_minimum_length"]):
            kind, metrics = "burst", {"period": None, "phase_start_production_order": run[0],
                                       "precision": 1.0, "coverage": 1.0, "confidence": 1.0,
                                       "missing_production_orders": ""}
        else:
            kind, metrics = "recurrent", {"period": None, "phase_start_production_order": None,
                                           "precision": None, "coverage": None, "confidence": None,
                                           "missing_production_orders": ""}
        names = [value for value in group["defect_name"].dropna().astype(str) if value]
        rows.append({
            "pattern_id": f"CP{len(rows) + 1:04d}", "evidence_source": "code",
            "analysis_scope": keys[0], "station_id": keys[1], "source_type": keys[2],
            "canonical_code": keys[3], "defect_name": " / ".join(dict.fromkeys(names)),
            "pattern_type": kind, "occurrence_count": len(orders),
            "first_production_order": min(orders), "last_production_order": max(orders),
            "observed_production_orders": ";".join(map(str, orders)), **metrics,
        })
    return pd.DataFrame(rows, columns=CODE_PATTERN_COLUMNS)


def _thumbnail(path: str, width: int = 512) -> np.ndarray:
    image = read_image(Path(path), cv2.IMREAD_GRAYSCALE)
    height = max(32, round(image.shape[0] * width / image.shape[1]))
    return cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)


def align_detection_coordinates(products: pd.DataFrame, detections: pd.DataFrame,
                                minimum_score: float = 0.80) -> pd.DataFrame:
    """Use ECC to remove product translation/rotation before trajectory analysis."""
    result = detections.copy()
    result["aligned_x_norm"] = pd.to_numeric(result.get("center_x_norm"), errors="coerce")
    result["aligned_y_norm"] = pd.to_numeric(result.get("center_y_norm"), errors="coerce")
    result["registration_score"] = 0.0
    result["registration_quality"] = "unavailable"
    if result.empty or "a_image_path" not in products:
        return result
    group_columns = ["analysis_scope"] + (["station_id"] if "station_id" in products else [])
    for _, scope_products in products.groupby(group_columns, dropna=False):
        order_column = "production_order" if "production_order" in scope_products else "global_order"
        usable = scope_products.sort_values(order_column)
        usable = usable[usable["a_image_path"].astype(str).map(lambda value: Path(value).is_file())]
        if usable.empty:
            continue
        reference_path = str(usable.iloc[0]["a_image_path"])
        reference = _thumbnail(reference_path)
        template = reference.astype(np.float32) / 255.0
        for product in usable.to_dict("records"):
            selected = result["global_order"].astype(int).eq(int(product["global_order"]))
            if not selected.any():
                continue
            try:
                current = _thumbnail(str(product["a_image_path"]), reference.shape[1])
                if current.shape != reference.shape:
                    current = cv2.resize(current, (reference.shape[1], reference.shape[0]))
                warp = np.eye(2, 3, dtype=np.float32)
                if str(product["a_image_path"]) == reference_path:
                    score = 1.0
                else:
                    shift, _ = cv2.phaseCorrelate(template, current.astype(np.float32) / 255.0)
                    warp[0, 2], warp[1, 2] = shift
                    score, warp = cv2.findTransformECC(
                        template, current.astype(np.float32) / 255.0, warp, cv2.MOTION_EUCLIDEAN,
                        (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 80, 1e-5),
                    )
                inverse = cv2.invertAffineTransform(warp)
                points = result.loc[selected, ["center_x_norm", "center_y_norm"]].to_numpy(float)
                points *= np.array([reference.shape[1], reference.shape[0]])
                aligned = cv2.transform(points.reshape(-1, 1, 2).astype(np.float32), inverse).reshape(-1, 2)
                result.loc[selected, "aligned_x_norm"] = aligned[:, 0] / reference.shape[1]
                result.loc[selected, "aligned_y_norm"] = aligned[:, 1] / reference.shape[0]
                result.loc[selected, "registration_score"] = float(score)
                result.loc[selected, "registration_quality"] = "high" if score >= minimum_score else "low"
            except (cv2.error, OSError, ValueError):
                result.loc[selected, "registration_quality"] = "low"
    return result


def _spearman(left: pd.Series, right: pd.Series) -> float:
    if len(left) < 2 or left.nunique() < 2 or right.nunique() < 2:
        return 0.0
    value = left.rank(method="average").corr(right.rank(method="average"))
    return 0.0 if pd.isna(value) else float(value)


def discover_spatial_trajectories(products: pd.DataFrame, detections: pd.DataFrame,
                                  config: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    score_limit = float(config.get("registration_min_score", 0.80))
    aligned = align_detection_coordinates(products, detections, score_limit)
    if aligned.empty:
        return aligned, pd.DataFrame(columns=TRAJECTORY_COLUMNS)
    product_index = products.set_index("global_order")
    order_series = (
        product_index["production_order"] if "production_order" in product_index
        else product_index.index.to_series()
    )
    aligned["production_order"] = aligned["global_order"].map(order_series)
    aligned["trajectory_id"] = ""
    y_tolerance = float(config.get("horizontal_y_tolerance_norm", 0.02))
    minimum_span = float(config.get("horizontal_min_x_span_norm", 0.05))
    minimum_count = int(config.get("trajectory_min_occurrences", config["minimum_repeat_occurrences"]))
    rho_limit = float(config.get("linear_drift_min_abs_spearman", 0.80))
    rows: list[dict[str, Any]] = []
    analyzable = aligned[
        aligned.get("detection_type", pd.Series("local", index=aligned.index)).astype(str).ne("region_anomaly")
    ]
    group_columns = ["analysis_scope"] + (["station_id"] if "station_id" in analyzable else [])
    for group_key, group in analyzable.groupby(group_columns, dropna=False):
        if isinstance(group_key, tuple):
            scope = group_key[0]
            station_id = str(group_key[1]) if len(group_key) > 1 else ""
        else:
            scope, station_id = group_key, ""
        bands: list[list[int]] = []
        means: list[float] = []
        for index, item in group.sort_values(["aligned_y_norm", "aligned_x_norm"]).iterrows():
            y = float(item["aligned_y_norm"])
            candidate = min(range(len(means)), key=lambda position: abs(means[position] - y), default=-1)
            if candidate < 0 or abs(means[candidate] - y) > y_tolerance:
                bands.append([index]); means.append(y)
            else:
                bands[candidate].append(index)
                means[candidate] = float(aligned.loc[bands[candidate], "aligned_y_norm"].mean())
        for indices in bands:
            samples = aligned.loc[indices].dropna(subset=["production_order"])
            per_order = samples.groupby("production_order", as_index=False).agg(
                aligned_x_norm=("aligned_x_norm", "mean"), aligned_y_norm=("aligned_y_norm", "mean"),
                registration_score=("registration_score", "min"), global_order=("global_order", "first"),
            ).sort_values("production_order")
            if len(per_order) < minimum_count:
                continue
            x_span = float(per_order["aligned_x_norm"].max() - per_order["aligned_x_norm"].min())
            y_span = float(per_order["aligned_y_norm"].max() - per_order["aligned_y_norm"].min())
            if x_span < minimum_span or y_span > y_tolerance * 2:
                continue
            rho = _spearman(per_order["production_order"], per_order["aligned_x_norm"])
            deltas = np.diff(per_order["aligned_x_norm"].to_numpy())
            changes = int(np.sum(np.sign(deltas[1:]) != np.sign(deltas[:-1]))) if len(deltas) > 1 else 0
            kind = (
                "linear_drift" if abs(rho) >= rho_limit
                else "oscillating_translation" if changes >= 2
                else "horizontal_translation"
            )
            prefix = re.sub(r"[^A-Za-z0-9_-]+", "-", station_id or str(scope))
            trajectory_id = f"{prefix}-T{len(rows) + 1:03d}"
            aligned.loc[indices, "trajectory_id"] = trajectory_id
            minimum_score = float(per_order["registration_score"].min())
            clusters = sorted(set(samples.get("cluster_id", pd.Series(dtype=str)).dropna().astype(str)))
            rows.append({
                "trajectory_id": trajectory_id, "analysis_scope": scope,
                "station_id": station_id, "pattern_type": kind,
                "occurrence_count": len(per_order),
                "first_production_order": int(per_order["production_order"].min()),
                "last_production_order": int(per_order["production_order"].max()),
                "observed_production_orders": ";".join(map(str, per_order["production_order"].astype(int))),
                "task_orders": ";".join(map(str, per_order["global_order"].astype(int))),
                "mean_y_norm": round(float(per_order["aligned_y_norm"].mean()), 7),
                "x_span_norm": round(x_span, 7), "y_span_norm": round(y_span, 7),
                "spearman_order_x": round(rho, 5),
                "direction": "right" if rho > 0 else "left" if rho < 0 else "mixed",
                "registration_quality": "high" if minimum_score >= score_limit else "low",
                "minimum_registration_score": round(minimum_score, 4),
                "related_cluster_ids": ";".join(clusters),
            })
    return aligned, pd.DataFrame(rows, columns=TRAJECTORY_COLUMNS)


def analyze_code_spatial_associations(products: pd.DataFrame, code_events: pd.DataFrame,
                                      detections: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if products.empty or code_events.empty:
        return pd.DataFrame(columns=ASSOCIATION_COLUMNS), pd.DataFrame(columns=CONFLICT_COLUMNS)
    defect_codes = code_events[
        code_events["code_status"].isin(["defect", "state_code_conflict"])
    ].copy()
    codes = _map_code_events_to_products(products, defect_codes)
    if codes.empty:
        return pd.DataFrame(columns=ASSOCIATION_COLUMNS), pd.DataFrame(columns=CONFLICT_COLUMNS)
    production_lookup = products.set_index("global_order").get(
        "production_order", pd.Series(dtype="Int64")
    )
    total_by_scope = products.groupby("analysis_scope")["global_order"].nunique().to_dict()
    categories = [("cluster", "cluster_id")]
    if "trajectory_id" in detections:
        categories.append(("trajectory", "trajectory_id"))
    rows: list[dict[str, Any]] = []
    for category_type, column in categories:
        spatial = detections[detections[column].fillna("").astype(str).str.strip().ne("")]
        for (scope, source, code), code_group in codes.groupby(["analysis_scope", "source_type", "canonical_code"]):
            code_orders = set(code_group["global_order"].astype(int))
            scoped = spatial[spatial["analysis_scope"].astype(str).eq(str(scope))]
            for category, spatial_group in scoped.groupby(column):
                spatial_orders = set(spatial_group["global_order"].astype(int))
                support = len(code_orders & spatial_orders)
                if not support:
                    continue
                p_space = support / len(code_orders)
                p_code = support / len(spatial_orders)
                total = max(1, int(total_by_scope.get(scope, 1)))
                lift = (support / total) / ((len(code_orders) / total) * (len(spatial_orders) / total))
                strength = (
                    "strong" if support >= 5 and min(p_space, p_code) >= 0.70 and lift >= 1.50
                    else "medium" if support >= 4 and lift >= 1.20 else "weak"
                )
                rows.append({
                    "analysis_scope": scope, "source_type": source, "canonical_code": code,
                    "station_id": ";".join(sorted(set(spatial_group.get(
                        "station_id", pd.Series(dtype=str)
                    ).dropna().astype(str)))),
                    "defect_name": " / ".join(dict.fromkeys(code_group["defect_name"].dropna().astype(str))),
                    "spatial_type": category_type, "spatial_id": category,
                    "support_products": support, "code_products": len(code_orders),
                    "spatial_products": len(spatial_orders), "p_spatial_given_code": round(p_space, 5),
                    "p_code_given_spatial": round(p_code, 5), "lift": round(lift, 5),
                    "association_strength": strength,
                })
    conflicts: list[dict[str, Any]] = []
    for (scope, global_order, dmc), group in codes.groupby(
        ["analysis_scope", "global_order", "dmc_raw"]
    ):
        aoi = sorted(set(group.loc[group["source_type"].eq("AOI_FAILURE"), "canonical_code"].astype(str)))
        vi = sorted(set(group.loc[group["source_type"].eq("VI_BLOCK"), "canonical_code"].astype(str)))
        status = (
            "missing_vi" if aoi and not vi else "missing_aoi" if vi and not aoi
            else "consistent" if set(aoi) & set(vi) else "label_conflict"
        )
        conflicts.append({"analysis_scope": scope, "global_order": int(global_order),
                          "production_order": production_lookup.get(global_order, pd.NA),
                          "dmc_raw": dmc, "aoi_codes": ";".join(aoi),
                          "vi_codes": ";".join(vi), "comparison_status": status,
                          "review_note": "并列证据，未自动修改原始代码" if status == "label_conflict" else ""})
    return (
        pd.DataFrame(rows, columns=ASSOCIATION_COLUMNS),
        pd.DataFrame(conflicts, columns=CONFLICT_COLUMNS),
    )


def _map_code_events_to_products(products: pd.DataFrame, codes: pd.DataFrame) -> pd.DataFrame:
    """Prefer exact AOI/VI event IDs; DMC fallback is allowed only when both sides are unique."""
    if codes.empty:
        return codes.assign(global_order=pd.Series(dtype="Int64"))
    product_columns = [
        column for column in (
            "analysis_scope", "dmc_raw", "global_order", "aoi_event_id", "vi_event_id",
            "evidence_status",
        ) if column in products
    ]
    product_keys = products[product_columns].copy()
    mapped_parts: list[pd.DataFrame] = []
    for source_type, event_column in (
        ("AOI_FAILURE", "aoi_event_id"), ("VI_BLOCK", "vi_event_id"),
    ):
        source_codes = codes[codes["source_type"].astype(str).eq(source_type)].copy()
        if source_codes.empty:
            continue
        if "event_id" not in source_codes:
            source_codes["event_id"] = ""
        matched_indices: set[int] = set()
        if event_column in product_keys and "event_id" in source_codes:
            exact_codes = source_codes[source_codes["event_id"].fillna("").astype(str).str.strip().ne("")].copy()
            exact_products = product_keys[
                product_keys[event_column].fillna("").astype(str).str.strip().ne("")
            ].copy()
            if not exact_codes.empty and not exact_products.empty:
                exact = exact_codes.reset_index(names="_code_index").merge(
                    exact_products, left_on=["analysis_scope", "event_id"],
                    right_on=["analysis_scope", event_column], how="inner", suffixes=("", "_product"),
                )
                if not exact.empty:
                    matched_indices.update(exact["_code_index"].astype(int))
                    mapped_parts.append(exact.drop(columns=["_code_index"], errors="ignore"))
        remaining = source_codes[~source_codes.index.isin(matched_indices)].copy()
        if remaining.empty:
            continue
        fallback_products = product_keys.copy()
        if "evidence_status" in fallback_products:
            fallback_products = fallback_products[
                fallback_products["evidence_status"].astype(str).ne("ambiguous_event")
            ]
        product_counts = fallback_products.groupby(
            ["analysis_scope", "dmc_raw"]
        )["global_order"].transform("nunique")
        fallback_products = fallback_products[product_counts.eq(1)].drop_duplicates(
            ["analysis_scope", "dmc_raw"]
        )
        code_counts = remaining.groupby(["analysis_scope", "dmc_raw"])["source_type"].transform("size")
        remaining = remaining[code_counts.eq(1)]
        fallback = remaining.merge(
            fallback_products, on=["analysis_scope", "dmc_raw"], how="inner", suffixes=("", "_product")
        )
        if not fallback.empty:
            mapped_parts.append(fallback)
    if not mapped_parts:
        return pd.DataFrame(columns=[*codes.columns, "global_order"])
    result = pd.concat(mapped_parts, ignore_index=True)
    return result.drop_duplicates(["source_type", "event_id", "canonical_code", "global_order"])


def build_station_attribution(
    code_patterns: pd.DataFrame, trajectories: pd.DataFrame, associations: pd.DataFrame,
    products: pd.DataFrame | None = None, detections: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Never claim equipment causality without upstream/downstream controlled evidence."""
    rows: list[dict[str, Any]] = []
    for frame, key, source in ((code_patterns, "pattern_id", "code"),
                               (trajectories, "trajectory_id", "spatial_trajectory")):
        for item in frame.to_dict("records") if not frame.empty else []:
            high = source == "spatial_trajectory" and item.get("registration_quality") == "high"
            station_id = text(item.get("station_id"))
            level = "疑似工站相关" if high or station_id else "观察到规律"
            reason = "规律集中在同一工站；尚缺同一DMC的上游无缺陷对照"
            if source == "spatial_trajectory" and high:
                level, reason = _trajectory_station_level(
                    text(item.get(key)), station_id, products, detections,
                )
            rows.append({"evidence_id": item.get(key), "analysis_scope": item.get("analysis_scope"),
                         "station_id": station_id, "evidence_source": source,
                         "pattern_type": item.get("pattern_type"),
                         "association_level": level,
                         "equipment_conclusion": "设备原因待确认",
                         "reason": reason})
    strong_associations = (
        associations[associations["association_strength"].isin(["strong", "medium"])]
        if not associations.empty and "association_strength" in associations else associations.iloc[0:0]
    )
    for item in strong_associations.to_dict("records"):
        rows.append({"evidence_id": f"{item['source_type']}:{item['canonical_code']}:{item['spatial_id']}",
                     "analysis_scope": item["analysis_scope"], "station_id": item.get("station_id", ""),
                     "evidence_source": "code_spatial", "pattern_type": item["spatial_type"],
                     "association_level": "疑似工站相关", "equipment_conclusion": "设备原因待确认",
                     "reason": "代码与空间证据统计关联；关联不等于因果关系"})
    return pd.DataFrame(rows, columns=ATTRIBUTION_COLUMNS)


def _trajectory_station_level(
    trajectory_id: str, station_id: str, products: pd.DataFrame | None,
    detections: pd.DataFrame | None,
) -> tuple[str, str]:
    """Elevate only when the same DMC has an evaluable earlier-station image without the feature."""
    fallback = ("疑似工站相关", "高可信空间轨迹集中在同一工站；缺少充分的上游对照")
    if not trajectory_id or not station_id or products is None or detections is None:
        return fallback
    required_products = {"global_order", "dmc_raw", "station_id"}
    if products.empty or detections.empty or not required_products.issubset(products.columns):
        return fallback
    target = detections[detections.get(
        "trajectory_id", pd.Series("", index=detections.index)
    ).astype(str).eq(trajectory_id)]
    if target.empty:
        return fallback
    time_column = next(
        (column for column in ("aoi_test_date", "test_date", "capture_date") if column in products), None
    )
    if time_column is None:
        return fallback
    product_rows = products.copy()
    product_rows["_event_time"] = pd.to_datetime(product_rows[time_column], errors="coerce")
    target_products = target[["global_order", "dmc_raw"]].drop_duplicates().merge(
        product_rows[["global_order", "_event_time"]], on="global_order", how="left"
    )
    comparable = 0
    upstream_without_feature = 0
    mean_y = float(pd.to_numeric(target.get("aligned_y_norm"), errors="coerce").mean())
    for sample in target_products.to_dict("records"):
        if pd.isna(sample["_event_time"]):
            continue
        upstream = product_rows[
            product_rows["dmc_raw"].astype(str).eq(str(sample["dmc_raw"]))
            & product_rows["station_id"].astype(str).ne(station_id)
            & product_rows["_event_time"].lt(sample["_event_time"])
        ]
        if "evidence_status" in upstream:
            upstream = upstream[upstream["evidence_status"].astype(str).eq("evaluable")]
        if upstream.empty:
            continue
        comparable += 1
        upstream_orders = set(upstream["global_order"].astype(int))
        upstream_detections = detections[detections["global_order"].astype(int).isin(upstream_orders)]
        y_values = pd.to_numeric(
            upstream_detections.get("aligned_y_norm", upstream_detections.get("center_y_norm")),
            errors="coerce",
        )
        if upstream_detections.empty or not y_values.sub(mean_y).abs().le(0.02).any():
            upstream_without_feature += 1
    ratio = upstream_without_feature / comparable if comparable else 0.0
    if comparable >= 5 and ratio >= 0.80:
        return (
            "较强工站关联",
            f"同一DMC上游可评估样本{comparable}片，其中{upstream_without_feature}片无该空间表现（{ratio:.0%}）",
        )
    return fallback
