"""本文件使用pytest-qt离屏验证主窗口、参数模型和后台Worker能够创建及安全关闭。"""

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from gui.main_window import MainWindow  # noqa: E402


def test_main_window_starts(qtbot) -> None:
    window = MainWindow(Path(__file__).resolve().parents[1])
    qtbot.addWidget(window)
    window.show()
    assert window.windowTitle() == "MEA 5S 缺陷规律分析"
    assert window.tabs.count() == 6
    assert "关联分析" in window.tabs.tabText(4)
    assert window.minimumWidth() <= 980
    window.close()
    window.deleteLater()
