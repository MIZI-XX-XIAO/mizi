"""本文件测试参数交互风险热力图和样本对照视图。"""

import pandas as pd

from gui.interaction_detail import InteractionDetailWidget


def test_interaction_detail_builds_heatmap_and_balanced_samples(qtbot) -> None:
    widget = InteractionDetailWidget()
    qtbot.addWidget(widget)
    finding = pd.Series({
        "analysis_scope": "5S", "target": "VI_BLOCK:5011", "source_type": "VI_BLOCK",
        "canonical_code": "5011", "defect_name": "折皱",
        "subject": "静电值", "related_subject": "张力",
    })
    regions = pd.DataFrame([
        {
            "analysis_scope": "5S", "target": "VI_BLOCK:5011",
            "参数A": "静电值", "参数B": "张力", "参数A区间": "(0, 10]", "参数B区间": "(0, 20]",
            "参数A下界": 0, "参数A上界": 10, "参数B下界": 0, "参数B上界": 20,
            "样本量": 2, "缺陷数量": 2, "缺陷率": 1.0, "总体缺陷率": 0.5,
            "风险比": 2.0, "是否高风险": True, "高风险排名": 1,
        },
        {
            "analysis_scope": "5S", "target": "VI_BLOCK:5011",
            "参数A": "静电值", "参数B": "张力", "参数A区间": "(10, 20]", "参数B区间": "(0, 20]",
            "参数A下界": 10, "参数A上界": 20, "参数B下界": 0, "参数B上界": 20,
            "样本量": 2, "缺陷数量": 0, "缺陷率": 0.0, "总体缺陷率": 0.5,
            "风险比": 0.0, "是否高风险": False, "高风险排名": float("nan"),
        },
    ])
    samples = pd.DataFrame({
        "analysis_scope": ["5S"] * 4, "target": ["VI_BLOCK:5011"] * 4,
        "global_order": [1, 2, 3, 4], "dmc_raw": ["A", "B", "C", "D"],
        "静电值": [5, 7, 15, 17], "张力": [10, 12, 10, 12],
        "has_detected_defect": [1, 1, 0, 0],
    })

    widget.set_data(finding, regions, samples)

    assert "折皱（VI 5011）" in widget.title.text()
    assert widget.heatmap.rowCount() == 1
    assert widget.heatmap.columnCount() == 2
    assert "★" in widget.heatmap.item(0, 0).text()
    assert len(widget.hit_samples.frame()) == 2
    assert len(widget.control_samples.frame()) == 2
    assert set(widget.hit_samples.frame()["是否缺陷"]) == {"缺陷"}
