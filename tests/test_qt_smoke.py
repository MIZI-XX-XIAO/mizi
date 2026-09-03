"""本文件使用pytest-qt离屏验证主窗口、参数模型和后台Worker能够创建及安全关闭。"""

import os
from pathlib import Path

import cv2
import numpy as np
import pandas as pd
import pytest
from openpyxl import Workbook

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtCore import QSize, Qt  # noqa: E402
from PySide6.QtTest import QSignalSpy, QTest  # noqa: E402
from PySide6.QtWidgets import QLabel, QLineEdit, QPushButton  # noqa: E402

from gui.image_download_dialog import ImageDownloadDialog, parse_pasted_product_ids  # noqa: E402
from gui.main_window import MainWindow  # noqa: E402
from gui.parameter_dialog import ParameterDialog  # noqa: E402
from gui.workbench import LayoutProfile, WorkbenchStack, resolve_layout_profile  # noqa: E402
from src.image_download import ProductIdSummary  # noqa: E402
from src.result_views import ResultView  # noqa: E402
from src.station_workbook import StationWorkbookData  # noqa: E402


def test_main_window_starts(qtbot) -> None:
    window = MainWindow(Path(__file__).resolve().parents[1])
    qtbot.addWidget(window)
    window.show()
    assert window.windowTitle() == "MEA多工站缺陷规律分析"
    assert window.tabs.count() == 7
    assert isinstance(window.tabs, WorkbenchStack)
    assert "Excel分析" in window.tabs.tabText(4)
    assert "关联分析" in window.tabs.tabText(5)
    assert window.workbench.navigation.buttons[0].isChecked()
    assert window.workbench.assistant.connection.text() == "未连接"
    assert "#091424" in window.styleSheet()
    assert window.minimumWidth() <= 980
    assert window.defect_code_filter.isEditable()
    assert not window.code_list_button.isEnabled()
    assert window.findChild(QPushButton, "mesDownloadButton") is not None
    assert window.findChild(type(window.analysis_target_group), "analysisTargetGroup") is not None
    assert not window.analysis_target_group.isEnabled()
    normalized_codes = pd.DataFrame([
        {"canonical_code": "5011", "defect_name": "折皱", "code_status": "defect"},
        {"canonical_code": "5050", "defect_name": "白点", "code_status": "defect"},
        {"canonical_code": "9997", "defect_name": "图片不完整或变形", "code_status": "sealed"},
    ])
    window._populate_defect_code_filter(normalized_codes)
    assert window.code_list_button.isEnabled()
    assert window.defect_code_filter.count() == 3
    assert window.defect_code_filter.itemData(1) == "5011"
    assert window.defect_code_filter.itemData(2) == "5050"
    window.defect_code_filter.completer().setCompletionPrefix("白点")
    assert window.defect_code_filter.completer().completionCount() == 1
    window.defect_code_filter.setCurrentIndex(window.defect_code_filter.findData("5050"))
    assert window._selected_defect_codes() == {"5050"}
    qtbot.mouseClick(window.code_list_button, Qt.LeftButton)
    assert window.defect_code_filter.view().isVisible()
    window.defect_code_filter.hidePopup()
    window.defect_code_filter.setEditText("5520")
    assert window._selected_defect_codes() == {"5520"}
    window._clear_code_filter()
    assert window._selected_defect_codes() == set()
    window.close()
    window.deleteLater()


