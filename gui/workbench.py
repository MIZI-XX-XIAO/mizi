"""本文件实现深蓝工业工作台的导航、顶栏、页面栈与响应式布局。"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .assistant import AssistantContext, AssistantPanel


@dataclass(frozen=True)
class NavigationItem:
    title: str
    icon: str
    subtitle: str


NAVIGATION_ITEMS = (
    NavigationItem("新建任务", "＋", "配置数据与分析参数"),
    NavigationItem("数据检查", "✓", "验证字段和图片路径"),
    NavigationItem("执行分析", "▶", "查看进度与实时告警"),
    NavigationItem("结果概览", "▦", "规律、预警与序列关系"),
    NavigationItem("Excel分析", "▤", "测试数据、容差与质量诊断"),
    NavigationItem("关联分析", "⌁", "工艺参数与缺陷关联"),
    NavigationItem("图片复核", "▣", "对比图像与缺陷区域"),
)


class WorkbenchStack(QStackedWidget):
    """提供旧 QTabWidget 的少量兼容方法，业务流程无需感知外壳重构。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._titles: list[str] = []

    def addTab(self, widget: QWidget, title: str) -> int:
        self._titles.append(title.lstrip("①②③④⑤⑥ ").strip())
        return self.addWidget(widget)

    def tabText(self, index: int) -> str:
        return self._titles[index]


class NavigationRail(QFrame):
    route_selected = Signal(int)
    exit_requested = Signal()

    def __init__(self, version: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("navigationRail")
        self.setMinimumWidth(190)
        self.setMaximumWidth(230)

        brand = QFrame()
        brand.setObjectName("brandCard")
        brand_layout = QVBoxLayout(brand)
        brand_layout.setContentsMargins(14, 14, 14, 14)
        self.brand_title = QLabel("◆  MEA 5S")
        self.brand_title.setObjectName("brandTitle")
        self.brand_caption = QLabel("DEFECT ANALYTICS")
        self.brand_caption.setObjectName("brandCaption")
        brand_layout.addWidget(self.brand_title)
        brand_layout.addWidget(self.brand_caption)

        self.user = QLabel("●  质量分析工作台")
        self.user.setObjectName("currentUser")

        self.section = QLabel("分析流程")
        self.section.setObjectName("navigationSection")
        self.buttons: list[QPushButton] = []
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        nav_layout = QVBoxLayout()
        nav_layout.setSpacing(6)
        for index, item in enumerate(NAVIGATION_ITEMS):
            button = QPushButton(f"{item.icon}   {item.title}")
            button.setObjectName("navigationButton")
            button.setCheckable(True)
            button.setProperty("routeIndex", index)
            button.setToolTip(item.subtitle)
            button.clicked.connect(lambda checked=False, route=index: self.route_selected.emit(route))
            self.group.addButton(button, index)
            self.buttons.append(button)
            nav_layout.addWidget(button)
        self.buttons[0].setChecked(True)

        self.exit_button = QPushButton("↪   退出程序")
        self.exit_button.setObjectName("exitButton")
        self.exit_button.clicked.connect(self.exit_requested)
        self.version_label = QLabel(f"版本 {version}")
        self.version_label.setObjectName("navVersion")
        self.version_label.setAlignment(Qt.AlignCenter)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 20, 16, 16)
        layout.setSpacing(14)
        layout.addWidget(brand)
        layout.addWidget(self.user)
        layout.addWidget(self.section)
        layout.addLayout(nav_layout)
        layout.addStretch()
        layout.addWidget(self.exit_button)
        layout.addWidget(self.version_label)

    def set_current(self, index: int) -> None:
        if 0 <= index < len(self.buttons):
            self.buttons[index].setChecked(True)

    def set_compact(self, compact: bool) -> None:
        self.setFixedWidth(76 if compact else 214)
        for index, button in enumerate(self.buttons):
            item = NAVIGATION_ITEMS[index]
            button.setText(item.icon if compact else f"{item.icon}   {item.title}")
            button.setToolTip(item.title if compact else item.subtitle)
        self.brand_title.setText("M5" if compact else "◆  MEA 5S")
        self.brand_title.setAlignment(Qt.AlignCenter if compact else Qt.AlignLeft)
        self.brand_caption.setVisible(not compact)
        self.user.setVisible(not compact)
        self.section.setVisible(not compact)
        self.exit_button.setText("↪" if compact else "↪   退出程序")
        self.exit_button.setToolTip("退出程序" if compact else "")
        self.version_label.setVisible(not compact)


