"""本文件测试重点发现卡片的排序、筛选和明细入口。"""

import pandas as pd
from PySide6.QtCore import Qt

from gui.association_findings import AssociationFindingsWidget


def test_top_findings_render_in_score_order_and_filter(qtbot) -> None:
    widget = AssociationFindingsWidget()
    qtbot.addWidget(widget)
    frame = pd.DataFrame([
        {
            "finding_id": "F1", "finding_type": "工艺参数—缺陷", "analysis_scope": "3-5",
            "target": "5011", "statement": "压力高风险", "evidence_score": 88,
            "evidence_level": "高", "warning": "统计关联，尚未证明因果",
        },
        {
            "finding_id": "F2", "finding_type": "缺陷共现", "analysis_scope": "3-5",
            "target": "", "statement": "缺陷共现", "evidence_score": 65,
            "evidence_level": "中", "warning": "统计关联，尚未证明因果",
        },
    ])
    widget.set_findings(frame)

    assert widget.filtered_findings()["evidence_score"].tolist() == [88, 65]
    widget.level_filter.setCurrentText("高")
    assert widget.filtered_findings()["finding_id"].tolist() == ["F1"]
    qtbot.mouseClick(widget.toggle, Qt.LeftButton)
    assert not widget.all_findings.isHidden()
