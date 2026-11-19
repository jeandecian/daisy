from typing import Any

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from daisy.core.llm import LocalBrain
from daisy.core.storage import ChatSession

DAISY: str = "D.A.I.S.Y."


class BrainWorker(QThread):
    reply_ready: Signal = Signal(str)
    tool_called: Signal = Signal(str, dict)

    def __init__(self, brain: LocalBrain, prompt: str):
        super().__init__()
        self.brain: LocalBrain = brain
        self.prompt: str = prompt

    def run(self) -> None:
        reply: str = self.brain.chat(
            self.prompt,
            on_tool_call=self.tool_called.emit,
        )
        self.reply_ready.emit(reply)


class DaisyWindow(QMainWindow):
    def __init__(self, brain: LocalBrain):
        super().__init__()
        self.brain: LocalBrain = brain
        self.worker: BrainWorker | None = None

        self.setWindowTitle(DAISY)
        self.resize(960, 640)

        splitter: QSplitter = QSplitter(Qt.Orientation.Horizontal)

        sidebar_widget: QWidget = QWidget()
        sidebar_layout: QVBoxLayout = QVBoxLayout(sidebar_widget)
        sidebar_layout.setContentsMargins(8, 8, 8, 8)

        self.new_chat_btn: QPushButton = QPushButton("+ New Chat")
        self.new_chat_btn.clicked.connect(self.on_new_chat)
        sidebar_layout.addWidget(self.new_chat_btn)

        self.session_list: QListWidget = QListWidget()
        self.session_list.itemClicked.connect(self.on_session_selected)
        sidebar_layout.addWidget(self.session_list)

        splitter.addWidget(sidebar_widget)

        chat_container: QWidget = QWidget()
        chat_layout: QVBoxLayout = QVBoxLayout(chat_container)
        chat_layout.setContentsMargins(8, 8, 8, 8)

        self.chat_display: QTextEdit = QTextEdit()
        self.chat_display.setReadOnly(True)

        self.status_label: QLabel = QLabel("Ready")

        self.input_field: QLineEdit = QLineEdit()
        self.input_field.setPlaceholderText("Ask D.A.I.S.Y. something...")
        self.input_field.returnPressed.connect(self.send_message)

        self.send_button: QPushButton = QPushButton("Send")
        self.send_button.clicked.connect(self.send_message)

        input_row: QHBoxLayout = QHBoxLayout()
        input_row.addWidget(self.input_field)
        input_row.addWidget(self.send_button)

        chat_layout.addWidget(self.chat_display)
        chat_layout.addWidget(self.status_label)
        chat_layout.addLayout(input_row)

        splitter.addWidget(chat_container)

        tools_widget: QWidget = QWidget()
        tools_layout: QVBoxLayout = QVBoxLayout(tools_widget)
        tools_layout.setContentsMargins(8, 8, 8, 8)

        tools_header: QLabel = QLabel("Available Tools")
        tools_header.setStyleSheet("font-weight: bold;")
        tools_layout.addWidget(tools_header)

        self.tools_list: QListWidget = QListWidget()
        self.tools_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.tools_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        tools_layout.addWidget(self.tools_list)

        splitter.addWidget(tools_widget)
        splitter.setSizes([190, 570, 200])

        self.setCentralWidget(splitter)

        self.refresh_tools_list()
        self.refresh_session_list()
        self._render_current_session()

    def refresh_tools_list(self) -> None:
        self.tools_list.clear()

        for tool_name, label in self.brain.tools.display_names():
            item: QListWidgetItem = QListWidgetItem(label)
            item.setToolTip(tool_name)
            self.tools_list.addItem(item)

    def refresh_session_list(self) -> None:
        self.session_list.clear()

        sessions: list[ChatSession] = self.brain.store.list_sessions()
        for session in sessions:
            display_text: str = f"{session.title}\n({session.updated_at[11:16]})"
            item: QListWidgetItem = QListWidgetItem(display_text)
            item.setData(Qt.ItemDataRole.UserRole, session.id)
            self.session_list.addItem(item)

            if session.id == self.brain.session.id:
                item.setSelected(True)

    def _render_current_session(self) -> None:
        self.chat_display.clear()

        for msg in self.brain.session.messages:
            role: str | None = msg.get("role")
            content: str = msg.get("content", "")
            if role == "user":
                self._append_message("You", content)
            elif role == "assistant" and content:
                self._append_message(DAISY, content)
            elif role == "tool":
                tool_name: str = msg.get("tool_name", "unknown")
                tool_args: dict[str, Any] = msg.get("tool_args", {})
                self._append_tool_action(tool_name, tool_args)

    def on_new_chat(self) -> None:
        if self.worker and self.worker.isRunning():
            return

        self.brain.new_session()
        self._render_current_session()
        self.refresh_session_list()
        self.status_label.setText("Ready")

    def on_session_selected(self, item: QListWidgetItem) -> None:
        if self.worker and self.worker.isRunning():
            return

        session_id: str = item.data(Qt.ItemDataRole.UserRole)
        if session_id and self.brain.load_session(session_id):
            self._render_current_session()
            self.refresh_session_list()
            self.status_label.setText("Ready")

    def send_message(self) -> None:
        prompt: str = self.input_field.text().strip()
        if not prompt or (self.worker and self.worker.isRunning()):
            return

        self.input_field.clear()
        self._set_busy(True, "Thinking...")
        self._append_message("You", prompt)

        self.worker = BrainWorker(self.brain, prompt)
        self.worker.tool_called.connect(self._on_tool_called)
        self.worker.reply_ready.connect(self._on_reply)
        self.worker.start()

    def _on_tool_called(self, tool_name: str, tool_args: dict[str, Any]) -> None:
        self.status_label.setText(f"Running {tool_name}...")
        self._append_tool_action(tool_name, tool_args)

    def _on_reply(self, reply: str) -> None:
        self._append_message(DAISY, reply)
        self._set_busy(False, "Ready")
        self.refresh_session_list()

    def _append_message(self, sender: str, text: str) -> None:
        self.chat_display.append(f"<b>{sender}:</b> {text}<br>")

    def _append_tool_action(self, tool_name: str, tool_args: dict[str, Any]) -> None:
        args_str: str = ", ".join(f"{k}={v!r}" for k, v in tool_args.items())
        self.chat_display.append(
            f"<span style='color: #888888; font-size: 11px;'>"
            f"⚙ <i>Used tool: <b>{tool_name}({args_str})</b></i>"
            f"</span><br>"
        )

    def _set_busy(self, busy: bool, status: str) -> None:
        self.input_field.setEnabled(not busy)
        self.send_button.setEnabled(not busy)
        self.new_chat_btn.setEnabled(not busy)
        self.session_list.setEnabled(not busy)
        self.status_label.setText(status)
        if not busy:
            self.input_field.setFocus()