class HeaderBar(QFrame):
    assistant_toggled = Signal(bool)
    navigation_toggled = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("headerBar")
        menu = QToolButton()
        menu.setObjectName("headerMenu")
        menu.setText("☰")
        menu.setToolTip("收起或展开导航")
        menu.clicked.connect(self.navigation_toggled)
        self.title = QLabel(NAVIGATION_ITEMS[0].title)
        self.title.setObjectName("workspaceTitle")
        self.subtitle = QLabel(NAVIGATION_ITEMS[0].subtitle)
        self.subtitle.setObjectName("workspaceSubtitle")
        title_layout = QVBoxLayout()
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(2)
        title_layout.addWidget(self.title)
        title_layout.addWidget(self.subtitle)

        self.task_context = QLabel("任务：5S分析")
        self.task_context.setObjectName("taskContext")
        self.run_status = QLabel("●  就绪")
        self.run_status.setObjectName("readyBadge")
        self.assistant_button = QPushButton("AI  智能助手")
        self.assistant_button.setObjectName("assistantToggle")
        self.assistant_button.setCheckable(True)
        self.assistant_button.setChecked(True)
        self.assistant_button.toggled.connect(self.assistant_toggled)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 12, 20, 12)
        layout.setSpacing(14)
        layout.addWidget(menu)
        layout.addLayout(title_layout)
        layout.addStretch()
        layout.addWidget(self.task_context)
        layout.addWidget(self.run_status)
        layout.addWidget(self.assistant_button)

    def set_route(self, index: int) -> None:
        item = NAVIGATION_ITEMS[index]
        self.title.setText(item.title)
        self.subtitle.setText(item.subtitle)

    def set_task_name(self, task_name: str) -> None:
        self.task_context.setText(f"任务：{task_name.strip() or '未命名任务'}")

    def set_run_state(self, text: str, state: str = "ready") -> None:
        symbols = {"ready": "●", "running": "◉", "success": "●", "error": "!"}
        self.run_status.setText(f"{symbols.get(state, '●')}  {text}")
        self.run_status.setObjectName(f"{state}Badge")
        self.run_status.style().unpolish(self.run_status)
        self.run_status.style().polish(self.run_status)


class WorkbenchShell(QWidget):
    """包裹业务页面的可调整三栏工作台。"""

    def __init__(self, stack: WorkbenchStack, version: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("workbenchShell")
        self.stack = stack
        self.navigation = NavigationRail(version)
        self.header = HeaderBar()
        self.assistant = AssistantPanel()
        self._navigation_compact = False
        self._small_screen = False

        content = QFrame()
        content.setObjectName("contentSurface")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        content_layout.addWidget(self.header)
        page_frame = QFrame()
        page_frame.setObjectName("pageSurface")
        page_layout = QVBoxLayout(page_frame)
        page_layout.setContentsMargins(18, 16, 18, 16)
        page_layout.addWidget(stack)
        content_layout.addWidget(page_frame, 1)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setObjectName("workspaceSplitter")
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(content)
        self.splitter.addWidget(self.assistant)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([1050, 350])

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.navigation)
        layout.addWidget(self.splitter, 1)

        self.navigation.route_selected.connect(self.stack.setCurrentIndex)
        self.stack.currentChanged.connect(self._route_changed)
        self.header.assistant_toggled.connect(self.set_assistant_visible)
        self.header.navigation_toggled.connect(self.toggle_navigation)
        self.assistant.collapse_requested.connect(lambda: self.set_assistant_visible(False))

    def _route_changed(self, index: int) -> None:
        self.navigation.set_current(index)
        self.header.set_route(index)
        task = self.header.task_context.text().removeprefix("任务：")
        self.assistant.set_context(AssistantContext(task, NAVIGATION_ITEMS[index].title))

    def set_assistant_visible(self, visible: bool) -> None:
        self.assistant.setVisible(visible)
        self.header.assistant_button.blockSignals(True)
        self.header.assistant_button.setChecked(visible)
        self.header.assistant_button.blockSignals(False)
        if visible:
            self.splitter.setSizes([max(700, self.width() - 360), 350])

    def toggle_navigation(self) -> None:
        self._navigation_compact = not self._navigation_compact
        self.navigation.set_compact(self._navigation_compact)

    def apply_responsive_layout(self, width: int) -> None:
        small = width < 1420
        if small == self._small_screen:
            return
        self._small_screen = small
        self._navigation_compact = small
        self.navigation.set_compact(small)
        self.set_assistant_visible(not small)
