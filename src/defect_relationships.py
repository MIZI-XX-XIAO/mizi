"""本文件分析缺陷类别或空间簇的共现、条件概率、提升度与序列转移。"""

from __future__ import annotations

from itertools import combinations

import pandas as pd


def analyze_defect_relationships(
    products: pd.DataFrame, defects: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    columns = ["缺陷A", "缺陷B", "共现产品数", "P(B|A)", "P(A|B)", "提升度"]
    transition_columns = ["前一缺陷", "后一缺陷", "转移次数", "条件概率", "平均间隔片数"]
    if defects.empty:
        return pd.DataFrame(columns=columns), pd.DataFrame(columns=transition_columns)
    if "detection_type" in defects:
        defects = defects[defects["detection_type"] != "region_anomaly"]
        if defects.empty:
            return pd.DataFrame(columns=columns), pd.DataFrame(columns=transition_columns)
    category = "defect_type" if "defect_type" in defects else "cluster_id"
    if category not in defects or defects[category].dropna().empty:
        return pd.DataFrame(columns=columns), pd.DataFrame(columns=transition_columns)
    events = defects.dropna(subset=[category]).copy()
    events = events[events[category].astype(str).str.strip() != ""]
    events[category] = events[category].astype(str)
    by_order = {
        int(order): sorted(set(group[category]))
        for order, group in events.groupby("global_order")
    }
    total_products = max(1, len(products))
    category_counts = events.groupby(category)["global_order"].nunique().to_dict()
    pair_counts: dict[tuple[str, str], int] = {}
    for values in by_order.values():
        for pair in combinations(values, 2):
            pair_counts[pair] = pair_counts.get(pair, 0) + 1
    rows = []
    for (left, right), count in pair_counts.items():
        left_count, right_count = category_counts[left], category_counts[right]
        lift = (count / total_products) / (
            (left_count / total_products) * (right_count / total_products)
        )
        rows.append({
            "缺陷A": left, "缺陷B": right, "共现产品数": count,
            "P(B|A)": round(count / left_count, 5),
            "P(A|B)": round(count / right_count, 5),
            "提升度": round(lift, 5),
        })
    cooccurrence = pd.DataFrame(rows, columns=columns)
    if not cooccurrence.empty:
        cooccurrence = cooccurrence.sort_values(
            ["提升度", "共现产品数"], ascending=False, ignore_index=True
        )

    ordered = sorted(by_order.items())
    transitions: dict[tuple[str, str], list[int]] = {}
    source_counts: dict[str, int] = {}
    for (previous_order, previous), (next_order, following) in zip(ordered, ordered[1:]):
        gap = next_order - previous_order
        for left in previous:
            source_counts[left] = source_counts.get(left, 0) + 1
            for right in following:
                transitions.setdefault((left, right), []).append(gap)
    transition_rows = [
        {
            "前一缺陷": left, "后一缺陷": right, "转移次数": len(gaps),
            "条件概率": round(len(gaps) / source_counts[left], 5),
            "平均间隔片数": round(sum(gaps) / len(gaps), 3),
        }
        for (left, right), gaps in transitions.items()
    ]
    transition_frame = pd.DataFrame(transition_rows, columns=transition_columns)
    if not transition_frame.empty:
        transition_frame = transition_frame.sort_values(
            ["条件概率", "转移次数"], ascending=False, ignore_index=True
        )
    return cooccurrence, transition_frame
