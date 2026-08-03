"""本文件使用pytest-qt离屏验证主窗口、参数模型和后台Worker能够创建及安全关闭。"""

import os
from pathlib import Path

import pytest
from openpyxl import Workbook

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtCore import QSize  # noqa: E402
from PySide6.QtWidgets import QLabel  # noqa: E402

from gui.main_window import MainWindow  # noqa: E402
from gui.workbench import LayoutProfile, WorkbenchStack, resolve_layout_profile  # noqa: E402


def test_main_window_starts(qtbot) -> None:
    window = MainWindow(Path(__file__).resolve().parents[1])
    qtbot.addWidget(window)
    window.show()
    assert window.windowTitle() == "MEA 5S 缺陷规律分析"
    assert window.tabs.count() == 7
    assert isinstance(window.tabs, WorkbenchStack)
    assert "Excel分析" in window.tabs.tabText(4)
    assert "关联分析" in window.tabs.tabText(5)
    assert window.workbench.navigation.buttons[0].isChecked()
    assert window.workbench.assistant.connection.text() == "未连接"
    assert "#091424" in window.styleSheet()
    assert window.minimumWidth() <= 980
    window.close()
    window.deleteLater()


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