def test_task_target_filter_populates_codes_and_parameters_after_inspection(qtbot) -> None:
    window = MainWindow(Path(__file__).resolve().parents[1])
    qtbot.addWidget(window)
    events = pd.DataFrame([
        {
            "event_id": f"A{order}", "dmc_raw": f"DMC-{order}",
            "station_id": "35_5s_aoi", "test_date": f"2026-08-01 10:0{order}:00",
            "state": "NOK", "source_sheet": "AOI", "source_row": order + 1,
            "production_order": order, "Result.AOIFailureCode": "501100000000",
        }
        for order in range(1, 5)
    ])
    parameters = pd.DataFrame([
        {
            "event_id": f"P{order}", "dmc_raw": f"DMC-{order}",
            "station_id": "35_wp1", "test_date": f"2026-08-01 09:0{order}:00",
            "parameter_name": "Result.Force", "numeric_value": float(order),
        }
        for order in range(1, 5)
    ])
    window.station_workbook = StationWorkbookData(
        products=pd.DataFrame({"dmc_raw": [f"DMC-{order}" for order in range(1, 5)]}),
        events=events, parameters=parameters, package=pd.DataFrame(),
    )

    window._populate_analysis_target_filters(True)
    window.analysis_filter_mode.setCurrentIndex(
        window.analysis_filter_mode.findData("selected_codes")
    )
    window.code_selection_list.item(0).setCheckState(Qt.Checked)

    selection = window._current_analysis_selection()
    assert window.analysis_target_group.isEnabled()
    assert window.code_selection_list.count() == 1
    assert window.parameter_selection_list.count() == 1
    assert selection.defect_codes == ("AOI_FAILURE:5011",)
    assert selection.scopes == ("5S",)
    assert not window.analysis_module_checks["image_patterns"].isChecked()
    assert window.analysis_module_checks["process_relationships"].isChecked()
    window._populate_analysis_target_filters(True)
    assert window._current_analysis_selection().defect_codes == ("AOI_FAILURE:5011",)
    window.close(); window.deleteLater()


def test_mes_download_dialog_opens_from_workbook_row(qtbot) -> None:
    window = MainWindow(Path(__file__).resolve().parents[1])
    qtbot.addWidget(window)
    window.show()
    qtbot.mouseClick(window.findChild(QPushButton, "mesDownloadButton"), Qt.LeftButton)
    dialog = window._mes_dialog
    assert dialog is not None
    qtbot.addWidget(dialog)
    assert dialog.isVisible()
    assert dialog.end_edit.dateTime() > dialog.begin_edit.dateTime()
    assert dialog.password_edit.echoMode() == QLineEdit.Password
    dialog.close()
    window.close()
    window.deleteLater()


def test_image_download_dialog_reads_mes_products_and_uses_safe_defaults(qtbot, tmp_path: Path) -> None:
    product = "P" + "1" * 24
    workbook = Workbook(); sheet = workbook.active; sheet.title = "MS0310all"
    sheet.append(["Ident No.", "Test Date", "Line", "ST", "SI", "FU", "WP", "Result.Force"])
    sheet.append([product, "2026-08-26 08:00:00", 3003, 10, 1, 1, 6, 1])
    path = tmp_path / "mes_for_images.xlsx"; workbook.save(path); workbook.close()

    window = MainWindow(Path(__file__).resolve().parents[1])
    qtbot.addWidget(window); window.show(); window.source_excel_edit.setText(str(path))
    qtbot.mouseClick(window.findChild(QPushButton, "imageDownloadButton"), Qt.LeftButton)
    dialog = window._image_dialog
    assert dialog is not None
    qtbot.addWidget(dialog)
    assert dialog.product_summary.valid_ids == (product,)
    assert dialog.batch_size.value() == 80
    assert dialog.batch_size.maximum() == 100
    assert dialog.skip_rework.isChecked()
    assert dialog._selected_codes() == ()
    assert dialog._quality() == ""
    assert dialog.password_edit.echoMode() == QLineEdit.Password
    assert "普通Edge" in dialog.login_hint.text()
    assert "Windows Security" in dialog.login_hint.text()
    assert dialog.login_hint.isVisible()
    assert "5S/MS03106" in dialog.product_stats.text()
    assert "工站匹配：MS0310all" in dialog.product_stats.text()
    assert dialog.product_selection_count.text() == "已选 1 / 1"
    assert dialog._selected_products_by_family()["D"] == (product,)
    dialog.code_checks["EE"].setChecked(True)
    dialog.origin_radio.setChecked(True)
    dialog._start()
    assert dialog.worker is None
    assert "没有选中产品号" in dialog.notice.text()
    dialog.close(); window.close(); window.deleteLater()


