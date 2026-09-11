"""本文件发现可解释的非线性工艺关系、参数交互并生成重点发现。"""

from __future__ import annotations

from itertools import combinations
from math import log2, sqrt
from typing import Any

import numpy as np
import pandas as pd


FINDING_COLUMNS = [
    "finding_id", "finding_type", "analysis_scope", "target", "source_type",
    "canonical_code", "defect_name", "subject", "related_subject", "statement", "evidence_score",
    "evidence_level", "effect_strength", "sample_size", "positive_count", "negative_count",
    "risk_ratio", "lift", "validation_auc", "stability", "data_quality", "warning",
    "detail_type", "detail_key", "analysis_question", "discovery_method",
    "interpretation", "limitations", "recommended_action",
    "sample_filter_field", "sample_filter_operator", "sample_filter_value",
    "sample_filter_min", "sample_filter_max", "is_effective",
]
IMPORTANCE_COLUMNS = ["参数", "置换重要性", "重要性波动"]
EFFECT_COLUMNS = [
    "参数", "高风险区间", "区间样本量", "区间缺陷率", "总体缺陷率", "风险比",
    "P值", "FDR_Q值", "稳定性", "部分依赖风险差",
]
INTERACTION_COLUMNS = ["参数A", "参数B", "交互强度", "双参数AUC", "最佳单参数AUC", "AUC增益"]
INTERACTION_REGION_COLUMNS = [
    "参数A", "参数B", "参数A区间", "参数B区间",
    "参数A下界", "参数A上界", "参数B下界", "参数B上界",
    "样本量", "缺陷数量", "缺陷率", "总体缺陷率", "风险比",
    "保守风险差", "P值", "FDR_Q值", "是否高风险", "高风险排名",
]
VALIDATION_COLUMNS = [
    "验证方式", "有效折数", "非线性AUC", "AUC标准差", "线性基线AUC", "AUC增益",
    "样本量", "正样本", "负样本",
]
CURVE_COLUMNS = ["参数", "参数值", "模型预测缺陷率"]


def empty_findings() -> pd.DataFrame:
    return pd.DataFrame(columns=FINDING_COLUMNS)


def _auc(y_true: np.ndarray, scores: np.ndarray) -> float | None:
    positives = int(y_true.sum())
    negatives = len(y_true) - positives
    if positives == 0 or negatives == 0:
        return None
    ranks = pd.Series(scores).rank(method="average").to_numpy()
    return float((ranks[y_true == 1].sum() - positives * (positives + 1) / 2) /
                 (positives * negatives))


