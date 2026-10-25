"""Premium theme switcher — labeled pill + custom column dropdown panel.

Purpose is explicit on the button ("Tema  <current>") and the dropdown is a
fully styled panel (colour swatch, name, one-line description, check mark on
the active theme) instead of the plain native menu, so it reads as a real
product control across every theme.
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from chopster.ui.theme import THEMES, THEME_NAMES

THEME_ORDER: tuple[str, ...] = ("dark", "modern", "3d", "casual")
THEME_TAGLINE: dict[str, str] = {
    "dark": "Charcoal + emas, gaya klasik",
    "modern": "Terang bersih, aksen indigo",
    "3d": "Neon cyan-violet gelap",
    "casual": "Krem hangat yang membulat",
}


def _swatch(color: str, size: int = 14) -> QPixmap:
    """Round colour dot used as the row icon."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.NoPen)
    p.drawEllipse(0, 0, size - 1, size - 1)
    p.end()
    return pm


class _ThemeRow(QFrame):
    """One clickable option inside the dropdown panel."""

    clicked = Signal(str)

    def __init__(self, tid: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._tid = tid
        self.setObjectName("themeRow")
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_StyledBackground, True)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(10)

        icon = QLabel(self)
        icon.setPixmap(_swatch(THEMES[tid]["GOLD"], 16))
        icon.setFixedSize(18, 18)
        lay.addWidget(icon)

        names = QVBoxLayout()
        names.setSpacing(1)
        nm = QLabel(THEME_NAMES.get(tid, tid), self)
        nm.setObjectName("themeRowName")
        names.addWidget(nm)
        tg = QLabel(THEME_TAGLINE.get(tid, ""), self)
        tg.setObjectName("themeRowTag")
        names.addWidget(tg)
        lay.addLayout(names, 1)

        self.mark = QLabel("", self)
        self.mark.setObjectName("themeRowMark")
        self.mark.setFixedWidth(18)
        self.mark.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.mark)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._tid)
        super().mousePressEvent(event)


class ThemeSwitcher(QFrame):
    themeSelected = Signal(str)

    def __init__(self, theme: str = "dark", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("themeSwitcher")
        self.setAttribute(Qt.WA_StyledBackground, True)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(0)

        self.btn = QPushButton(self)
        self.btn.setObjectName("themeMenu")
        self.btn.setCursor(Qt.PointingHandCursor)
        self.btn.clicked.connect(self._toggle_panel)
        lay.addWidget(self.btn)

        self._panel = QFrame(None)
        self._panel.setObjectName("themePanel")
        self._panel.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self._panel.setAttribute(Qt.WA_StyledBackground, True)
        pl = QVBoxLayout(self._panel)
        pl.setContentsMargins(10, 10, 10, 10)
        pl.setSpacing(4)

        cap = QLabel("PILIH TEMA", self._panel)
        cap.setObjectName("themePanelCap")
        pl.addWidget(cap)

        self._rows: dict[str, _ThemeRow] = {}
        for tid in THEME_ORDER:
            row = _ThemeRow(tid, self._panel)
            row.clicked.connect(self._on_picked)
            pl.addWidget(row)
            self._rows[tid] = row

        self._theme = theme if theme in THEMES else "dark"
        self.set_theme(self._theme)

    # -- panel lifecycle --------------------------------------------------
    def _toggle_panel(self) -> None:
        if self._panel.isVisible():
            self._panel.hide()
            return
        self._position_panel()
        self._panel.show()
        self._panel.raise_()

    def _position_panel(self) -> None:
        self._panel.adjustSize()
        size = self._panel.sizeHint()
        anchor = self.btn.mapToGlobal(QPoint(0, self.btn.height()))
        x = anchor.x() - (size.width() - self.btn.width()) // 2
        y = anchor.y() + 6
        try:
            screen = QApplication.screenAt(anchor) or QApplication.primaryScreen()
            if screen is not None:
                geo: QRect = screen.availableGeometry()
                x = max(geo.left() + 4, min(x, geo.right() - size.width() - 4))
                y = max(geo.top() + 4, min(y, geo.bottom() - size.height() - 4))
        except Exception:
            pass
        self._panel.move(x, y)

    def _on_picked(self, tid: str) -> None:
        self._panel.hide()
        if tid in THEMES:
            self.themeSelected.emit(tid)

    # -- state ------------------------------------------------------------
    def set_theme(self, theme: str) -> None:
        """Mark *theme* active: button label, icon, and the checked row."""
        self._panel.hide()
        self._theme = theme if theme in THEMES else "dark"
        self.btn.setText(f"Tema  {THEME_NAMES.get(self._theme, self._theme)}  ▾")
        for tid, row in self._rows.items():
            active = tid == self._theme
            row.mark.setText("✓" if active else "")
            nm = row.findChild(QLabel, "themeRowName")
            if nm is not None:
                nm.setStyleSheet(
                    (f"color: {THEMES[tid]['GOLD']}; font-size: 13px; font-weight: 900;"
                     if active else ""))

    def current_theme(self) -> str:
        return self._theme
