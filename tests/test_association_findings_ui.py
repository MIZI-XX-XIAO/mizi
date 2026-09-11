"""本文件测试全部有效发现的解释卡片、筛选和上下文明细入口。"""

import pandas as pd
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton

from gui.association_findings import AssociationFindingsWidget


def test_all_effective_findings_render_as_cards_and_filter(qtbot) -> None:
    widget = AssociationFindingsWidget()
    qtbot.addWidget(widget)
    rows = [{
        "finding_id": f"F{index:02d}", "finding_type": "停机—缺陷",
        "analysis_scope": "5S", "target": "VI_BLOCK:5011",
        "statement": f"发现{index}", "evidence_score": 100 - index,
        "evidence_level": "较强相关" if index < 3 else "探索性线索",
        "warning": "统计关联，尚未证明因果", "detail_type": "cause_evidence",
        "analysis_question": "停机是否与5011相关？", "discovery_method": "比较两组缺陷率。",
        "interpretation": "暴露组风险更高。", "limitations": "缺少PLC日志。",
        "recommended_action": "核对DMC。", "is_effective": True,
    } for index in range(12)]
    rows.append({**rows[0], "finding_id": "INVALID", "is_effective": False})
    frame = pd.DataFrame(rows)
    widget.set_findings(frame)

    assert len(widget._cards) == 12
    assert "Top" not in widget.summary.text()
    assert "前10" not in widget.summary.text()
    assert widget.filtered_findings()["finding_id"].tolist()[0] == "F00"
    widget.level_filter.setCurrentText("较强相关")
    assert widget.filtered_findings()["finding_id"].tolist() == ["F00", "F01", "F02"]


def test_cause_card_expands_reasoning_and_requests_product_details(qtbot) -> None:
    widget = AssociationFindingsWidget()
    qtbot.addWidget(widget)
    widget.set_findings(pd.DataFrame([{
        "finding_id": "CAUSE-0001", "finding_type": "停机—缺陷",
        "analysis_scope": "5S", "target": "VI_BLOCK:5011",
        "statement": "经历疑似停机组24/82，未经历组1/202，风险比59.12。",
        "evidence_score": 81, "evidence_level": "较强相关",
        "warning": "统计关联，尚未证明根本原因", "detail_type": "cause_evidence",
        "analysis_question": "疑似停机是否与5011有关？",
        "discovery_method": "按30秒和正常节拍10倍识别同步空档，再比较两组产品。",
        "interpretation": "5011与疑似停机存在较强统计关联。",
        "limitations": "缺少PLC停机状态。", "recommended_action": "核对PLC日志。",
        "is_effective": True,
    }]))

    card = widget._cards[0]
    expand = next(button for button in card.findChildren(QPushButton)
                  if button.text() == "展开分析依据")
    explanation = card.findChild(QLabel, "associationFindingExplanation")
    assert explanation.isHidden()
    qtbot.mouseClick(expand, Qt.LeftButton)
    assert not explanation.isHidden()
    assert "怎么发现" in explanation.text()
    assert "24/82" in card.findChildren(QLabel)[2].text()

    detail = next(button for button in card.findChildren(QPushButton)
                  if button.text() == "查看产品明细")
    with qtbot.waitSignal(widget.detail_requested) as blocker:
        qtbot.mouseClick(detail, Qt.LeftButton)
    assert blocker.args[0]["requested_action"] == "samples"
    assert blocker.args[0]["detail_type"] == "cause_evidence"


def test_one_defect_is_shown_at_a_time_and_summary_is_not_repeated(qtbot) -> None:
    widget = AssociationFindingsWidget()
    qtbot.addWidget(widget)
    findings = pd.DataFrame([
        {
            "finding_id": "5011-A", "finding_type": "停机—缺陷",
            "analysis_scope": "5S", "target": "VI_BLOCK:5011",
            "statement": "5011停机线索", "evidence_score": 90,
            "evidence_level": "较强相关", "is_effective": True,
        },
        {
            "finding_id": "5011-B", "finding_type": "复产—缺陷",
            "analysis_scope": "5S", "target": "VI_BLOCK:5011",
            "statement": "5011复产线索", "evidence_score": 80,
            "evidence_level": "较强相关", "is_effective": True,
        },
        {
            "finding_id": "5020-A", "finding_type": "停机—缺陷",
            "analysis_scope": "5S", "target": "VI_BLOCK:5020",
            "statement": "5020线索", "evidence_score": 70,
            "evidence_level": "探索性线索", "is_effective": True,
        },
    ])
    summaries = [{
        "analysis_scope": "5S", "source_type": "VI_BLOCK", "canonical_code": "5011",
        "population_count": 1243, "current_window_traceable_count": 284,
        "complete_route_count": 242, "partial_route_count": 42,
        "downtime_event_count": 10, "top_hypothesis": "与停机/复产较强相关",
    }]

    widget.set_findings(
        findings, cause_summaries=summaries, preferred_target="VI_BLOCK:5011"
    )

    assert widget.target_filter.count() == 2
    assert widget.filtered_findings()["finding_id"].tolist() == ["5011-A", "5011-B"]
    assert widget.summary.text().count("284/1243") == 1
    assert "与停机/复产较强相关" in widget.summary.text()
    widget.target_filter.setCurrentIndex(1)
    assert widget.filtered_findings()["finding_id"].tolist() == ["5020-A"]
    assert "284/1243" not in widget.summary.text()


def test_process_card_uses_distinct_curve_and_heatmap_actions(qtbot) -> None:
    widget = AssociationFindingsWidget()
    qtbot.addWidget(widget)
    widget.set_findings(pd.DataFrame([
        {
            "finding_id": "I1", "finding_type": "参数交互", "analysis_scope": "5S",
            "target": "VI_BLOCK:5011", "defect_name": "折皱",
            "statement": "目标缺陷：折皱。高风险区间。", "evidence_score": 80,
            "evidence_level": "高", "detail_type": "interaction", "is_effective": True,
        },
        {
            "finding_id": "N1", "finding_type": "工艺参数—缺陷", "analysis_scope": "5S",
            "target": "VI_BLOCK:5011", "defect_name": "折皱",
            "statement": "静电值高风险。", "evidence_score": 70,
            "evidence_level": "中", "detail_type": "nonlinear_effect", "is_effective": True,
        },
    ]))

    labels = [button.text() for button in widget.findChildren(QPushButton)]
    assert "查看风险热力图" in labels
    assert "查看单参数曲线" in labels
    assert "折皱" in widget.target_filter.currentText()
