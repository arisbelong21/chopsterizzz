"""Chopster Auto Clip Studio embedded as an isolated workspace."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QStackedLayout,
    QVBoxLayout, QWidget,
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
    publishStatus = Signal(str)

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.output_directory = str(default_output_directory())
        # Polled by the web UI: the ACS page reads this instead of relying only
        # on cross-thread Qt signals (which QWebChannel may drop).
        self._publish_lock = threading.Lock()
        self._publish_state = {"running": False, "stage": "", "result": None, "seq": 0,
                               "started_at": 0.0}

    def _set_publish(self, *, running=None, stage=None, result=None) -> None:
        with self._publish_lock:
            if running is not None:
                self._publish_state["running"] = bool(running)
                self._publish_state["started_at"] = time.time() if running else 0.0
            if stage is not None:
                self._publish_state["stage"] = str(stage)
            if result is not None:
                self._publish_state["result"] = result
            self._publish_state["seq"] = int(self._publish_state.get("seq", 0)) + 1

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

    @Slot(result=str)
    def listPublishTemplates(self):
        """JSON list of ACS one-click templates (presets flagged acs_publish)."""
        try:
            mgr = getattr(self.app, "automation", None)
            templates = mgr.list_acs_templates() if mgr else []
        except Exception:
            templates = []
        try:
            self.app.logger.info("[Automation][template] web listPublishTemplates -> %d", len(templates))
        except Exception:
            pass
        return json.dumps(templates, ensure_ascii=False)

    @Slot(str, result=str)
    def publishViaTemplate(self, template_id=""):
        """Start one-click template publish in a background thread. The final
        state is readable via ``publishStatusSnapshot`` (polled by the UI),
        and also pushed through the ``publishStatus`` signal."""
        with self._publish_lock:
            already = bool(self._publish_state.get("running"))
        tid = str(template_id or "")
        try:
            self.app.logger.info("[Automation][template] web publishViaTemplate tid=%s", tid)
        except Exception:
            pass
        if already:
            return json.dumps({"ok": True, "message": "Proses lain masih berjalan…"},
                              ensure_ascii=False)
        self._set_publish(running=True, stage="Memulai publish…", result=None)

        def _work(tid: str) -> None:
            def _stage(msg):
                self._set_publish(stage=str(msg))
                try:
                    self.publishStatus.emit(str(msg))
                except Exception:
                    pass
            try:
                mgr = getattr(self.app, "automation", None)
                if mgr is None:
                    result = {"ok": False, "message": "Automation belum tersedia."}
                else:
                    result = mgr.run_template_acs(tid, status_cb=_stage)
            except Exception as exc:
                result = {"ok": False, "message": f"Gagal publish: {exc}"}
            self._set_publish(running=False, stage="", result=result)
            try:
                self.publishStatus.emit(json.dumps(result, ensure_ascii=False))
            except Exception:
                pass

        threading.Thread(target=_work, args=(tid,), daemon=True).start()
        return json.dumps({"ok": True, "message": "Publish template dimulai di latar belakang."},
                          ensure_ascii=False)

    @Slot(result=str)
    def publishStatusSnapshot(self):
        """JSON of the current/last template publish state (UI polls this)."""
        with self._publish_lock:
            snap = dict(self._publish_state)
        # Never let a crashed worker leave the UI spinning forever.
        if snap.get("running") and snap.get("started_at"):
            if time.time() - float(snap["started_at"]) > 1800:
                snap["running"] = False
                snap["result"] = {"ok": False,
                                  "message": "Proses publish timeout. Cek tab Queue di Automation."}
        return json.dumps(snap, ensure_ascii=False)


class AutoClipStudioPage(QWidget):
    runtime_ready = Signal(str)
    runtime_failed = Signal(str)
    navigate = Signal(str)
    publish_progress = Signal(str)

    def __init__(self, app, theme="dark", parent=None):
        super().__init__(parent)
        self.app=app; self._theme=theme; self._running=False; self._profile=None; self.web=None
        C=THEMES.get(theme,THEMES["dark"])
        self.setObjectName("autoClipStudioWeb")
        self.setStyleSheet(f"QWidget#autoClipStudioWeb{{background:{C['BG']};}} QFrame#webLoader{{background:{C['PANEL']};border:1px solid {C['BORDER']};border-radius:18px;}}")
        # Slim native strip so the ACS <-> Automation handoff is discoverable
        # inside this web workspace (the web UI itself has no link to it).
        outer=QVBoxLayout(self); outer.setContentsMargins(0,0,0,0); outer.setSpacing(0)
        bar=QFrame(); bar.setStyleSheet(f"QFrame{{background:{C['PANEL']};border-bottom:1px solid {C['BORDER']};}}")
        bar_l=QHBoxLayout(bar); bar_l.setContentsMargins(14,4,14,4); bar_l.setSpacing(10)
        note=QLabel("⚡ Hasil render otomatis dikirim ke tab Automation sebagai media siap publish (Settings → Automation).")
        note.setStyleSheet(f"color:{C['MUTED']};font-size:11px;")
        bar_l.addWidget(note); bar_l.addStretch()
        # Native one-click template publish: works even if the embedded web UI
        # bridge is unavailable, and shows progress right here.
        self._tpl_status = QLabel("", bar)
        self._tpl_status.setStyleSheet(f"color:{C['MUTED']};font-size:11px;")
        bar_l.addWidget(self._tpl_status)
        # Shows how many rendered clips are actually ready to publish, so it is
        # obvious whether Render still has to run first.
        self._ready_lbl = QLabel("", bar)
        self._ready_lbl.setStyleSheet(f"color:{C['GOLD']};font-size:11px;font-weight:700;")
        bar_l.addWidget(self._ready_lbl)
        self._tpl_combo = QComboBox(bar)
        self._tpl_combo.setMinimumWidth(180)
        bar_l.addWidget(self._tpl_combo)
        self._tpl_btn = QPushButton("⚡ Publish via Template")
        self._tpl_btn.setObjectName("primary")
        self._tpl_btn.setCursor(Qt.PointingHandCursor)
        self._tpl_btn.clicked.connect(self._publish_via_template)
        bar_l.addWidget(self._tpl_btn)
        to_aut=QPushButton("Buka Automation"); to_aut.setObjectName("ghost")
        to_aut.setCursor(Qt.PointingHandCursor)
        to_aut.clicked.connect(lambda: self.navigate.emit("automation"))
        bar_l.addWidget(to_aut)
        outer.addWidget(bar)
        host=QWidget(); outer.addWidget(host,1)
        root=QStackedLayout(host); root.setContentsMargins(0,0,0,0); self._stack=root
        loader=QWidget(); ll=QVBoxLayout(loader); ll.setContentsMargins(40,40,40,40); ll.addStretch()
        card=QFrame(); card.setObjectName("webLoader"); card.setMaximumWidth(580); cl=QVBoxLayout(card); cl.setContentsMargins(32,30,32,30); cl.setSpacing(12)
        icon=QLabel("⚡"); icon.setAlignment(Qt.AlignCenter); icon.setStyleSheet(f"font-size:38px;color:{C['GOLD']};"); cl.addWidget(icon)
        self.loader_title=QLabel("Starting Auto Clip Studio"); self.loader_title.setAlignment(Qt.AlignCenter); self.loader_title.setStyleSheet(f"font-size:22px;font-weight:800;color:{C['TEXT']};"); cl.addWidget(self.loader_title)
        self.loader_text=QLabel("Loading Chopster Auto Clip Studio and media engine…"); self.loader_text.setAlignment(Qt.AlignCenter); self.loader_text.setWordWrap(True); self.loader_text.setStyleSheet(f"color:{C['MUTED']};"); cl.addWidget(self.loader_text)
        self.retry=QPushButton("Retry"); self.retry.setObjectName("primary"); self.retry.hide(); self.retry.clicked.connect(self._start); cl.addWidget(self.retry)
        line=QHBoxLayout(); line.addStretch(); line.addWidget(card); line.addStretch(); ll.addLayout(line); ll.addStretch(); root.addWidget(loader); self._loader=loader
        self.runtime_ready.connect(self._open_runtime); self.runtime_failed.connect(self._show_error)
        self.publish_progress.connect(self._on_publish_progress)
        self._publish_running = False
        self._publish_started_at = 0.0
        self._ready_timer = QTimer(self)
        self._ready_timer.setInterval(4000)
        self._ready_timer.timeout.connect(self._refresh_ready_count)
        self._refresh_publish_templates()
        QTimer.singleShot(0,self._start)

    # -- native one-click template publish --------------------------------
    def _refresh_ready_count(self):
        """How many rendered ACS clips are ready to publish right now."""
        # Watchdog: a worker that dies without emitting would leave the button
        # disabled forever. Release it after 30 minutes of no completion.
        if (self._publish_running and self._publish_started_at
                and time.time() - self._publish_started_at > 1800):
            try:
                self.app.logger.warning("[Automation][template] publish watchdog reset")
            except Exception:
                pass
            self._publish_running = False
            self._publish_started_at = 0.0
            self._tpl_btn.setEnabled(self._tpl_combo.count() > 0)
            self._tpl_combo.setEnabled(self._tpl_combo.count() > 0)
            self._tpl_status.setText("Proses publish timeout — cek tab Queue di Automation.")
        try:
            mgr = getattr(self.app, "automation", None)
            rows = mgr.list_media(status="ready", source="acs") if mgr else []
            ready = [m for m in rows
                     if m.get("media_path") and Path(str(m.get("media_path"))).exists()]
            count = len(ready)
        except Exception:
            count = 0
        if self._tpl_combo.count() == 0:
            self._ready_lbl.setText("")
            return
        if count > 0:
            self._ready_lbl.setText(f"✔ {count} klip siap publish")
        else:
            self._ready_lbl.setText("⚠ Belum ada klip ter-render — klik Render dulu")

    def _refresh_publish_templates(self):
        try:
            mgr = getattr(self.app, "automation", None)
            templates = mgr.list_acs_templates() if mgr else []
        except Exception:
            templates = []
        self._tpl_combo.clear()
        for tpl in templates:
            self._tpl_combo.addItem(f"{tpl.get('name')}  ·  {tpl.get('summary')}", tpl.get("id"))
        has = bool(templates)
        self._tpl_combo.setEnabled(has and not self._publish_running)
        self._tpl_btn.setEnabled(has and not self._publish_running)
        if not has:
            self._tpl_status.setText("Belum ada template — buat di Automation → Presets")
        self._refresh_ready_count()

    def _publish_via_template(self):
        if self._publish_running:
            return
        tid = str(self._tpl_combo.currentData() or "")
        if not tid:
            self._tpl_status.setText("Pilih template dulu (buat di Automation → Presets).")
            return
        self._publish_running = True
        self._publish_started_at = time.time()
        self._tpl_btn.setEnabled(False)
        self._tpl_combo.setEnabled(False)
        self._tpl_status.setText("Menyiapkan konten AI & antrean…")
        try:
            self.app.logger.info("[Automation][template] native publish clicked tid=%s", tid)
        except Exception:
            pass

        def worker():
            def _stage(msg):
                self.publish_progress.emit(str(msg))
                try:
                    if getattr(self, "_desktop_bridge", None) is not None:
                        self._desktop_bridge._set_publish(stage=str(msg))
                except Exception:
                    pass
            try:
                mgr = getattr(self.app, "automation", None)
                result = (mgr.run_template_acs(tid, status_cb=_stage) if mgr
                          else {"ok": False, "message": "Automation belum tersedia."})
            except Exception as exc:
                result = {"ok": False, "message": f"Gagal publish: {exc}"}
            try:
                if getattr(self, "_desktop_bridge", None) is not None:
                    self._desktop_bridge._set_publish(running=False, stage="", result=result)
            except Exception:
                pass
            self.publish_progress.emit(json.dumps(result, ensure_ascii=False))

        threading.Thread(target=worker, daemon=True, name="acs-template-publish").start()

    def _on_publish_progress(self, payload: str):
        try:
            data = json.loads(payload)
        except Exception:
            data = None
        if isinstance(data, dict) and isinstance(data.get("ok"), bool):
            self._publish_running = False
            self._tpl_btn.setEnabled(self._tpl_combo.count() > 0)
            self._tpl_combo.setEnabled(self._tpl_combo.count() > 0)
            self._tpl_status.setText(str(data.get("message") or "Selesai."))
            self._refresh_ready_count()
        else:
            self._tpl_status.setText(str(payload))

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

    def showEvent(self, event):
        super().showEvent(event)
        # Templates may have been created/edited in Automation since last view.
        try:
            self._refresh_publish_templates()
        except Exception:
            pass
        try:
            self._refresh_ready_count()
            self._ready_timer.start()
        except Exception:
            pass

    def hideEvent(self, event):
        super().hideEvent(event)
        try:
            self._ready_timer.stop()
        except Exception:
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
