"""Sidebar navigation widget."""
from __future__ import annotations

from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QFrame, QPushButton, QVBoxLayout, QLabel, QWidget

from chopster.app.paths import resource_path
from chopster.ui.theme import THEMES

NAV_ITEMS: list[tuple[str, str]] = [
    ("dashboard", "◈  Dashboard"),
    ("download", "⬇  Download"),
    ("clipper", "✂  Content Clipper AI"),
    ("auto_clip", "✦  Auto Clip Studio"),
    ("history", "🕘  History"),
    ("settings", "⚙  Settings"),
]


class Sidebar(QFrame):
    navigate = Signal(str)

    def __init__(self, theme: str = "dark", parent: QWidget | None = None):
        super().__init__(parent)
        C = THEMES.get(theme, THEMES["dark"])
        self.setObjectName("sidebar")
        self.setStyleSheet(f"QFrame#sidebar {{ background: {C['PANEL']}; border-right: 1px solid {C['BORDER']}; }}")
        self.setFixedWidth(236)
        self._buttons: dict[str, QPushButton] = {}
        self._current = "dashboard"

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 20, 16, 20)
        lay.setSpacing(6)

        # logo area
        logo_lbl = QLabel(self)
        logo_lbl.setAlignment(Qt.AlignCenter)
        self._logo_pix: QPixmap | None = None
        try:
            _pix = QPixmap(str(resource_path("resources/icons/logo.png")))
            if not _pix.isNull():
                _pix.setDevicePixelRatio(1.0)
                self._logo_pix = _pix.scaled(104, 104, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                logo_lbl.setPixmap(self._logo_pix)
        except Exception:
            self._logo_pix = None
        logo_lbl.setFixedSize(104, 104)
        lay.addWidget(logo_lbl)

        title = QLabel("Chopster", self)
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(f"color: {C['GOLD']}; font-size: 20px; font-weight: 800;")
        lay.addWidget(title)
        sub = QLabel("by Aris", self)
        sub.setAlignment(Qt.AlignCenter)
        sub.setStyleSheet(f"color: {C['TEXT']}; font-size: 11px;")
        lay.addWidget(sub)
        lay.addSpacing(14)

        for key, label in NAV_ITEMS:
            btn = QPushButton(label, self)
            btn.setObjectName("navActive" if key == self._current else "nav")
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _=False, k=key: self._on_click(k))
            lay.addWidget(btn)
            self._buttons[key] = btn

        lay.addStretch()
        foot = QLabel("Download Faster,\nWatch Better", self)
        foot.setStyleSheet(f"color: {C['FOOT']}; font-size: 10px;")
        lay.addWidget(foot)
        self._theme = theme
        self._compact=False
        self._lay=lay; self._title=title; self._sub=sub; self._foot=foot; self._logo_lbl=logo_lbl

    def set_compact(self,compact: bool):
        compact=bool(compact); self._compact=compact; self.setFixedWidth(72 if compact else 220)
        try:
            self._lay.setContentsMargins(6 if compact else 14,14,6 if compact else 14,14)
            self._title.setText("C" if compact else "Chopster"); self._sub.setVisible(not compact); self._foot.setVisible(not compact)
            if self._logo_pix is not None:
                size = 40 if compact else 104
                self._logo_lbl.setFixedSize(size, size)
                self._logo_lbl.setPixmap(self._logo_pix.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            else:
                self._logo_lbl.setVisible(not compact)
            for key,btn in self._buttons.items():
                label=dict(NAV_ITEMS)[key]
                if compact:
                    btn.setText(label.split()[0]); btn.setToolTip(label); btn.setMinimumWidth(48); btn.setMaximumWidth(58)
                else:
                    btn.setText(label); btn.setToolTip(""); btn.setMinimumWidth(0); btn.setMaximumWidth(16777215)
        except Exception: pass

    def _on_click(self, key: str) -> None:
        self.set_active(key)
        self.navigate.emit(key)

    def set_active(self, key: str) -> None:
        self._current = key
        for k, btn in self._buttons.items():
            btn.setObjectName("navActive" if k == key else "nav")
            btn.style().unpolish(btn)
            btn.style().polish(btn)
