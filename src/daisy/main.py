import sys

from PySide6.QtWidgets import QApplication

from daisy.core.llm import LocalBrain
from daisy.ui.window import DaisyWindow

app: QApplication = QApplication(sys.argv)

brain: LocalBrain = LocalBrain()
window: DaisyWindow = DaisyWindow(brain)
window.show()

sys.exit(app.exec())