def test_all_result_cards_open_embedded_details_and_return_to_overview(qtbot) -> None:
    window = MainWindow(Path(__file__).resolve().parents[1])
    qtbot.addWidget(window)
    window.show()
    sections = {
        "periodic": pd.DataFrame(columns=["pattern_id"]),
        "burst": pd.DataFrame(columns=["pattern_id"]),
        "code": pd.DataFrame([{"canonical_code": "5520", "pattern_type": "periodic"}]),
        "trajectory": pd.DataFrame(columns=["trajectory_id"]),
        "cooccurrence": pd.DataFrame(columns=["缺陷A", "缺陷B"]),
        "transition": pd.DataFrame(columns=["前一缺陷", "后一缺陷"]),
        "other": pd.DataFrame(columns=["pattern_id"]),
    }
    alerts = pd.DataFrame([{"severity": "warning", "alert_at_order": 2}])
    details = {
        key: pd.DataFrame([{"global_order": 1, "detail_type": key}])
        for key in (
            "analyzed_product_count", "extracted_defect_count", "micro_defect_count",
            "local_defect_count", "region_anomaly_count", "spatial_cluster_count",
            "code_label_conflict_count",
        )
    }
    window._current_result_view = ResultView(
        pd.DataFrame(), pd.DataFrame(), alerts, sections, {}, details,
    )
    window.evidence_mode.setCurrentIndex(window.evidence_mode.findData("code"))

    assert set(window.result_cards) == {
        "analyzed_product_count", "extracted_defect_count", "micro_defect_count",
        "local_defect_count", "region_anomaly_count", "spatial_cluster_count",
        "code_label_conflict_count", "discovered_pattern_count", "alert_count",
    }
    for key in details:
        qtbot.mouseClick(window.result_cards[key], Qt.LeftButton)
        assert window.result_stack.currentWidget() is window.result_details
        assert window.result_details.current_key == key
        assert window.result_details.table.model.rowCount() == 1
        qtbot.mouseClick(window.result_details.back_button, Qt.LeftButton)
        assert window.result_stack.currentWidget() is window.result_overview

    qtbot.mouseClick(window.pattern_result_card, Qt.LeftButton)
    assert window.result_stack.currentWidget() is window.result_details
    assert window.result_details.tabs.currentWidget() is window.result_details.widgets["code"]
    assert "(1)" in window.result_details.tabs.tabText(
        window.result_details.tabs.indexOf(window.result_details.widgets["code"])
    )
    assert "(0)" in window.result_details.tabs.tabText(
        window.result_details.tabs.indexOf(window.result_details.widgets["periodic"])
    )
    qtbot.mouseClick(window.result_details.back_button, Qt.LeftButton)

    qtbot.mouseClick(window.alert_result_card, Qt.LeftButton)
    assert window.result_stack.currentWidget() is window.result_details
    model = window.result_details.table.model
    index = model.index(0, 0)
    assert model.data(index, Qt.BackgroundRole).name() == "#493b20"
    assert model.data(index, Qt.ForegroundRole).name() == "#ffe09b"
    window.close()
    window.deleteLater()


def test_image_download_product_selection_search_and_paste(qtbot, tmp_path: Path) -> None:
    shared = "S" + "1" * 24
    d_only = "D" + "2" * 24
    unmatched = "Z" * 25
    workbook = Workbook(); workbook.remove(workbook.active)
    d_sheet = workbook.create_sheet("MS03106"); d_sheet.append(["Ident No.", "Test Date"])
    d_sheet.append([shared, "2026-08-26 08:00:00"])
    d_sheet.append([d_only, "2026-08-26 09:00:00"])
    e_sheet = workbook.create_sheet("MS03206"); e_sheet.append(["Ident No.", "Test Date"])
    e_sheet.append([shared, "2026-08-26 10:00:00"])
    path = tmp_path / "selectable.xlsx"; workbook.save(path); workbook.close()

    dialog = ImageDownloadDialog(Path(__file__).resolve().parents[1], path, tmp_path)
    qtbot.addWidget(dialog)

    assert dialog.product_selection_count.text() == "已选 3 / 3"
    assert dialog._selected_products_by_family()["D"] == (shared, d_only)
    assert dialog._selected_products_by_family()["E"] == (shared,)
    dialog.product_search.setText(d_only)
    dialog._set_visible_products(Qt.Unchecked)
    assert dialog.product_selection_count.text() == "已选 2 / 3"
    dialog.product_search.clear()

    valid, invalid = parse_pasted_product_ids(f"{shared}\n{shared}, {unmatched}; SHORT")
    assert valid == (shared, unmatched)
    assert invalid == ("SHORT",)
    invalid, missing = dialog._apply_pasted_product_ids(f"{shared}\n{unmatched}\nSHORT")
    assert invalid == ("SHORT",)
    assert missing == (unmatched,)
    assert dialog.product_selection_count.text() == "已选 2 / 3"
    assert dialog._selected_products_by_family()["D"] == (shared,)
    assert dialog._selected_products_by_family()["E"] == (shared,)
    dialog.close()


