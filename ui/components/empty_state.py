from __future__ import annotations
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel
from chopster.ui.theme import THEMES

class EmptyState(QWidget):
    def __init__(self, text: str, theme: str = "dark", parent: QWidget | None = None):
        super().__init__(parent)
        C = THEMES.get(theme, THEMES["dark"])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 40, 20, 40)
        lbl = QLabel(text, self)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"color: {C['FOOT']}; font-size: 12px;")
        lbl.setAlignment(__import__("PySide6.QtCore", fromlist=["Qt"]).Qt.AlignCenter)  # type: ignore
        lay.addWidget(lbl)
