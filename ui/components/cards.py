"""Reusable card / stat widgets."""
from __future__ import annotations

from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QHBoxLayout, QWidget

from chopster.ui.theme import THEMES


class Card(QFrame):
    def __init__(self, parent: QWidget | None = None, theme: str = "dark"):
        super().__init__(parent)
        self.setObjectName("card")
        C = THEMES.get(theme, THEMES["dark"])
        self._theme = theme


class StatCard(Card):
    def __init__(self, title: str, value: str = "0", color: str | None = None, theme: str = "dark", parent: QWidget | None = None):
        super().__init__(parent, theme)
        C = THEMES.get(theme, THEMES["dark"])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(4)
        t = QLabel(title, self)
        t.setStyleSheet(f"color: {C['MUTED']}; font-size: 10px; font-weight: 700;")
        lay.addWidget(t)
        self.value_label = QLabel(value, self)
        self.value_label.setStyleSheet(f"color: {color or C['TEXT']}; font-size: 24px; font-weight: 800;")
        lay.addWidget(self.value_label)

    def set_value(self, v: str) -> None:
        self.value_label.setText(v)