def test_large_product_group_toggle_emits_once_and_keeps_other_families(qtbot, tmp_path: Path) -> None:
    d_products = tuple(f"D{index:024d}" for index in range(3000))
    e_product = "E" + "9" * 24
    summary = ProductIdSummary(
        d_products + (e_product,), (), 0,
        products_by_family={"D": d_products, "E": (e_product,), "F": (), "G": ()},
    )
    dialog = ImageDownloadDialog(Path(__file__).resolve().parents[1], None, tmp_path)
    qtbot.addWidget(dialog)
    dialog._populate_products(summary)
    root = dialog._product_groups["D"]
    dialog.code_checks["DE"].setChecked(True)
    dialog.code_checks["EE"].setChecked(True)
    spy = QSignalSpy(dialog.product_tree.itemChanged)

    root.setCheckState(0, Qt.Unchecked)

    assert spy.count() == 1
    assert dialog.product_selection_count.text() == "已选 1 / 3001"
    assert dialog._selected_products_by_family()["D"] == ()
    assert dialog._selected_products_by_family()["E"] == (e_product,)
    assert not dialog.code_checks["DE"].isEnabled()
    assert dialog.code_checks["EE"].isEnabled()
    assert dialog._selected_codes() == ("EE",)
    assert all(root.child(index).checkState(0) == Qt.Unchecked for index in range(root.childCount()))
    root.child(0).setCheckState(0, Qt.Checked)
    assert root.checkState(0) == Qt.PartiallyChecked
    assert dialog.product_selection_count.text() == "已选 2 / 3001"
    assert dialog.code_checks["DE"].isEnabled()
    assert dialog._selected_codes() == ("DE", "EE")
    dialog.close()


def test_shift_click_toggles_visible_range_without_crossing_family(qtbot, tmp_path: Path) -> None:
    d_products = tuple(f"D{index:024d}" for index in range(6))
    e_products = tuple(f"E{index:024d}" for index in range(2))
    summary = ProductIdSummary(
        d_products + e_products, (), 0,
        products_by_family={"D": d_products, "E": e_products, "F": (), "G": ()},
    )
    dialog = ImageDownloadDialog(Path(__file__).resolve().parents[1], None, tmp_path)
    qtbot.addWidget(dialog); dialog._populate_products(summary); dialog.show()
    d_root = dialog._product_groups["D"]
    e_root = dialog._product_groups["E"]

    QTest.mouseClick(
        dialog.product_tree.viewport(), Qt.LeftButton, Qt.NoModifier,
        dialog.product_tree.visualItemRect(d_root.child(1)).center(),
    )
    QTest.mouseClick(
        dialog.product_tree.viewport(), Qt.LeftButton, Qt.ShiftModifier,
        dialog.product_tree.visualItemRect(d_root.child(4)).center(),
    )

    assert dialog._selected_products_by_family()["D"] == (d_products[0], d_products[5])
    assert dialog._selected_products_by_family()["E"] == e_products
    dialog._remember_product_anchor(d_root.child(0), 0)
    dialog._apply_shift_product_range(e_root.child(1))
    assert dialog._selected_products_by_family()["D"] == (d_products[0], d_products[5])
    assert dialog._selected_products_by_family()["E"] == (e_products[0],)
    assert dialog._product_range_anchor == ("E", e_products[1])
    dialog.close()


