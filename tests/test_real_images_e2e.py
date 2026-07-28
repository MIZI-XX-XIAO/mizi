"""本文件使用项目现有A/E图片验证新版GUI、实际分析、复核视图和工艺关联全流程。"""

import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from PySide6.QtWidgets import QMessageBox

from gui.main_window import MainWindow


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _real_dataset_root() -> Path:
    configured = os.environ.get("MEA5S_REAL_DATA_ROOT", "").strip()
    root = Path(configured).expanduser() if configured else PROJECT_ROOT / "data/dataset_realistic"
    if not (root / "products.csv").is_file():
        pytest.skip(
            "未配置真实图片数据；设置MEA5S_REAL_DATA_ROOT为包含products.csv的目录后运行"
        )
    return root.resolve()


def test_real_images_complete_gui_workflow(qtbot, tmp_path, monkeypatch) -> None:
    dataset_root = _real_dataset_root()
    products = pd.read_csv(dataset_root / "products.csv")
    selected_orders = list(range(1, 25)) + [37, 61, 85, 109]
    subset = products[products.global_order.isin(selected_orders)].copy()
    products_path = tmp_path / "真实图片测试_products.csv"
    subset.to_csv(products_path, index=False, encoding="utf-8-sig")
    process_path = tmp_path / "真实图片测试_工艺参数.csv"
    pd.DataFrame({
        "global_order": subset.global_order,
        "temperature": 20.0 + subset.global_order * 0.05,
        "pressure": 5.0 + np.sin(subset.global_order / 5),
        "line_speed": 100.0 + np.cos(subset.global_order / 7),
    }).to_csv(process_path, index=False, encoding="utf-8-sig")

    messages: list[tuple[str, str]] = []
    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox, "information",
        lambda _parent, title, message: messages.append((title, message)),
    )
    monkeypatch.setattr(
        QMessageBox, "critical",
        lambda _parent, title, message: errors.append((title, message)),
    )

    window = MainWindow(PROJECT_ROOT)
    qtbot.addWidget(window)
    window.show()
    window.products_edit.setText(str(products_path))
    window.image_root_edit.setText(str(dataset_root))
    window.output_edit.setText(str(tmp_path / "任务输出"))
    window.task_edit.setText("真实图片端到端测试")
    assert window._inspect_products()
    window._start_analysis()
    qtbot.waitUntil(lambda: window.thread is None, timeout=120_000)

    assert not errors
    assert window.current_result is not None
    result = window.current_result
    assert result.summary["analyzed_product_count"] == len(subset)
    assert result.summary["extracted_defect_count"] >= 4
    for name in (
        "extracted_defects.csv", "discovered_patterns.csv", "alerts.csv",
        "analysis_summary.json", "analysis_config_snapshot.yaml", "task_manifest.json",
    ):
        assert (result.output_dir / name).exists()

    qtbot.waitUntil(
        lambda: window.review._payload is not None
        and "正在后台加载" not in window.review.info.text()
        and "加载失败" not in window.review.info.text(),
        timeout=30_000,
    )
    assert all(view.scene.items() for view in window.review.views.values())
    window.tabs.setCurrentWidget(window.review)
    window.review.layout_combo.setCurrentText("A/E左右对比")
    assert window.review.cards["A图"].isVisible()
    assert window.review.cards["E图"].isVisible()
    window.review.layout_combo.setCurrentText("单图放大")
    window.review.single_combo.setCurrentText("Mask")
    assert window.review.cards["Mask"].isVisible()
    window.review.layout_combo.setCurrentText("2×2")
    window.review.overlay_check.setChecked(False)
    window.review.overlay_check.setChecked(True)
    window.review.diff_check.setChecked(False)
    window.review.diff_check.setChecked(True)
    window.review.mask_check.setChecked(False)
    window.review.mask_check.setChecked(True)
    request_id = window.review._request_id
    window.review._load_current(True)
    qtbot.waitUntil(
        lambda: window.review._request_id > request_id
        and window.review._payload is not None
        and "原图区域" in window.review.info.text(),
        timeout=30_000,
    )
    window.review._fit_all()

    window.process_edit.setText(str(process_path))
    window._analyze_process_parameters()
    assert not errors
    assert not window.relationship_metrics.model.frame.empty
    assert not window.relationship_bins.model.frame.empty
    assert not window.relationship_model.model.frame.empty
    assert "统计关联不等于因果关系" in window.findChild(
        type(window.relationship_summary), "warningBanner"
    ).text()
    assert not window.review.process_data.empty
    assert messages
