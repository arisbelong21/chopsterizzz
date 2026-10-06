"""History page — SQLite-backed history with search/filter."""
from __future__ import annotations

import os, subprocess, sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QMessageBox, QComboBox
)

from chopster.ui.theme import THEMES

def open_path(p: str):
    try:
        if sys.platform.startswith("win"):
            os.startfile(p)  # type: ignore
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])
    except Exception as e:
        QMessageBox.warning(None, "Gagal membuka", str(e))


class HistoryPage(QWidget):
    def __init__(self, app, theme: str = "dark", parent: QWidget | None = None):
        super().__init__(parent)
        self.app = app
        self._theme = theme
        C = THEMES.get(theme, THEMES["dark"])

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(12)

        h = QHBoxLayout()
        title = QLabel("History", self)
        title.setObjectName("h1")
        h.addWidget(title)
        h.addStretch()
        clr = QPushButton("Hapus riwayat", self)
        clr.setObjectName("danger")
        clr.clicked.connect(self._clear)
        h.addWidget(clr)
        root.addLayout(h)

        # filters
        filt = QHBoxLayout()
        filt.addWidget(QLabel("Cari", self))
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("judul / url / platform")
        self.search.textChanged.connect(self.refresh)
        filt.addWidget(self.search, 1)
        self.platform_filter = QComboBox(self)
        self.platform_filter.addItems(["Semua platform", "YouTube", "TikTok", "Facebook", "Instagram", "Threads", "Lainnya"])
        self.platform_filter.currentTextChanged.connect(self.refresh)
        filt.addWidget(self.platform_filter)
        self.status_filter = QComboBox(self)
        self.status_filter.addItems(["Semua status", "Berhasil", "Gagal"])
        self.status_filter.currentTextChanged.connect(self.refresh)
        filt.addWidget(self.status_filter)
        root.addLayout(filt)

        self.table = QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels(["Tanggal", "Platform", "Judul", "Status", "URL"])
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Fixed)
        hdr.setSectionResizeMode(1, QHeaderView.Fixed)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.Fixed)
        hdr.setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 135)
        self.table.setColumnWidth(1, 95)
        self.table.setColumnWidth(3, 90)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        self.table.cellDoubleClicked.connect(self._open_row)
        root.addWidget(self.table, 1)

        self.refresh()

    def refresh(self) -> None:
        q = self.search.text().strip()
        plat = self.platform_filter.currentText()
        if plat == "Semua platform":
            plat = ""
        stat = self.status_filter.currentText()
        if stat == "Semua status":
            stat = ""
        rows = self.app.db.list_history(limit=500, search=q, platform=plat, status=stat)
        self.table.setRowCount(0)
        for r in rows:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(r.get("created_at","")))
            self.table.setItem(row, 1, QTableWidgetItem(r.get("platform","")))
            self.table.setItem(row, 2, QTableWidgetItem(r.get("title","")))
            self.table.setItem(row, 3, QTableWidgetItem(r.get("status","")))
            self.table.setItem(row, 4, QTableWidgetItem(r.get("url","")))
            # store id + path in row data
            self.table.item(row, 0).setData(Qt.UserRole, r)

    def _menu(self, pos):
        idx = self.table.indexAt(pos)
        if not idx.isValid():
            return
        r = idx.row()
        data = self.table.item(r, 0).data(Qt.UserRole) if self.table.item(r, 0) else {}
        from PySide6.QtWidgets import QMenu
        m = QMenu(self)
        url = data.get("url","") if isinstance(data, dict) else ""
        path = data.get("file_path","") if isinstance(data, dict) else ""
        m.addAction("Salin URL", lambda: self._copy(url))
        m.addAction("Buka file", lambda: open_path(path) if path and Path(path).exists() else QMessageBox.information(self, "File", "File tidak ditemukan."))
        m.addAction("Buka folder", lambda: open_path(str(Path(path).parent)) if path and Path(path).exists() else open_path(self.app.config.get("out_dir")))
        m.addSeparator()
        m.addAction("Hapus entri", lambda: self._delete_row(data))
        m.exec(self.table.mapToGlobal(pos))

    def _copy(self, t: str):
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(t)

    def _open_row(self, r, c):
        data = self.table.item(r, 0).data(Qt.UserRole) if self.table.item(r, 0) else {}
        path = data.get("file_path","") if isinstance(data, dict) else ""
        if path and Path(path).exists():
            open_path(path)
        elif path:
            QMessageBox.information(self, "File", f"File tidak ditemukan:\n{path}")

    def _delete_row(self, data):
        if not isinstance(data, dict) or not data.get("id"):
            return
        if QMessageBox.question(self, "Hapus", f"Hapus entri '{data.get('title','')}'?") != QMessageBox.Yes:
            return
        self.app.db.delete_history(int(data["id"]))
        self.refresh()

    def _clear(self):
        if QMessageBox.question(self, "Hapus riwayat", "Hapus seluruh riwayat? File media TIDAK akan dihapus.") != QMessageBox.Yes:
            return
        self.app.db.clear_history()
        self.refresh()