def test_shift_range_uses_visible_items_and_selection_actions_reset_anchor(qtbot, tmp_path: Path, monkeypatch) -> None:
    products = tuple(f"D{index:024d}" for index in range(6))
    summary = ProductIdSummary(
        products, (), 0,
        products_by_family={"D": products, "E": (), "F": (), "G": ()},
    )
    dialog = ImageDownloadDialog(Path(__file__).resolve().parents[1], None, tmp_path)
    qtbot.addWidget(dialog); dialog._populate_products(summary)
    root = dialog._product_groups["D"]
    root.setCheckState(0, Qt.Unchecked)
    root.child(2).setHidden(True)
    dialog._remember_product_anchor(root.child(1), 0)
    refresh_count = 0
    original_refresh = dialog._update_selection_count

    def counted_refresh() -> None:
        nonlocal refresh_count
        refresh_count += 1
        original_refresh()

    monkeypatch.setattr(dialog, "_update_selection_count", counted_refresh)
    dialog._apply_shift_product_range(root.child(4))

    assert refresh_count == 1
    assert dialog._selected_products_by_family()["D"] == (products[1], products[3], products[4])
    assert root.child(2).checkState(0) == Qt.Unchecked
    dialog.product_search.setText(products[0])
    assert dialog._product_range_anchor is None
    dialog._remember_product_anchor(root.child(1), 0)
    dialog._apply_pasted_product_ids(products[5])
    assert dialog._product_range_anchor is None
    dialog._remember_product_anchor(root.child(1), 0)
    dialog._populate_products(summary)
    assert dialog._product_range_anchor is None
    dialog.close()


def test_parameter_dialog_keeps_independent_detection_profiles(qtbot) -> None:
    project = Path(__file__).resolve().parents[1]
    import yaml
    config = yaml.safe_load((project / "config/analysis_config.yaml").read_text(encoding="utf-8"))
    dialog = ParameterDialog(config, project / "config/analysis_config.yaml")
    qtbot.addWidget(dialog)
    dialog.editors["red_min"].setValue(111)
    dialog.profile_combo.setCurrentText("5X")
    dialog.editors["red_min"].setValue(222)
    values = dialog.values()
    assert values["detection_profiles"]["5S"]["red_min"] == 111
    assert values["detection_profiles"]["5X"]["red_min"] == 222
    assert values["detection_profiles"]["7S"]["red_min"] == 150
    dialog.close()


def test_workbench_navigation_and_responsive_layout(qtbot) -> None:
    window = MainWindow(Path(__file__).resolve().parents[1])
    qtbot.addWidget(window)
    window.resize(1600, 900)
    window.show()

    window.workbench.navigation.buttons[5].click()
    assert window.tabs.currentIndex() == 5
    assert window.workbench.header.title.text() == "关联分析"

    window.workbench.apply_responsive_layout(QSize(1366, 768))
    assert window.workbench.profile is LayoutProfile.COMPACT
    assert window.workbench.navigation.width() == 76
    assert not window.workbench.assistant.isVisible()

    qtbot.wait(10)
    content_width = window.workbench.content.width()
    window.workbench.set_assistant_visible(True)
    qtbot.wait(10)
    assert window.workbench._assistant_overlay
    assert window.workbench.content.width() == content_width
    assert 320 <= window.workbench.assistant.width() <= 400

    window.workbench.apply_responsive_layout(QSize(1920, 1200))
    assert window.workbench.profile is LayoutProfile.FULL
    assert window.workbench.navigation.width() == 214
    assert window.workbench.assistant.isVisible()
    window.close()
    window.deleteLater()


def test_layout_profiles_cover_windows_scaling_targets() -> None:
    assert resolve_layout_profile(QSize(1920, 1200)) is LayoutProfile.FULL
    assert resolve_layout_profile(QSize(1536, 960)) is LayoutProfile.FULL
    assert resolve_layout_profile(QSize(1280, 800)) is LayoutProfile.COMPACT
    assert resolve_layout_profile(QSize(1366, 768)) is LayoutProfile.COMPACT
    assert resolve_layout_profile(QSize(980, 620)) is LayoutProfile.TIGHT


