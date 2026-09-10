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
    "target_time", "route_first_time", "route_last_time", "route_match_status",
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
]
HYPOTHESIS_COLUMNS = [
    "hypothesis_id", "analysis_scope", "source_type", "canonical_code",
    "defect_name", "candidate_cause", "process_zone", "conclusion",
    "evidence_level", "evidence_score", "supporting_evidence", "counter_evidence",
    "missing_evidence", "recommended_action", "warning",
]
FINDING_COLUMNS = [
    "finding_id", "finding_type", "analysis_scope", "target", "source_type",
    "canonical_code", "subject", "related_subject", "statement", "evidence_score",
    "evidence_level", "effect_strength", "sample_size", "positive_count",
    "negative_count", "risk_ratio", "lift", "validation_auc", "stability",
    "data_quality", "warning", "detail_type", "detail_key",
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
    scoped = scoped.drop_duplicates("dmc_raw", keep="last").copy()
    target_dmcs = set(code_events.loc[
        code_events["source_type"].astype(str).eq(source_type)
        & code_events["analysis_scope"].astype(str).eq(scope)
        & code_events["canonical_code"].astype(str).eq(code)
        & code_events["code_status"].isin(["defect", "state_code_conflict"]), "dmc_raw"
    ].astype(str))
    scoped["is_target_defect"] = scoped["dmc_raw"].astype(str).isin(target_dmcs)
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
    route["_time"] = pd.to_datetime(route.get("test_date"), errors="coerce")
    route = route.dropna(subset=["_time"]).sort_values("_time", kind="stable")
    route["_rank"] = route["station_id"].map(_route_rank)
    route_times = route.pivot_table(index="dmc_raw", columns="_rank", values="_time", aggfunc="last")
    route_groups = {str(dmc): group for dmc, group in route.groupby(route["dmc_raw"].astype(str))}

    anchor = route[route["station_id"].astype(str).str.endswith("_aoi")]
    anchor_times = anchor.groupby(anchor["dmc_raw"].astype(str))["_time"].last().sort_values()
    anchor_order = anchor_times.reset_index(drop=False)
    anchor_order["_output_order"] = range(1, len(anchor_order) + 1)
    anchor_order_map = dict(zip(anchor_order["dmc_raw"].astype(str), anchor_order["_output_order"]))
    # A process signal may support a cause only when it precedes the target
    # inspection for that DMC. This prevents downstream measurements from being
    # presented as explanatory variables.
    parameter_history = station_parameters.copy()
    if not parameter_history.empty and {"dmc_raw", "test_date"}.issubset(parameter_history.columns):
        fallback_times = dict(zip(
            population["dmc_raw"].astype(str),
            pd.to_datetime(population["test_date"], errors="coerce"),
        ))
        reference_times = {
            dmc: anchor_times.get(dmc, fallback_times.get(dmc, pd.NaT))
            for dmc in population["dmc_raw"].astype(str)
        }
        parameter_history["_parameter_time"] = pd.to_datetime(
            parameter_history["test_date"], errors="coerce"
        )
        parameter_history["_reference_time"] = parameter_history["dmc_raw"].astype(str).map(reference_times)
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
        group = route_groups.get(dmc, pd.DataFrame())
        observed = int(group["station_id"].nunique()) if not group.empty else 0
        first = group["_time"].min() if observed else pd.NaT
        last = group["_time"].max() if observed else pd.NaT
        target_time = anchor_times.get(dmc, pd.to_datetime(item.get("test_date"), errors="coerce"))
        ranks = route_times.loc[dmc].dropna().sort_index() if dmc in route_times.index else pd.Series(dtype="datetime64[ns]")
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
                product_order = anchor_order_map.get(dmc)
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
            "target_time": target_time, "route_first_time": first, "route_last_time": last,
            "route_match_status": "matched" if observed else "unmatched",
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
    return pd.DataFrame(rows, columns=EXPOSURE_COLUMNS)


def _fisher_p(a: int, b: int, c: int, d: int) -> float:
    try:
        from scipy.stats import fisher_exact
        return float(fisher_exact([[a, b], [c, d]]).pvalue)
    except ImportError:
        return np.nan


def _association_row(frame: pd.DataFrame, column: str, label: str, evidence_type: str,
                     index: int, scope: str, source_type: str, code: str,
                     data_quality: float) -> dict[str, Any]:
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
        f"对照产品为{unexposed_rate:.1%}（{c}/{unexposed_total}），风险比{rr:.2f}。"
    ) if pd.notna(rr) and np.isfinite(rr) else f"{label}的对照样本不足，暂不能稳定估计风险比。"
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
                f"在{len(matched)}件可关联产品中观测到{observed_count}件{code}；"
                f"任意5分钟窗口内最多{max_five_minutes}件。"
            ),
            "exposed_count": len(matched), "unexposed_count": 0,
            "exposed_defects": observed_count, "unexposed_defects": 0,
            "exposed_rate": observed_count / len(matched), "unexposed_rate": np.nan,
            "risk_ratio": np.nan, "risk_difference": np.nan, "ci95_low": np.nan,
            "ci95_high": np.nan, "p_value": np.nan, "evidence_level": "已观测事实",
            "evidence_score": 100.0, "data_quality": quality, "warning": observation_warning,
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
        ))
    for bucket in ("1-10", "11-30", "31-100"):
        column = f"_bucket_{bucket}"
        matched[column] = matched["restart_bucket"].eq(bucket)
        rows.append(_association_row(
            matched, column, f"复产后第{bucket}件", "复产窗口—缺陷",
            len(rows) + 1, scope, source_type, code, quality,
        ))
    duration_groups = [
        ("0-180秒", matched["experienced_downtime"] & matched["longest_downtime_seconds"].le(180)),
        ("181-600秒", matched["longest_downtime_seconds"].gt(180)
         & matched["longest_downtime_seconds"].le(600)),
        (">600秒", matched["longest_downtime_seconds"].gt(600)),
    ]
    for label, values in duration_groups:
        column = f"_duration_{len(rows)}"
        matched[column] = values
        rows.append(_association_row(
            matched, column, f"最长疑似停机{label}", "停机时长—缺陷",
            len(rows) + 1, scope, source_type, code, quality,
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
        })
    for item in hypotheses.to_dict("records"):
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
    summary = {
        "analysis_scope": scope, "source_type": source_type, "canonical_code": code,
        "population_count": len(exposure), "matched_route_count": int(matched.sum()) if len(matched) else 0,
        "target_defect_count": int(exposure["is_target_defect"].sum()) if not exposure.empty else 0,
        "downtime_event_count": len(downtime),
        "top_hypothesis": hypotheses.iloc[0]["conclusion"] if not hypotheses.empty else "数据不足",
        "warning": "Excel仅能推断疑似停机和统计关联，不能确认设备停机原因或物理根因。",
    }
    return DefectCauseAnalysisResult(downtime, exposure, hypotheses, evidence, findings, summary)
