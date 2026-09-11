"""本文件执行以缺陷为中心的离线停机识别、事件暴露和初步原因分析。

The module deliberately separates observations, statistical associations and
mechanism hypotheses.  It never promotes an Excel-inferred production gap to a
confirmed equipment stop and it never invents unmeasured process variables.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import exp, log, sqrt
from pathlib import Path
from typing import Any
import re

import numpy as np
import pandas as pd
import yaml

from .defect_evidence import scope_for_station


DOWNTIME_COLUMNS = [
    "downtime_id", "analysis_scope", "start_time", "restart_time",
    "duration_seconds", "anchor_station_id", "station_ids", "station_count",
    "nominal_cycle_seconds", "confidence", "confirmation_status", "event_source",
    "minimum_speed_near_event", "speed_baseline", "speed_change_percent",
]
EXPOSURE_COLUMNS = [
    "analysis_scope", "source_type", "canonical_code", "dmc_raw", "is_target_defect",
    "source_event_time", "target_time", "route_first_time", "route_last_time",
    "route_match_status", "route_coverage_status", "route_match_reason",
    "linkage_method", "route_data_start", "route_data_end",
    "observed_station_count", "left_censored", "right_censored",
    "experienced_downtime", "downtime_count", "longest_downtime_seconds",
    "downtime_ids", "downtime_segments", "post_restart_90_seconds",
    "post_restart_30_products", "nearest_restart_seconds", "nearest_restart_rank",
    "restart_bucket", "material_or_batch_change", "speed_anomaly",
    "tension_anomaly", "static_anomaly",
]
EVIDENCE_COLUMNS = [
    "evidence_id", "analysis_scope", "source_type", "canonical_code",
    "evidence_type", "subject", "statement", "exposed_count", "unexposed_count",
    "exposed_defects", "unexposed_defects", "exposed_rate", "unexposed_rate",
    "risk_ratio", "risk_difference", "ci95_low", "ci95_high", "p_value",
    "evidence_level", "evidence_score", "data_quality", "warning",
    "analysis_question", "discovery_method", "interpretation", "limitations",
    "recommended_action", "sample_filter_field", "sample_filter_operator",
    "sample_filter_value", "sample_filter_min", "sample_filter_max", "is_effective",
]
HYPOTHESIS_COLUMNS = [
    "hypothesis_id", "analysis_scope", "source_type", "canonical_code",
    "defect_name", "candidate_cause", "process_zone", "conclusion",
    "evidence_level", "evidence_score", "supporting_evidence", "counter_evidence",
    "supporting_evidence_ids", "missing_evidence", "recommended_action", "warning",
]
FINDING_COLUMNS = [
    "finding_id", "finding_type", "analysis_scope", "target", "source_type",
    "canonical_code", "subject", "related_subject", "statement", "evidence_score",
    "evidence_level", "effect_strength", "sample_size", "positive_count",
    "negative_count", "risk_ratio", "lift", "validation_auc", "stability",
    "data_quality", "warning", "detail_type", "detail_key",
    "analysis_question", "discovery_method", "interpretation", "limitations",
    "recommended_action", "sample_filter_field", "sample_filter_operator",
    "sample_filter_value", "sample_filter_min", "sample_filter_max", "is_effective",
]


@dataclass
class DefectCauseAnalysisResult:
    downtime_events: pd.DataFrame
    product_exposure: pd.DataFrame
    hypotheses: pd.DataFrame
    evidence: pd.DataFrame
    findings: pd.DataFrame
    summary: dict[str, Any]


def load_cause_rules(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return dict(payload.get("defects") or {})


def _route_rank(station_id: Any) -> int:
    station = str(station_id or "")
    match = re.search(r"_wp(\d+)$", station)
    if match:
        return int(match.group(1))
    if station.endswith("_aoi"):
        return 6
    if station.endswith("_vi"):
        return 7
    return 999


def _scope_frame(frame: pd.DataFrame, scope: str) -> pd.DataFrame:
    if frame.empty or "station_id" not in frame:
        return frame.iloc[0:0].copy()
    def station_scope(value: Any) -> str:
        station = str(value or "").lower()
        explicit = scope_for_station(station)
        if explicit:
            return explicit
        if station.startswith("35_wp"):
            return "5S"
        if station.startswith("57_wp"):
            return "5X"
        return ""
    mask = frame["station_id"].map(station_scope).astype(str).eq(str(scope))
    return frame.loc[mask].copy()


def _dmc_key(value: Any) -> str:
    """Normalize formatting only; never infer that two different DMCs are one product."""
    if value is None or pd.isna(value):
        return ""
    return str(value).strip().upper()


def _normal_cycle_seconds(times: pd.Series) -> float:
    ordered = pd.to_datetime(times, errors="coerce").dropna().drop_duplicates().sort_values()
    gaps = ordered.diff().dt.total_seconds()
    gaps = gaps[gaps.gt(0)]
    if gaps.empty:
        return np.nan
    # The median is intentionally robust to long no-output periods.
    return float(gaps.median())


def _speed_context(parameters: pd.DataFrame, scope: str, start: pd.Timestamp,
                   restart: pd.Timestamp, stations: set[str]) -> tuple[float, float, float]:
    if parameters.empty:
        return np.nan, np.nan, np.nan
    scoped = _scope_frame(parameters, scope)
    if scoped.empty or not {"parameter_name", "numeric_value", "test_date"}.issubset(scoped.columns):
        return np.nan, np.nan, np.nan
    speed = scoped[
        scoped["station_id"].astype(str).isin(stations)
        & scoped["parameter_name"].astype(str).str.contains("RollingSpeed", case=False, na=False)
    ].copy()
    speed["_time"] = pd.to_datetime(speed["test_date"], errors="coerce")
    speed["_value"] = pd.to_numeric(speed["numeric_value"], errors="coerce")
    speed = speed.dropna(subset=["_time", "_value"])
    if speed.empty:
        return np.nan, np.nan, np.nan
    baseline = float(speed["_value"].median())
    near = speed[
        speed["_time"].between(start - pd.Timedelta(seconds=30),
                               restart + pd.Timedelta(seconds=30))
    ]["_value"]
    minimum = float(near.min()) if not near.empty else np.nan
    change = (minimum / baseline - 1) * 100 if baseline and pd.notna(minimum) else np.nan
    return minimum, baseline, change


def detect_downtime_events(
    station_events: pd.DataFrame,
    station_parameters: pd.DataFrame | None = None,
    *,
    scope: str,
    minimum_stop_seconds: float = 30.0,
    cycle_multiplier: float = 10.0,
    merge_tolerance_seconds: float = 15.0,
) -> pd.DataFrame:
    """Infer no-output intervals and merge synchronous station gaps."""
    events = _scope_frame(station_events, scope)
    if events.empty:
        return pd.DataFrame(columns=DOWNTIME_COLUMNS)
    events = events[
        events["station_id"].astype(str).str.contains(r"_wp\d+$|_aoi$", regex=True)
    ].copy()
    events["_time"] = pd.to_datetime(events.get("test_date"), errors="coerce")
    candidates: list[dict[str, Any]] = []
    cycles: dict[str, float] = {}
    for station_id, group in events.dropna(subset=["_time"]).groupby("station_id"):
        ordered = group.sort_values("_time", kind="stable").drop_duplicates("_time")
        nominal = _normal_cycle_seconds(ordered["_time"])
        cycles[str(station_id)] = nominal
        if pd.isna(nominal):
            continue
        ordered = ordered.assign(_previous=ordered["_time"].shift())
        ordered["_gap"] = (ordered["_time"] - ordered["_previous"]).dt.total_seconds()
        threshold = max(float(minimum_stop_seconds), float(cycle_multiplier) * nominal)
        for row in ordered.loc[ordered["_gap"].gt(threshold)].to_dict("records"):
            candidates.append({
                "station_id": str(station_id), "start": row["_previous"],
                "restart": row["_time"], "duration": float(row["_gap"]),
                "nominal": nominal,
            })
    if not candidates:
        return pd.DataFrame(columns=DOWNTIME_COLUMNS)

    groups: list[list[dict[str, Any]]] = []
    tolerance = pd.Timedelta(seconds=merge_tolerance_seconds)
    for candidate in sorted(candidates, key=lambda item: (item["start"], item["restart"])):
        matching: list[dict[str, Any]] | None = None
        for group in groups:
            reference = group[0]
            # Corresponding gaps from different stations have almost identical
            # start and restart endpoints. Requiring both prevents two genuine
            # stops separated by only a few produced parts from being merged.
            if (abs(candidate["start"] - reference["start"]) <= tolerance
                    and abs(candidate["restart"] - reference["restart"]) <= tolerance):
                matching = group
                break
        if matching is None:
            groups.append([candidate])
        else:
            matching.append(candidate)

    rows: list[dict[str, Any]] = []
    parameters = station_parameters if station_parameters is not None else pd.DataFrame()
    for index, group in enumerate(groups, 1):
        # Prefer the AOI gap as the process-output anchor; otherwise use the
        # furthest downstream available station. This keeps defect timing clear.
        anchor = sorted(
            group, key=lambda item: (item["station_id"].endswith("_aoi"),
                                     _route_rank(item["station_id"])), reverse=True,
        )[0]
        stations = {item["station_id"] for item in group}
        minimum, baseline, change = _speed_context(
            parameters, scope, anchor["start"], anchor["restart"], stations,
        )
        station_count = len(stations)
        confidence = "高" if station_count >= 3 else "中" if station_count == 2 else "低"
        rows.append({
            "downtime_id": f"STOP-{scope}-{index:04d}", "analysis_scope": scope,
            "start_time": anchor["start"], "restart_time": anchor["restart"],
            "duration_seconds": anchor["duration"],
            "anchor_station_id": anchor["station_id"],
            "station_ids": ";".join(sorted(stations, key=_route_rank)),
            "station_count": station_count,
            "nominal_cycle_seconds": round(float(anchor["nominal"]), 3),
            "confidence": confidence,
            "confirmation_status": "疑似停机" if station_count >= 2 else "数据空档/低可信疑似停机",
            "event_source": "excel_inferred", "minimum_speed_near_event": minimum,
            "speed_baseline": baseline, "speed_change_percent": change,
        })
    return pd.DataFrame(rows, columns=DOWNTIME_COLUMNS).sort_values("restart_time").reset_index(drop=True)


def _population(code_events: pd.DataFrame, source_type: str, code: str,
                scope: str) -> pd.DataFrame:
    scoped = code_events[
        code_events["source_type"].astype(str).eq(source_type)
        & code_events["analysis_scope"].astype(str).eq(scope)
    ].sort_values("production_order", na_position="last", kind="stable")
    scoped = scoped.copy()
    scoped["_dmc_key"] = scoped["dmc_raw"].map(_dmc_key)
    scoped = scoped.drop_duplicates("_dmc_key", keep="last").copy()
    target_dmcs = set(code_events.loc[
        code_events["source_type"].astype(str).eq(source_type)
        & code_events["analysis_scope"].astype(str).eq(scope)
        & code_events["canonical_code"].astype(str).eq(code)
        & code_events["code_status"].isin(["defect", "state_code_conflict"]), "dmc_raw"
    ].map(_dmc_key))
    scoped["is_target_defect"] = scoped["_dmc_key"].isin(target_dmcs)
    return scoped


def _parameter_flags(parameters: pd.DataFrame, scope: str) -> dict[str, set[str]]:
    flags = {"material": set(), "speed": set(), "tension": set(), "static": set()}
    scoped = _scope_frame(parameters, scope)
    required = {"dmc_raw", "station_id", "parameter_name", "test_date"}
    if scoped.empty or not required.issubset(scoped.columns):
        return flags
    scoped = scoped.copy()
    scoped["_time"] = pd.to_datetime(scoped["test_date"], errors="coerce")
    scoped = scoped.sort_values(["station_id", "parameter_name", "_time"], kind="stable")
    for (_, name), group in scoped.groupby(["station_id", "parameter_name"], dropna=False):
        key = re.sub(r"[^a-z0-9]", "", str(name).lower())
        numeric = pd.to_numeric(group.get("numeric_value"), errors="coerce")
        if numeric.notna().sum() >= 5:
            median = float(numeric.median())
            mad = float((numeric - median).abs().median())
            robust = (numeric - median).abs().gt(3 * 1.4826 * mad) if mad > 0 else pd.Series(False, index=group.index)
            if "rollingspeed" in key:
                relative = (numeric / median - 1).abs().gt(0.05) if median else pd.Series(False, index=group.index)
                flags["speed"].update(group.loc[robust | relative, "dmc_raw"].astype(str))
            elif "tension" in key:
                flags["tension"].update(group.loc[robust, "dmc_raw"].astype(str))
            elif "static" in key:
                sign_change = np.sign(numeric).ne(np.sign(median)) if median else pd.Series(False, index=group.index)
                flags["static"].update(group.loc[robust | sign_change, "dmc_raw"].astype(str))
        elif any(token in key for token in ("material", "lot", "dmcinformation", "rollid")):
            values = group.get("raw_value", pd.Series(index=group.index, dtype=object)).astype(str)
            changed = values.ne(values.shift()) & values.shift().notna()
            for position in np.flatnonzero(changed.to_numpy()):
                flags["material"].update(
                    group.iloc[position:position + 30]["dmc_raw"].astype(str)
                )
    return flags


def build_product_event_exposure(
    station_events: pd.DataFrame,
    station_parameters: pd.DataFrame,
    code_events: pd.DataFrame,
    downtime_events: pd.DataFrame,
    *, source_type: str, code: str, scope: str,
) -> pd.DataFrame:
    population = _population(code_events, source_type, code, scope)
    if population.empty:
        return pd.DataFrame(columns=EXPOSURE_COLUMNS)
    route = _scope_frame(station_events, scope)
    route = route[route["station_id"].astype(str).str.contains(r"_wp\d+$|_aoi$", regex=True)].copy()
    route["dmc_raw"] = route["dmc_raw"].astype(str)
    route["_dmc_key"] = route["dmc_raw"].map(_dmc_key)
    route["_time"] = pd.to_datetime(route.get("test_date"), errors="coerce")
    route = route.dropna(subset=["_time"]).sort_values("_time", kind="stable")
    route["_rank"] = route["station_id"].map(_route_rank)
    route_times = route.pivot_table(index="_dmc_key", columns="_rank", values="_time", aggfunc="last")
    route_groups = {str(dmc): group for dmc, group in route.groupby("_dmc_key")}
    route_data_start = route["_time"].min() if not route.empty else pd.NaT
    route_data_end = route["_time"].max() if not route.empty else pd.NaT

    anchor = route[route["station_id"].astype(str).str.endswith("_aoi")]
    anchor_times = anchor.groupby("_dmc_key")["_time"].last().sort_values()
    anchor_order = anchor_times.reset_index(drop=False)
    anchor_order["_output_order"] = range(1, len(anchor_order) + 1)
    anchor_order_map = dict(zip(anchor_order["_dmc_key"].astype(str), anchor_order["_output_order"]))
    # A process signal may support a cause only when it precedes the target
    # inspection for that DMC. This prevents downstream measurements from being
    # presented as explanatory variables.
    parameter_history = station_parameters.copy()
    if not parameter_history.empty and {"dmc_raw", "test_date"}.issubset(parameter_history.columns):
        fallback_times = dict(zip(
            population["_dmc_key"].astype(str),
            pd.to_datetime(population["test_date"], errors="coerce"),
        ))
        reference_times = {
            dmc: anchor_times.get(dmc, fallback_times.get(dmc, pd.NaT))
            for dmc in population["_dmc_key"].astype(str)
        }
        parameter_history["_parameter_time"] = pd.to_datetime(
            parameter_history["test_date"], errors="coerce"
        )
        parameter_history["_reference_time"] = parameter_history["dmc_raw"].map(_dmc_key).map(reference_times)
        parameter_history = parameter_history[
            parameter_history["_reference_time"].notna()
            & parameter_history["_parameter_time"].le(parameter_history["_reference_time"])
        ]
    flags = _parameter_flags(parameter_history, scope)
    if "batch" in route:
        for _, group in route.dropna(subset=["batch"]).groupby("station_id"):
            ordered = group.sort_values("_time", kind="stable")
            values = ordered["batch"].astype(str).str.strip()
            valid = values.ne("") & values.str.lower().ne("nan")
            changed = valid & values.ne(values.shift()) & values.shift().notna()
            for position in np.flatnonzero(changed.to_numpy()):
                flags["material"].update(
                    ordered.iloc[position:position + 30]["dmc_raw"].astype(str)
                )

    rows: list[dict[str, Any]] = []
    for item in population.to_dict("records"):
        dmc = str(item.get("dmc_raw", ""))
        dmc_key = str(item.get("_dmc_key", _dmc_key(dmc)))
        group = route_groups.get(dmc_key, pd.DataFrame())
        observed = int(group["station_id"].nunique()) if not group.empty else 0
        first = group["_time"].min() if observed else pd.NaT
        last = group["_time"].max() if observed else pd.NaT
        source_event_time = pd.to_datetime(item.get("test_date"), errors="coerce")
        target_time = anchor_times.get(dmc_key, source_event_time)
        ranks = route_times.loc[dmc_key].dropna().sort_index() if dmc_key in route_times.index else pd.Series(dtype="datetime64[ns]")
        complete_route = set(range(1, 7)).issubset(set(ranks.index))
        stop_ids: list[str] = []
        durations: list[float] = []
        segments: list[str] = []
        restart_seconds: list[float] = []
        restart_ranks: list[int] = []
        for stop in downtime_events.to_dict("records"):
            start = pd.to_datetime(stop["start_time"])
            restart = pd.to_datetime(stop["restart_time"])
            if pd.notna(first) and pd.notna(target_time) and first <= start and target_time >= restart:
                stop_ids.append(str(stop["downtime_id"]))
                durations.append(float(stop["duration_seconds"]))
            rank_values = list(ranks.items())
            for (left_rank, left_time), (right_rank, right_time) in zip(rank_values, rank_values[1:]):
                if left_time <= start and right_time >= restart:
                    segment = f"WP{int(left_rank)}—WP{int(right_rank)}" if right_rank <= 6 else f"{int(left_rank)}—{int(right_rank)}"
                    segments.append(segment)
            if pd.notna(target_time) and target_time >= restart:
                restart_seconds.append(float((target_time - restart).total_seconds()))
                product_order = anchor_order_map.get(dmc_key)
                if product_order is not None:
                    before = int(anchor_order.loc[anchor_order["_time"].lt(restart), "_output_order"].max()) \
                        if anchor_order["_time"].lt(restart).any() else 0
                    restart_ranks.append(int(product_order - before))
        nearest_seconds = min(restart_seconds) if restart_seconds else np.nan
        positive_ranks = [value for value in restart_ranks if value > 0]
        nearest_rank = min(positive_ranks) if positive_ranks else np.nan
        if pd.notna(nearest_rank) and nearest_rank <= 10:
            bucket = "1-10"
        elif pd.notna(nearest_rank) and nearest_rank <= 30:
            bucket = "11-30"
        elif pd.notna(nearest_rank) and nearest_rank <= 100:
            bucket = "31-100"
        else:
            bucket = "其他"
        rows.append({
            "analysis_scope": scope, "source_type": source_type, "canonical_code": code,
            "dmc_raw": dmc, "is_target_defect": bool(item["is_target_defect"]),
            "source_event_time": source_event_time, "target_time": target_time,
            "route_first_time": first, "route_last_time": last,
            "route_match_status": "matched" if observed else "unmatched",
            "route_coverage_status": (
                "complete_route" if complete_route else "partial_route" if observed else "unclassified"
            ),
            "route_match_reason": (
                "相同DMC在WP1—WP5和AOI均有记录" if complete_route
                else "相同DMC仅在当前数据窗口的部分工站有记录" if observed
                else "当前导出的上游工站时间窗口内没有该DMC记录"
            ),
            "linkage_method": "exact_dmc" if observed else "none",
            "route_data_start": route_data_start, "route_data_end": route_data_end,
            "observed_station_count": observed,
            "left_censored": bool(observed and 1 not in ranks.index),
            "right_censored": bool(observed and 6 not in ranks.index),
            "experienced_downtime": bool(stop_ids), "downtime_count": len(set(stop_ids)),
            "longest_downtime_seconds": max(durations) if durations else 0.0,
            "downtime_ids": ";".join(dict.fromkeys(stop_ids)),
            "downtime_segments": ";".join(dict.fromkeys(segments)),
            "post_restart_90_seconds": bool(pd.notna(nearest_seconds) and nearest_seconds <= 90),
            "post_restart_30_products": bool(pd.notna(nearest_rank) and nearest_rank <= 30),
            "nearest_restart_seconds": nearest_seconds, "nearest_restart_rank": nearest_rank,
            "restart_bucket": bucket,
            "material_or_batch_change": dmc in flags["material"],
            "speed_anomaly": dmc in flags["speed"], "tension_anomaly": dmc in flags["tension"],
            "static_anomaly": dmc in flags["static"],
        })
    result = pd.DataFrame(rows, columns=EXPOSURE_COLUMNS)
    if result.empty:
        return result

    # A same-clock-time export from every station is not a same-product cohort on
    # a pipeline.  Infer only the obvious left boundary from the first VI/source
    # event that has an exact upstream DMC; do not manufacture time-nearest links.
    matched = result["route_match_status"].eq("matched")
    matched_source_times = pd.to_datetime(
        result.loc[matched, "source_event_time"], errors="coerce"
    ).dropna()
    if not matched_source_times.empty:
        overlap_start = matched_source_times.min()
        source_times = pd.to_datetime(result["source_event_time"], errors="coerce")
        suspected_left = ~matched & source_times.notna() & source_times.lt(overlap_start)
        result.loc[suspected_left, "route_match_status"] = "suspected_left_window_censored"
        result.loc[suspected_left, "route_coverage_status"] = "outside_current_route_window"
        result.loc[suspected_left, "route_match_reason"] = (
            "目标检出早于本数据中首个可跨工站精确追溯产品；所需上游记录疑似位于导出开始时间之前"
        )
        result.loc[suspected_left, "left_censored"] = True
    remaining = result["route_match_status"].eq("unmatched")
    result.loc[remaining, "route_match_status"] = "not_in_current_route_window"
    result.loc[remaining, "route_coverage_status"] = "not_in_current_route_window"
    result.loc[remaining, "route_match_reason"] = (
        "当前上游数据窗口没有相同DMC记录；需检查窗口边界、返工/乱序记录或数据完整性"
    )
    return result


def select_product_exposure_details(
    exposure: pd.DataFrame, finding: pd.Series | dict[str, Any],
) -> pd.DataFrame:
    """Return the exact exposed and comparison products behind one cause finding."""
    if exposure.empty:
        return exposure.copy()
    item = dict(finding)
    result = exposure.copy()
    for finding_key, exposure_key in (
        ("analysis_scope", "analysis_scope"),
        ("source_type", "source_type"),
        ("canonical_code", "canonical_code"),
    ):
        value = str(item.get(finding_key, "") or "").strip()
        if value and exposure_key in result:
            result = result[result[exposure_key].astype(str).eq(value)]
    if "route_match_status" in result:
        result = result[result["route_match_status"].eq("matched")]
    field = str(item.get("sample_filter_field", "") or "")
    operator = str(item.get("sample_filter_operator", "") or "")
    if not field or field not in result:
        return result.iloc[0:0].copy()
    if operator == "truthy":
        condition = result[field].fillna(False).astype(bool)
    elif operator == "equals":
        condition = result[field].astype(str).eq(str(item.get("sample_filter_value", "")))
    elif operator == "contains_token":
        token = str(item.get("sample_filter_value", ""))
        condition = result[field].fillna("").astype(str).map(lambda value: token in value.split(";"))
    elif operator == "range_left_open":
        numeric = pd.to_numeric(result[field], errors="coerce")
        minimum = pd.to_numeric(pd.Series([item.get("sample_filter_min")]), errors="coerce").iloc[0]
        maximum = pd.to_numeric(pd.Series([item.get("sample_filter_max")]), errors="coerce").iloc[0]
        condition = numeric.gt(minimum) if pd.notna(minimum) else pd.Series(True, index=result.index)
        if pd.notna(maximum):
            condition &= numeric.le(maximum)
    else:
        return result.iloc[0:0].copy()
    result.insert(0, "comparison_group", np.where(condition, "暴露组", "未满足条件组"))
    result.insert(1, "matches_finding", condition.to_numpy())
    result["_group_order"] = (~condition).astype(int)
    sort_columns = ["_group_order"]
    ascending = [True]
    if "is_target_defect" in result:
        sort_columns.append("is_target_defect"); ascending.append(False)
    if "target_time" in result:
        sort_columns.append("target_time"); ascending.append(True)
    return result.sort_values(sort_columns, ascending=ascending, kind="stable").drop(
        columns="_group_order"
    ).reset_index(drop=True)


def _fisher_p(a: int, b: int, c: int, d: int) -> float:
    try:
        from scipy.stats import fisher_exact
        return float(fisher_exact([[a, b], [c, d]]).pvalue)
    except ImportError:
        return np.nan


def _evidence_explanation(evidence_type: str, label: str, code: str) -> tuple[str, str, str, str]:
    question = f"{label}是否与{code}缺陷有关？"
    if evidence_type == "停机—缺陷":
        method = (
            "先按工站Test Date计算正常节拍中位数；相邻记录空档同时超过30秒和正常节拍10倍时"
            "标记为疑似停机，三个以上工站同步时提高可信度。随后仅使用缺陷检出前的事件，"
            "在当前数据窗口内能按相同DMC追溯的产品中，比较经历疑似停机与未经历疑似停机的产品。"
        )
        limitation = (
            "Excel中的无产出空档只能推断疑似停机，仍缺少PLC停机状态、停机原因和现场记录；"
            "相同时间范围导出的上下游数据会因产线传输延迟而在边界处无法追溯。"
        )
        action = "核对高风险时间段的PLC停机日志，并按同一口径复算对应DMC。"
    elif evidence_type in {"复产—缺陷", "复产窗口—缺陷"}:
        method = (
            "以疑似停机后的首个工站输出作为复产点，按产品检出时间和复产后产品序号建立窗口，"
            "比较窗口内外的缺陷率。所有事件必须早于缺陷检出。"
        )
        limitation = "复产点来自Excel工站输出，不等同于PLC启动时刻；实际节拍变化会使时间窗与件数窗不同。"
        action = "核对复产后逐件节拍、设备速度及前30件产品，确认风险持续到第几件。"
    elif evidence_type == "工站区间—缺陷":
        method = (
            "根据同一DMC在相邻WP的过站时间，判断疑似停机开始与复产是否落在两个工站之间，"
            "再在当前数据窗口可追溯产品中比较该区间暴露组与其他产品的缺陷率。"
        )
        limitation = "区间定位表示产品停机时所处位置，不等于缺陷一定在该区间形成。"
        action = "调取该区间设备状态、张力和材料路径记录，并对相关DMC进行现场追溯。"
    elif evidence_type == "停机时长—缺陷":
        method = "按每件产品经历的最长疑似停机时长分组，再与当前数据窗口内其余可追溯产品比较缺陷率。"
        limitation = "停机时长来自无产出间隔，且可能与批次、换料和复产状态同时发生。"
        action = "按多个停机时长做受控验证，并保持材料、速度和张力条件一致。"
    elif evidence_type == "批次—缺陷":
        method = "识别材料或批次字段变化，并比较换料/换批后首段产品与其他产品的缺陷率。"
        limitation = "表格只能识别字段变化，不能确认卷料接头位置或来料性能差异。"
        action = "核对卷料接头、原材料批次和来料检验记录。"
    else:
        method = f"从缺陷检出前的工站参数中识别{label}事件，并比较有无该事件的产品缺陷率。"
        limitation = "低频Excel采样可能遗漏瞬态变化，统计异常也不等同于设备异常。"
        action = f"调取{label}的高频原始曲线并开展单因子验证。"
    return question, method, limitation, action


def _association_row(frame: pd.DataFrame, column: str, label: str, evidence_type: str,
                     index: int, scope: str, source_type: str, code: str,
                     data_quality: float, *, sample_filter_field: str | None = None,
                     sample_filter_operator: str = "truthy", sample_filter_value: Any = True,
                     sample_filter_min: float | None = None,
                     sample_filter_max: float | None = None) -> dict[str, Any]:
    usable = frame.dropna(subset=[column, "is_target_defect"])
    exposed = usable[column].astype(bool)
    target = usable["is_target_defect"].astype(bool)
    a = int((exposed & target).sum()); b = int((exposed & ~target).sum())
    c = int((~exposed & target).sum()); d = int((~exposed & ~target).sum())
    exposed_total, unexposed_total = a + b, c + d
    exposed_rate = a / exposed_total if exposed_total else np.nan
    unexposed_rate = c / unexposed_total if unexposed_total else np.nan
    if not exposed_total or not unexposed_total:
        rr = np.nan
    elif unexposed_rate > 0:
        rr = exposed_rate / unexposed_rate
    else:
        rr = np.inf if a else np.nan
    # Haldane correction makes the confidence interval finite for zero cells.
    aa, bb, cc, dd = (value + 0.5 for value in (a, b, c, d)) if 0 in (a, b, c, d) else (a, b, c, d)
    corrected_rr = (aa / (aa + bb)) / (cc / (cc + dd))
    se = sqrt(1 / aa - 1 / (aa + bb) + 1 / cc - 1 / (cc + dd))
    low, high = exp(log(corrected_rr) - 1.96 * se), exp(log(corrected_rr) + 1.96 * se)
    p_value = _fisher_p(a, b, c, d)
    if a + c >= 10 and exposed_total >= 10 and unexposed_total >= 10 and rr >= 3 and (pd.isna(p_value) or p_value <= 0.05):
        level = "较强相关"
    elif a + c >= 5 and exposed_total >= 5 and unexposed_total >= 5 and rr >= 2:
        level = "中等相关"
    else:
        level = "探索性线索"
    magnitude = min(1.0, abs(log(max(float(corrected_rr), 1e-6), 3)))
    support = min(1.0, (a + c) / 20) * min(1.0, min(exposed_total, unexposed_total) / 50)
    score = round(100 * (0.45 * magnitude + 0.30 * support + 0.25 * data_quality), 1)
    statement = (
        f"{label}产品的{code}缺陷率为{exposed_rate:.1%}（{a}/{exposed_total}），"
        f"未满足该条件组为{unexposed_rate:.1%}（{c}/{unexposed_total}），风险比{rr:.2f}。"
    ) if pd.notna(rr) and np.isfinite(rr) else (
        f"{label}产品的{code}缺陷率为{exposed_rate:.1%}（{a}/{exposed_total}），"
        f"未满足该条件组为0.0%（0/{unexposed_total}），风险比趋于无穷。"
        if np.isinf(rr) else f"{label}的对照样本不足，暂不能稳定估计风险比。"
    )
    question, method, limitation, action = _evidence_explanation(evidence_type, label, code)
    if pd.notna(rr):
        direction = "高于" if rr > 1 else "低于" if rr < 1 else "接近"
        significance = f"Fisher检验P={p_value:.4g}" if pd.notna(p_value) else "Fisher检验不可用"
        interpretation = (
            f"该条件组缺陷率{direction}未满足条件组，风险差{exposed_rate - unexposed_rate:.1%}；"
            f"95%置信区间为{low:.2f}～{high:.2f}，{significance}。"
            "这支持统计关联判断，但不能单独确认物理根因。"
        )
    else:
        interpretation = "两组样本不完整，当前只能保留为待补充数据的线索。"
    effective = bool(exposed_total and unexposed_total and (a + c) and pd.notna(rr))
    return {
        "evidence_id": f"CAUSE-{index:04d}", "analysis_scope": scope,
        "source_type": source_type, "canonical_code": code, "evidence_type": evidence_type,
        "subject": label, "statement": statement, "exposed_count": exposed_total,
        "unexposed_count": unexposed_total, "exposed_defects": a, "unexposed_defects": c,
        "exposed_rate": exposed_rate, "unexposed_rate": unexposed_rate,
        "risk_ratio": rr, "risk_difference": exposed_rate - unexposed_rate
        if pd.notna(exposed_rate) and pd.notna(unexposed_rate) else np.nan,
        "ci95_low": low, "ci95_high": high, "p_value": p_value,
        "evidence_level": level, "evidence_score": score, "data_quality": data_quality,
        "warning": "统计关联，尚未证明根本原因",
        "analysis_question": question, "discovery_method": method,
        "interpretation": interpretation, "limitations": limitation,
        "recommended_action": action,
        "sample_filter_field": sample_filter_field or column,
        "sample_filter_operator": sample_filter_operator,
        "sample_filter_value": sample_filter_value,
        "sample_filter_min": sample_filter_min, "sample_filter_max": sample_filter_max,
        "is_effective": effective,
    }


def build_cause_evidence(exposure: pd.DataFrame, *, scope: str, source_type: str,
                         code: str) -> pd.DataFrame:
    matched = exposure[exposure["route_match_status"].eq("matched")].copy()
    if matched.empty:
        return pd.DataFrame(columns=EVIDENCE_COLUMNS)
    quality = len(matched) / max(1, len(exposure))
    subjects = [
        ("experienced_downtime", "经历疑似停机", "停机—缺陷"),
        ("post_restart_90_seconds", "复产后90秒内", "复产—缺陷"),
        ("post_restart_30_products", "复产后前30件", "复产—缺陷"),
        ("material_or_batch_change", "换料或批次变化", "批次—缺陷"),
        ("speed_anomaly", "速度异常", "参数事件—缺陷"),
        ("tension_anomaly", "张力异常", "参数事件—缺陷"),
        ("static_anomaly", "静电异常", "参数事件—缺陷"),
    ]
    rows = [
        _association_row(matched, column, label, kind, index, scope, source_type, code, quality)
        for index, (column, label, kind) in enumerate(subjects, 1)
    ]
    ordered = matched.sort_values("target_time", kind="stable").reset_index(drop=True)
    target = ordered["is_target_defect"].astype(bool)
    longest_run = current_run = 0
    for value in target:
        current_run = current_run + 1 if value else 0
        longest_run = max(longest_run, current_run)
    defect_times = pd.to_datetime(
        ordered.loc[target, "target_time"], errors="coerce"
    ).dropna().sort_values().tolist()
    max_five_minutes = 0
    left = 0
    for right, moment in enumerate(defect_times):
        while moment - defect_times[left] > pd.Timedelta(minutes=5):
            left += 1
        max_five_minutes = max(max_five_minutes, right - left + 1)
    observed_count = int(target.sum())
    observation_warning = "已观测的描述性事实，不等于原因"
    rows.extend([
        {
            "evidence_id": f"EVD-{scope}-{code}-{len(rows) + 1:03d}",
            "analysis_scope": scope, "source_type": source_type, "canonical_code": code,
            "evidence_type": "已观测事实—时间聚集", "subject": "缺陷时间聚集",
            "statement": (
                f"在{len(matched)}件当前数据窗口可追溯产品中观测到{observed_count}件{code}；"
                f"任意5分钟窗口内最多{max_five_minutes}件。"
            ),
            "exposed_count": len(matched), "unexposed_count": 0,
            "exposed_defects": observed_count, "unexposed_defects": 0,
            "exposed_rate": observed_count / len(matched), "unexposed_rate": np.nan,
            "risk_ratio": np.nan, "risk_difference": np.nan, "ci95_low": np.nan,
            "ci95_high": np.nan, "p_value": np.nan, "evidence_level": "已观测事实",
            "evidence_score": 100.0, "data_quality": quality, "warning": observation_warning,
            "analysis_question": f"{code}是否在时间上集中出现？",
            "discovery_method": "按缺陷检出时间排序，滑动统计任意5分钟窗口内的目标缺陷数量。",
            "interpretation": "时间聚集用于定位需要核查的生产事件窗口，本身不能说明形成原因。",
            "limitations": "未与生产事件或工艺参数对照前，只能作为描述性事实。",
            "recommended_action": "检查缺陷密集时间段对应的停机、复产、换料和设备日志。",
            "sample_filter_field": "is_target_defect", "sample_filter_operator": "truthy",
            "sample_filter_value": True, "sample_filter_min": np.nan,
            "sample_filter_max": np.nan, "is_effective": observed_count > 0,
        },
        {
            "evidence_id": f"EVD-{scope}-{code}-{len(rows) + 2:03d}",
            "analysis_scope": scope, "source_type": source_type, "canonical_code": code,
            "evidence_type": "已观测事实—连续异常", "subject": "连续异常段",
            "statement": f"按检出时间排序，{code}最长连续异常段为{longest_run}件。",
            "exposed_count": len(matched), "unexposed_count": 0,
            "exposed_defects": observed_count, "unexposed_defects": 0,
            "exposed_rate": observed_count / len(matched), "unexposed_rate": np.nan,
            "risk_ratio": np.nan, "risk_difference": np.nan, "ci95_low": np.nan,
            "ci95_high": np.nan, "p_value": np.nan, "evidence_level": "已观测事实",
            "evidence_score": 100.0, "data_quality": quality, "warning": observation_warning,
            "analysis_question": f"{code}是否连续出现在相邻产品中？",
            "discovery_method": "按检出时间排列当前数据窗口内可追溯的产品，计算目标缺陷连续出现的最长段。",
            "interpretation": "连续异常可提示设备或材料状态持续存在，但不能直接定位工站。",
            "limitations": "时间窗截断或产品记录缺失可能切断真实连续段。",
            "recommended_action": "追溯连续异常段首尾产品及其过站、停机和参数记录。",
            "sample_filter_field": "is_target_defect", "sample_filter_operator": "truthy",
            "sample_filter_value": True, "sample_filter_min": np.nan,
            "sample_filter_max": np.nan, "is_effective": longest_run > 1,
        },
    ])
    segments = sorted({
        segment for value in matched["downtime_segments"].fillna("").astype(str)
        for segment in value.split(";") if segment
    })
    for segment in segments:
        column = f"_segment_{len(rows)}"
        matched[column] = matched["downtime_segments"].fillna("").astype(str).map(
            lambda value: segment in value.split(";")
        )
        rows.append(_association_row(
            matched, column, f"{segment}区间经历疑似停机", "工站区间—缺陷",
            len(rows) + 1, scope, source_type, code, quality,
            sample_filter_field="downtime_segments",
            sample_filter_operator="contains_token", sample_filter_value=segment,
        ))
    for bucket in ("1-10", "11-30", "31-100"):
        column = f"_bucket_{bucket}"
        matched[column] = matched["restart_bucket"].eq(bucket)
        rows.append(_association_row(
            matched, column, f"复产后第{bucket}件", "复产窗口—缺陷",
            len(rows) + 1, scope, source_type, code, quality,
            sample_filter_field="restart_bucket", sample_filter_operator="equals",
            sample_filter_value=bucket,
        ))
    duration_groups = [
        ("0-180秒", matched["experienced_downtime"] & matched["longest_downtime_seconds"].le(180), 0, 180),
        ("181-600秒", matched["longest_downtime_seconds"].gt(180)
         & matched["longest_downtime_seconds"].le(600), 180, 600),
        (">600秒", matched["longest_downtime_seconds"].gt(600), 600, np.nan),
    ]
    for label, values, minimum, maximum in duration_groups:
        column = f"_duration_{len(rows)}"
        matched[column] = values
        rows.append(_association_row(
            matched, column, f"最长疑似停机{label}", "停机时长—缺陷",
            len(rows) + 1, scope, source_type, code, quality,
            sample_filter_field="longest_downtime_seconds",
            sample_filter_operator="range_left_open", sample_filter_min=minimum,
            sample_filter_max=maximum,
        ))
    return pd.DataFrame(rows, columns=EVIDENCE_COLUMNS)


def _level_rank(value: Any) -> int:
    return {"较强相关": 3, "中等相关": 2, "探索性线索": 1}.get(str(value), 0)


def build_hypotheses(evidence: pd.DataFrame, rules: dict[str, Any], *, scope: str,
                     source_type: str, code: str, defect_name: str) -> pd.DataFrame:
    key = f"{scope}:{code}"
    definition = rules.get(key) or rules.get(code) or {}
    mechanisms = definition.get("mechanisms") or [{
        "id": "generic_event_context", "cause": "生产事件或工艺状态变化",
        "process_zone": "需结合工站暴露确定", "evidence_types": ["停机—缺陷", "复产—缺陷", "工站区间—缺陷"],
        "missing_evidence": ["PLC停机状态与原因", "缺陷形态或现场复核"],
        "recommended_action": "核对高风险事件时间、受影响DMC及设备日志",
    }]
    rows: list[dict[str, Any]] = []
    for index, mechanism in enumerate(mechanisms, 1):
        kinds = set(map(str, mechanism.get("evidence_types") or []))
        related = evidence[evidence["evidence_type"].astype(str).isin(kinds)]
        subjects = set(map(str, mechanism.get("evidence_subjects") or []))
        if subjects:
            related = related[related["subject"].astype(str).isin(subjects)]
        supporting = related[related["risk_ratio"].fillna(0).gt(1)].sort_values(
            ["evidence_score", "risk_ratio"], ascending=False,
        )
        counter = related[related["risk_ratio"].fillna(1).lt(1)]
        best = supporting.iloc[0] if not supporting.empty else None
        level = str(best["evidence_level"]) if best is not None else "探索性线索"
        score = float(best["evidence_score"]) if best is not None else 0.0
        support_text = "；".join(supporting.head(3)["statement"].astype(str)) or "当前表格未发现直接支持证据"
        counter_text = "；".join(counter.head(2)["statement"].astype(str)) or "当前表格未发现明确反对证据"
        process_zone = str(mechanism.get("process_zone", "需结合工站暴露确定"))
        strongest_segment = evidence[
            evidence["evidence_type"].eq("工站区间—缺陷")
        ].sort_values(["risk_ratio", "evidence_score"], ascending=False)
        if ("工站区间—缺陷" in kinds and not strongest_segment.empty
                and _level_rank(strongest_segment.iloc[0]["evidence_level"]) >= 2):
            process_zone = str(strongest_segment.iloc[0]["subject"]).replace("区间经历疑似停机", "")
        cause = str(mechanism.get("cause", "候选形成机理"))
        if best is not None and mechanism.get("id") == "stop_restart_material_state":
            conclusion = f"{code}与停机/复产{level}，{process_zone}停机暴露是首要线索。"
        elif best is not None:
            conclusion = f"{code}与{best['subject']}呈{level}；{process_zone}是当前关联最强的暴露区间。"
        else:
            conclusion = f"当前数据不足以支持“{cause}”假设。"
        rows.append({
            "hypothesis_id": f"HYP-{scope}-{code}-{index:02d}", "analysis_scope": scope,
            "source_type": source_type, "canonical_code": code, "defect_name": defect_name,
            "candidate_cause": cause, "process_zone": process_zone, "conclusion": conclusion,
            "evidence_level": level, "evidence_score": score,
            "supporting_evidence": support_text, "counter_evidence": counter_text,
            "supporting_evidence_ids": ";".join(supporting["evidence_id"].astype(str)),
            "missing_evidence": "；".join(map(str, mechanism.get("missing_evidence") or [])),
            "recommended_action": str(mechanism.get("recommended_action", "结合现场工艺与受控实验验证")),
            "warning": "候选机理，不是已确认根本原因",
        })
    return pd.DataFrame(rows, columns=HYPOTHESIS_COLUMNS).sort_values(
        "evidence_score", ascending=False, ignore_index=True,
    )


def _findings(evidence: pd.DataFrame, hypotheses: pd.DataFrame, *, target: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for item in evidence.to_dict("records"):
        if not bool(item.get("is_effective", False)):
            continue
        rows.append({
            "finding_id": item["evidence_id"], "finding_type": item["evidence_type"],
            "analysis_scope": item["analysis_scope"], "target": target,
            "source_type": item["source_type"], "canonical_code": item["canonical_code"],
            "subject": item["subject"], "related_subject": "", "statement": item["statement"],
            "evidence_score": item["evidence_score"], "evidence_level": item["evidence_level"],
            "effect_strength": min(1.0, max(0.0, log(max(float(item["risk_ratio"]), 1), 3)))
            if pd.notna(item["risk_ratio"]) and np.isfinite(item["risk_ratio"]) else 0.0,
            "sample_size": int(item["exposed_count"] + item["unexposed_count"]),
            "positive_count": int(item["exposed_defects"] + item["unexposed_defects"]),
            "negative_count": int(item["exposed_count"] + item["unexposed_count"]
                                  - item["exposed_defects"] - item["unexposed_defects"]),
            "risk_ratio": item["risk_ratio"], "lift": np.nan, "validation_auc": np.nan,
            "stability": np.nan, "data_quality": item["data_quality"], "warning": item["warning"],
            "detail_type": "cause_evidence", "detail_key": item["evidence_id"],
            "analysis_question": item.get("analysis_question", ""),
            "discovery_method": item.get("discovery_method", ""),
            "interpretation": item.get("interpretation", ""),
            "limitations": item.get("limitations", ""),
            "recommended_action": item.get("recommended_action", ""),
            "sample_filter_field": item.get("sample_filter_field", ""),
            "sample_filter_operator": item.get("sample_filter_operator", ""),
            "sample_filter_value": item.get("sample_filter_value", ""),
            "sample_filter_min": item.get("sample_filter_min", np.nan),
            "sample_filter_max": item.get("sample_filter_max", np.nan),
            "is_effective": True,
        })
    for item in hypotheses.to_dict("records"):
        if float(item.get("evidence_score", 0) or 0) <= 0:
            continue
        rows.append({
            "finding_id": item["hypothesis_id"], "finding_type": "机理假设",
            "analysis_scope": item["analysis_scope"], "target": target,
            "source_type": item["source_type"], "canonical_code": item["canonical_code"],
            "subject": item["candidate_cause"], "related_subject": item["process_zone"],
            "statement": item["conclusion"], "evidence_score": item["evidence_score"],
            "evidence_level": item["evidence_level"], "effect_strength": 0.0,
            "sample_size": np.nan, "positive_count": np.nan, "negative_count": np.nan,
            "risk_ratio": np.nan, "lift": np.nan, "validation_auc": np.nan,
            "stability": np.nan, "data_quality": np.nan, "warning": item["warning"],
            "detail_type": "cause_hypothesis", "detail_key": item["hypothesis_id"],
            "analysis_question": f"候选机理“{item['candidate_cause']}”是否能够解释当前缺陷？",
            "discovery_method": f"汇总与该机理相关的统计证据：{item['supporting_evidence']}",
            "interpretation": f"{item['conclusion']}\n反对证据：{item['counter_evidence']}",
            "limitations": f"仍缺少：{item['missing_evidence']}",
            "recommended_action": item["recommended_action"],
            "sample_filter_field": "", "sample_filter_operator": "",
            "sample_filter_value": item.get("supporting_evidence_ids", ""),
            "sample_filter_min": np.nan, "sample_filter_max": np.nan,
            "is_effective": True,
        })
    return pd.DataFrame(rows, columns=FINDING_COLUMNS).sort_values(
        "evidence_score", ascending=False, ignore_index=True,
    ) if rows else pd.DataFrame(columns=FINDING_COLUMNS)


def analyze_defect_causes(
    station_events: pd.DataFrame,
    station_parameters: pd.DataFrame,
    code_events: pd.DataFrame,
    *, source_type: str, code: str, scope: str,
    cause_rules: dict[str, Any] | None = None,
) -> DefectCauseAnalysisResult:
    downtime = detect_downtime_events(
        station_events, station_parameters, scope=scope,
    )
    exposure = build_product_event_exposure(
        station_events, station_parameters, code_events, downtime,
        source_type=source_type, code=code, scope=scope,
    )
    evidence = build_cause_evidence(
        exposure, scope=scope, source_type=source_type, code=code,
    )
    selected = code_events[
        code_events["source_type"].astype(str).eq(source_type)
        & code_events["analysis_scope"].astype(str).eq(scope)
        & code_events["canonical_code"].astype(str).eq(code)
    ]
    defect_name = next((str(value) for value in selected.get(
        "defect_name", pd.Series(dtype=str)
    ).dropna() if str(value).strip()), "")
    hypotheses = build_hypotheses(
        evidence, cause_rules or {}, scope=scope, source_type=source_type,
        code=code, defect_name=defect_name,
    )
    target = f"{source_type}:{code}"
    findings = _findings(evidence, hypotheses, target=target)
    matched = exposure["route_match_status"].eq("matched") if not exposure.empty else pd.Series(dtype=bool)
    complete = exposure["route_coverage_status"].eq("complete_route") \
        if not exposure.empty and "route_coverage_status" in exposure else pd.Series(dtype=bool)
    partial = exposure["route_coverage_status"].eq("partial_route") \
        if not exposure.empty and "route_coverage_status" in exposure else pd.Series(dtype=bool)
    suspected_left = exposure["route_match_status"].eq("suspected_left_window_censored") \
        if not exposure.empty else pd.Series(dtype=bool)
    not_in_window = exposure["route_match_status"].eq("not_in_current_route_window") \
        if not exposure.empty else pd.Series(dtype=bool)
    traceable_count = int(matched.sum()) if len(matched) else 0
    summary = {
        "analysis_scope": scope, "source_type": source_type, "canonical_code": code,
        # matched_route_count is retained for old result readers.
        "population_count": len(exposure), "matched_route_count": traceable_count,
        "current_window_traceable_count": traceable_count,
        "current_window_traceability_rate": traceable_count / len(exposure) if len(exposure) else 0.0,
        "complete_route_count": int(complete.sum()) if len(complete) else 0,
        "partial_route_count": int(partial.sum()) if len(partial) else 0,
        "suspected_left_window_censored_count": int(suspected_left.sum()) if len(suspected_left) else 0,
        "not_in_current_route_window_count": int(not_in_window.sum()) if len(not_in_window) else 0,
        "target_defect_count": int(exposure["is_target_defect"].sum()) if not exposure.empty else 0,
        "downtime_event_count": len(downtime),
        "top_hypothesis": hypotheses.iloc[0]["conclusion"] if not hypotheses.empty else "数据不足",
        "warning": (
            "Excel仅能推断疑似停机和统计关联，不能确认设备停机原因或物理根因。"
            "仅对当前数据窗口内可按相同DMC追溯的产品计算事件暴露；"
            "窗口外产品不等于DMC匹配失败。"
        ),
    }
    return DefectCauseAnalysisResult(downtime, exposure, hypotheses, evidence, findings, summary)
