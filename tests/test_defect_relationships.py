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
