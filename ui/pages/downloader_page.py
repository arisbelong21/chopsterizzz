"""Downloader workspace — URL input, preview, queue, progress."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt, Signal, Slot, QPropertyAnimation, QEasingCurve, QTimer, QRect
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QBoxLayout, QLabel, QPushButton, QTextEdit, QLineEdit,
    QTableWidget, QTableWidgetItem, QHeaderView, QProgressBar, QComboBox, QCheckBox,
    QSpinBox, QFileDialog, QMessageBox, QFrame, QSplitter, QAbstractItemView, QGridLayout
)

from chopster.ui.theme import THEMES
from chopster.ui.components.cards import Card
from chopster.downloader.validators import clean_urls, detect_platform, valid_url


def open_path(path: str) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as exc:
        QMessageBox.warning(None, "Gagal membuka", str(exc))


class DownloaderPage(QWidget):
    request_log = Signal(str, str)  # msg, tag
    # --- cross-thread signals (queued to main thread) ---
    sig_add_row = Signal(int, str, str, str, str)  # no, platform, title, dur, status
    sig_log = Signal(str)
    sig_preview_done = Signal()
    sig_progress = Signal(int, str)
    sig_row_update = Signal(int, str, str, str)  # no, platform, title, status
    sig_row_add = Signal(int, str, str, str, str)
    sig_done = Signal(int, int, str)  # ok, fail, out_dir
    sig_download_log = Signal(str)

    def __init__(self, app, theme: str = "dark", parent: QWidget | None = None):
        super().__init__(parent)
        self.app = app
        self._theme = theme
        C = THEMES.get(theme, THEMES["dark"])
        self.setObjectName("downloadWorkspace")
        self.setStyleSheet(f"""
            QWidget#downloadWorkspace {{ background: transparent; }}
            QFrame#downloadHero, QFrame#downloadPanel, QFrame#downloadProgressCard,
            QFrame#downloadQueuePanel, QFrame#downloadStat {{
                background: {C['PANEL']};
                border: 1px solid {C['BORDER']};
                border-radius: 12px;
            }}
            QLabel#downloadEyebrow, QLabel#downloadSectionLabel {{
                color: {C['MUTED']}; font-size: 10px; font-weight: 800; letter-spacing: 1px;
            }}
            QLabel#downloadStatValue {{ font-size: 26px; font-weight: 900; }}
            QTableWidget#downloadQueue {{
                border-radius: 8px; alternate-background-color: {C['PANEL']};
                background: {C['INPUT']};
            }}
            QTableWidget#downloadQueue::item {{ min-height: 34px; border-bottom: 1px solid {C['BORDER']}; }}
            QProgressBar#downloadProgress {{
                min-height: 12px; max-height: 12px; border: 1px solid {C['BORDER']};
                border-radius: 6px; background: {C['INPUT']};
            }}
            QProgressBar#downloadProgress::chunk {{ border-radius: 6px; background: {C['GOLD']}; }}
            QFrame#downloadStatTotal {{ border-top: 2px solid {C['BLUE']}; }}
            QFrame#downloadStatSuccess {{ border-top: 2px solid {C['GREEN']}; }}
            QFrame#downloadStatFailure {{ border-top: 2px solid {C['RED']}; }}
        """)

        root = QVBoxLayout(self)
        self._root_layout = root
        root.setContentsMargins(20, 16, 20, 18)
        root.setSpacing(10)

        # Compact brand header, modeled on Chopster's earlier Download workspace.
        hero = QFrame(self); self._hero = hero; hero.setObjectName("downloadHero")
        hl = QHBoxLayout(hero); self._hero_layout = hl
        hl.setContentsMargins(16, 10, 16, 10); hl.setSpacing(12)
        icon = QLabel("▶", hero); self._hero_icon = icon
        icon.setAlignment(Qt.AlignCenter); icon.setFixedSize(44, 44)
        icon.setStyleSheet(f"background:{C['GOLD']};color:{C['ON_GOLD']};border-radius:10px;font-size:20px;font-weight:900;")
        hl.addWidget(icon)
        brand = QVBoxLayout(); brand.setSpacing(1)
        title = QLabel("Chopster", hero); title.setStyleSheet("font-size:22px;font-weight:900;")
        brand.addWidget(title)
        subtitle = QLabel("YouTube  •  TikTok  •  Facebook  •  Instagram  •  Threads", hero)
        self._hero_subtitle = subtitle; subtitle.setStyleSheet(f"color:{C['MUTED']};font-size:11px;")
        brand.addWidget(subtitle); hl.addLayout(brand, 1)
        self.url_count = QLabel("0 link", hero)
        self.url_count.setStyleSheet(f"color:{C['GOLD']};font-weight:800;background:{C['PANEL2']};border:1px solid {C['BORDER']};border-radius:8px;padding:7px 11px;")
        hl.addWidget(self.url_count, 0, Qt.AlignVCenter)
        self.engine_badge = QLabel("Memeriksa engine…", hero)
        self.engine_badge.setStyleSheet(f"color:{C['GREEN']};font-size:10px;font-weight:800;background:{C['PILL_BG']};border:1px solid {C['PILL_BD']};border-radius:8px;padding:8px 11px;")
        hl.addWidget(self.engine_badge, 0, Qt.AlignVCenter)
        root.addWidget(hero)

        body = QHBoxLayout(); self._main_row = body; body.setSpacing(12)
        left = QVBoxLayout(); left.setSpacing(9)
        right = QVBoxLayout(); right.setSpacing(9)

        # Source URLs
        url_card = QFrame(self); self._url_card = url_card; url_card.setObjectName("downloadPanel")
        ul = QVBoxLayout(url_card); ul.setContentsMargins(14, 12, 14, 12); ul.setSpacing(7)
        top = QHBoxLayout()
        lab = QLabel("TEMPEL LINK VIDEO", url_card); lab.setObjectName("downloadSectionLabel"); top.addWidget(lab)
        top.addStretch()
        self.queue_meta = QLabel("Menunggu URL", url_card); self.queue_meta.setStyleSheet(f"color:{C['MUTED']};font-size:10px;")
        top.addWidget(self.queue_meta)
        self.paste_btn = QPushButton("▣  Tempel", url_card); self.paste_btn.setObjectName("ghost")
        self.paste_btn.clicked.connect(self._paste); top.addWidget(self.paste_btn)
        self.clear_btn = QPushButton("Bersihkan", url_card); self.clear_btn.setObjectName("ghost")
        self.clear_btn.clicked.connect(lambda: self.url_edit.clear()); top.addWidget(self.clear_btn)
        ul.addLayout(top)
        hint = QLabel("Satu link per baris. Banyak link diproses berurutan dalam satu antrean.", url_card)
        self._url_hint = hint; hint.setStyleSheet(f"color:{C['MUTED']};font-size:10px;"); ul.addWidget(hint)
        self.url_edit = QTextEdit(url_card)
        self.url_edit.setPlaceholderText("Tempel satu atau beberapa link video…")
        self.url_edit.setMinimumHeight(112); self.url_edit.setMaximumHeight(210)
        self.url_edit.textChanged.connect(self._on_url_change); ul.addWidget(self.url_edit)
        left.addWidget(url_card, 3)

        # Existing quick settings, now presented as a compact download form.
        info = QFrame(self); self._info_card = info; info.setObjectName("downloadPanel")
        il = QVBoxLayout(info); il.setContentsMargins(14, 12, 14, 12); il.setSpacing(7)
        settings_title = QLabel("PENGATURAN DOWNLOAD", info); settings_title.setObjectName("downloadSectionLabel"); il.addWidget(settings_title)
        quality_format = QHBoxLayout(); quality_format.setSpacing(10)
        self.quick_quality = QComboBox(info)
        self.quick_quality.addItems(["Terbaik yang tersedia", "Maks 4K (2160p)", "Maks 1440p", "Maks 1080p", "Maks 720p", "Maks 540p"])
        self.quick_quality.setCurrentText(self.app.config.get("quality") or "Terbaik yang tersedia")
        quality_col = QVBoxLayout(); quality_col.setSpacing(4)
        qlabel = QLabel("KUALITAS VIDEO", info); qlabel.setObjectName("downloadSectionLabel"); quality_col.addWidget(qlabel); quality_col.addWidget(self.quick_quality)
        quality_format.addLayout(quality_col, 1)
        self.quick_format = QComboBox(info)
        self.quick_format.addItems(["MP4 (Video + Audio)", "MP3 (Audio saja)", "M4A (Audio saja)"])
        self.quick_format.setCurrentText(self.app.config.get("output_format") or "MP4 (Video + Audio)")
        format_col = QVBoxLayout(); format_col.setSpacing(4)
        flabel = QLabel("FORMAT OUTPUT", info); flabel.setObjectName("downloadSectionLabel"); format_col.addWidget(flabel); format_col.addWidget(self.quick_format)
        quality_format.addLayout(format_col, 1); il.addLayout(quality_format)

        self.h264_checkbox = QCheckBox("Prioritaskan H.264 (kompatibilitas lebih baik)", info)
        self.h264_checkbox.setChecked(bool(self.app.config.get("h264", True)))
        self.h264_checkbox.setToolTip("Memilih H.264 bila tersedia agar file MP4 lebih kompatibel.")
        il.addWidget(self.h264_checkbox)
        self.audio_row = QHBoxLayout(); self.audio_row.setSpacing(8)
        audio_label = QLabel("Audio track", info); audio_label.setStyleSheet(f"color:{C['MUTED']};font-size:10px;font-weight:700;")
        self.audio_row.addWidget(audio_label)
        self.quick_audio = QComboBox(info)
        self.quick_audio.addItems(["Auto — Indonesia jika tersedia", "Indonesia", "English", "Original / default"])
        self.quick_audio.setCurrentText(self.app.config.get("audio_language") or "Auto — Indonesia jika tersedia")
        self.quick_audio.setToolTip("Auto memprioritaskan audio Indonesia bila tersedia; URL tidak membawa pilihan track player browser.")
        self.audio_row.addWidget(self.quick_audio, 1); il.addLayout(self.audio_row)
        hint_audio = QLabel("Audio Auto: Indonesia bila tersedia, lalu fallback ke track yang tersedia.", info)
        self._audio_hint = hint_audio; hint_audio.setWordWrap(True); hint_audio.setStyleSheet(f"color:{C['MUTED']};font-size:10px;"); il.addWidget(hint_audio)
        self._auth_note = QLabel(f"Login fallback: {self.app.config.get('browser_cookie_source') or 'Auto'} (untuk post terbatas)", info)
        self._auth_note.setWordWrap(True); self._auth_note.setStyleSheet(f"color:{C['MUTED']};font-size:10px;"); il.addWidget(self._auth_note)
        left.addWidget(info, 2)

        # Direct, visible output-folder control.
        folder_card = QFrame(self); folder_card.setObjectName("downloadPanel")
        folder_layout = QVBoxLayout(folder_card); folder_layout.setContentsMargins(14, 11, 14, 11); folder_layout.setSpacing(6)
        folder_label = QLabel("LOKASI PENYIMPANAN", folder_card); folder_label.setObjectName("downloadSectionLabel"); folder_layout.addWidget(folder_label)
        folder_row = QHBoxLayout(); folder_row.setSpacing(8)
        self.out_dir_edit = QLineEdit(str(self.app.config.get("out_dir") or ""), folder_card)
        self.out_dir_edit.setPlaceholderText("Pilih folder untuk file hasil download")
        self.out_dir_edit.setToolTip("Folder tujuan untuk seluruh file download.")
        folder_row.addWidget(self.out_dir_edit, 1)
        self.browse_folder_btn = QPushButton("Pilih Folder", folder_card); self.browse_folder_btn.setObjectName("ghost")
        self.browse_folder_btn.clicked.connect(self._choose_output_folder); folder_row.addWidget(self.browse_folder_btn)
        folder_layout.addLayout(folder_row)
        left.addWidget(folder_card, 0)

        actions = QHBoxLayout(); actions.setSpacing(8)
        self.preview_btn = QPushButton("◉  Pratinjau", self); self.preview_btn.setObjectName("ghost")
        self.preview_btn.clicked.connect(self._preview); actions.addWidget(self.preview_btn)
        self.download_btn = QPushButton("⇩  MULAI DOWNLOAD", self); self.download_btn.setObjectName("primary")
        self.download_btn.setMinimumHeight(46); self.download_btn.clicked.connect(self._download); actions.addWidget(self.download_btn, 1)
        self.cancel_btn = QPushButton("□  Batal", self); self.cancel_btn.setObjectName("danger")
        self.cancel_btn.setMinimumHeight(46); self.cancel_btn.setEnabled(False); self.cancel_btn.clicked.connect(self._cancel); actions.addWidget(self.cancel_btn)
        left.addLayout(actions)

        # Right side: results at a glance, progress, and queue/log.
        stat_row = QHBoxLayout(); stat_row.setSpacing(8)
        def make_stat(title_text, value_text, accent_name):
            card = QFrame(self); card.setObjectName("downloadStat")
            card.setProperty("class", accent_name)
            card.setStyleSheet(f"QFrame#downloadStat {{ border-top: 2px solid {accent_name}; }}")
            sl = QVBoxLayout(card); sl.setContentsMargins(12, 9, 12, 8); sl.setSpacing(1)
            title_label = QLabel(title_text.upper(), card); title_label.setStyleSheet(f"color:{C['MUTED']};font-size:9px;font-weight:800;"); sl.addWidget(title_label)
            value_label = QLabel(value_text, card); value_label.setObjectName("downloadStatValue"); value_label.setStyleSheet(f"color:{accent_name};font-size:24px;font-weight:900;"); sl.addWidget(value_label)
            stat_row.addWidget(card, 1)
            return value_label
        self.stat_total = make_stat("Total link", "0", C["BLUE"])
        self.stat_success = make_stat("Berhasil", "0", C["GREEN"])
        self.stat_failure = make_stat("Gagal", "0", C["RED"])
        right.addLayout(stat_row)

        progress_card = QFrame(self); progress_card.setObjectName("downloadProgressCard")
        pl = QVBoxLayout(progress_card); pl.setContentsMargins(13, 10, 13, 10); pl.setSpacing(6)
        progress_top = QHBoxLayout()
        self.status_label = QLabel("Siap digunakan", progress_card); self.status_label.setStyleSheet("font-weight:700;")
        progress_top.addWidget(self.status_label, 1)
        self.pct_label = QLabel("0%", progress_card); self.pct_label.setStyleSheet(f"color:{C['GOLD']};font-weight:900;font-size:14px;")
        progress_top.addWidget(self.pct_label); pl.addLayout(progress_top)
        self.progress = QProgressBar(progress_card); self.progress.setObjectName("downloadProgress")
        self.progress.setRange(0, 100); self.progress.setValue(0); self.progress.setTextVisible(False); pl.addWidget(self.progress)
        right.addWidget(progress_card)

        queue_panel = QFrame(self); queue_panel.setObjectName("downloadQueuePanel")
        ql = QVBoxLayout(queue_panel); ql.setContentsMargins(8, 7, 8, 8); ql.setSpacing(5)
        tabs = QHBoxLayout(); tabs.setSpacing(4)
        self.tab_queue = QPushButton("▤  Antrean", queue_panel); self.tab_queue.setObjectName("tabActive")
        self.tab_queue.clicked.connect(lambda: self._switch_tab("queue")); tabs.addWidget(self.tab_queue)
        self.tab_log = QPushButton("▤  Log Aktivitas", queue_panel); self.tab_log.setObjectName("tab")
        self.tab_log.clicked.connect(lambda: self._switch_tab("log")); tabs.addWidget(self.tab_log)
        tabs.addStretch(); ql.addLayout(tabs)
        self.queue = QTableWidget(0, 5, queue_panel); self.queue.setObjectName("downloadQueue")
        self.queue.setAlternatingRowColors(True); self.queue.setShowGrid(False)
        self.queue.setHorizontalHeaderLabels(["#", "PLATFORM", "JUDUL / LINK", "DURASI", "STATUS"])
        hdr = self.queue.horizontalHeader(); hdr.setSectionResizeMode(0, QHeaderView.Fixed); hdr.setSectionResizeMode(1, QHeaderView.Fixed)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch); hdr.setSectionResizeMode(3, QHeaderView.Fixed); hdr.setSectionResizeMode(4, QHeaderView.Fixed)
        self.queue.setColumnWidth(0, 42); self.queue.setColumnWidth(1, 92); self.queue.setColumnWidth(3, 70); self.queue.setColumnWidth(4, 140)
        self.queue.setSelectionBehavior(QAbstractItemView.SelectRows); self.queue.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.queue.verticalHeader().setVisible(False); self.queue.verticalHeader().setDefaultSectionSize(36)
        self.queue.setContextMenuPolicy(Qt.CustomContextMenu); self.queue.customContextMenuRequested.connect(self._queue_menu); self.queue.cellDoubleClicked.connect(self._show_detail)
        ql.addWidget(self.queue, 1)
        self.log_view = QTextEdit(queue_panel); self.log_view.setReadOnly(True); self.log_view.setPlaceholderText("Aktivitas downloader akan tampil di sini…")
        self.log_view.hide(); ql.addWidget(self.log_view, 1)
        footer = QLabel("Klik dua kali pada baris antrean untuk melihat detail atau pesan error.", queue_panel)
        footer.setStyleSheet(f"color:{C['MUTED']};font-size:9px;padding:2px 4px;"); ql.addWidget(footer)
        right.addWidget(queue_panel, 1)

        body.addLayout(left, 46); body.addLayout(right, 54); root.addLayout(body, 1)

        self.banner = QFrame(self); self.banner.setStyleSheet(f"background:{C['PANEL']};border:1px solid {C['GOLD']};border-radius:12px;")
        bl = QHBoxLayout(self.banner); bl.setContentsMargins(14, 9, 14, 9)
        self.banner_label = QLabel("", self.banner); self.banner_label.setWordWrap(True); bl.addWidget(self.banner_label, 1)
        self.banner_btn = QPushButton("📂 Buka Folder", self.banner); self.banner_btn.setObjectName("ghost")
        self.banner_btn.clicked.connect(lambda: open_path(self.app.config.get("out_dir"))); bl.addWidget(self.banner_btn)
        self.banner.hide(); self._banner_anim = None; self._banner_timer = None

        self._info_values = {"Queue": self.queue_meta, "Engine": self.engine_badge, "Output": self.out_dir_edit}
        self._current_tab = "queue"; self._running = False; self._cancel_flag = False; self._real_progress_seen = False; self._progress_anim_value = 0
        self._progress_anim = QTimer(self); self._progress_anim.setInterval(220); self._progress_anim.timeout.connect(self._animate_start_progress)
        self._value_anim = QPropertyAnimation(self.progress, b"value", self); self._value_anim.setDuration(180); self._value_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._compact = False
        try:
            from chopster.downloader.engine import _find_js_runtime
            from chopster.downloader.ffmpeg import ffmpeg_available
            rt = _find_js_runtime(); media_ready = ffmpeg_available()
            try:
                import curl_cffi  # noqa: F401
                tiktok_ready = True
            except ImportError:
                tiktok_ready = False
            engine_bits = [
                "FFmpeg ✓" if media_ready else "FFmpeg !",
                f"{rt[0].title()} ✓" if rt else "JS runtime !",
                "TikTok ✓" if tiktok_ready else "TikTok !",
            ]
            self.engine_badge.setText("  •  ".join(engine_bits))
            self.engine_badge.setStyleSheet(f"color:{C['GREEN'] if media_ready else C['GOLD']};font-size:10px;font-weight:800;background:{C['PILL_BG']};border:1px solid {C['PILL_BD']};border-radius:8px;padding:8px 11px;")
            self._log(f"YouTube runtime: {rt[0]} — {rt[1]}" if rt else "YouTube runtime: belum ditemukan (Deno/Node).")
            self._log("TikTok impersonation: curl-cffi aktif." if tiktok_ready else "TikTok impersonation: curl-cffi belum terpasang.")
        except Exception as exc:
            self.engine_badge.setText("Engine perlu pemeriksaan")
            self.app.logger.warning("download preflight UI failed: %s", exc)
        self.sig_add_row.connect(self._add_row); self.sig_log.connect(self._log); self.sig_preview_done.connect(self._done_preview)
        self.sig_progress.connect(self._apply_progress); self.sig_row_update.connect(self._apply_row_update); self.sig_row_add.connect(self._add_row)
        self.sig_download_log.connect(self._log); self.sig_done.connect(self._finalize_download); self._errors = {}; self._pending_urls = []
        self.quick_quality.currentTextChanged.connect(self._persist_quick_settings)
        self.quick_format.currentTextChanged.connect(self._persist_quick_settings)
        self.quick_audio.currentTextChanged.connect(self._persist_quick_settings)
        self.h264_checkbox.toggled.connect(self._persist_quick_settings)
        self.out_dir_edit.editingFinished.connect(self._persist_quick_settings)
        self._refresh_stats()

    # -- helpers ----------------------------------------------------

    def get_urls(self) -> list[str]:
        return clean_urls(self.url_edit.toPlainText())

    def receive_external_url(self, url: str) -> None:
        """Appended by MainWindow when browser bridge sends a URL. Auto behavior: clear if idle, queue, and show notification."""
        url = (url or "").strip()
        if not valid_url(url):
            return
        existing = set(self.get_urls())
        if url in existing:
            self._log(f"URL sudah ada di antrean: {url}")
            # still flash status so user knows bridge worked
            self.status_label.setText("Link sudah ada di antrean")
            return
        # UX parity with Tkinter: if user has browsed but hasn't downloaded yet, extension URL goes to input
        # If currently downloading, append without clearing so queue not lost; otherwise keep existing + append
        cur = self.url_edit.toPlainText().strip()
        sep = "\n" if cur else ""
        self.url_edit.setPlainText(cur + sep + url if cur else url)
        self._log(f"Ditambahkan dari browser: {url}")
        self.status_label.setText("Link diterima dari browser — klik Pratinjau atau Mulai Download")

    @Slot()
    def _on_url_change(self) -> None:
        n = len(self.get_urls())
        self.url_count.setText(f"{n} link")
        if hasattr(self, "queue_meta"):
            self.queue_meta.setText("Siap" if n == 1 else (f"{n} item dalam antrean" if n else "Menunggu URL"))
        if hasattr(self, "_info_values"):
            self._info_values["Queue"].setText("Siap" if n == 1 else (f"{n} item" if n else "Menunggu URL"))
        self._refresh_stats()

    def _persist_quick_settings(self, *_args) -> None:
        """Save the same downloader preferences exposed in Settings."""
        if not hasattr(self, "quick_quality"):
            return
        self.app.config.update({
            "quality": self.quick_quality.currentText(),
            "output_format": self.quick_format.currentText(),
            "audio_language": self.quick_audio.currentText(),
            "h264": self.h264_checkbox.isChecked(),
            "out_dir": self.out_dir_edit.text().strip(),
        })

    def refresh_download_settings(self) -> None:
        """Reload quick controls from Settings without saving stale values."""
        if getattr(self, "_running", False):
            return
        config = self.app.config
        pairs = (
            (self.quick_quality, config.get("quality") or "Terbaik yang tersedia"),
            (self.quick_format, config.get("output_format") or "MP4 (Video + Audio)"),
            (self.quick_audio, config.get("audio_language") or "Auto — Indonesia jika tersedia"),
        )
        for widget, value in pairs:
            was_blocked = widget.blockSignals(True)
            widget.setCurrentText(str(value))
            widget.blockSignals(was_blocked)
        was_blocked = self.h264_checkbox.blockSignals(True)
        self.h264_checkbox.setChecked(bool(config.get("h264", True)))
        self.h264_checkbox.blockSignals(was_blocked)
        self.out_dir_edit.setText(str(config.get("out_dir") or ""))
        if hasattr(self, "_auth_note"):
            self._auth_note.setText(
                f"Login fallback: {config.get('browser_cookie_source') or 'Auto'} (untuk post terbatas)"
            )

    def _choose_output_folder(self) -> None:
        current = self.out_dir_edit.text().strip() or str(Path.home() / "Downloads")
        folder = QFileDialog.getExistingDirectory(self, "Pilih folder penyimpanan", current)
        if folder:
            self.out_dir_edit.setText(folder)
            self._persist_quick_settings()

    def _refresh_stats(self) -> None:
        if not hasattr(self, "stat_total"):
            return
        total = max(len(self.get_urls()), self.queue.rowCount())
        successes = 0
        failures = 0
        for row in range(self.queue.rowCount()):
            item = self.queue.item(row, 4)
            status = item.text().strip().lower() if item else ""
            if "berhasil" in status:
                successes += 1
            elif "gagal" in status:
                failures += 1
        self.stat_total.setText(str(total))
        self.stat_success.setText(str(successes))
        self.stat_failure.setText(str(failures))

    def _paste(self) -> None:
        from PySide6.QtWidgets import QApplication
        txt = QApplication.clipboard().text() or ""
        if not txt.strip():
            QMessageBox.information(self, "Clipboard", "Tidak ada teks di clipboard.")
            return
        cur = self.url_edit.toPlainText().strip()
        if cur:
            self.url_edit.append(txt.strip())
        else:
            self.url_edit.setPlainText(txt.strip())

    # -- download logic (real yt-dlp, threaded) ---------------------

    def _quick_labeled(self,label,widget,C):
        card=QFrame(self._info_card if hasattr(self,"_info_card") else self); row=QHBoxLayout(card); row.setContentsMargins(0,0,0,0); row.setSpacing(8)
        a=QLabel(label,card); a.setStyleSheet(f"color:{C['MUTED']};font-size:10px;font-weight:700;"); row.addWidget(a); row.addWidget(widget,1); return card

    def set_compact(self, compact: bool):
        self._compact = bool(compact)
        try:
            if hasattr(self, "_root_layout"):
                m = 10 if compact else 24
                self._root_layout.setContentsMargins(m, 10 if compact else 20, m, 12 if compact else 22)
                self._root_layout.setSpacing(8 if compact else 14)
            # On a narrow window, stack the URL and control cards instead of
            # squeezing two wide columns into a tiny viewport.
            if hasattr(self, "_main_row"):
                self._main_row.setDirection(QBoxLayout.TopToBottom if compact else QBoxLayout.LeftToRight)
                self._main_row.setSpacing(8 if compact else 14)
            if hasattr(self, "url_edit"):
                self.url_edit.setMinimumHeight(84 if compact else 126)
                self.url_edit.setMaximumHeight(150 if compact else 240)
            if hasattr(self, "_info_card"):
                self._info_card.setVisible(True)
            if hasattr(self, "_hero_layout"):
                self._hero_layout.setContentsMargins(14 if compact else 22, 12 if compact else 18, 14 if compact else 22, 12 if compact else 18)
                self._hero_layout.setSpacing(10 if compact else 18)
            if hasattr(self, "_hero_icon"):
                self._hero_icon.setVisible(not compact)
            if hasattr(self, "_hero_subtitle"):
                self._hero_subtitle.setVisible(not compact)
            if hasattr(self, "_url_hint"):
                self._url_hint.setVisible(not compact)
            if hasattr(self, "_audio_hint"):
                self._audio_hint.setVisible(not compact)
            if hasattr(self, "_auth_note"):
                self._auth_note.setVisible(not compact)
            self.queue.setColumnHidden(1, compact)
            self.queue.setColumnHidden(3, compact)
            self.queue.setColumnHidden(0, compact)
            self.queue.setColumnWidth(4, 118 if compact else 170)
            self.preview_btn.setText("👁" if compact else "👁  Preview")
            self.cancel_btn.setText("⏹" if compact else "⏹  Cancel")
            if not self._running:
                self.download_btn.setText("↓" if compact else "⬇  MULAI DOWNLOAD")
            # Keep the tab strip usable at small widths.
            try:
                self.tab_queue.setText("QUEUE" if compact else "📑  QUEUE")
                self.tab_log.setText("LOG" if compact else "📜  ACTIVITY LOG")
            except Exception:
                pass
        except Exception:
            pass

    def _set_running(self, v: bool) -> None:
        self._running = v
        self.download_btn.setEnabled(not v); self.preview_btn.setEnabled(not v); self.cancel_btn.setEnabled(v); self.url_edit.setReadOnly(v)
        if v:
            self.download_btn.setText("⏳" if self._compact else "⏳  MENGUNDUH...")
            self.progress.setRange(0,0); self.pct_label.setText("• • •")
            self._real_progress_seen=False; self._progress_anim_value=0; self._progress_anim.stop()
            self.status_label.setText("Menghubungkan… menunggu progress nyata")
            self.banner.hide()
        else:
            self.download_btn.setText("↓" if self._compact else "⬇  MULAI DOWNLOAD"); self._progress_anim.stop(); self.progress.setRange(0,100)

    def _animate_start_progress(self):
        return

    def _log(self, msg: str, tag: str = "") -> None:
        text=str(msg or "").strip(); low=text.lower()
        important=("error" in low or "gagal" in low or "berhasil" in low or "selesai" in low or "dibatalkan" in low or "pratinjau selesai" in low or text.startswith("✓") or text.startswith("✗") or text.lower().startswith("info "))
        if "subtitle tidak tersedia/ditolak sumber" in low:
            important=True
        if not important: return
        self.log_view.append(text); self.request_log.emit(text, tag)

    def _preview(self) -> None:
        urls = self.get_urls()
        if not urls:
            QMessageBox.warning(self, "URL kosong", "Tempel minimal satu link valid.")
            return
        self._log(f"Memeriksa {len(urls)} link...")
        # run metadata fetch in background
        self.queue.setRowCount(0)
        self._cancel_flag = False
        self._set_running(True)
        self.status_label.setText("Mengambil metadata...")
        import threading
        from concurrent.futures import ThreadPoolExecutor

        def work():
            # Use the same hardened yt-dlp metadata path as the real downloader,
            # including YouTube JS runtime and client fallback handling.
            from chopster.downloader.engine import fetch_metadata
            def inspect(item):
                idx, url = item
                try:
                    info = fetch_metadata(url)
                    title = info.get("title") or url
                    dur = info.get("duration")
                    dur_s = f"{int(dur)//60}:{int(dur)%60:02d}" if dur else "-"
                    platform = detect_platform(url)
                    langs=sorted({str(f.get("language")) for f in (info.get("formats") or []) if isinstance(f,dict) and f.get("language") and f.get("acodec") not in (None,"none")})
                    audio_note=(" • audio: "+", ".join(langs[:8])) if langs else ""
                    return idx, platform, title, dur_s, "Siap"+audio_note[:80], f"[pratinjau] {title} ({dur_s}){audio_note}"
                except Exception as exc:
                    return idx, detect_platform(url), url, "-", f"Gagal: {exc}", f"[gagal pratinjau] {url}: {exc}"
            with ThreadPoolExecutor(max_workers=min(3, len(urls))) as pool:
                for result in pool.map(inspect, enumerate(urls, 1)):
                    if self._cancel_flag:
                        break
                    idx, platform, title, dur_s, status, log_text = result
                    self._safe_add_row(idx, platform, title, dur_s, status)
                    self._safe_log(log_text)
            self._safe_done_preview()

        threading.Thread(target=work, daemon=True).start()

    def _safe_add_row(self, *a):
        # emit queued signal — thread-safe even from plain Python threads
        try:
            self.sig_add_row.emit(int(a[0]), str(a[1]), str(a[2]), str(a[3]), str(a[4]))
        except Exception:
            pass

    def _safe_log(self, msg):
        try:
            self.sig_log.emit(str(msg))
        except Exception:
            pass

    def _safe_done_preview(self):
        try:
            self.sig_preview_done.emit()
        except Exception:
            pass

    @Slot()
    def _log_slot(self): pass

    @Slot()
    def _done_preview(self):
        self._set_running(False)
        if self._cancel_flag:
            self._cancel_flag = False
            self.status_label.setText("Pratinjau dibatalkan")
        else:
            self.status_label.setText("Pratinjau selesai")

    def _add_row(self, no: int, platform: str, title: str, dur: str, status: str) -> None:
        r = self.queue.rowCount()
        self.queue.insertRow(r)
        for c, v in enumerate([str(no), platform, title, dur, status]):
            self.queue.setItem(r, c, QTableWidgetItem(v))
        self._refresh_stats()

    def _ask_overwrite_archive(self, urls: list[str]) -> tuple[list[str], set[str]]:
        """Parity with Tkinter _ask_duplicates: check history for duplicates, offer skip/retry/cancel.
        Returns (filtered_urls_to_download, skipped_set). If user cancels, returns ([], set(urls))."""
        if not bool(self.app.config.get("ask_duplicates", True)):
            return urls, set()
        # Check which urls were already downloaded (history status Berhasil)
        dupes: list[str] = []
        try:
            for u in urls:
                if self.app.db.has_url(u):
                    # Confirm via history query — only count Berhasil
                    rows = self.app.db.list_history(search=u, status="Berhasil")
                    if any(r.get("url") == u for r in rows):
                        dupes.append(u)
        except Exception:
            pass
        if not dupes:
            return urls, set()
        # Show dialog on main thread (we are on main thread here — _download called from button)
        box = QMessageBox(self)
        box.setWindowTitle("Link sudah pernah diunduh")
        box.setText(f"{len(dupes)} link sudah pernah diunduh (arsip).\n\nYa = unduh ulang  |  Tidak = lewati duplikat  |  Batal = batal semua")
        box.setIcon(QMessageBox.Question)
        yes = box.addButton("Ya — unduh ulang", QMessageBox.YesRole)
        no = box.addButton("Lewati duplikat", QMessageBox.NoRole)
        cancel = box.addButton("Batal", QMessageBox.RejectRole)
        box.setDefaultButton(no)
        box.exec()
        clicked = box.clickedButton()
        if clicked == cancel:
            return [], set(urls)
        if clicked == no:
            return [u for u in urls if u not in set(dupes)], set(dupes)
        self._retry_mode = True
        return urls, set()

    def _check_disk(self, out: str) -> bool:
        """Parity with Tkinter _prepare_folder disk check: 200MB hard block, 1GB soft warn."""
        import shutil
        try:
            tgt = out if Path(out).is_dir() else str(Path(out).parent)
            free = shutil.disk_usage(tgt).free if Path(tgt).exists() else None
        except Exception:
            free = None
        if free is not None and free < 200 * 1024 * 1024:
            # Hard block
            free_s = f"{free/1024/1024:.0f} MB" if free < 1024*1024*1024 else f"{free/1024/1024/1024:.1f} GB"
            QMessageBox.critical(self, "Ruang disk hampir habis", f"Sisa ruang hanya {free_s}. Kosongkan disk sebelum mengunduh.")
            return False
        if free is not None and free < 1024 * 1024 * 1024:
            free_s = f"{free/1024/1024:.1f} MB"
            if QMessageBox.question(self, "Ruang disk tipis", f"Sisa ruang {free_s}. Lanjutkan unduhan?") != QMessageBox.Yes:
                return False
        return True

    def _download(self) -> None:
        urls = self.get_urls()
        if not urls:
            QMessageBox.warning(self, "URL kosong", "Tempel minimal satu link valid.")
            return
        self._persist_quick_settings()
        # Validate out dir + disk
        out = (self.app.config.get("out_dir") or "").strip()
        if not out:
            QMessageBox.warning(self, "Folder tidak valid", "Pilih folder penyimpanan di Pengaturan.")
            return
        try:
            Path(out).mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            QMessageBox.critical(self, "Folder tidak valid", str(exc))
            return
        if not self._check_disk(out):
            return
        # Guarantee that MP4 is assembled from the selected video+audio streams
        # and that the final artifact can be validated before it is marked successful.
        from chopster.downloader.ffmpeg import find_ffmpeg, find_ffprobe
        needs_ffprobe = not str(self.quick_format.currentText()).startswith(("MP3", "M4A"))
        if not find_ffmpeg() or (needs_ffprobe and not find_ffprobe()):
            QMessageBox.critical(
                self,
                "FFmpeg belum lengkap",
                "Semua format memerlukan ffmpeg; mode video juga memerlukan ffprobe.\n\n"
                "Letakkan komponen yang diperlukan di sebelah aplikasi atau tambahkan ke PATH.",
            )
            return
        # Duplicate ask — an explicit re-download must bypass yt-dlp's archive.
        self._retry_mode = False
        filtered, skipped = self._ask_overwrite_archive(urls)
        if not filtered and skipped:
            # User cancelled or all skipped — still show queue as dilewati
            self._log(f"Duplikat: {len(skipped)} dilewati")
            self.queue.setRowCount(0)
            for i, u in enumerate(urls, 1):
                self._add_row(i, detect_platform(u), u, "-", "Dilewati (duplikat)" if u in skipped else "Menunggu")
            self._show_banner("warn", f"↷ {len(skipped)} dilewati (sudah di arsip)")
            return
        if not filtered:
            return
        # If some skipped, we keep full list but engine will skip them via skip_urls
        self._skip_urls = skipped
        self._filtered_urls = filtered
        self._cancel_flag = False
        try: self.app.config.update({"quality":self.quick_quality.currentText(),"output_format":self.quick_format.currentText(),"audio_language":self.quick_audio.currentText()})
        except Exception: pass
        self.queue.setRowCount(0)
        self._errors.clear()
        self._pending_urls = urls
        self._set_running(True)
        self.progress.setValue(0)
        self.pct_label.setText("0%")
        self.status_label.setText(f"Running — menghubungkan ke sumber ({len(filtered)} file)...")
        
        # Log FFmpeg path like Tkinter so audio failures are obvious
        try:
            from chopster.downloader.ffmpeg import find_ffmpeg
            if not find_ffmpeg() and str(self.app.config.get("output_format") or "").startswith(("MP3","M4A")):
                self._log("ERROR: FFmpeg tidak ditemukan — mode audio tidak dapat diproses.")
        except Exception:pass
        import threading
        def work():
            self._run_downloads(urls)
        threading.Thread(target=work, daemon=True).start()

    def _run_downloads(self, urls: list[str]) -> None:
        """Run the single downloader engine; do not maintain a second legacy yt-dlp path."""
        try:
            from chopster.downloader.engine import download_batch
            cfg = self.app.config.as_dict()
            parallel = max(1, min(3, int(cfg.get("parallel") or 2)))

            def on_progress(pct: int, msg: str):
                self.sig_progress.emit(int(pct), str(msg))

            def on_row(no: int, plat: str, title: str, status: str):
                if status == "Menunggu":
                    self.sig_row_add.emit(int(no), str(plat), str(title), "-", str(status))
                else:
                    self.sig_row_update.emit(int(no), str(plat), str(title), str(status))

            def on_log(msg: str):
                self.sig_download_log.emit(str(msg))

            ok, fail = download_batch(
                urls, cfg,
                parallel=parallel,
                on_progress=on_progress,
                on_row=on_row,
                on_log=on_log,
                cancel_flag=lambda: bool(getattr(self, "_cancel_flag", False)),
                db=self.app.db,
                retry_mode=bool(getattr(self, "_retry_mode", False)),
                skip_urls=getattr(self, "_skip_urls", set()),
            )
            out_dir = cfg.get("out_dir") or ""
            self.sig_done.emit(int(ok), int(fail), str(out_dir))
        except Exception as exc:
            # Never silently switch to a second, outdated downloader implementation.
            # Surface the real engine error so it can be fixed instead of masked.
            self.sig_download_log.emit(f"Downloader engine error: {exc}")
            self.sig_done.emit(0, len(urls), str(self.app.config.get("out_dir") or ""))

    def _finalize_download(self, ok: int, fail: int, out_dir: str) -> None:
        if getattr(self, "_cancel_flag", False):
            self._set_running(False)
            self._cancel_flag = False
            self.status_label.setText(f"Dibatalkan — {ok} berhasil, {fail} gagal")
            self._log(f"Dibatalkan oleh pengguna. Sebelum pembatalan: {ok} berhasil, {fail} gagal.")
            self._show_banner("warn", f"⏹  Dibatalkan — {ok} berhasil, {fail} gagal.")
            return
        self._on_download_done(ok, fail, out_dir)

    # thread-safe emitters via signals would be cleaner; use queued calls
    def _emit_progress(self, pct: int, msg: str):
        self._pending_progress = (pct, msg)
        from PySide6.QtCore import QMetaObject, Q_ARG
        # fallback: direct call via singleShot
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, lambda p=pct, m=msg: self._apply_progress(p, m))

    @Slot(int, str, str, str)
    def _apply_row_update(self, no: int, platform: str, title: str, status: str) -> None:
        for r in range(self.queue.rowCount()):
            it = self.queue.item(r, 0)
            if it and it.text() == str(no):
                self.queue.setItem(r, 1, QTableWidgetItem(platform))
                self.queue.setItem(r, 2, QTableWidgetItem(title))
                self.queue.setItem(r, 4, QTableWidgetItem(status))
                self._refresh_stats()
                break

    @Slot(int, int, str)
    def _on_download_done(self, ok: int, fail: int, out_dir: str) -> None:
        self._cancel_flag = False
        self._set_running(False)
        self.progress.setRange(0,100)
        self.progress.setValue(100)
        self.pct_label.setText("100%")
        if fail == 0:
            self.status_label.setText(f"Selesai — {ok} berhasil")
            self._show_banner("ok", f"✓  {ok} file berhasil diunduh.")
        elif ok == 0:
            self.status_label.setText(f"Selesai — {fail} gagal")
            self._show_banner("fail", f"✗  {fail} gagal. Cek log untuk detail.")
        else:
            self.status_label.setText(f"Selesai — {ok} berhasil, {fail} gagal")
            self._show_banner("warn", f"◐  {ok} berhasil, {fail} gagal.")
        self._log(f"Selesai: {ok} berhasil, {fail} gagal.")
        self._refresh_stats()

    def _apply_progress(self, pct: int, msg: str):
        pct=max(0,min(100,int(pct))); msg_text=str(msg)
        if self.progress.minimum()==0 and self.progress.maximum()==0:
            low=msg_text.lower()
            if pct <= 0 and ("menghubungkan" in low or "menyiapkan" in low or "memeriksa" in low):
                self.pct_label.setText("• • •"); self.status_label.setText(msg_text); return
            self.progress.setRange(0,100); self.progress.setValue(0)
        self._real_progress_seen = pct > 0 or "selesai" in msg_text.lower()
        if self._real_progress_seen:self._progress_anim.stop()
        try:
            self._value_anim.stop()
            self._value_anim.setStartValue(int(self.progress.value()))
            self._value_anim.setEndValue(pct)
            self._value_anim.start()
        except Exception:
            self.progress.setValue(pct)
        self.pct_label.setText(f"{pct}%"); self.status_label.setText(msg_text)

    def _emit_log(self, msg: str):
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, lambda m=msg: self._log(m))

    def _emit_row_add(self, no, plat, title, dur, status):
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, lambda: self._add_row(no, plat, title, dur, status))

    def _emit_row_update(self, no, plat, title, status):
        from PySide6.QtCore import QTimer
        def _upd():
            for r in range(self.queue.rowCount()):
                if self.queue.item(r, 0) and self.queue.item(r, 0).text() == str(no):
                    self.queue.setItem(r, 1, QTableWidgetItem(plat))
                    self.queue.setItem(r, 2, QTableWidgetItem(title))
                    self.queue.setItem(r, 4, QTableWidgetItem(status))
                    break
        QTimer.singleShot(0, _upd)

    def _emit_stats(self, done, ok, fail):
        pass

    def _emit_done(self, ok, fail, out_dir):
        from PySide6.QtCore import QTimer
        def _done():
            self._set_running(False)
            self.progress.setValue(100)
            self.pct_label.setText("100%")
            if fail == 0:
                self.status_label.setText(f"Selesai — {ok} berhasil")
                self._show_banner("ok", f"✓  {ok} file berhasil diunduh.")
            elif ok == 0:
                self.status_label.setText(f"Selesai — {fail} gagal")
                self._show_banner("fail", f"✗  {fail} gagal. Cek log untuk detail.")
            else:
                self.status_label.setText(f"Selesai — {ok} berhasil, {fail} gagal")
                self._show_banner("warn", f"◐  {ok} berhasil, {fail} gagal.")
            self._log(f"Selesai: {ok} berhasil, {fail} gagal.")
        QTimer.singleShot(0, _done)

    def _show_banner(self, kind, text):
        C = THEMES.get(self._theme, THEMES["dark"])
        colors = {"ok": C["GREEN"], "warn": C["GOLD"], "fail": C["RED"]}
        self.banner_label.setText(text)
        self.banner_label.setStyleSheet(f"color: {colors.get(kind, C['TEXT'])}; font-weight:800;")
        self.banner_btn.setVisible(kind != "fail")
        self.banner.adjustSize()
        w=max(360,min(560,self.width()-40)); h=max(66,self.banner.sizeHint().height())
        self.banner.resize(w,max(h,78))
        end=QRect(self.width()-w-20,20,w,h)
        start=QRect(self.width()-w+50,20,w,h)
        self.banner.setGeometry(start); self.banner.show(); self.banner.raise_(); self.banner.activateWindow() if hasattr(self.banner,"activateWindow") else None
        self._banner_anim=QPropertyAnimation(self.banner,b"geometry",self)
        self._banner_anim.setDuration(360); self._banner_anim.setStartValue(start); self._banner_anim.setEndValue(end); self._banner_anim.setEasingCurve(QEasingCurve.OutCubic); self._banner_anim.start()
        if self._banner_timer: self._banner_timer.stop()
        self._banner_timer=QTimer(self); self._banner_timer.setSingleShot(True); self._banner_timer.timeout.connect(self.banner.hide); self._banner_timer.start(6000)
        try:
            from PySide6.QtWidgets import QApplication
            QApplication.beep()
        except Exception: pass

    def resizeEvent(self,e):
        super().resizeEvent(e)
        if self.banner.isVisible():
            w=self.banner.width(); self.banner.move(max(10,self.width()-w-20),20)

    def _cancel(self):
        self._cancel_flag = True
        self._log("Membatalkan — menunggu proses aktif berhenti...")
        self.status_label.setText("Membatalkan...")
        self.cancel_btn.setEnabled(False)
        # Status UI tetap running sampai _run_downloads selesai dan memanggil _emit_done

    def _switch_tab(self, name: str):
        self._current_tab = name
        is_q = name == "queue"
        self.queue.setVisible(is_q)
        self.log_view.setVisible(not is_q)
        self.tab_queue.setObjectName("tabActive" if is_q else "tab")
        self.tab_log.setObjectName("tabActive" if not is_q else "tab")
        for b in (self.tab_queue, self.tab_log):
            b.style().unpolish(b); b.style().polish(b)

    def _queue_menu(self, pos):
        idx = self.queue.indexAt(pos)
        if not idx.isValid():
            return
        r = idx.row()
        title = self.queue.item(r, 2).text() if self.queue.item(r, 2) else ""
        status = self.queue.item(r, 4).text() if self.queue.item(r, 4) else ""
        # Retrieve URL stored during add/pending (col 2 may be URL before title is known)
        url_raw = self._pending_urls[r] if 0 <= r < len(self._pending_urls) else title
        # Retrieve file_path from error dict or history payload
        file_path = ""
        try:
            err_entry = self._errors.get(r)
            if isinstance(err_entry, dict) and err_entry.get("filepath"):
                file_path = err_entry["filepath"]
            elif isinstance(err_entry, dict) and err_entry.get("path"):
                file_path = err_entry["path"]
        except Exception:
            pass
        # Fallback: try to read from DB by matching URL/title
        if not file_path:
            try:
                for h in self.app.db.list_history(limit=200, search=url_raw) if hasattr(self.app, "db") else []:
                    if h.get("url") == url_raw and h.get("file_path"):
                        file_path = h["file_path"]; break
            except Exception:
                pass
        from PySide6.QtWidgets import QMenu
        m = QMenu(self)
        has_file = bool(file_path and Path(file_path).exists())
        folder = str(Path(file_path).parent) if has_file else (self.app.config.get("out_dir") or "")
        a1 = m.addAction("Buka file")
        a1.setEnabled(has_file)
        a1.triggered.connect(lambda: open_path(file_path) if has_file else None)
        a2 = m.addAction("Buka folder")
        a2.triggered.connect(lambda: open_path(folder) if folder and Path(folder).exists() else open_path(self.app.config.get("out_dir")))
        m.addSeparator()
        m.addAction("Salin link", lambda: self._copy(url_raw or title))
        m.addSeparator()
        a_retry = m.addAction("Coba lagi (item ini)")
        is_failed = "Gagal" in status or "gagal" in status
        a_retry.setEnabled(is_failed)
        a_retry.triggered.connect(lambda: self._retry_row(r))
        m.exec(self.queue.mapToGlobal(pos))

    def _retry_row(self, row: int):
        if 0 <= row < len(self._pending_urls):
            url = self._pending_urls[row]
            self.url_edit.setPlainText(url)
            self._download()
            return
        # Fallback: extract URL from queue title col
        row_title = self.queue.item(row, 2).text() if self.queue.item(row, 2) else ""
        self.url_edit.setPlainText(row_title)
        self._download()

    def _copy(self, text: str):
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(text)

    def _show_detail(self, r, c):
        vals = [self.queue.item(r, i).text() if self.queue.item(r, i) else "" for i in range(5)]
        QMessageBox.information(self, f"Detail #{vals[0]}", "\n".join(f"{k}: {v}" for k, v in zip(["#","Platform","Judul","Durasi","Status"], vals)))

    # legacy slots kept for compatibility (no longer used by preview thread)
    @Slot()
    def _add_row_slot(self): pass
    @Slot()
    def _log_slot(self): pass
    @Slot()
    def _done_preview_slot(self):
        self._done_preview()
