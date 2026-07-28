"""本文件测试周期24的主动发现、漏检容忍、连续异常识别和在线预警时机。"""

from pathlib import Path

import pandas as pd
import yaml

from src.pattern_analyzer import OnlinePatternEngine, fit_period


CONFIG = Path(__file__).resolve().parents[1] / "config" / "analysis_config.yaml"


def _config() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def test_period_fit_prefers_24_over_divisors_and_tolerates_missing() -> None:
    orders = [value for value in range(13, 501, 24) if value not in {85, 373}]
    result = fit_period(orders, _config())
    assert result is not None
    assert result["period"] == 24
    assert result["phase_start"] == 13


def test_online_replay_discovers_period_burst_and_warnings() -> None:
    config = _config()
    products = pd.DataFrame({"global_order": range(1, 501)})
    periodic = [value for value in range(13, 501, 24) if value not in {85, 373}]
    rows = []
    for index, order in enumerate(periodic, 1):
        rows.append({"detected_id": f"X{index:04d}", "global_order": order,
                     "center_x_norm": 0.78, "center_y_norm": 0.23, "cluster_id": ""})
    for order in range(280, 284):
        rows.append({"detected_id": f"X{len(rows)+1:04d}", "global_order": order,
                     "center_x_norm": 0.27, "center_y_norm": 0.70, "cluster_id": ""})
    assigned, clusters, patterns, alerts = OnlinePatternEngine(config).process(products, pd.DataFrame(rows))
    periodic_pattern = patterns[patterns.pattern_type == "periodic"].iloc[0]
    assert int(periodic_pattern.period) == 24
    assert periodic_pattern.inferred_missing_orders == "85;373"
    assert not patterns[patterns.pattern_type == "burst"].empty
    assert 109 in alerts.loc[alerts.alert_type == "periodic_pattern_confirmed", "alert_at_order"].tolist()
    assert 370 in alerts.loc[alerts.alert_type == "upcoming_periodic_defect", "alert_at_order"].tolist()
    assert 374 in alerts.loc[alerts.alert_type == "expected_occurrence_missing", "alert_at_order"].tolist()
    assert len(assigned) == len(rows)
    assert len(clusters[clusters.product_count >= 4]) == 2