def test_compact_excel_page_does_not_set_oversized_window_hint(qtbot) -> None:
    window = MainWindow(Path(__file__).resolve().parents[1])
    qtbot.addWidget(window)
    window.resize(1280, 800)
    window.show()
    window.tabs.setCurrentIndex(4)
    window.workbench.apply_responsive_layout(QSize(1280, 800), force=True)
    qtbot.wait(10)

    assert window.minimumSizeHint().height() < 800
    assert window.workbench.content.width() >= 1100
    assert window.excel_page.result_tabs.usesScrollButtons()
    assert window.excel_page.config_scroll.horizontalScrollBar().maximum() == 0
    assert not window.workbench.header.subtitle.isVisible()
    assert not window.workbench.header.task_context.isVisible()
    window.close()
    window.deleteLater()


def test_assistant_shell_is_offline_and_context_is_explicit(qtbot) -> None:
    window = MainWindow(Path(__file__).resolve().parents[1])
    qtbot.addWidget(window)
    window.show()
    assistant = window.workbench.assistant

    assert not assistant.backend.available
    assert assistant.connection.text() == "未连接"
    assert "不会发送" in assistant.findChild(QLabel, "privacyBanner").text()

    window.tabs.setCurrentIndex(6)
    assert "图片复核" in assistant.context_label.text()
    assistant.clear_context()
    assert "尚未附加" in assistant.context_label.text()
    window.close()
    window.deleteLater()


def test_pattern_evidence_strip_and_fullscreen_review(qtbot, tmp_path: Path) -> None:
    project = Path(__file__).resolve().parents[1]
    import yaml
    config = yaml.safe_load((project / "config/analysis_config.yaml").read_text(encoding="utf-8"))
    a_image = np.full((80, 120), 70, dtype=np.uint8)
    e_image = cv2.cvtColor(a_image, cv2.COLOR_GRAY2BGR)
    cv2.rectangle(e_image, (30, 20), (42, 32), (0, 0, 255), thickness=-1)
    a_path, e_path = tmp_path / "a.png", tmp_path / "e.png"
    cv2.imencode(".png", a_image)[1].tofile(str(a_path))
    cv2.imencode(".png", e_image)[1].tofile(str(e_path))
    products = pd.DataFrame({
        "global_order": [1, 2, 3], "analysis_scope": ["5S"] * 3,
        "order_code": ["P1", "P2", "P3"], "dmc_raw": ["P1", "P2", "P3"],
        "camera": ["5S"] * 3, "a_image_path": [str(a_path)] * 3,
        "e_image_path": [str(e_path)] * 3,
    })
    window = MainWindow(project)
    qtbot.addWidget(window)
    window.show()
    window.tabs.setCurrentWidget(window.review)
    window.review.set_data(products, pd.DataFrame({"global_order": pd.Series(dtype=int)}), config)
    qtbot.waitUntil(lambda: window.review._payload is not None, timeout=10_000)

    pattern = pd.Series({
        "pattern_id": "5S-P001", "pattern_type": "periodic", "cluster_id": "5S-C001",
        "period": 2, "confidence": 0.9, "first_order": 1,
        "observed_orders": "1;3", "inferred_missing_orders": "2",
    })
    window.tabs.setCurrentIndex(3)
    window._jump_from_pattern(pattern)
    assert window.tabs.currentWidget() is window.review
    assert window.review.pattern_record is not None
    assert window.review.pattern_panel.isVisible()
    assert window.review.pattern_list.count() == 3
    assert window.review.pattern_list.item(1).data(Qt.UserRole + 1) == "missing"
    window.review.pattern_list.setCurrentRow(2)
    qtbot.waitUntil(lambda: window.review.current_order == 3, timeout=5_000)

    window.review._open_pattern_item_fullscreen(window.review.pattern_list.item(0))
    qtbot.waitUntil(
        lambda: window.review._fullscreen is not None and window.review._fullscreen.isVisible(),
        timeout=10_000,
    )
    assert window.review._fullscreen.image_type == "E图"
    assert window.review.current_order == 1
    qtbot.keyClick(window.review._fullscreen, Qt.Key_Escape)
    qtbot.waitUntil(lambda: not window.review._fullscreen.isVisible(), timeout=5_000)
    assert window.review.pattern_panel.isVisible()
    assert window.review.current_order == 1

    code_pattern = pd.Series({
        "pattern_id": "CP0001", "pattern_type": "periodic", "canonical_code": "5011",
        "period": 6, "confidence": 0.82, "first_production_order": 100,
        "observed_production_orders": "100;106", "evidence_task_orders": "1;3",
        "missing_task_orders": "2",
    })
    window._jump_from_pattern(code_pattern)
    assert window.review.current_order == 1
    assert window.review.pattern_list.count() == 3

    joint_evidence = pd.Series({
        "pattern_type": "cluster", "canonical_code": "5011", "spatial_id": "5S-C001",
        "support_task_orders": "2;3",
    })
    window._jump_from_pattern(joint_evidence)
    assert window.review.current_order == 2
    assert window.review.pattern_list.count() == 2

    trajectory = pd.Series({
        "trajectory_id": "5S-T001", "pattern_type": "linear_drift", "task_orders": "1;2;3",
    })
    window._jump_from_pattern(trajectory)
    assert window.review.current_order == 1

    conflict = pd.Series({"global_order": 3, "comparison_status": "label_conflict"})
    window._jump_from_pattern(conflict)
    assert window.review.current_order == 3
    window.close()
    window.deleteLater()


