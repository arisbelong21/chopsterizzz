"""Dashboard — overview stats + recent activity + quick actions."""
from __future__ import annotations

from pathlib import Path
import shutil

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame, QScrollArea
)

from chopster.ui.theme import THEMES
from chopster.ui.components.cards import Card, StatCard


class DashboardPage(QWidget):
    navigate = Signal(str)

    def __init__(self, app, theme: str = "dark", parent: QWidget | None = None):
        super().__init__(parent)
        self.app = app
        self._theme = theme
        C = THEMES.get(theme, THEMES["dark"])

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(14)

        # header row
        head = QHBoxLayout()
        title = QLabel("Dashboard", self)
        title.setObjectName("h1")
        head.addWidget(title)
        head.addStretch()
        root.addLayout(head)

        # stats row
        stats = QHBoxLayout()
        stats.setSpacing(10)
        self.stat_downloads = StatCard("TOTAL DOWNLOAD", "0", C["TEXT"], theme, self)
        self.stat_success = StatCard("BERHASIL", "0", C["GREEN"], theme, self)
        self.stat_failed = StatCard("GAGAL", "0", C["RED"], theme, self)
        self.stat_projects = StatCard("PROJECT CLIPPER", "0", C["GOLD"], theme, self)
        for w in (self.stat_downloads, self.stat_success, self.stat_failed, self.stat_projects):
            stats.addWidget(w)
        root.addLayout(stats)

        # two columns: quick actions + info
        cols = QHBoxLayout()
        cols.setSpacing(12)

        # quick actions card
        qa = Card(self, theme)
        qa_lay = QVBoxLayout(qa)
        qa_lay.setContentsMargins(18, 16, 18, 16)
        qa_lay.setSpacing(10)
        qh = QLabel("Aksi Cepat", qa)
        qh.setObjectName("h2")
        qa_lay.addWidget(qh)
        for label, target in [("⬇  Buka Downloader", "download"), ("✂  Buka Content Clipper AI", "clipper"), ("⚙  Pengaturan", "settings")]:
            btn = QPushButton(label, qa)
            btn.setObjectName("ghost")
            btn.clicked.connect(lambda _=False, t=target: self.navigate.emit(t))
            qa_lay.addWidget(btn)
        qa_lay.addStretch()
        cols.addWidget(qa, 1)

        # info card
        info = Card(self, theme)
        info_lay = QVBoxLayout(info)
        info_lay.setContentsMargins(18, 16, 18, 16)
        info_lay.setSpacing(8)
        ih = QLabel("Status Sistem", info)
        ih.setObjectName("h2")
        info_lay.addWidget(ih)
        self.info_label = QLabel("", info)
        self.info_label.setWordWrap(True)
        self.info_label.setStyleSheet(f"color: {C['MUTED']}; font-size: 11px;")
        info_lay.addWidget(self.info_label)
        self.disk_label = QLabel("", info)
        self.disk_label.setStyleSheet(f"color: {C['FOOT']}; font-size: 10px;")
        info_lay.addWidget(self.disk_label)
        info_lay.addStretch()
        cols.addWidget(info, 1)

        root.addLayout(cols, 1)
        self.refresh()

    def refresh(self) -> None:
        C = THEMES.get(self._theme, THEMES["dark"])
        rows = self.app.db.list_history(limit=1000)
        total = len(rows)
        ok = sum(1 for r in rows if r["status"] == "Berhasil")
        fail = sum(1 for r in rows if r["status"] == "Gagal")
        projs = len(self.app.db.list_projects())
        self.stat_downloads.set_value(str(total))
        self.stat_success.set_value(str(ok))
        self.stat_failed.set_value(str(fail))
        self.stat_projects.set_value(str(projs))

        # yt-dlp + ffmpeg info
        try:
            import yt_dlp
            yv = getattr(yt_dlp.version, "__version__", "?")
        except Exception:
            yv = "?"
        import shutil as _sh
        ff = _sh.which("ffmpeg") or "tidak ditemukan"
        # ffmpeg local bundled?
        from chopster.app.paths import app_dir
        local_ff = app_dir() / "ffmpeg.exe"
        if local_ff.exists():
            ff = str(local_ff)
        self.info_label.setText(f"yt-dlp {yv}\nFFmpeg: {ff}\nTema: {self.app.config.get('theme')}")
        # disk
        out = self.app.config.get("out_dir") or str(Path.home() / "Downloads")
        try:
            free = shutil.disk_usage(out).free
            free_s = f"{free / (1024**3):.1f} GB tersedia di {out}"
        except Exception:
            free_s = "Ruang disk tidak dapat dibaca."
        self.disk_label.setText(free_s)
