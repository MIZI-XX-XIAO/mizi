"""本文件测试非线性、参数交互、小样本降级和统一发现排序。"""

import numpy as np
import pandas as pd

from src.nonlinear_relationships import build_unified_findings
from src.process_relationships import analyze_process_relationships


def _frames(values: dict[str, np.ndarray], defective: np.ndarray):
    count = len(defective)
    products = pd.DataFrame({
        "global_order": np.arange(1, count + 1),
        "dmc_raw": [f"DMC-{index:04d}" for index in range(count)],
    })
    parameters = pd.DataFrame({"global_order": products["global_order"], **values})
    defects = pd.DataFrame({
        "global_order": products.loc[defective, "global_order"], "component_area": 1,
    })
    return products, defects, parameters


def test_u_shaped_relationship_is_found_when_spearman_is_near_zero() -> None:
    pressure = np.tile(np.linspace(-2, 2, 24), 8)
    defective = np.abs(pressure) > 1.05
    products, defects, parameters = _frames({"pressure": pressure}, defective)

    result = analyze_process_relationships(products, defects, parameters)

    spearman = float(result.parameter_metrics.loc[0, "Spearman相关"])
    assert abs(spearman) < 0.1
    assert result.summary["nonlinear_auc"] > 0.8
    assert not result.nonlinear_effects.empty
    assert result.findings.iloc[0]["subject"] == "pressure"
    assert "尚未证明因果" in result.findings.iloc[0]["warning"]


def test_xor_relationship_is_reported_as_parameter_interaction() -> None:
    rng = np.random.default_rng(42)
    left = rng.normal(size=240)
    right = rng.normal(size=240)
    defective = (left > 0) != (right > 0)
    products, defects, parameters = _frames({"pressure": left, "temperature": right}, defective)

    result = analyze_process_relationships(products, defects, parameters)

    assert not result.interactions.empty
    pair = set(result.interactions.iloc[0][["参数A", "参数B"]])
    assert pair == {"pressure", "temperature"}
    assert float(result.interactions.iloc[0]["AUC增益"]) > 0.03
    high_risk = result.interaction_regions[
        result.interaction_regions["是否高风险"].astype(bool)
    ]
    assert len(high_risk) >= 2
    assert high_risk["高风险排名"].notna().all()
    interaction_statement = result.findings.loc[
        result.findings["detail_type"].eq("interaction"), "statement"
    ].iloc[0]
    assert "件中有" in interaction_statement
    assert "总体为" in interaction_statement
    assert "风险约" in interaction_statement


def test_small_sample_only_returns_descriptive_results() -> None:
    products, defects, parameters = _frames(
        {"pressure": np.arange(20, dtype=float)}, np.arange(20) % 2 == 0
    )

    result = analyze_process_relationships(products, defects, parameters)

    assert result.summary["nonlinear_status"] == "insufficient_sample"
    assert result.findings.empty
    assert result.model_validation.iloc[0]["有效折数"] == 0


def test_unified_findings_are_sorted_by_evidence_score() -> None:
    process = pd.DataFrame([
        {
            "finding_id": "P1", "finding_type": "工艺参数—缺陷", "subject": "pressure",
            "statement": "test", "evidence_score": 61, "effect_strength": 0.5,
            "sample_size": 100, "data_quality": 1,
        }
    ])
    code_space = pd.DataFrame([{
        "canonical_code": "5011", "spatial_type": "cluster", "spatial_id": "C1",
        "support_products": 10, "code_products": 12, "spatial_products": 11, "lift": 3,
    }])

    findings = build_unified_findings(
        process_findings=process, code_space=code_space, product_count=100
    )

    assert findings["evidence_score"].is_monotonic_decreasing
    assert set(findings["finding_type"]) == {"工艺参数—缺陷", "代码—空间"}
