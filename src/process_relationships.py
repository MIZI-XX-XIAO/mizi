"""本文件执行工艺参数与缺陷的本地可解释关联分析。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from .data_quality import DataQualityReport, validate_process_parameters, validate_products


EXACT_KEYS = ("product_id", "order_code", "dmc_raw", "global_order")
TIME_KEYS = ("production_timestamp", "timestamp")


@dataclass
class ProcessRelationshipResult:
    joined: pd.DataFrame
    parameter_metrics: pd.DataFrame
    binned_rates: pd.DataFrame
    model_importance: pd.DataFrame
    summary: dict[str, Any]
    quality_reports: list[DataQualityReport]


def _auc_score(y_true: np.ndarray, scores: np.ndarray) -> float | None:
    positives = int(y_true.sum())
    negatives = len(y_true) - positives
    if positives == 0 or negatives == 0:
        return None
    ranks = pd.Series(scores).rank(method="average").to_numpy()
    return float((ranks[y_true == 1].sum() - positives * (positives + 1) / 2) / (positives * negatives))


def _join_tables(products: pd.DataFrame, parameters: pd.DataFrame,
                 tolerance_seconds: int) -> tuple[pd.DataFrame, str]:
    for key in EXACT_KEYS:
        if key in products and key in parameters:
            numeric_columns = parameters.select_dtypes(include="number").columns.tolist()
            aggregations = {column: "mean" for column in numeric_columns if column != key}
            for column in parameters.columns:
                if column != key and column not in aggregations:
                    aggregations[column] = "first"
            right = parameters.groupby(key, as_index=False).agg(aggregations) if parameters[key].duplicated().any() else parameters
            joined = products.merge(right, on=key, how="left", suffixes=("", "_process"), indicator=True)
            joined["match_quality"] = joined["_merge"].map({"both": "exact", "left_only": "unmatched"})
            return joined.drop(columns="_merge"), key

    product_time = next((key for key in TIME_KEYS if key in products), None)
    parameter_time = next((key for key in TIME_KEYS if key in parameters), None)
    if product_time and parameter_time:
        left = products.copy()
        right = parameters.copy()
        left["_join_time"] = pd.to_datetime(left[product_time], errors="coerce")
        right["_join_time"] = pd.to_datetime(right[parameter_time], errors="coerce")
        left = left.sort_values("_join_time")
        right = right.dropna(subset=["_join_time"]).sort_values("_join_time")
        joined = pd.merge_asof(
            left, right, on="_join_time", direction="nearest",
            tolerance=pd.Timedelta(seconds=tolerance_seconds), suffixes=("", "_process")
        )
        process_columns = [column for column in parameters.columns if column != parameter_time]
        joined["match_quality"] = np.where(
            joined[process_columns].notna().any(axis=1), "time_nearest", "unmatched"
        )
        return joined.drop(columns="_join_time"), f"{product_time}≈{parameter_time}"
    raise ValueError("产品表与工艺参数表没有共同产品标识，也没有可匹配的时间戳")


def _fit_logistic_importance(frame: pd.DataFrame, features: list[str],
                             target: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    usable = frame[features + [target]].copy()
    for column in features:
        usable[column] = pd.to_numeric(usable[column], errors="coerce")
        usable[column] = usable[column].fillna(usable[column].median())
    usable = usable.dropna(subset=[target])
    if len(usable) < 20 or usable[target].nunique() < 2:
        return pd.DataFrame(columns=["参数", "标准化系数", "相对重要性"]), {
            "validation_method": "样本不足", "validation_auc": None
        }
    batch_column = next((name for name in ("batch", "batch_id") if name in frame), None)
    if batch_column and frame.loc[usable.index, batch_column].nunique() >= 2:
        batches = frame.loc[usable.index, batch_column].drop_duplicates().tolist()
        test_batches = set(batches[max(1, int(len(batches) * 0.8)):])
        is_test = frame.loc[usable.index, batch_column].isin(test_batches)
        train, test = usable.loc[~is_test], usable.loc[is_test]
        validation_method = "按批次后20%留出"
    else:
        split = max(1, min(len(usable) - 1, int(len(usable) * 0.8)))
        train, test = usable.iloc[:split], usable.iloc[split:]
        validation_method = "按生产顺序后20%留出"
    if train[target].nunique() < 2 or test[target].nunique() < 2:
        train, test = usable, usable.iloc[0:0]
        validation_method = "全量拟合（验证集类别不足）"
    mean = train[features].mean()
    std = train[features].std(ddof=0).replace(0, 1.0)
    x_train = ((train[features] - mean) / std).to_numpy(dtype=float)
    y_train = train[target].to_numpy(dtype=float)
    x_train = np.column_stack([np.ones(len(x_train)), x_train])
    weights = np.zeros(x_train.shape[1], dtype=float)
    for _ in range(800):
        logits = np.clip(x_train @ weights, -30, 30)
        predicted = 1.0 / (1.0 + np.exp(-logits))
        gradient = x_train.T @ (predicted - y_train) / len(y_train)
        gradient[1:] += 0.01 * weights[1:]
        weights -= 0.08 * gradient
    absolute = np.abs(weights[1:])
    total = float(absolute.sum()) or 1.0
    importance = pd.DataFrame({
        "参数": features,
        "标准化系数": np.round(weights[1:], 5),
        "相对重要性": np.round(absolute / total, 5),
    }).sort_values("相对重要性", ascending=False, ignore_index=True)
    auc = None
    if not test.empty:
        x_test = ((test[features] - mean) / std).to_numpy(dtype=float)
        scores = 1.0 / (1.0 + np.exp(-np.clip(
            np.column_stack([np.ones(len(x_test)), x_test]) @ weights, -30, 30
        )))
        auc = _auc_score(test[target].to_numpy(dtype=int), scores)
    return importance, {
        "validation_method": validation_method,
        "validation_auc": None if auc is None else round(auc, 4),
    }


def analyze_process_relationships(
    products: pd.DataFrame,
    defects: pd.DataFrame,
    parameters: pd.DataFrame,
    tolerance_seconds: int = 60,
) -> ProcessRelationshipResult:
    product_report = validate_products(products)
    parameter_report = validate_process_parameters(parameters)
    if not product_report.is_valid:
        raise ValueError("产品表校验失败：" + "；".join(product_report.errors))
    if not parameter_report.is_valid:
        raise ValueError("工艺参数表校验失败：" + "；".join(parameter_report.errors))

    joined, join_key = _join_tables(products, parameters, tolerance_seconds)
    defect_counts = defects.groupby("global_order").size().rename("detected_defect_count")
    joined = joined.join(defect_counts, on="global_order")
    joined["detected_defect_count"] = joined["detected_defect_count"].fillna(0).astype(int)
    joined["has_detected_defect"] = (joined["detected_defect_count"] > 0).astype(int)

    excluded = {
        "global_order", "has_defect", "defect_count", "detected_defect_count",
        "has_detected_defect", "image_width", "image_height",
    }
    parameter_names = [
        column for column in parameters.select_dtypes(include="number").columns
        if column not in excluded and column in joined
    ]
    metric_rows: list[dict[str, Any]] = []
    bin_rows: list[dict[str, Any]] = []
    target = joined["has_detected_defect"]
    for name in parameter_names:
        values = pd.to_numeric(joined[name], errors="coerce")
        valid = values.notna()
        defect_values = values[valid & target.eq(1)]
        normal_values = values[valid & target.eq(0)]
        spearman = (
            values[valid].rank(method="average").corr(
                target[valid].rank(method="average"), method="pearson"
            )
            if valid.sum() > 1 and values[valid].nunique() > 1 and target[valid].nunique() > 1
            else np.nan
        )
        pooled = values[valid].std(ddof=1)
        effect = (
            (defect_values.mean() - normal_values.mean()) / pooled
            if pd.notna(pooled) and pooled > 0 and not defect_values.empty and not normal_values.empty
            else np.nan
        )
        metric_rows.append({
            "参数": name,
            "样本量": int(valid.sum()),
            "缺失率": round(float(1 - valid.mean()), 4),
            "缺陷均值": round(float(defect_values.mean()), 5) if not defect_values.empty else np.nan,
            "正常均值": round(float(normal_values.mean()), 5) if not normal_values.empty else np.nan,
            "Spearman相关": round(float(spearman), 5) if pd.notna(spearman) else np.nan,
            "标准化效应量": round(float(effect), 5) if pd.notna(effect) else np.nan,
            "最小值": round(float(values.min()), 5) if valid.any() else np.nan,
            "最大值": round(float(values.max()), 5) if valid.any() else np.nan,
        })
        if valid.sum() >= 5 and values[valid].nunique() > 1:
            bins = pd.qcut(values[valid], q=min(5, values[valid].nunique()), duplicates="drop")
            grouped = joined.loc[valid].assign(_bin=bins).groupby("_bin", observed=True)
            for interval, group in grouped:
                bin_rows.append({
                    "参数": name, "区间": str(interval), "样本量": len(group),
                    "缺陷数量": int(group["has_detected_defect"].sum()),
                    "缺陷率": round(float(group["has_detected_defect"].mean()), 5),
                })
    metrics = pd.DataFrame(metric_rows)
    if not metrics.empty:
        metrics["_rank"] = metrics["Spearman相关"].abs().fillna(0)
        metrics = metrics.sort_values("_rank", ascending=False).drop(columns="_rank").reset_index(drop=True)
    importance, model_summary = _fit_logistic_importance(
        joined.sort_values("global_order"), parameter_names, "has_detected_defect"
    )
    matched = int(joined["match_quality"].ne("unmatched").sum())
    summary = {
        "join_key": join_key,
        "product_count": len(joined),
        "matched_count": matched,
        "match_rate": round(matched / len(joined), 4) if len(joined) else 0.0,
        "parameter_count": len(parameter_names),
        "defective_product_count": int(joined["has_detected_defect"].sum()),
        "warning": "统计关联不等于因果关系；结论需结合工艺机理和受控实验验证。",
        **model_summary,
    }
    return ProcessRelationshipResult(
        joined=joined,
        parameter_metrics=metrics,
        binned_rates=pd.DataFrame(bin_rows),
        model_importance=importance,
        summary=summary,
        quality_reports=[product_report, parameter_report],
    )
