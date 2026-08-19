"""Build the filtered result views used by overview cards and detail dialogs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import pandas as pd

from .defect_evidence import discover_code_patterns
from .defect_relationships import analyze_defect_relationships


PATTERN_SECTION_ORDER = (
    "periodic", "burst", "code", "trajectory", "cooccurrence", "transition", "other",
)


@dataclass
class ResultView:
    products: pd.DataFrame
    extracted: pd.DataFrame
    alerts: pd.DataFrame
    sections: dict[str, pd.DataFrame]
    counts: dict[str, int]


def _text_orders(value: Any) -> set[int]:
    result: set[int] = set()
    if value is None or pd.isna(value):
        return result
    for part in str(value).replace(",", ";").split(";"):
        try:
            result.add(int(float(part.strip())))
        except (TypeError, ValueError):
            continue
    return result


def _filter_scope(frame: pd.DataFrame, scopes: set[str]) -> pd.DataFrame:
    if frame.empty or "analysis_scope" not in frame or not scopes:
        return frame.copy()
    return frame[frame["analysis_scope"].astype(str).isin(scopes)].copy()


def _filter_order_evidence(
    frame: pd.DataFrame,
    orders: set[int],
    *,
    list_columns: Iterable[str] = (),
    scalar_columns: Iterable[str] = (),
) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    available_lists = [name for name in list_columns if name in frame]
    available_scalars = [name for name in scalar_columns if name in frame]
    if not available_lists and not available_scalars:
        return frame.copy()
    keep = pd.Series(False, index=frame.index)
    for name in available_lists:
        keep |= frame[name].map(lambda value: bool(_text_orders(value) & orders))
    for name in available_scalars:
        values = pd.to_numeric(frame[name], errors="coerce")
        keep |= values.isin(orders)
    return frame[keep].copy()


def _defect_code_links(
    frames: dict[str, pd.DataFrame], products: pd.DataFrame, source: str
) -> pd.DataFrame:
    links = frames.get("code_image_links", pd.DataFrame()).copy()
    required = {"global_order", "canonical_code"}
    if links.empty or not required.issubset(links.columns):
        return links.iloc[0:0].copy()
    orders = set(pd.to_numeric(products["global_order"], errors="coerce").dropna().astype(int))
    links = links[pd.to_numeric(links["global_order"], errors="coerce").isin(orders)]
    if "code_status" in links:
        links = links[links["code_status"].isin(["defect", "state_code_conflict"])]
    if source != "all" and "source_type" in links:
        links = links[links["source_type"].astype(str).eq(str(source))]
    return links.copy()


def _code_relationships(
    products: pd.DataFrame, links: pd.DataFrame, selected_codes: set[str]
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if links.empty:
        empty = pd.DataFrame(columns=["缺陷A", "缺陷B", "共现产品数", "P(B|A)", "P(A|B)", "提升度"])
        transition = pd.DataFrame(columns=["前一缺陷", "后一缺陷", "转移次数", "条件概率", "平均间隔片数"])
        return empty, transition
    events = links[[name for name in ("analysis_scope", "global_order", "canonical_code") if name in links]].copy()
    events = events.rename(columns={"canonical_code": "defect_type"})
    events = events.drop_duplicates([name for name in ("analysis_scope", "global_order", "defect_type") if name in events])
    if "analysis_scope" in events and "analysis_scope" in products:
        cooccurrence_parts: list[pd.DataFrame] = []
        transition_parts: list[pd.DataFrame] = []
        for scope in events["analysis_scope"].dropna().astype(str).drop_duplicates():
            scoped_products = products[products["analysis_scope"].astype(str).eq(scope)]
            scoped_events = events[events["analysis_scope"].astype(str).eq(scope)]
            cooccurrence, transitions = analyze_defect_relationships(scoped_products, scoped_events)
            cooccurrence.insert(0, "analysis_scope", scope)
            transitions.insert(0, "analysis_scope", scope)
            cooccurrence_parts.append(cooccurrence)
            transition_parts.append(transitions)
        cooccurrence = pd.concat(cooccurrence_parts, ignore_index=True) if cooccurrence_parts else empty
        transitions = pd.concat(transition_parts, ignore_index=True) if transition_parts else transition
    else:
        cooccurrence, transitions = analyze_defect_relationships(products, events)
    if selected_codes and not cooccurrence.empty:
        cooccurrence = cooccurrence[
            cooccurrence["缺陷A"].astype(str).isin(selected_codes)
            | cooccurrence["缺陷B"].astype(str).isin(selected_codes)
        ].copy()
    if selected_codes and not transitions.empty:
        transitions = transitions[
            transitions["前一缺陷"].astype(str).isin(selected_codes)
            | transitions["后一缺陷"].astype(str).isin(selected_codes)
        ].copy()
    return cooccurrence, transitions


def build_result_view(
    frames: dict[str, pd.DataFrame],
    products: pd.DataFrame,
    extracted: pd.DataFrame,
    config: dict[str, Any],
    *,
    selected_codes: Iterable[str] = (),
    code_source: str = "all",
    merge_selected_codes: bool = False,
) -> ResultView:
    """Return all result categories after applying one shared set of filters."""
    selected = {str(value) for value in selected_codes if str(value)}
    scopes = set(products.get("analysis_scope", pd.Series(dtype=str)).dropna().astype(str))
    base_orders = set(pd.to_numeric(products["global_order"], errors="coerce").dropna().astype(int))
    links = _defect_code_links(frames, products, code_source)
    selected_links = (
        links[links["canonical_code"].astype(str).isin(selected)].copy()
        if selected and not links.empty else links.copy()
    )
    visible_orders = (
        set(pd.to_numeric(selected_links["global_order"], errors="coerce").dropna().astype(int))
        if selected else base_orders
    )
    visible_products = products[
        pd.to_numeric(products["global_order"], errors="coerce").isin(visible_orders)
    ].copy()
    visible_extracted = extracted[
        pd.to_numeric(extracted["global_order"], errors="coerce").isin(visible_orders)
    ].copy()

    spatial_patterns = _filter_scope(frames.get("patterns", pd.DataFrame()), scopes)
    spatial_patterns = _filter_order_evidence(
        spatial_patterns, base_orders,
        list_columns=("observed_orders", "evidence_orders"),
        scalar_columns=("first_order",),
    )
    alerts = _filter_scope(frames.get("alerts", pd.DataFrame()), scopes)
    alerts = _filter_order_evidence(
        alerts, base_orders,
        list_columns=("evidence_orders",), scalar_columns=("alert_at_order",),
    )
    trajectories = _filter_scope(frames.get("trajectories", pd.DataFrame()), scopes)
    trajectories = _filter_order_evidence(
        trajectories, base_orders, list_columns=("task_orders",),
    )

    associations = _filter_scope(frames.get("code_space", pd.DataFrame()), scopes)
    if not associations.empty:
        if code_source != "all" and "source_type" in associations:
            associations = associations[associations["source_type"].astype(str).eq(str(code_source))]
        associations = _filter_order_evidence(
            associations, base_orders, list_columns=("support_task_orders",),
        )
    if selected:
        associations = (
            associations[associations["canonical_code"].astype(str).isin(selected)].copy()
            if not associations.empty and "canonical_code" in associations else associations.iloc[0:0].copy()
        )
        cluster_ids = set(
            associations.loc[associations.get("spatial_type", pd.Series(index=associations.index, dtype=str)).astype(str).eq("cluster"), "spatial_id"].astype(str)
        ) if not associations.empty and "spatial_id" in associations else set()
        trajectory_ids = set(
            associations.loc[associations.get("spatial_type", pd.Series(index=associations.index, dtype=str)).astype(str).eq("trajectory"), "spatial_id"].astype(str)
        ) if not associations.empty and "spatial_id" in associations else set()
        spatial_patterns = (
            spatial_patterns[spatial_patterns["cluster_id"].astype(str).isin(cluster_ids)].copy()
            if "cluster_id" in spatial_patterns else spatial_patterns.iloc[0:0].copy()
        )
        alerts = (
            alerts[alerts["cluster_id"].astype(str).isin(cluster_ids)].copy()
            if "cluster_id" in alerts else alerts.iloc[0:0].copy()
        )
        trajectories = (
            trajectories[trajectories["trajectory_id"].astype(str).isin(trajectory_ids)].copy()
            if "trajectory_id" in trajectories else trajectories.iloc[0:0].copy()
        )

    if selected and merge_selected_codes:
        code_patterns = discover_code_patterns(
            frames.get("normalized_codes", pd.DataFrame()), config, selected,
            merge_selected=True, image_links=frames.get("code_image_links", pd.DataFrame()),
        )
    else:
        code_patterns = frames.get("code_patterns", pd.DataFrame()).copy()
    code_patterns = _filter_scope(code_patterns, scopes)
    if selected and not code_patterns.empty and "canonical_code" in code_patterns and not merge_selected_codes:
        code_patterns = code_patterns[code_patterns["canonical_code"].astype(str).isin(selected)].copy()
    if code_source != "all" and not code_patterns.empty and "source_type" in code_patterns:
        code_patterns = code_patterns[code_patterns["source_type"].astype(str).eq(str(code_source))]
    code_patterns = _filter_order_evidence(
        code_patterns, base_orders, list_columns=("evidence_task_orders",),
    )

    cooccurrence, transitions = _code_relationships(products, links, selected)
    pattern_types = spatial_patterns.get("pattern_type", pd.Series(index=spatial_patterns.index, dtype=str)).astype(str)
    sections = {
        "periodic": spatial_patterns[pattern_types.eq("periodic")].copy(),
        "burst": spatial_patterns[pattern_types.eq("burst")].copy(),
        "code": code_patterns,
        "trajectory": trajectories,
        "cooccurrence": cooccurrence,
        "transition": transitions,
        "other": spatial_patterns[~pattern_types.isin(["periodic", "burst"])].copy(),
    }

    detection = visible_extracted.get("detection_type", pd.Series(index=visible_extracted.index, dtype=str)).astype(str)
    conflicts = _filter_scope(frames.get("code_conflicts", pd.DataFrame()), scopes)
    conflicts = _filter_order_evidence(conflicts, visible_orders, scalar_columns=("global_order",))
    counts = {
        "analyzed_product_count": len(visible_products),
        "extracted_defect_count": len(visible_extracted),
        "micro_defect_count": int(detection.eq("micro_defect").sum()),
        "local_defect_count": int(detection.eq("local_defect").sum()),
        "region_anomaly_count": int(detection.eq("region_anomaly").sum()),
        "spatial_cluster_count": int(visible_extracted.get("cluster_id", pd.Series(dtype=str)).replace("", pd.NA).dropna().nunique()),
        "code_label_conflict_count": len(conflicts),
        "alert_count": len(alerts),
        "discovered_pattern_count": sum(len(sections[name]) for name in PATTERN_SECTION_ORDER),
    }
    return ResultView(visible_products, visible_extracted, alerts, sections, counts)


def pattern_count(view: ResultView, selected_section: str) -> int:
    if selected_section == "all":
        return sum(len(view.sections[name]) for name in PATTERN_SECTION_ORDER)
    return len(view.sections.get(selected_section, pd.DataFrame()))
