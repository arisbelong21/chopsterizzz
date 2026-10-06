"""Chopster Auto Clip Studio embedded as an isolated workspace."""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QStackedLayout, QVBoxLayout, QWidget,
)
from PySide6.QtGui import QDesktopServices
from PySide6.QtWebChannel import QWebChannel
from chopster.auto_clip_studio.engine.output_locations import default_output_directory, resolve_output_directory

from chopster.app.paths import user_data_dir
from chopster.auto_clip_studio.web_runtime import runtime
from chopster.ui.theme import THEMES

try:
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
    from PySide6.QtWebEngineWidgets import QWebEngineView
    WEBENGINE_AVAILABLE = True
except Exception:
    WEBENGINE_AVAILABLE = False


class AutoClipStudioDesktopBridge(QObject):
    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.output_directory = str(default_output_directory())

    @Slot(str, result=str)
    def chooseOutputDirectory(self, current_path=""):
        start = current_path or str(default_output_directory())
        selected = QFileDialog.getExistingDirectory(None, "Choose Auto Clip Studio export folder", start)
        if selected:
            try:
                self.output_directory = str(resolve_output_directory(selected))
            except Exception:
                return ""
        return self.output_directory if selected else ""

    @Slot(str, result=bool)
    def setOutputDirectory(self, path=""):
        try:
            self.output_directory = str(resolve_output_directory(path))
            return True
        except Exception:
            return False

    @Slot(result=str)
    def getAcsApiKey(self):
        """Read the ACS-only Gemini key from the native secure configuration."""
        try:
            return str(self.app.config.get("acs_gemini_key") or "")
        except Exception:
            return ""

    @Slot(str, result=bool)
    def setAcsApiKey(self, api_key=""):
        """Persist the ACS-only Gemini key through Configuration/Windows DPAPI."""
        try:
            return bool(self.app.config.set("acs_gemini_key", str(api_key or "").strip()))
        except Exception:
            return False

    @Slot(str, result=bool)
    def openOutputDirectory(self, path=""):
        try:
            folder = resolve_output_directory(path)
            return QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        except Exception:
            return False