def test_excel_page_runs_analysis_in_background(qtbot, tmp_path: Path) -> None:
    workbook = Workbook(); sheet = workbook.active; sheet.title = "Data"
    sheet.append(["Ident No.", "State", "Result.Force", "Tolerance"])
    sheet.append(["DMC-1", "OK", 30, "25 ... 70"])
    sheet.append(["DMC-2", "NOK", 80, "25 ... 70"])
    path = tmp_path / "gui_excel.xlsx"; workbook.save(path)

    window = MainWindow(Path(__file__).resolve().parents[1])
    qtbot.addWidget(window); window.show(); window.tabs.setCurrentIndex(4)
    page = window.excel_page
    page.workbook_edit.setText(str(path)); page.output_edit.setText(str(tmp_path))
    page.task_name.setText("GUI Excel测试"); page.start_analysis()
    qtbot.waitUntil(lambda: page.current_result is not None, timeout=15_000)
    qtbot.waitUntil(lambda: page.thread is None, timeout=5_000)
    assert page.current_result.summary["tolerance_nok_count"] == 1
    assert window.current_excel_result is page.current_result
    assert window.use_current_excel.isEnabled()
    window.close(); window.deleteLater()


def test_station_task_builds_products_without_company_csv(qtbot, tmp_path: Path) -> None:
    dmc = "376W020BGO57424F00VF004AK"
    workbook = Workbook(); data = workbook.active; data.title = "Data"
    data.append(["Ident No.", "State", "Result.Force", "Tolerance"])
    data.append([dmc, "OK", 30, "25 ... 70"])
    query = workbook.create_sheet("Query parameter")
    query.append(["Query parameter", None]); query.append(["Location(s)", "3003.10.1.1.6"])
    excel_path = tmp_path / "station.xlsx"; workbook.save(excel_path)
    for code in ("DA", "DE"):
        (tmp_path / f"{dmc}20250624{code}.png").write_bytes(b"index-only")

    window = MainWindow(Path(__file__).resolve().parents[1])
    qtbot.addWidget(window)
    window.products_edit.clear()
    window.station_combo.setCurrentIndex(window.station_combo.findData("35_5s_aoi"))
    window.source_excel_edit.setText(str(excel_path))
    window.image_root_edit.setText(str(tmp_path))
    assert window._inspect_products()
    assert len(window.loaded_products) == 1
    assert len(window.analysis_products) == 1
    assert window.analysis_products.iloc[0].a_image_path.endswith("DA.png")
    assert window.analysis_products.iloc[0].e_image_path.endswith("DE.png")
    window.close(); window.deleteLater()
