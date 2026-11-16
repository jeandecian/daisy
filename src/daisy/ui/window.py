from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from daisy.core.llm import LocalBrain


class BrainWorker(QThread):
    reply_ready: Signal = Signal(str)

    def __init__(self, brain: LocalBrain, prompt: str):
        super().__init__()
        self.brain: LocalBrain = brain
        self.prompt: str = prompt

    def run(self) -> None:
        reply: str = self.brain.chat(self.prompt)
        self.reply_ready.emit(reply)


class DaisyWindow(QMainWindow):
    def __init__(self, brain: LocalBrain):
        super().__init__()
        self.brain: LocalBrain = brain
        self.worker: BrainWorker | None = None

        self.setWindowTitle("D.A.I.S.Y.")
        self.resize(520, 640)

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

        layout: QVBoxLayout = QVBoxLayout()
        layout.addWidget(self.chat_display)
        layout.addWidget(self.status_label)
        layout.addLayout(input_row)

        container: QWidget = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

    def send_message(self) -> None:
        prompt: str = self.input_field.text().strip()
        if not prompt or (self.worker and self.worker.isRunning()):
            return

        self.input_field.clear()
        self._set_busy(True, "Thinking...")
        self._append_message("You", prompt)

        self.worker = BrainWorker(self.brain, prompt)
        self.worker.reply_ready.connect(self._on_reply)
        self.worker.start()

    def _on_reply(self, reply: str) -> None:
        self._append_message("D.A.I.S.Y.", reply)
        self._set_busy(False, "Ready")

    def _append_message(self, sender: str, text: str) -> None:
        self.chat_display.append(f"<b>{sender}:</b> {text}<br>")

    def _set_busy(self, busy: bool, status: str) -> None:
        self.input_field.setEnabled(not busy)
        self.send_button.setEnabled(not busy)
        self.status_label.setText(status)
        if not busy:
            self.input_field.setFocus()
