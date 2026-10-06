"""Main window — sidebar + stacked pages."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMainWindow, QWidget, QHBoxLayout, QStackedWidget, QMessageBox, QLabel, QPushButton, QVBoxLayout

from chopster.ui.navigation import Sidebar
from chopster.ui.theme import stylesheet, THEMES
from chopster.ui.pages.dashboard_page import DashboardPage
from chopster.ui.pages.downloader_page import DownloaderPage
from chopster.ui.pages.clipper_page import ClipperPage
from chopster.ui.pages.auto_clip_studio_page import AutoClipStudioPage
from chopster.ui.pages.history_page import HistoryPage
from chopster.ui.pages.settings_page import SettingsPage


class _PageError(QWidget):
    def __init__(self, name: str, error: Exception, parent=None):
        super().__init__(parent)
        lay=QVBoxLayout(self); lay.setContentsMargins(28,28,28,28); lay.setSpacing(12)
        title=QLabel(f"{name} belum bisa dimuat")
        title.setObjectName("h1"); lay.addWidget(title)
        info=QLabel("Chopster tetap berjalan. Detail error dicatat ke crash.log. Tutup page ini atau jalankan ulang setelah dependency/codec diperbaiki.")
        info.setWordWrap(True); lay.addWidget(info)
        detail=QLabel(str(error)[:1200]); detail.setWordWrap(True); detail.setObjectName("muted"); lay.addWidget(detail)
        lay.addStretch()



class MainWindow(QMainWindow):
    def __init__(self, app):
        super().__init__()
        self.app = app
        theme = self.app.config.get("theme") or "dark"
        if theme not in THEMES:
            theme = "dark"
        self._theme = theme
        self.setWindowTitle("Chopster")
        self.setStyleSheet(stylesheet(theme))
        self.resize(int(self.app.config.get("window_width") or 1360), int(self.app.config.get("window_height") or 900))
        self.setMinimumSize(820, 580)

        central = QWidget(self)
        central.setObjectName("root")
        self.setCentralWidget(central)
        lay = QHBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.sidebar = Sidebar(theme, central)
        self.sidebar.navigate.connect(self.navigate)
        lay.addWidget(self.sidebar)

        self.stack = QStackedWidget(central)
        lay.addWidget(self.stack, 1)

        # pages
        self.page_dashboard = self._safe_page("Dashboard", lambda: DashboardPage(app, theme, self.stack))
        if hasattr(self.page_dashboard, "navigate"):
            self.page_dashboard.navigate.connect(self.navigate)
        self.page_downloader = self._safe_page("Download", lambda: DownloaderPage(app, theme, self.stack))
        self.page_clipper = self._safe_page("Content Clipper", lambda: ClipperPage(app, theme, self.stack))
        self.page_auto_clip = self._safe_page("Auto Clip Studio", lambda: AutoClipStudioPage(app, theme, self.stack))
        self.page_history = self._safe_page("History", lambda: HistoryPage(app, theme, self.stack))
        self.page_settings = self._safe_page("Settings", lambda: SettingsPage(app, theme, self.stack))

        self._pages = {
            "dashboard": self.page_dashboard,
            "download": self.page_downloader,
            "clipper": self.page_clipper,
            "auto_clip": self.page_auto_clip,
            "history": self.page_history,
            "settings": self.page_settings,
        }
        for w in self._pages.values():
            self.stack.addWidget(w)

        try: self.page_settings.ai_settings_changed.connect(self.page_clipper.refresh_ai_status)
        except Exception: pass
        try: self.page_settings.download_settings_changed.connect(self.page_downloader.refresh_download_settings)
        except Exception: pass

        # forward downloader logs? (optional)
        # restore last workspace
        last = self.app.config.get("last_workspace") or "dashboard"
        if last not in self._pages:
            last = "dashboard"
        self.navigate(last)
        self._apply_responsive_layout()

    def _safe_page(self, name, factory):
        try:
            return factory()
        except Exception as exc:
            try:
                import logging
                logging.getLogger("chopster.ui").exception("Page %s gagal dimuat", name)
            except Exception:
                pass
            return _PageError(name, exc, self.stack)

    def _apply_responsive_layout(self):
        try:
            compact=self.width()<1100
            self.sidebar.set_compact(compact)
            self.page_downloader.set_compact(self.width()<1000)
            self.page_clipper.set_compact(self.width()<1080)
            self.page_auto_clip.set_compact(self.width()<1080)
        except Exception: pass

    def resizeEvent(self,event):
        super().resizeEvent(event); self._apply_responsive_layout()

    def navigate(self, key: str) -> None:
        if key not in self._pages:
            return
        self.stack.setCurrentWidget(self._pages[key])
        self.sidebar.set_active(key)
        self.app.config.set("last_workspace", key)
        # refresh dashboard/history on show
        if key == "dashboard":
            self.page_dashboard.refresh()
        elif key == "history":
            self.page_history.refresh()
        elif key == "settings":
            try: self.page_settings.refresh_download_settings()
            except Exception: pass
        elif key == "download":
            try: self.page_downloader.refresh_download_settings()
            except Exception: pass
        elif key == "clipper":
            try: self.page_clipper.refresh_ai_status()
            except Exception: pass

    def receive_external_url(self, url: str) -> None:
        """Called on Qt main thread when Browser Bridge receives a URL. Brings app to front."""
        try:
            self.navigate("download")
            if hasattr(self.page_downloader, "receive_external_url"):
                self.page_downloader.receive_external_url(url)  # type: ignore[attr-defined]
            # Parity with Tkinter: deiconify + raise so window visible
            try:
                if self.isMinimized():
                    self.showNormal()
                self.show()
                self.raise_()
                self.activateWindow()
            except Exception:
                pass
        except Exception:
            pass

    def closeEvent(self, event):
        # persist window size
        try:
            self.app.config.update({"window_width": self.width(), "window_height": self.height()})
        except Exception:
            pass
        # confirm if downloading
        if getattr(self.page_downloader, "_running", False):
            ret = QMessageBox.question(self, "Keluar", "Download sedang berjalan. Yakin keluar?", QMessageBox.Yes | QMessageBox.No)
            if ret != QMessageBox.Yes:
                event.ignore()
                return
        auto_clip_busy = getattr(self.page_auto_clip, "_running", False)
        try:
            auto_clip_busy = auto_clip_busy or self.page_auto_clip.has_active_jobs()
        except Exception:
            pass
        if auto_clip_busy:
            ret = QMessageBox.question(self, "Keluar", "Auto Clip Studio sedang bekerja. Yakin keluar?", QMessageBox.Yes | QMessageBox.No)
            if ret != QMessageBox.Yes:
                event.ignore()
                return
        try:
            if hasattr(self.page_auto_clip, "shutdown"):
                self.page_auto_clip.shutdown()
        except Exception:
            pass
        try:
            self.app.shutdown()
        except Exception:
            pass
        super().closeEvent(event)