def _ordered_splits(frame: pd.DataFrame) -> tuple[list[tuple[np.ndarray, np.ndarray]], str]:
    batch = next((name for name in ("batch", "batch_id") if name in frame), None)
    splits: list[tuple[np.ndarray, np.ndarray]] = []
    if batch and frame[batch].nunique(dropna=True) >= 3:
        labels = frame[batch].fillna("缺失批次").astype(str)
        groups = labels.drop_duplicates().tolist()
        for position in range(1, len(groups)):
            train = np.flatnonzero(labels.isin(groups[:position]))
            test = np.flatnonzero(labels.eq(groups[position]))
            if len(train) >= 20 and len(test) >= 5:
                splits.append((train, test))
        return splits[-5:], "按批次滚动验证"
    count = len(frame)
    block = max(5, count // 6)
    first_end = max(20, count - 5 * block)
    for end in range(first_end, count, block):
        test_end = min(count, end + block)
        if test_end - end >= 5:
            splits.append((np.arange(end), np.arange(end, test_end)))
    return splits[-5:], "按生产顺序滚动验证"


def _bh_adjust(values: list[float]) -> list[float]:
    if not values:
        return []
    order = np.argsort(values)
    adjusted = np.ones(len(values))
    running = 1.0
    for offset in range(len(order) - 1, -1, -1):
        original = int(order[offset])
        running = min(running, values[original] * len(values) / (offset + 1))
        adjusted[original] = min(1.0, running)
    return adjusted.tolist()


def _wilson_lower(positives: int, total: int, z: float = 1.96) -> float:
    """Return a conservative lower confidence bound for a binomial rate."""
    if total <= 0:
        return 0.0
    rate = positives / total
    denominator = 1 + z * z / total
    centre = rate + z * z / (2 * total)
    margin = z * sqrt((rate * (1 - rate) + z * z / (4 * total)) / total)
    return max(0.0, (centre - margin) / denominator)


def _format_interval(interval: pd.Interval) -> str:
    left_bracket = "[" if interval.closed_left else "("
    right_bracket = "]" if interval.closed_right else ")"
    return f"{left_bracket}{float(interval.left):.6g}, {float(interval.right):.6g}{right_bracket}"


def _describe_interaction_regions(
    x: pd.DataFrame,
    y: pd.Series,
    left: str,
    right: str,
    fisher_exact,
) -> list[dict[str, Any]]:
    """Describe the observable two-dimensional risk cells for one parameter pair."""
    valid = x[left].notna() & x[right].notna()
    if int(valid.sum()) < 20:
        return []
    try:
        left_bins = pd.qcut(x.loc[valid, left], q=4, duplicates="drop", precision=6)
        right_bins = pd.qcut(x.loc[valid, right], q=4, duplicates="drop", precision=6)
    except ValueError:
        return []
    if left_bins.cat.categories.size < 2 or right_bins.cat.categories.size < 2:
        return []

    base_rate = float(y.loc[valid].mean())
    minimum = max(5, int(np.ceil(valid.sum() * 0.02)))
    cells = pd.DataFrame({
        "_left": left_bins,
        "_right": right_bins,
        "_target": y.loc[valid].astype(int),
    })
    rows: list[dict[str, Any]] = []
    p_values: list[float] = []
    total_positives = int(cells["_target"].sum())
    total = len(cells)
    for (left_interval, right_interval), group in cells.groupby(
        ["_left", "_right"], observed=True
    ):
        sample_size = len(group)
        defects = int(group["_target"].sum())
        outside_total = total - sample_size
        outside_defects = total_positives - defects
        if outside_total > 0:
            _, p_value = fisher_exact([
                [defects, sample_size - defects],
                [outside_defects, outside_total - outside_defects],
            ])
        else:
            p_value = 1.0
        defect_rate = defects / sample_size if sample_size else 0.0
        risk_ratio = defect_rate / base_rate if base_rate else np.nan
        conservative_difference = _wilson_lower(defects, sample_size) - base_rate
        eligible = (
            sample_size >= minimum
            and defects >= 2
            and defect_rate > base_rate
            and conservative_difference > 0
        )
        rows.append({
            "参数A": left, "参数B": right,
            "参数A区间": _format_interval(left_interval),
            "参数B区间": _format_interval(right_interval),
            "参数A下界": float(left_interval.left), "参数A上界": float(left_interval.right),
            "参数B下界": float(right_interval.left), "参数B上界": float(right_interval.right),
            "样本量": sample_size, "缺陷数量": defects,
            "缺陷率": round(defect_rate, 6), "总体缺陷率": round(base_rate, 6),
            "风险比": round(float(risk_ratio), 6) if pd.notna(risk_ratio) else np.nan,
            "保守风险差": round(conservative_difference, 6),
            "P值": round(float(p_value), 6), "FDR_Q值": np.nan,
            "是否高风险": bool(eligible), "高风险排名": np.nan,
        })
        p_values.append(float(p_value))
    for row, q_value in zip(rows, _bh_adjust(p_values)):
        row["FDR_Q值"] = round(q_value, 6)
    eligible_indices = sorted(
        (index for index, row in enumerate(rows) if row["是否高风险"]),
        key=lambda index: (
            rows[index]["保守风险差"], rows[index]["风险比"], rows[index]["样本量"]
        ),
        reverse=True,
    )
    for rank, index in enumerate(eligible_indices, 1):
        rows[index]["高风险排名"] = rank
    return rows


def _region_summary(region: pd.Series) -> str:
    return (
        f"{region['参数A']}处于{region['参数A区间']}且{region['参数B']}处于"
        f"{region['参数B区间']}时，{int(region['样本量'])}件中有"
        f"{int(region['缺陷数量'])}件缺陷（{float(region['缺陷率']):.1%}），"
        f"总体为{float(region['总体缺陷率']):.1%}，风险约{float(region['风险比']):.2f}倍"
    )


def _level(score: float, high_allowed: bool) -> str:
    if high_allowed and score >= 80:
        return "高"
    return "中" if score >= 60 else "探索性"


def _model(max_leaf_nodes: int = 15):
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import make_pipeline

    return make_pipeline(
        SimpleImputer(strategy="median"),
        HistGradientBoostingClassifier(
            learning_rate=0.07, max_iter=100, max_leaf_nodes=max_leaf_nodes,
            l2_regularization=0.1, class_weight="balanced", random_state=42,
        ),
    )


def _linear_model():
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    return make_pipeline(
        SimpleImputer(strategy="median"), StandardScaler(),
        LogisticRegression(class_weight="balanced", max_iter=1000, random_state=42),
    )


def _cross_validated_auc(x: pd.DataFrame, y: pd.Series,
                         splits: list[tuple[np.ndarray, np.ndarray]], model_factory) -> list[float]:
    values: list[float] = []
    for train, test in splits:
        if y.iloc[train].nunique() < 2 or y.iloc[test].nunique() < 2:
            continue
        model = model_factory()
        model.fit(x.iloc[train], y.iloc[train])
        score = _auc(y.iloc[test].to_numpy(), model.predict_proba(x.iloc[test])[:, 1])
        if score is not None:
            values.append(score)
    return values


def _insufficient_validation(sample_size: int, positives: int, reason: str) -> pd.DataFrame:
    return pd.DataFrame([{
        "验证方式": reason, "有效折数": 0, "非线性AUC": np.nan, "AUC标准差": np.nan,
        "线性基线AUC": np.nan, "AUC增益": np.nan, "样本量": sample_size,
        "正样本": positives, "负样本": sample_size - positives,
    }], columns=VALIDATION_COLUMNS)


def analyze_nonlinear_relationships(
    joined: pd.DataFrame,
    features: list[str],
    *,
    target: str = "has_detected_defect",
    match_rate: float = 1.0,
) -> dict[str, Any]:
    """Return nonlinear evidence while safely degrading for small samples or missing dependencies."""
    extra = [name for name in ("batch", "batch_id") if name in joined]
    usable = joined.loc[joined["match_quality"].ne("unmatched"), features + [target] + extra].copy()
    usable = usable.reset_index(drop=True)
    features = [name for name in features
                if pd.to_numeric(usable[name], errors="coerce").nunique() > 1]
    positives = int(usable[target].sum()) if not usable.empty else 0
    eligible = len(usable) >= 30 and positives >= 10 and len(usable) - positives >= 10 and bool(features)
    empty_result = {
        "nonlinear_importance": pd.DataFrame(columns=IMPORTANCE_COLUMNS),
        "nonlinear_effects": pd.DataFrame(columns=EFFECT_COLUMNS),
        "interactions": pd.DataFrame(columns=INTERACTION_COLUMNS),
        "interaction_regions": pd.DataFrame(columns=INTERACTION_REGION_COLUMNS),
        "risk_curves": pd.DataFrame(columns=CURVE_COLUMNS),
        "model_validation": _insufficient_validation(len(usable), positives, "样本不足，仅描述性结果"),
        "findings": empty_findings(),
        "summary": {"nonlinear_status": "insufficient_sample", "nonlinear_auc": None},
    }
    if not eligible:
        return empty_result
    try:
        from scipy.stats import fisher_exact
        from sklearn.inspection import permutation_importance
    except ImportError:
        empty_result["model_validation"] = _insufficient_validation(
            len(usable), positives, "scikit-learn不可用，仅保留线性结果"
        )
        empty_result["summary"]["nonlinear_status"] = "dependency_unavailable"
        return empty_result

    x = usable[features].apply(pd.to_numeric, errors="coerce")
    y = usable[target].astype(int)
    splits, validation_method = _ordered_splits(usable)
    nonlinear_aucs = _cross_validated_auc(x, y, splits, _model)
    linear_aucs = _cross_validated_auc(x, y, splits, _linear_model)
    nonlinear_auc = float(np.mean(nonlinear_aucs)) if nonlinear_aucs else np.nan
    linear_auc = float(np.mean(linear_aucs)) if linear_aucs else np.nan

    final_model = _model()
    final_model.fit(x, y)
    permutation = permutation_importance(
        final_model, x, y, scoring="roc_auc", n_repeats=10, random_state=42, n_jobs=1
    )
    importance = pd.DataFrame({
        "参数": features, "置换重要性": np.round(permutation.importances_mean, 5),
        "重要性波动": np.round(permutation.importances_std, 5),
    }).sort_values("置换重要性", ascending=False, ignore_index=True)
    candidates = importance.head(8)["参数"].tolist()
    base_rate = float(y.mean())
    rng = np.random.default_rng(42)
    effects: list[dict[str, Any]] = []
    curve_rows: list[dict[str, Any]] = []
    p_values: list[float] = []
    single_aucs: dict[str, float] = {}

    for name in candidates:
        values = x[name]
        valid = values.notna()
        quantiles = pd.qcut(values[valid], q=min(10, values[valid].nunique()), duplicates="drop")
        grouped = usable.loc[valid].assign(_bin=quantiles).groupby("_bin", observed=True)[target].agg(
            ["size", "sum", "mean"]
        )
        grouped = grouped[grouped["size"] >= 5]
        if grouped.empty:
            continue
        interval = grouped.sort_values(["mean", "size"], ascending=False).index[0]
        high = grouped.loc[interval]
        selected = valid & values.gt(float(interval.left)) & values.le(float(interval.right))
        outside = valid & ~selected
        in_pos, in_total = int(y[selected].sum()), int(selected.sum())
        out_pos, out_total = int(y[outside].sum()), int(outside.sum())
        _, p_value = fisher_exact([[in_pos, in_total - in_pos], [out_pos, out_total - out_pos]])
        risk_ratio = float(high["mean"] / base_rate) if base_rate else np.nan
        stable = 0
        for _ in range(100):
            sample = rng.integers(0, len(usable), len(usable))
            sampled_selection = selected.iloc[sample].to_numpy()
            sampled_y = y.iloc[sample].to_numpy()
            if sampled_selection.any() and sampled_y[sampled_selection].mean() > sampled_y.mean():
                stable += 1
        grid = np.unique(np.quantile(values[valid], np.linspace(0.05, 0.95, 20)))
        pd_predictions = []
        for point in grid:
            changed = x.copy()
            changed[name] = point
            prediction = float(final_model.predict_proba(changed)[:, 1].mean())
            pd_predictions.append(prediction)
            curve_rows.append({
                "参数": name, "参数值": round(float(point), 7),
                "模型预测缺陷率": round(prediction, 6),
            })
        effects.append({
            "参数": name, "高风险区间": str(interval), "区间样本量": int(high["size"]),
            "区间缺陷率": round(float(high["mean"]), 5), "总体缺陷率": round(base_rate, 5),
            "风险比": round(risk_ratio, 5), "P值": round(float(p_value), 6),
            "稳定性": round(stable / 100, 4),
            "部分依赖风险差": round(max(pd_predictions) - min(pd_predictions), 5),
        })
        p_values.append(float(p_value))
        scores = _cross_validated_auc(x[[name]], y, splits, lambda: _model(7))
        single_aucs[name] = float(np.mean(scores)) if scores else 0.5

    for row, q_value in zip(effects, _bh_adjust(p_values)):
        row["FDR_Q值"] = round(q_value, 6)
    effects_frame = pd.DataFrame(effects, columns=EFFECT_COLUMNS)

    interactions: list[dict[str, Any]] = []
    interaction_regions: list[dict[str, Any]] = []
    for left, right in combinations(candidates, 2):
        scores = _cross_validated_auc(x[[left, right]], y, splits, lambda: _model(9))
        if not scores:
            continue
        pair_auc = float(np.mean(scores))
        best_single = max(single_aucs.get(left, 0.5), single_aucs.get(right, 0.5))
        gain = pair_auc - best_single
        if gain > 0:
            interactions.append({
                "参数A": left, "参数B": right, "交互强度": round(min(1.0, gain / 0.15), 5),
                "双参数AUC": round(pair_auc, 5), "最佳单参数AUC": round(best_single, 5),
                "AUC增益": round(gain, 5),
            })
            interaction_regions.extend(
                _describe_interaction_regions(x, y, left, right, fisher_exact)
            )
    interaction_frame = pd.DataFrame(interactions, columns=INTERACTION_COLUMNS)
    if not interaction_frame.empty:
        interaction_frame = interaction_frame.sort_values(
            ["交互强度", "双参数AUC"], ascending=False, ignore_index=True
        )
    interaction_region_frame = pd.DataFrame(
        interaction_regions, columns=INTERACTION_REGION_COLUMNS
    )

    validation = pd.DataFrame([{
        "验证方式": validation_method, "有效折数": len(nonlinear_aucs),
        "非线性AUC": round(nonlinear_auc, 5) if pd.notna(nonlinear_auc) else np.nan,
        "AUC标准差": round(float(np.std(nonlinear_aucs)), 5) if nonlinear_aucs else np.nan,
        "线性基线AUC": round(linear_auc, 5) if pd.notna(linear_auc) else np.nan,
        "AUC增益": round(nonlinear_auc - linear_auc, 5)
        if pd.notna(nonlinear_auc) and pd.notna(linear_auc) else np.nan,
        "样本量": len(usable), "正样本": positives, "负样本": len(usable) - positives,
    }], columns=VALIDATION_COLUMNS)

    findings: list[dict[str, Any]] = []
    negatives = len(usable) - positives
    support = sqrt(min(1.0, positives / 20) * min(1.0, negatives / 50))
    for index, effect in effects_frame.iterrows():
        risk_ratio = float(effect["风险比"])
        stability = float(effect["稳定性"])
        q_value = float(effect["FDR_Q值"])
        auc_component = float(np.clip((nonlinear_auc - 0.5) / 0.3, 0, 1)) if pd.notna(nonlinear_auc) else 0
        gain_component = float(np.clip((nonlinear_auc - linear_auc) / 0.10, 0, 1)) \
            if pd.notna(nonlinear_auc) and pd.notna(linear_auc) else 0
        validation_component = 0.6 * auc_component + 0.2 * gain_component + 0.2 * stability
        magnitude = float(np.clip(abs(log2(max(risk_ratio, 1e-6))) / log2(3), 0, 1))
        missing_rate = float(pd.to_numeric(joined[effect["参数"]], errors="coerce").isna().mean())
        quality = float(np.clip(match_rate * (1 - missing_rate), 0, 1))
        score = round(100 * (0.35 * validation_component + 0.30 * magnitude +
                             0.20 * support + 0.15 * quality), 1)
        high_allowed = pd.notna(nonlinear_auc) and nonlinear_auc >= 0.60 and q_value <= 0.05
        level = _level(score, high_allowed)
        findings.append({
            "finding_id": f"PROCESS-{index + 1:04d}", "finding_type": "工艺参数—缺陷",
            "subject": effect["参数"], "related_subject": "",
            "statement": (
                f"{effect['参数']}在{effect['高风险区间']}时，目标缺陷率"
                f"{float(effect['区间缺陷率']):.1%}，总体{float(effect['总体缺陷率']):.1%}，"
                f"风险提升{risk_ratio:.2f}倍；验证AUC {nonlinear_auc:.2f}，"
                f"证据分{score:.0f}/100（{level}）。"
            ),
            "evidence_score": score, "evidence_level": level, "effect_strength": magnitude,
            "sample_size": len(usable), "positive_count": positives, "negative_count": negatives,
            "risk_ratio": risk_ratio, "lift": np.nan,
            "validation_auc": round(nonlinear_auc, 5) if pd.notna(nonlinear_auc) else np.nan,
            "stability": stability, "data_quality": round(quality, 5),
            "warning": "统计关联，尚未证明因果" if high_allowed else "探索性结果，需要更多样本或受控试验",
            "detail_type": "nonlinear_effect", "detail_key": effect["参数"],
        })
    for index, interaction in interaction_frame.head(10).iterrows():
        auc_component = float(np.clip((interaction["双参数AUC"] - 0.5) / 0.3, 0, 1))
        score = round(100 * (0.35 * auc_component + 0.30 * float(interaction["交互强度"]) +
                             0.20 * support + 0.15 * match_rate), 1)
        high_allowed = interaction["双参数AUC"] >= 0.60 and interaction["AUC增益"] >= 0.03
        level = _level(score, bool(high_allowed))
        pair_regions = interaction_region_frame[
            interaction_region_frame["参数A"].eq(interaction["参数A"])
            & interaction_region_frame["参数B"].eq(interaction["参数B"])
            & interaction_region_frame["是否高风险"].eq(True)
        ].sort_values("高风险排名") if not interaction_region_frame.empty else pd.DataFrame()
        shown_regions = pair_regions.head(3)
        if shown_regions.empty:
            region_text = "模型发现两参数联合后区分能力提高，但当前样本不足以定位可靠高风险区间"
        else:
            descriptions = [
                _region_summary(region) for _, region in shown_regions.iterrows()
            ]
            remaining = max(0, len(pair_regions) - len(shown_regions))
            region_text = "；".join(descriptions)
            if remaining:
                region_text += f"；另有{remaining}个高风险组合可在热力图中查看"
        findings.append({
            "finding_id": f"INTERACTION-{index + 1:04d}", "finding_type": "参数交互",
            "subject": interaction["参数A"], "related_subject": interaction["参数B"],
            "statement": (
                f"目标缺陷：{region_text}。联合AUC "
                f"{float(interaction['双参数AUC']):.2f}，比最佳单参数提升"
                f"{float(interaction['AUC增益']):.2f}；证据分{score:.0f}/100（{level}）。"
            ),
            "evidence_score": score, "evidence_level": level,
            "effect_strength": interaction["交互强度"], "sample_size": len(usable),
            "positive_count": positives, "negative_count": negatives,
            "risk_ratio": np.nan, "lift": np.nan, "validation_auc": interaction["双参数AUC"],
            "stability": np.nan, "data_quality": match_rate, "warning": "统计关联，尚未证明因果",
            "detail_type": "interaction", "detail_key": f"{interaction['参数A']}|{interaction['参数B']}",
        })
    finding_frame = pd.DataFrame(findings)
    for name in FINDING_COLUMNS:
        if name not in finding_frame:
            finding_frame[name] = "" if name in {
                "analysis_scope", "target", "source_type", "canonical_code", "defect_name"
            } else np.nan
    finding_frame = finding_frame[FINDING_COLUMNS]
    return {
        "nonlinear_importance": importance,
        "nonlinear_effects": effects_frame,
        "interactions": interaction_frame,
        "interaction_regions": interaction_region_frame,
        "risk_curves": pd.DataFrame(curve_rows, columns=CURVE_COLUMNS),
        "model_validation": validation,
        "findings": finding_frame,
        "summary": {
            "nonlinear_status": "ok",
            "nonlinear_auc": None if pd.isna(nonlinear_auc) else round(nonlinear_auc, 4),
            "linear_baseline_auc": None if pd.isna(linear_auc) else round(linear_auc, 4),
            "valid_validation_folds": len(nonlinear_aucs),
            "nonlinear_feature_count": len(candidates),
        },
    }


def enrich_findings(frame: pd.DataFrame, **metadata: str) -> pd.DataFrame:
    result = frame.copy()
    for name, value in metadata.items():
        result[name] = value
    source_caption = {
        "VI_BLOCK": "VI", "VI_FAILURE": "VI", "AOI_FAILURE": "AOI", "IMAGE": "图片",
    }.get(str(metadata.get("source_type", "")), str(metadata.get("source_type", "")))
    code = str(metadata.get("canonical_code", "")).strip()
    raw_defect_name = metadata.get("defect_name", "")
    defect_name = (
        "" if raw_defect_name is None or pd.isna(raw_defect_name)
        else str(raw_defect_name).strip()
    )
    if code or defect_name:
        label = defect_name or "目标缺陷"
        suffix = " ".join(part for part in (source_caption, code) if part)
        target_caption = f"{label}（{suffix}）" if suffix else label
        process_mask = result.get("detail_type", pd.Series(index=result.index, dtype=str)).isin(
            ["nonlinear_effect", "interaction"]
        )
        for index in result.index[process_mask]:
            raw_statement = result.at[index, "statement"]
            statement = "" if raw_statement is None or pd.isna(raw_statement) else str(raw_statement)
            prefix = f"目标缺陷：{target_caption}。"
            if statement.startswith("目标缺陷："):
                statement = prefix + statement.removeprefix("目标缺陷：")
            elif not statement.startswith(prefix):
                statement = prefix + statement
            result.at[index, "statement"] = statement
    return result.reindex(columns=FINDING_COLUMNS)


def sort_findings(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return empty_findings()
    result = frame.copy()
    for name in ("evidence_score", "effect_strength", "sample_size", "data_quality"):
        result[name] = pd.to_numeric(result[name], errors="coerce").fillna(0)
    return result.sort_values(
        ["evidence_score", "effect_strength", "sample_size", "data_quality"],
        ascending=False, kind="stable", ignore_index=True,
    )


def _bootstrap_lift_stability(support: int, left_total: int, right_total: int,
                              population: int, seed: int) -> float:
    population = max(population, left_total, right_total, 1)
    joint = max(0, min(support, left_total, right_total))
    counts = np.array([
        joint, max(0, left_total - joint), max(0, right_total - joint),
        max(0, population - left_total - right_total + joint),
    ], dtype=float)
    probabilities = counts / max(1.0, counts.sum())
    rng = np.random.default_rng(seed)
    stable = 0
    for _ in range(100):
        sample = rng.multinomial(population, probabilities)
        sampled_joint, sampled_left_only, sampled_right_only, _ = sample
        sampled_left = sampled_joint + sampled_left_only
        sampled_right = sampled_joint + sampled_right_only
        expected = sampled_left * sampled_right / population
        if expected > 0 and sampled_joint / expected > 1:
            stable += 1
    return stable / 100


def build_unified_findings(
    *, process_findings: pd.DataFrame | None = None,
    code_space: pd.DataFrame | None = None,
    cooccurrence: pd.DataFrame | None = None,
    transitions: pd.DataFrame | None = None,
    trajectories: pd.DataFrame | None = None,
    attribution: pd.DataFrame | None = None,
    conflicts: pd.DataFrame | None = None,
    product_count: int = 0,
) -> pd.DataFrame:
    """Normalize heterogeneous evidence into one deterministic ranked list."""
    parts: list[pd.DataFrame] = []
    if process_findings is not None and not process_findings.empty:
        parts.append(process_findings.reindex(columns=FINDING_COLUMNS))
    rows: list[dict[str, Any]] = []

    for index, item in (code_space if code_space is not None else pd.DataFrame()).iterrows():
        support = int(item.get("support_products", 0) or 0)
        code_total = int(item.get("code_products", 0) or 0)
        spatial_total = int(item.get("spatial_products", 0) or 0)
        lift = float(item.get("lift", 0) or 0)
        stability = _bootstrap_lift_stability(
            support, code_total, spatial_total, product_count, 4200 + int(index)
        )
        magnitude = float(np.clip(log2(max(lift, 1)) / log2(3), 0, 1))
        support_score = sqrt(min(1, support / 20) * min(1, max(code_total, spatial_total) / 50))
        score = round(100 * (0.35 * stability + 0.30 * magnitude + 0.20 * support_score + 0.15), 1)
        high_allowed = support >= 5 and lift >= 1.5
        level = _level(score, high_allowed)
        rows.append({
            "finding_id": f"CODE-SPACE-{index + 1:04d}", "finding_type": "代码—空间",
            "analysis_scope": item.get("analysis_scope", ""), "source_type": item.get("source_type", ""),
            "canonical_code": item.get("canonical_code", ""), "subject": item.get("canonical_code", ""),
            "related_subject": item.get("spatial_id", ""),
            "statement": (
                f"缺陷代码{item.get('canonical_code', '')}与{item.get('spatial_type', '空间表现')}"
                f"{item.get('spatial_id', '')}共同出现在{support}个产品，提升度{lift:.2f}；"
                f"证据分{score:.0f}/100（{level}）。"
            ),
            "evidence_score": score, "evidence_level": level, "effect_strength": magnitude,
            "sample_size": max(code_total, spatial_total), "positive_count": support,
            "negative_count": max(0, product_count - support), "lift": lift,
            "stability": stability, "data_quality": 1.0, "warning": "统计关联，尚未证明因果",
            "detail_type": "code_space", "detail_key": item.get("spatial_id", ""),
        })

    for index, item in (cooccurrence if cooccurrence is not None else pd.DataFrame()).iterrows():
        support = int(item.get("共现产品数", 0) or 0)
        lift = float(item.get("提升度", 0) or 0)
        left_probability = float(item.get("P(B|A)", 0) or 0)
        right_probability = float(item.get("P(A|B)", 0) or 0)
        left_total = round(support / left_probability) if left_probability else support
        right_total = round(support / right_probability) if right_probability else support
        stability = _bootstrap_lift_stability(
            support, left_total, right_total, product_count, 5200 + int(index)
        )
        magnitude = float(np.clip(log2(max(lift, 1)) / log2(3), 0, 1))
        support_score = min(1.0, support / 20)
        score = round(100 * (0.35 * stability + 0.30 * magnitude + 0.20 * support_score + 0.15), 1)
        level = _level(score, support >= 5 and lift >= 1.5)
        rows.append({
            "finding_id": f"COOCCURRENCE-{index + 1:04d}", "finding_type": "缺陷共现",
            "analysis_scope": item.get("analysis_scope", ""), "subject": item.get("缺陷A", ""),
            "related_subject": item.get("缺陷B", ""),
            "statement": f"缺陷{item.get('缺陷A', '')}与{item.get('缺陷B', '')}共现{support}次，提升度{lift:.2f}；证据分{score:.0f}/100（{level}）。",
            "evidence_score": score, "evidence_level": level, "effect_strength": magnitude,
            "sample_size": product_count, "positive_count": support,
            "negative_count": max(0, product_count - support), "lift": lift,
            "stability": stability, "data_quality": 1.0, "warning": "统计关联，尚未证明因果",
            "detail_type": "cooccurrence", "detail_key": f"{item.get('缺陷A', '')}|{item.get('缺陷B', '')}",
        })

    for index, item in (transitions if transitions is not None else pd.DataFrame()).iterrows():
        support = int(item.get("转移次数", 0) or 0)
        probability = float(item.get("条件概率", 0) or 0)
        stability = min(1.0, support / 10)
        score = round(100 * (0.35 * stability + 0.30 * probability +
                             0.20 * min(1, support / 20) + 0.15), 1)
        level = _level(score, support >= 5 and probability >= 0.7)
        rows.append({
            "finding_id": f"TRANSITION-{index + 1:04d}", "finding_type": "缺陷序列",
            "analysis_scope": item.get("analysis_scope", ""), "subject": item.get("前一缺陷", ""),
            "related_subject": item.get("后一缺陷", ""),
            "statement": f"缺陷{item.get('前一缺陷', '')}后出现{item.get('后一缺陷', '')}共{support}次，条件概率{probability:.1%}；证据分{score:.0f}/100（{level}）。",
            "evidence_score": score, "evidence_level": level, "effect_strength": probability,
            "sample_size": product_count, "positive_count": support,
            "negative_count": max(0, product_count - support), "stability": stability,
            "data_quality": 1.0, "warning": "序列关系不等于因果关系",
            "detail_type": "transition", "detail_key": f"{item.get('前一缺陷', '')}|{item.get('后一缺陷', '')}",
        })

    for index, item in (trajectories if trajectories is not None else pd.DataFrame()).iterrows():
        support = int(item.get("occurrence_count", 0) or 0)
        registration = 1.0 if str(item.get("registration_quality", "")) == "high" else 0.4
        magnitude = min(1.0, abs(float(item.get("spearman_order_x", 0) or 0)))
        stability = min(1.0, support / 10) * registration
        score = round(100 * (0.35 * stability + 0.30 * magnitude +
                             0.20 * min(1, support / 20) + 0.15 * registration), 1)
        level = _level(score, support >= 5 and registration == 1.0)
        rows.append({
            "finding_id": f"TRAJECTORY-{index + 1:04d}", "finding_type": "空间轨迹",
            "analysis_scope": item.get("analysis_scope", ""), "subject": item.get("trajectory_id", ""),
            "related_subject": item.get("station_id", ""),
            "statement": f"工站{item.get('station_id', '')}发现{item.get('pattern_type', '')}轨迹，覆盖{support}个产品；证据分{score:.0f}/100（{level}）。",
            "evidence_score": score, "evidence_level": level, "effect_strength": magnitude,
            "sample_size": product_count, "positive_count": support,
            "negative_count": max(0, product_count - support), "stability": stability,
            "data_quality": registration, "warning": "空间规律尚未证明设备因果",
            "detail_type": "trajectory", "detail_key": item.get("trajectory_id", ""),
        })

    for index, item in (attribution if attribution is not None else pd.DataFrame()).iterrows():
        level_text = str(item.get("association_level", ""))
        magnitude = 1.0 if "较强" in level_text else 0.65 if "疑似" in level_text else 0.35
        score = round(100 * (0.35 * magnitude + 0.30 * magnitude + 0.20 * 0.5 + 0.15 * magnitude), 1)
        level = _level(score, "较强" in level_text)
        rows.append({
            "finding_id": f"STATION-{index + 1:04d}", "finding_type": "工站归因证据",
            "analysis_scope": item.get("analysis_scope", ""), "subject": item.get("station_id", ""),
            "related_subject": item.get("evidence_id", ""),
            "statement": f"工站{item.get('station_id', '')}被评为“{level_text}”：{item.get('reason', '')}；证据分{score:.0f}/100（{level}）。",
            "evidence_score": score, "evidence_level": level, "effect_strength": magnitude,
            "sample_size": product_count, "positive_count": np.nan, "negative_count": np.nan,
            "stability": magnitude, "data_quality": magnitude,
            "warning": item.get("equipment_conclusion", "设备原因待确认"),
            "detail_type": "attribution", "detail_key": item.get("evidence_id", ""),
        })

    conflict_frame = conflicts if conflicts is not None else pd.DataFrame()
    if not conflict_frame.empty and "comparison_status" in conflict_frame:
        conflicts_only = conflict_frame[conflict_frame["comparison_status"].astype(str).eq("label_conflict")]
        if not conflicts_only.empty:
            count = len(conflicts_only)
            rate = count / max(1, product_count)
            score = round(100 * (0.55 * min(1, count / 10) + 0.30 * min(1, rate / 0.10) + 0.15), 1)
            level = _level(score, count >= 5)
            rows.append({
                "finding_id": "DATA-QUALITY-0001", "finding_type": "AOI—VI数据质量",
                "subject": "AOI—VI标签冲突", "related_subject": "",
                "statement": f"发现{count}个AOI—VI标签冲突，占产品{rate:.1%}；证据分{score:.0f}/100（{level}）。",
                "evidence_score": score, "evidence_level": level, "effect_strength": min(1, rate / 0.10),
                "sample_size": product_count, "positive_count": count,
                "negative_count": max(0, product_count - count), "stability": min(1, count / 10),
                "data_quality": 1 - min(1, rate), "warning": "数据一致性问题，不是工艺因果结论",
                "detail_type": "conflict", "detail_key": "label_conflict",
            })

    if rows:
        other = pd.DataFrame(rows)
        for name in FINDING_COLUMNS:
            if name not in other:
                other[name] = "" if name in {
                    "analysis_scope", "target", "source_type", "canonical_code", "defect_name"
                } else np.nan
        parts.append(other[FINDING_COLUMNS])
    return sort_findings(pd.concat(parts, ignore_index=True) if parts else empty_findings())
