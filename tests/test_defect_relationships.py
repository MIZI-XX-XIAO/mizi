"""本文件测试缺陷共现提升度和序列转移计算。"""

import pandas as pd

from src.defect_relationships import analyze_defect_relationships


def test_defect_relationships_use_cluster_when_type_missing() -> None:
    products = pd.DataFrame({"global_order": range(1, 7)})
    defects = pd.DataFrame({
        "global_order": [1, 1, 2, 3, 3, 5],
        "cluster_id": ["A", "B", "A", "A", "B", "B"],
    })
    cooccurrence, transitions = analyze_defect_relationships(products, defects)
    pair = cooccurrence.iloc[0]
    assert {pair["缺陷A"], pair["缺陷B"]} == {"A", "B"}
    assert pair["共现产品数"] == 2
    assert not transitions.empty


def test_relationships_do_not_cross_analysis_scopes() -> None:
    products = pd.DataFrame({"global_order": [1, 2], "analysis_scope": ["5S", "5X"]})
    defects = pd.DataFrame({
        "global_order": [1, 1, 2, 2],
        "analysis_scope": ["5S", "5S", "5X", "5X"],
        "cluster_id": ["5S-C001", "5S-C002", "5X-C001", "5X-C002"],
    })
    cooccurrence, transitions = analyze_defect_relationships(products, defects)
    assert set(cooccurrence.analysis_scope) == {"5S", "5X"}
    assert not any(
        row["缺陷A"].startswith("5S") and row["缺陷B"].startswith("5X")
        for _, row in cooccurrence.iterrows()
    )
    assert transitions.empty
