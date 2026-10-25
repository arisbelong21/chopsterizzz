"""Main window — sidebar + stacked pages."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QMainWindow, QWidget, QHBoxLayout, QStackedWidget, QMessageBox, QLabel, QPushButton, QVBoxLayout

from chopster.ui.navigation import Sidebar
from chopster.ui.theme import stylesheet, THEMES
from chopster.ui.components.theme_switcher import ThemeSwitcher
from chopster.ui.pages.dashboard_page import DashboardPage
from chopster.ui.pages.downloader_page import DownloaderPage
from chopster.ui.pages.clipper_page import ClipperPage
from chopster.ui.pages.auto_clip_studio_page import AutoClipStudioPage
from chopster.ui.pages.automation_page import AutomationPage
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

        self._build_pages(theme)

        # forward downloader logs? (optional)
        # restore last workspace
        last = self.app.config.get("last_workspace") or "dashboard"
        if last not in self._pages:
            last = "dashboard"
        self.navigate(last)
        self._apply_responsive_layout()
        self.theme_switcher = ThemeSwitcher(self._theme, central)
        self.theme_switcher.themeSelected.connect(self._switch_theme)
        self._place_theme_switcher()

    def _build_pages(self, theme: str) -> None:
        """Create every page widget, wire cross-page signals and register them."""
        self.page_dashboard = self._safe_page("Dashboard", lambda: DashboardPage(self.app, theme, self.stack))
        self.page_downloader = self._safe_page("Download", lambda: DownloaderPage(self.app, theme, self.stack))
        self.page_clipper = self._safe_page("Content Clipper", lambda: ClipperPage(self.app, theme, self.stack))
        self.page_auto_clip = self._safe_page("Auto Clip Studio", lambda: AutoClipStudioPage(self.app, theme, self.stack))
        self.page_automation = self._safe_page("Automation", lambda: AutomationPage(self.app, theme, self.stack))
        self.page_history = self._safe_page("History", lambda: HistoryPage(self.app, theme, self.stack))
        self.page_settings = self._safe_page("Settings", lambda: SettingsPage(self.app, theme, self.stack))
        # Every page may expose a `navigate` signal (sidebar jumps such as
        # "Download", "Settings", "Automation"). Only the dashboard used to be
        # wired up, leaving the other pages' nav buttons dead.
        for _page in (self.page_dashboard, self.page_downloader, self.page_clipper,
                      self.page_auto_clip, self.page_automation, self.page_history,
                      self.page_settings):
            if hasattr(_page, "navigate"):
                try:
                    _page.navigate.connect(self.navigate)
                except Exception:
                    pass

        self._pages = {
            "dashboard": self.page_dashboard,
            "download": self.page_downloader,
            "clipper": self.page_clipper,
            "auto_clip": self.page_auto_clip,
            "automation": self.page_automation,
            "history": self.page_history,
            "settings": self.page_settings,
        }
        for w in self._pages.values():
            self.stack.addWidget(w)

        try: self.page_settings.ai_settings_changed.connect(self.page_clipper.refresh_ai_status)
        except Exception: pass
        try: self.page_settings.download_settings_changed.connect(self.page_downloader.refresh_download_settings)
        except Exception: pass

    def _is_busy(self) -> bool:
        """True while a download/render/publish/task is running — theme switch
        is deferred then so rebuilding the pages cannot kill active work."""
        busy = bool(getattr(self.page_downloader, "_running", False))
        try:
            busy = busy or bool(self.page_auto_clip.has_active_jobs())
        except Exception:
            pass
        try:
            mgr = getattr(self.app, "automation", None)
            busy = busy or bool(mgr and getattr(mgr.runner, "active_count", 0) > 0)
        except Exception:
            pass
        try:
            busy = busy or bool(self.app.tasks and self.app.tasks.has_active_tasks())
        except Exception:
            pass
        return busy

    def _switch_theme(self, theme: str) -> None:
        if theme not in THEMES:
            return
        if theme == self._theme:
            self.theme_switcher.set_theme(self._theme)
            return
        if self._is_busy():
            QMessageBox.information(
                self, "Ganti tema",
                "Selesaikan proses yang sedang berjalan (download/render/publish) dulu, lalu ganti tema lagi.",
            )
            self.theme_switcher.set_theme(self._theme)
            return
        try:
            self.app.config.set("theme", theme)
        except Exception:
            pass
        self._theme = theme
        self.setStyleSheet(stylesheet(theme))
        self._rebuild_ui()
        self.theme_switcher.set_theme(theme)

    def _rebuild_ui(self) -> None:
        """Recreate sidebar + pages under a new theme while keeping the
        current workspace open. The shared ACS runtime stays alive; the old
        Automation page removes its manager listener with shutdown()."""
        current = next((k for k, w in self._pages.items() if self.stack.currentWidget() is w), "dashboard")
        try:
            if hasattr(self.page_automation, "shutdown"):
                self.page_automation.shutdown()
        except Exception:
            pass
        for w in self._pages.values():
            try:
                w.setParent(None)
            except Exception:
                pass
            w.deleteLater()
        lay = self.centralWidget().layout()
        try:
            self.sidebar.navigate.disconnect(self.navigate)
        except Exception:
            pass
        lay.removeWidget(self.sidebar)
        self.sidebar.deleteLater()
        new_sidebar = Sidebar(self._theme, self.centralWidget())
        new_sidebar.navigate.connect(self.navigate)
        lay.insertWidget(0, new_sidebar)
        self.sidebar = new_sidebar
        self._build_pages(self._theme)
        if current not in self._pages:
            current = "dashboard"
        self.navigate(current)
        self._apply_responsive_layout()
        self._place_theme_switcher()

    def _place_theme_switcher(self) -> None:
        """Anchor the switcher pill top-centre so it never covers the page's
        own top-right header buttons (Create Automation, Buka Automation…)."""
        sw = getattr(self, "theme_switcher", None)
        if sw is None:
            return
        try:
            sw.adjustSize()
            cw = self.centralWidget()
            x = max(0, (cw.width() - sw.width()) // 2)
            sw.setGeometry(x, 12, sw.width(), sw.height())
            sw.raise_()
        except Exception:
            pass

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
        super().resizeEvent(event); self._apply_responsive_layout(); self._place_theme_switcher()

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
        elif key == "automation":
            try: self.page_automation.refresh_all()
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

    def _finish_task_shutdown(self):
        if self.app.tasks.wait_for_done(0):
            self._task_close_timer.stop()
            self.close()

    def closeEvent(self, event):
        if getattr(self, "_waiting_for_tasks", False):
            if not self.app.tasks.wait_for_done(0):
                event.ignore()
                return
            self._task_close_timer.stop()
            self.app.shutdown()
            super().closeEvent(event)
            return
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
        automation_busy = False
        try:
            mgr = getattr(self.app, "automation", None)
            automation_busy = bool(mgr and mgr.runner.active_count > 0)
        except Exception:
            automation_busy = False
        if automation_busy:
            ret = QMessageBox.question(self, "Keluar", "Automation sedang mengupload/menerbitkan. Batalkan job yang berjalan dan keluar?", QMessageBox.Yes | QMessageBox.No)
            if ret != QMessageBox.Yes:
                event.ignore()
                return
        if self.app.tasks.has_active_tasks() and not getattr(self.page_downloader, "_running", False):
            ret = QMessageBox.question(self, "Keluar", "Tugas masih berjalan. Batalkan tugas dan keluar setelah proses berhenti?", QMessageBox.Yes | QMessageBox.No)
            if ret != QMessageBox.Yes:
                event.ignore()
                return
        try:
            if hasattr(self.page_auto_clip, "shutdown"):
                self.page_auto_clip.shutdown()
        except Exception:
            pass
        try:
            if hasattr(self.page_automation, "shutdown"):
                self.page_automation.shutdown()
        except Exception:
            pass
        self.app.tasks.begin_shutdown()
        if not self.app.tasks.wait_for_done(0):
            # Keep the Qt event loop and widgets alive while cancellation settles.
            self._waiting_for_tasks = True
            self.setEnabled(False)
            self._task_close_timer = QTimer(self)
            self._task_close_timer.setInterval(100)
            self._task_close_timer.timeout.connect(self._finish_task_shutdown)
            self._task_close_timer.start()
            event.ignore()
            return
        self.app.shutdown()
        super().closeEvent(event)
