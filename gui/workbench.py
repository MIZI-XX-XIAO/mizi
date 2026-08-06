"""本文件实现深蓝工业工作台的导航、顶栏、页面栈与响应式布局。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from PySide6.QtCore import QSize, Signal, Qt
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


class LayoutProfile(Enum):
    """使用Qt逻辑像素描述工作台可用空间，而不是物理屏幕分辨率。"""

    FULL = "full"
    COMPACT = "compact"
    TIGHT = "tight"


def resolve_layout_profile(size: QSize) -> LayoutProfile:
    """根据窗口客户区逻辑尺寸选择稳定的响应式档位。"""

    if size.width() >= 1500 and size.height() >= 900:
        return LayoutProfile.FULL
    if size.width() >= 1100 and size.height() >= 720:
        return LayoutProfile.COMPACT
    return LayoutProfile.TIGHT


class ElidedLabel(QLabel):
    """在空间不足时省略长文本，同时在提示中保留完整内容。"""

    def __init__(self, text: str = "", parent=None, mode=Qt.ElideMiddle) -> None:
        super().__init__(parent)
        self._full_text = ""
        self._elide_mode = mode
        self.setMinimumWidth(0)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt API compatibility
        self._full_text = str(text)
        self.setToolTip(self._full_text)
        self._refresh_text()

    def fullText(self) -> str:  # noqa: N802 - Qt API compatibility
        return self._full_text

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh_text()

    def _refresh_text(self) -> None:
        shown = self.fontMetrics().elidedText(
            self._full_text, self._elide_mode, max(0, self.width() - 8)
        )
        QLabel.setText(self, shown)


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
        self._titles.append(title.lstrip("①②③④⑤⑥⑦ ").strip())
        return self.addWidget(widget)

    def tabText(self, index: int) -> str:
        return self._titles[index]

    def minimumSizeHint(self) -> QSize:
        return QSize(0, 0)

    def sizeHint(self) -> QSize:
        current = self.currentWidget()
        return current.sizeHint() if current is not None else QSize(0, 0)


class NavigationRail(QFrame):
    route_selected = Signal(int)
    exit_requested = Signal()

    def __init__(self, version: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("navigationRail")
        brand = QFrame()
        brand.setObjectName("brandCard")
        brand_layout = QVBoxLayout(brand)
        brand_layout.setContentsMargins(14, 14, 14, 14)
        self.brand_title = QLabel("◆  MEA 多工站")
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

        self.rail_layout = QVBoxLayout(self)
        self.rail_layout.setSpacing(14)
        self.rail_layout.addWidget(brand)
        self.rail_layout.addWidget(self.user)
        self.rail_layout.addWidget(self.section)
        self.rail_layout.addLayout(nav_layout)
        self.rail_layout.addStretch()
        self.rail_layout.addWidget(self.exit_button)
        self.rail_layout.addWidget(self.version_label)
        self.set_compact(False)

    def set_current(self, index: int) -> None:
        if 0 <= index < len(self.buttons):
            self.buttons[index].setChecked(True)

    def set_compact(self, compact: bool) -> None:
        self.setFixedWidth(76 if compact else 214)
        self.rail_layout.setContentsMargins(
            *((8, 12, 8, 10) if compact else (16, 20, 16, 16))
        )
        for index, button in enumerate(self.buttons):
            item = NAVIGATION_ITEMS[index]
            button.setText(item.icon if compact else f"{item.icon}   {item.title}")
            button.setToolTip(item.title if compact else item.subtitle)
        self.brand_title.setText("MEA" if compact else "◆  MEA 多工站")
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
        self.menu = QToolButton()
        self.menu.setObjectName("headerMenu")
        self.menu.setText("☰")
        self.menu.setToolTip("收起或展开导航")
        self.menu.clicked.connect(self.navigation_toggled)
        self.title = QLabel(NAVIGATION_ITEMS[0].title)
        self.title.setObjectName("workspaceTitle")
        self.subtitle = QLabel(NAVIGATION_ITEMS[0].subtitle)
        self.subtitle.setObjectName("workspaceSubtitle")
        self.title_layout = QVBoxLayout()
        self.title_layout.setContentsMargins(0, 0, 0, 0)
        self.title_layout.setSpacing(2)
        self.title_layout.addWidget(self.title)
        self.title_layout.addWidget(self.subtitle)

        self.task_context = ElidedLabel("任务：5S分析", mode=Qt.ElideRight)
        self.task_context.setObjectName("taskContext")
        self.task_context.setMaximumWidth(280)
        self.run_status = ElidedLabel("●  就绪", mode=Qt.ElideRight)
        self.run_status.setObjectName("readyBadge")
        self.run_status.setMaximumWidth(180)
        self.assistant_button = QPushButton("AI  智能助手")
        self.assistant_button.setObjectName("assistantToggle")
        self.assistant_button.setCheckable(True)
        self.assistant_button.setChecked(True)
        self.assistant_button.toggled.connect(self.assistant_toggled)

        self.header_layout = QHBoxLayout(self)
        self.header_layout.addWidget(self.menu)
        self.header_layout.addLayout(self.title_layout)
        self.header_layout.addStretch()
        self.header_layout.addWidget(self.task_context)
        self.header_layout.addWidget(self.run_status)
        self.header_layout.addWidget(self.assistant_button)
        self.set_layout_profile(LayoutProfile.FULL)

    def set_layout_profile(self, profile: LayoutProfile) -> None:
        full = profile is LayoutProfile.FULL
        tight = profile is LayoutProfile.TIGHT
        self.subtitle.setVisible(full)
        self.task_context.setVisible(full)
        self.assistant_button.setText("AI" if tight else "AI  智能助手")
        margins = (
            (8, 7, 8, 7) if tight else
            (12, 9, 12, 9) if not full else
            (20, 12, 20, 12)
        )
        self.header_layout.setContentsMargins(*margins)
        self.header_layout.setSpacing(8 if not full else 14)

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
    """包裹业务页面，并在窄逻辑视口中将助手改为浮层。"""

    def __init__(self, stack: WorkbenchStack, version: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("workbenchShell")
        self.stack = stack
        self.navigation = NavigationRail(version)
        self.header = HeaderBar()
        self.assistant = AssistantPanel()
        self.profile = LayoutProfile.FULL
        self._navigation_compact = False
        self._desktop_assistant_visible = True
        self._compact_assistant_visible = False
        self._assistant_overlay = False

        self.content = QFrame()
        self.content.setObjectName("contentSurface")
        content_layout = QVBoxLayout(self.content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        content_layout.addWidget(self.header)
        self.page_frame = QFrame()
        self.page_frame.setObjectName("pageSurface")
        self.page_layout = QVBoxLayout(self.page_frame)
        self.page_layout.setContentsMargins(18, 16, 18, 16)
        self.page_layout.addWidget(stack)
        content_layout.addWidget(self.page_frame, 1)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setObjectName("workspaceSplitter")
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.content)
        self.splitter.addWidget(self.assistant)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([1050, 350])

        self.workspace_host = QWidget()
        host_layout = QVBoxLayout(self.workspace_host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        host_layout.addWidget(self.splitter)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.navigation)
        layout.addWidget(self.workspace_host, 1)

        self.navigation.route_selected.connect(self.stack.setCurrentIndex)
        self.stack.currentChanged.connect(self._route_changed)
        self.header.assistant_toggled.connect(self.set_assistant_visible)
        self.header.navigation_toggled.connect(self.toggle_navigation)
        self.assistant.collapse_requested.connect(lambda: self.set_assistant_visible(False))

    def _route_changed(self, index: int) -> None:
        self.navigation.set_current(index)
        self.header.set_route(index)
        task = self.header.task_context.fullText().removeprefix("任务：")
        self.assistant.set_context(AssistantContext(task, NAVIGATION_ITEMS[index].title))

    def set_assistant_visible(self, visible: bool) -> None:
        if self.profile is LayoutProfile.FULL:
            self._desktop_assistant_visible = visible
        else:
            self._compact_assistant_visible = visible
        self.assistant.setVisible(visible)
        if visible:
            if self._assistant_overlay:
                self._place_assistant_overlay()
                self.assistant.raise_()
            else:
                self.splitter.setSizes([max(700, self.width() - 360), 350])
        self.header.assistant_button.blockSignals(True)
        self.header.assistant_button.setChecked(visible)
        self.header.assistant_button.blockSignals(False)

    def toggle_navigation(self) -> None:
        if self.profile is LayoutProfile.FULL:
            self._navigation_compact = not self._navigation_compact
            self.navigation.set_compact(self._navigation_compact)
        else:
            self.navigation.setVisible(not self.navigation.isVisible())

    def apply_responsive_layout(self, size: QSize, force: bool = False) -> None:
        profile = resolve_layout_profile(size)
        if not force and profile is self.profile:
            self._place_assistant_overlay()
            return
        previous = self.profile
        self.profile = profile
        compact = profile is not LayoutProfile.FULL
        self._navigation_compact = compact
        self.navigation.setVisible(True)
        self.navigation.set_compact(compact)
        self.header.set_layout_profile(profile)
        margin = 8 if profile is LayoutProfile.TIGHT else 12 if compact else 18
        vertical = 8 if profile is LayoutProfile.TIGHT else 12 if compact else 16
        self.page_layout.setContentsMargins(margin, vertical, margin, vertical)
        if compact:
            self._move_assistant_to_overlay()
            if previous is LayoutProfile.FULL:
                self._compact_assistant_visible = False
            self.set_assistant_visible(self._compact_assistant_visible)
        else:
            self._move_assistant_to_splitter()
            self.set_assistant_visible(self._desktop_assistant_visible)
        for index in range(self.stack.count()):
            setter = getattr(self.stack.widget(index), "set_layout_profile", None)
            if callable(setter):
                setter(profile)

    def _move_assistant_to_overlay(self) -> None:
        if self._assistant_overlay:
            return
        self.assistant.hide()
        self.assistant.setParent(self.workspace_host)
        self._assistant_overlay = True
        self._place_assistant_overlay()

    def _move_assistant_to_splitter(self) -> None:
        if not self._assistant_overlay:
            return
        self.assistant.hide()
        self.assistant.setParent(self.splitter)
        self.splitter.addWidget(self.assistant)
        self.splitter.setStretchFactor(1, 0)
        self._assistant_overlay = False
        self.splitter.setSizes([max(700, self.width() - 360), 350])

    def _place_assistant_overlay(self) -> None:
        if not self._assistant_overlay:
            return
        available = self.workspace_host.rect()
        width = min(400, max(320, int(available.width() * 0.38)))
        self.assistant.setGeometry(
            available.right() - width + 1, 0, width, available.height()
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_assistant_overlay()
