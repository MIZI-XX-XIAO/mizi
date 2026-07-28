"""本文件增量分析缺陷的空间重复、周期规律和连续异常，并生成逐片预警事件。"""

from dataclasses import dataclass, field
from math import floor, hypot
from typing import Any, Callable

import pandas as pd


@dataclass
class SpatialCluster:
    cluster_id: str
    points: list[tuple[float, float]] = field(default_factory=list)
    orders: list[int] = field(default_factory=list)

    @property
    def center(self) -> tuple[float, float]:
        return (sum(point[0] for point in self.points) / len(self.points),
                sum(point[1] for point in self.points) / len(self.points))

    @property
    def unique_orders(self) -> list[int]:
        return sorted(set(self.orders))

    def add(self, x: float, y: float, order: int) -> None:
        self.points.append((x, y)); self.orders.append(order)


def fit_period(orders: list[int], config: dict[str, Any]) -> dict[str, Any] | None:
    """Find the fundamental period by precision and expected-slot coverage."""
    values = sorted(set(map(int, orders)))
    if len(values) < int(config["minimum_repeat_occurrences"]):
        return None
    best: dict[str, Any] | None = None
    tolerance = int(config["period_order_tolerance"])
    for period in range(int(config["minimum_period"]), int(config["maximum_period"]) + 1):
        residues = sorted({value % period for value in values})
        for residue in residues:
            matched = [value for value in values if min((value - residue) % period, (residue - value) % period) <= tolerance]
            if len(matched) < int(config["minimum_repeat_occurrences"]):
                continue
            first, last = min(matched), max(matched)
            expected_slots = floor((last - first) / period) + 1
            precision = len(matched) / len(values)
            coverage = min(1.0, len(matched) / expected_slots)
            confidence = 2 * precision * coverage / (precision + coverage) if precision + coverage else 0.0
            candidate = {"period": period, "phase_start": first, "precision": precision,
                         "coverage": coverage, "confidence": confidence, "matched_orders": matched}
            if precision < float(config["minimum_period_precision"]) or coverage < float(config["minimum_period_coverage"]):
                continue
            # Confidence dominates; coverage penalizes divisor periods with many empty slots.
            key = (confidence, coverage, precision, -period)
            if best is None or key > best["_key"]:
                candidate["_key"] = key; best = candidate
    if best is not None:
        best.pop("_key", None)
    return best


def longest_consecutive_run(orders: list[int]) -> list[int]:
    values = sorted(set(orders))
    best: list[int] = []
    current: list[int] = []
    for value in values:
        if not current or value == current[-1] + 1:
            current.append(value)
        else:
            if len(current) > len(best): best = current
            current = [value]
    return current if len(current) > len(best) else best


