"""本文件测试工艺参数精确关联、统计指标和时间留出验证。"""

import numpy as np
import pandas as pd

from src.process_relationships import analyze_process_relationships


def test_process_relationship_mvp_uses_exact_key() -> None:
    orders = np.arange(1, 61)
    products = pd.DataFrame({
        "global_order": orders,
        "order_code": [f"P{value:03d}" for value in orders],
        "camera": "5S",
        "a_image_path": "a.png",
        "e_image_path": "e.png",
    })
    parameters = pd.DataFrame({
        "global_order": orders,
        "temperature": 20 + orders * 0.2,
        "pressure": np.sin(orders),
    })
    defect_orders = orders[(orders > 20) & (orders % 3 != 0)]
    defects = pd.DataFrame({
        "global_order": defect_orders,
        "center_x": 1.0,
        "center_y": 1.0,
        "component_area": 10,
    })
    result = analyze_process_relationships(products, defects, parameters)
    assert result.summary["join_key"] == "global_order"
    assert result.summary["match_rate"] == 1.0
    assert set(result.parameter_metrics["参数"]) == {"temperature", "pressure"}
    assert not result.binned_rates.empty
    assert not result.model_importance.empty


def test_process_relationship_time_matching_marks_quality() -> None:
    times = pd.date_range("2026-01-01", periods=5, freq="min")
    products = pd.DataFrame({
        "global_order": range(1, 6),
        "production_timestamp": times,
        "camera": "5S",
        "a_image_path": "a.png",
        "e_image_path": "e.png",
    })
    parameters = pd.DataFrame({
        "timestamp": times + pd.Timedelta(seconds=10),
        "temperature": range(5),
    })
    defects = pd.DataFrame(columns=["global_order", "center_x", "center_y", "component_area"])
    result = analyze_process_relationships(products, defects, parameters, tolerance_seconds=15)
    assert result.summary["matched_count"] == 5
    assert set(result.joined["match_quality"]) == {"time_nearest"}
