"""本文件定义智能助手的稳定数据接口与未连接模型时的面板实现。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


@dataclass(frozen=True)
class AssistantContext:
    """用户主动附加到一次对话中的业务上下文引用。"""

    task_name: str = ""
    page_name: str = ""
    references: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class AssistantRequest:
    """与具体模型供应商无关的助手请求。"""

    prompt: str
    context: AssistantContext = field(default_factory=AssistantContext)


class AssistantBackend(Protocol):
    """以后接入本地或云端模型时需要实现的最小接口。"""

    @property
    def available(self) -> bool: ...

    def submit(self, request: AssistantRequest) -> None: ...

    def cancel(self) -> None: ...


class NullAssistantBackend(QObject):
    """默认后端：明确保持离线且不传输任何数据。"""

    unavailable = Signal(str)

    @property
    def available(self) -> bool:
        return False

    def submit(self, request: AssistantRequest) -> None:
        self.unavailable.emit("当前未连接模型，数据不会发送。")

    def cancel(self) -> None:
        return


class AssistantPanel(QFrame):
    """可折叠工作台助手面板；首版只提供安全的离线外壳。"""

    collapse_requested = Signal()

    def __init__(self, backend: AssistantBackend | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("assistantPanel")
        self.setMinimumWidth(300)
        self.setMaximumWidth(440)
        self.backend = backend or NullAssistantBackend(self)
        if isinstance(self.backend, NullAssistantBackend):
            self.backend.unavailable.connect(self._show_notice)

        header = QHBoxLayout()
        identity = QLabel("AI  智能助手")
        identity.setObjectName("assistantTitle")
        self.connection = QLabel("未连接")
        self.connection.setObjectName("offlineBadge")
        collapse = QToolButton()
        collapse.setObjectName("assistantCollapse")
        collapse.setText("›")
        collapse.setToolTip("收起智能助手")
        collapse.clicked.connect(self.collapse_requested)
        header.addWidget(identity)
        header.addStretch()
        header.addWidget(self.connection)
        header.addWidget(collapse)

        privacy = QLabel("当前未连接模型，数据不会发送")
        privacy.setObjectName("privacyBanner")
        privacy.setWordWrap(True)

        self.messages = QListWidget()
        self.messages.setObjectName("assistantMessages")
        self.messages.addItem("你好，我是分析助手。\n连接模型后，我可以结合你主动选择的结果和图片回答问题。")

        context_header = QHBoxLayout()
        context_title = QLabel("本次上下文")
        context_title.setObjectName("sectionTitle")
        self.clear_context_button = QPushButton("清除")
        self.clear_context_button.setObjectName("quietButton")
        self.clear_context_button.clicked.connect(self.clear_context)
        context_header.addWidget(context_title)
        context_header.addStretch()
        context_header.addWidget(self.clear_context_button)
        self.context_label = QLabel("尚未附加任务、结果行或产品图片")
        self.context_label.setObjectName("assistantContext")
        self.context_label.setWordWrap(True)

        input_row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setObjectName("assistantInput")
        self.input.setPlaceholderText("输入问题（连接模型后可发送）")
        self.input.returnPressed.connect(self._submit)
        self.send_button = QPushButton("发送")
        self.send_button.setObjectName("assistantSendButton")
        self.send_button.clicked.connect(self._submit)
        input_row.addWidget(self.input, 1)
        input_row.addWidget(self.send_button)

        actions = QHBoxLayout()
        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.backend.cancel)
        clear = QPushButton("清空会话")
        clear.clicked.connect(self.clear_messages)
        actions.addWidget(self.stop_button)
        actions.addWidget(clear)
        actions.addStretch()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addLayout(header)
        layout.addWidget(privacy)
        layout.addWidget(self.messages, 1)
        layout.addLayout(context_header)
        layout.addWidget(self.context_label)
        layout.addLayout(input_row)
        layout.addLayout(actions)

    def set_context(self, context: AssistantContext) -> None:
        parts = [value for value in (context.task_name, context.page_name) if value]
        parts.extend(context.references)
        self.context_label.setText(" · ".join(parts) if parts else "尚未附加任务、结果行或产品图片")
        self.context_label.setProperty("assistantContextValue", context)

    def clear_context(self) -> None:
        self.set_context(AssistantContext())

    def clear_messages(self) -> None:
        self.messages.clear()
        self.messages.addItem("会话已清空。连接模型后可开始新的分析对话。")

    def _submit(self) -> None:
        prompt = self.input.text().strip()
        if not prompt:
            return
        context = self.context_label.property("assistantContextValue")
        if not isinstance(context, AssistantContext):
            context = AssistantContext()
        if not self.backend.available:
            self.backend.submit(AssistantRequest(prompt, context))
            return
        self.messages.addItem(f"你：{prompt}")
        self.input.clear()
        self.backend.submit(AssistantRequest(prompt, context))

    def _show_notice(self, message: str) -> None:
        self.messages.addItem(message)