class OnlinePatternEngine:
    """Assign detections incrementally and emit warnings without future leakage."""

    def __init__(self, config: dict[str, Any],
                 alert_callback: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.config = config
        self.clusters: list[SpatialCluster] = []
        self.alerts: list[dict[str, Any]] = []
        self.active_periods: dict[str, dict[str, Any]] = {}
        self.burst_alerted: set[str] = set()
        self.fixed_repeat_alerted: set[str] = set()
        self.alert_callback = alert_callback

    def _assign(self, row: Any) -> str:
        x, y = float(row.center_x_norm), float(row.center_y_norm)
        radius = float(self.config["spatial_cluster_radius_norm"])
        candidates = [(hypot(x - cluster.center[0], y - cluster.center[1]), cluster) for cluster in self.clusters]
        candidates = [(distance, cluster) for distance, cluster in candidates if distance <= radius]
        if candidates:
            cluster = min(candidates, key=lambda item: (item[0], item[1].cluster_id))[1]
        else:
            cluster = SpatialCluster(f"C{len(self.clusters) + 1:03d}")
            self.clusters.append(cluster)
        cluster.add(x, y, int(row.global_order))
        return cluster.cluster_id

    def _alert(self, order: int, alert_type: str, cluster: SpatialCluster, severity: str,
               confidence: float, predicted_order: int | None, evidence: list[int], message: str) -> None:
        alert = {
            "alert_id": f"A{len(self.alerts) + 1:04d}", "alert_at_order": order,
            "alert_type": alert_type, "severity": severity, "cluster_id": cluster.cluster_id,
            "confidence": round(confidence, 4), "predicted_order": predicted_order,
            "evidence_orders": ";".join(map(str, evidence)), "message": message,
        }
        self.alerts.append(alert)
        if self.alert_callback is not None:
            self.alert_callback(alert.copy())

    def process(self, products: pd.DataFrame, detections: pd.DataFrame,
                progress_callback: Callable[[int, int, int], None] | None = None,
                cancel_check: Callable[[], bool] | None = None
                ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        assigned = detections.copy()
        by_order = {int(order): group for order, group in assigned.groupby("global_order")}
        index_lookup = {str(row.detected_id): index for index, row in assigned.iterrows()}
        lead = int(self.config["warning_lead_products"])
        miss_tolerance = int(self.config["missing_order_tolerance"])
        ordered_products = products.sort_values("global_order")["global_order"].astype(int).tolist()
        for processed, current_order in enumerate(ordered_products, 1):
            if cancel_check is not None and cancel_check():
                raise InterruptedError("analysis cancelled")
            changed: set[str] = set()
            if current_order in by_order:
                for row in by_order[current_order].itertuples(index=False):
                    cluster_id = self._assign(row)
                    assigned.at[index_lookup[str(row.detected_id)], "cluster_id"] = cluster_id
                    changed.add(cluster_id)

            for cluster_id, active in list(self.active_periods.items()):
                cluster = next(item for item in self.clusters if item.cluster_id == cluster_id)
                predicted = int(active["next_expected_order"])
                if current_order == predicted and current_order in cluster.unique_orders:
                    active["next_expected_order"] = predicted + int(active["period"])
                    predicted = int(active["next_expected_order"])
                elif current_order > predicted + miss_tolerance:
                    self._alert(current_order, "expected_occurrence_missing", cluster, "warning",
                                float(active["confidence"]), predicted, cluster.unique_orders,
                                f"Expected periodic occurrence at product {predicted} was not observed")
                    while int(active["next_expected_order"]) < current_order:
                        active["next_expected_order"] += int(active["period"])
                    predicted = int(active["next_expected_order"])
                if current_order == predicted - lead:
                    self._alert(current_order, "upcoming_periodic_defect", cluster, "warning",
                                float(active["confidence"]), predicted, cluster.unique_orders,
                                f"Periodic defect is expected at product {predicted}")

            for cluster in [item for item in self.clusters if item.cluster_id in changed]:
                run = longest_consecutive_run(cluster.unique_orders)
                if len(run) >= int(self.config["burst_minimum_length"]) and cluster.cluster_id not in self.burst_alerted:
                    self.burst_alerted.add(cluster.cluster_id)
                    self._alert(current_order, "burst_detected", cluster, "critical", 1.0, None, run,
                                f"Defect repeated for {len(run)} consecutive products")
                fit = fit_period(cluster.unique_orders, self.config)
                if fit and cluster.cluster_id not in self.active_periods:
                    next_order = int(fit["phase_start"])
                    while next_order <= current_order: next_order += int(fit["period"])
                    fit["next_expected_order"] = next_order
                    fit["confirmed_at_order"] = current_order
                    self.active_periods[cluster.cluster_id] = fit
                    self._alert(current_order, "periodic_pattern_confirmed", cluster, "warning",
                                float(fit["confidence"]), next_order, list(fit["matched_orders"]),
                                f"Periodic defect pattern detected: period={fit['period']}")
                elif fit and cluster.cluster_id in self.active_periods:
                    active = self.active_periods[cluster.cluster_id]
                    for key in ("precision", "coverage", "confidence", "matched_orders"):
                        active[key] = fit[key]
                elif (len(cluster.unique_orders) >= int(self.config["minimum_repeat_occurrences"])
                      and len(run) < int(self.config["burst_minimum_length"])
                      and cluster.cluster_id not in self.fixed_repeat_alerted):
                    self.fixed_repeat_alerted.add(cluster.cluster_id)
                    self._alert(current_order, "fixed_position_repeat_confirmed", cluster, "notice", 0.5,
                                None, cluster.unique_orders, "Defect repeatedly appeared at a fixed position")
            if progress_callback is not None:
                progress_callback(current_order, processed, len(ordered_products))

        cluster_rows = []
        pattern_rows = []
        for cluster in self.clusters:
            center_x, center_y = cluster.center
            orders = cluster.unique_orders
            run = longest_consecutive_run(orders)
            fit = fit_period(orders, self.config)
            cluster_rows.append({
                "cluster_id": cluster.cluster_id, "center_x_norm": round(center_x, 7),
                "center_y_norm": round(center_y, 7), "detection_count": len(cluster.points),
                "product_count": len(orders), "first_order": min(orders), "last_order": max(orders),
                "orders": ";".join(map(str, orders)),
            })
            if fit:
                active = self.active_periods.get(cluster.cluster_id, {})
                expected = list(range(int(fit["phase_start"]), max(orders) + 1, int(fit["period"])))
                missing = sorted(set(expected) - set(fit["matched_orders"]))
                pattern_rows.append({
                    "pattern_id": f"P{len(pattern_rows) + 1:03d}", "pattern_type": "periodic",
                    "cluster_id": cluster.cluster_id, "occurrence_count": len(orders),
                    "period": int(fit["period"]), "phase_start": int(fit["phase_start"]),
                    "confidence": round(float(fit["confidence"]), 4),
                    "precision": round(float(fit["precision"]), 4), "coverage": round(float(fit["coverage"]), 4),
                    "first_order": min(orders), "last_order": max(orders),
                    "confirmed_at_order": active.get("confirmed_at_order"),
                    "next_expected_order": active.get("next_expected_order"),
                    "observed_orders": ";".join(map(str, orders)), "inferred_missing_orders": ";".join(map(str, missing)),
                })
            elif len(run) >= int(self.config["burst_minimum_length"]):
                pattern_rows.append({
                    "pattern_id": f"P{len(pattern_rows) + 1:03d}", "pattern_type": "burst",
                    "cluster_id": cluster.cluster_id, "occurrence_count": len(run), "period": None,
                    "phase_start": run[0], "confidence": 1.0, "precision": 1.0, "coverage": 1.0,
                    "first_order": run[0], "last_order": run[-1], "confirmed_at_order": run[-1],
                    "next_expected_order": None, "observed_orders": ";".join(map(str, run)), "inferred_missing_orders": "",
                })
            elif len(orders) >= int(self.config["minimum_repeat_occurrences"]):
                pattern_rows.append({
                    "pattern_id": f"P{len(pattern_rows) + 1:03d}", "pattern_type": "fixed_position_repeat",
                    "cluster_id": cluster.cluster_id, "occurrence_count": len(orders), "period": None,
                    "phase_start": None, "confidence": None, "precision": None, "coverage": None,
                    "first_order": min(orders), "last_order": max(orders), "confirmed_at_order": None,
                    "next_expected_order": None, "observed_orders": ";".join(map(str, orders)), "inferred_missing_orders": "",
                })
        return assigned, pd.DataFrame(cluster_rows), pd.DataFrame(pattern_rows), pd.DataFrame(self.alerts)