class AutoClipStudioPage(QWidget):
    runtime_ready = Signal(str)
    runtime_failed = Signal(str)

    def __init__(self, app, theme="dark", parent=None):
        super().__init__(parent)
        self.app=app; self._theme=theme; self._running=False; self._profile=None; self.web=None
        C=THEMES.get(theme,THEMES["dark"])
        self.setObjectName("autoClipStudioWeb")
        self.setStyleSheet(f"QWidget#autoClipStudioWeb{{background:{C['BG']};}} QFrame#webLoader{{background:{C['PANEL']};border:1px solid {C['BORDER']};border-radius:18px;}}")
        root=QStackedLayout(self); root.setContentsMargins(0,0,0,0); self._stack=root
        loader=QWidget(); ll=QVBoxLayout(loader); ll.setContentsMargins(40,40,40,40); ll.addStretch()
        card=QFrame(); card.setObjectName("webLoader"); card.setMaximumWidth(580); cl=QVBoxLayout(card); cl.setContentsMargins(32,30,32,30); cl.setSpacing(12)
        icon=QLabel("⚡"); icon.setAlignment(Qt.AlignCenter); icon.setStyleSheet(f"font-size:38px;color:{C['GOLD']};"); cl.addWidget(icon)
        self.loader_title=QLabel("Starting Auto Clip Studio"); self.loader_title.setAlignment(Qt.AlignCenter); self.loader_title.setStyleSheet(f"font-size:22px;font-weight:800;color:{C['TEXT']};"); cl.addWidget(self.loader_title)
        self.loader_text=QLabel("Loading Chopster Auto Clip Studio and media engine…"); self.loader_text.setAlignment(Qt.AlignCenter); self.loader_text.setWordWrap(True); self.loader_text.setStyleSheet(f"color:{C['MUTED']};"); cl.addWidget(self.loader_text)
        self.retry=QPushButton("Retry"); self.retry.setObjectName("primary"); self.retry.hide(); self.retry.clicked.connect(self._start); cl.addWidget(self.retry)
        line=QHBoxLayout(); line.addStretch(); line.addWidget(card); line.addStretch(); ll.addLayout(line); ll.addStretch(); root.addWidget(loader); self._loader=loader
        self.runtime_ready.connect(self._open_runtime); self.runtime_failed.connect(self._show_error)
        QTimer.singleShot(0,self._start)

    def _start(self):
        self.retry.hide(); self.loader_title.setText("Starting Auto Clip Studio"); self.loader_text.setText("Loading Chopster Auto Clip Studio and media engine…")
        if not WEBENGINE_AVAILABLE:
            self._show_error("Qt WebEngine is not installed. Reinstall dependencies from requirements.txt.")
            return
        def worker():
            try: self.runtime_ready.emit(runtime.start().url)
            except Exception as exc: self.runtime_failed.emit(str(exc))
        threading.Thread(target=worker,daemon=True,name="acs-runtime-loader").start()

    def _open_runtime(self,url):
        if self.web is None:
            data=user_data_dir()/"AutoClipStudio"/"web_profile"; cache=user_data_dir()/"AutoClipStudio"/"web_cache"
            data.mkdir(parents=True,exist_ok=True); cache.mkdir(parents=True,exist_ok=True)
            self._profile=QWebEngineProfile("ChopsterAutoClipStudio",self)
            self._profile.setPersistentStoragePath(str(data)); self._profile.setCachePath(str(cache))
            self._profile.setPersistentCookiesPolicy(QWebEngineProfile.ForcePersistentCookies)
            self._profile.downloadRequested.connect(self._accept_download)
            self.web=QWebEngineView(self); self.web.setPage(QWebEnginePage(self._profile,self.web))
            self._web_channel = QWebChannel(self.web.page())
            self._desktop_bridge = AutoClipStudioDesktopBridge(self.app, self)
            self._web_channel.registerObject("chopsterDesktopBridge", self._desktop_bridge)
            self.web.page().setWebChannel(self._web_channel)
            settings=self.web.settings(); settings.setAttribute(QWebEngineSettings.PlaybackRequiresUserGesture,False); settings.setAttribute(QWebEngineSettings.FullScreenSupportEnabled,True); settings.setAttribute(QWebEngineSettings.LocalStorageEnabled,True)
            self.web.loadStarted.connect(lambda:setattr(self,"_running",True)); self.web.loadFinished.connect(lambda _ok:setattr(self,"_running",False))
            self._stack.addWidget(self.web)
        self.web.setUrl(QUrl(url)); self._stack.setCurrentWidget(self.web)

    def _accept_download(self,item):
        output_dir = Path(self._desktop_bridge.output_directory)
        output_dir.mkdir(parents=True, exist_ok=True)
        item.setDownloadDirectory(str(output_dir))
        item.accept()

    def _show_error(self,message):
        self._running=False; self._stack.setCurrentWidget(self._loader); self.loader_title.setText("Auto Clip Studio could not start"); self.loader_text.setText(message); self.retry.show()

    def set_compact(self,compact):
        pass

    def has_active_jobs(self):
        """Report backend work so Chopster does not exit mid-render/download."""
        try:
            from chopster.auto_clip_studio.engine.services.download_service import (
                raw_clip_download_jobs, raw_download_jobs,
            )
            from chopster.auto_clip_studio.engine.services.render_service import RENDER_BATCHES
            render_busy=any(v.get("overall_status")=="running" for v in RENDER_BATCHES.values())
            raw_busy=any(v.get("status") in {"pending","downloading"} for v in raw_download_jobs.values())
            clip_busy=any(v.get("status") in {"pending","downloading"} for v in raw_clip_download_jobs.values())
            return render_busy or raw_busy or clip_busy
        except Exception:
            return bool(self._running)

    def shutdown(self):
        runtime.stop()
