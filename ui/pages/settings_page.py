"""Settings page — grouped sections (full PRD)."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox, QCheckBox,
    QPushButton, QSpinBox, QFileDialog, QScrollArea, QFrame, QMessageBox, QListWidget, QListWidgetItem
)

from chopster.app.configuration import FILENAME_TEMPLATES, QUALITIES, FORMATS, SPEED_PRESETS
from chopster.ui.theme import THEMES
from chopster.ui.components.cards import Card


class SettingsPage(QWidget):
    ai_settings_changed = Signal()
    download_settings_changed = Signal()
    def __init__(self, app, theme: str = "dark", parent: QWidget | None = None):
        super().__init__(parent)
        self.app = app
        self._theme = theme
        self._download_settings_dirty = False
        self._download_settings_dirty_keys: set[str] = set()
        C = THEMES.get(theme, THEMES["dark"])

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        root.addWidget(scroll)

        container = QWidget()
        scroll.setWidget(container)
        lay = QVBoxLayout(container)
        lay.setContentsMargins(18, 18, 18, 18)
        lay.setSpacing(14)

        title = QLabel("Settings", container)
        title.setObjectName("h1")
        lay.addWidget(title)

        # -- DOWNLOAD
        lay.addWidget(self._section_title("DOWNLOAD", container))
        card = Card(container, theme)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 14, 16, 14)
        cl.setSpacing(8)
        cl.addWidget(self._lbl("LOKASI PENYIMPANAN", card))
        row = QHBoxLayout()
        self.out_dir = QLineEdit(card)
        self.out_dir.setText(self.app.config.get("out_dir"))
        row.addWidget(self.out_dir, 1)
        btn = QPushButton("Pilih Folder", card)
        btn.setObjectName("ghost")
        btn.clicked.connect(self._pick_folder)
        row.addWidget(btn)
        cl.addLayout(row)

        row2 = QHBoxLayout()
        col1 = QVBoxLayout()
        col1.addWidget(self._lbl("KUALITAS", card))
        self.quality = QComboBox(card)
        self.quality.addItems(list(QUALITIES.keys()))
        self.quality.setCurrentText(self.app.config.get("quality"))
        col1.addWidget(self.quality)
        row2.addLayout(col1, 1)
        col2 = QVBoxLayout()
        col2.addWidget(self._lbl("FORMAT", card))
        self.format_cb = QComboBox(card)
        self.format_cb.addItems(list(FORMATS))
        self.format_cb.setCurrentText(self.app.config.get("output_format"))
        col2.addWidget(self.format_cb)
        row2.addLayout(col2, 1)
        cl.addLayout(row2)

        audio_row=QHBoxLayout()
        audio_col=QVBoxLayout(); audio_col.addWidget(self._lbl("AUDIO TRACK", card))
        self.audio_language=QComboBox(card)
        self.audio_language.addItems(["Auto — Indonesia jika tersedia","Indonesia","English","Original / default"])
        self.audio_language.setCurrentText(self.app.config.get("audio_language") or "Auto — Indonesia jika tersedia")
        self.audio_language.setToolTip("URL YouTube tidak menyimpan track player yang sedang dipilih. Auto memprioritaskan Indonesia jika tersedia.")
        audio_col.addWidget(self.audio_language); audio_row.addLayout(audio_col,1)
        info=QLabel("Auto: Indonesia → fallback. Untuk track lain, pilih bahasa secara eksplisit.",card); info.setWordWrap(True); info.setStyleSheet(f"color:{THEMES.get(self._theme, THEMES['dark'])['MUTED']};font-size:10px;"); audio_row.addWidget(info,1)
        cl.addLayout(audio_row)

        self.h264 = QCheckBox("Prioritaskan H.264", card)
        self.h264.setChecked(bool(self.app.config.get("h264")))
        cl.addWidget(self.h264)

        row3 = QHBoxLayout()
        c1 = QVBoxLayout()
        c1.addWidget(self._lbl("TEMPLATE NAMA FILE", card))
        self.tmpl = QComboBox(card)
        self.tmpl.addItems(list(FILENAME_TEMPLATES.keys()))
        self.tmpl.setCurrentText(self.app.config.get("filename_template"))
        c1.addWidget(self.tmpl)
        row3.addLayout(c1, 1)
        c2 = QVBoxLayout()
        c2.addWidget(self._lbl("BATAS KECEPATAN", card))
        self.speed = QComboBox(card)
        self.speed.addItems(list(SPEED_PRESETS.keys()))
        self.speed.setCurrentText(self.app.config.get("speed_limit"))
        c2.addWidget(self.speed)
        row3.addLayout(c2, 1)
        cl.addLayout(row3)

        prow = QHBoxLayout()
        prow.addWidget(self._lbl("PARALEL (1-3)", card))
        self.parallel = QSpinBox(card)
        self.parallel.setRange(1, 3)
        self.parallel.setValue(int(self.app.config.get("parallel") or 2))
        prow.addWidget(self.parallel)
        prow.addStretch()
        cl.addLayout(prow)

        for key, label in [
            ("subfolders", "Subfolder per platform"),
            ("subtitles", "Unduh subtitle (id/en)"),
            ("thumbnail", "Unduh thumbnail"),
            ("ask_duplicates", "Tanya jika duplikat"),
            ("download_playlist", "Unduh seluruh playlist"),
        ]:
            cb = QCheckBox(label, card)
            cb.setChecked(bool(self.app.config.get(key)))
            if key == "subtitles":
                cb.setToolTip("File subtitle disimpan terpisah di subfolder Subtitles, bukan di samping video.")
            setattr(self, f"cb_{key}", cb)
            cl.addWidget(cb)

        cl.addWidget(self._lbl("ITEM PLAYLIST (contoh 1-10,12)", card))
        self.playlist_items = QLineEdit(card)
        self.playlist_items.setText(self.app.config.get("playlist_items") or "")
        cl.addWidget(self.playlist_items)

        cl.addWidget(self._lbl("SESI BROWSER UNTUK SITUS YANG BUTUH LOGIN", card))
        self.browser_cookie = QComboBox(card)
        self.browser_cookie.addItems(["Auto", "Chrome", "Edge", "Firefox", "Brave", "Chromium", "None"])
        self.browser_cookie.setCurrentText(str(self.app.config.get("browser_cookie_source") or "Auto"))
        self.browser_cookie.setToolTip("Chopster hanya mencoba cookie browser saat situs menolak akses/login, misalnya TikTok restricted post.")
        cl.addWidget(self.browser_cookie)
        hint_cookie = QLabel("Dipakai hanya sebagai fallback autentikasi. Login tetap harus ada di browser kamu.", card)
        hint_cookie.setWordWrap(True)
        hint_cookie.setStyleSheet("color:#94A3B8;font-size:10px;")
        cl.addWidget(hint_cookie)

        cl.addWidget(self._lbl("FILE COOKIES (Netscape)", card))
        crow = QHBoxLayout()
        self.cookies = QLineEdit(card)
        self.cookies.setText(self.app.config.get("cookies_file") or "")
        crow.addWidget(self.cookies, 1)
        cb = QPushButton("Pilih", card)
        cb.setObjectName("ghost")
        cb.clicked.connect(self._pick_cookies)
        crow.addWidget(cb)
        cl.addLayout(crow)

        cl.addWidget(self._lbl("PROXY (opsional)", card))
        self.proxy = QLineEdit(card)
        self.proxy.setPlaceholderText("http://127.0.0.1:8080 atau socks5://127.0.0.1:9050")
        self.proxy.setText(self.app.config.get("proxy") or "")
        cl.addWidget(self.proxy)
        lay.addWidget(card)

        # -- TAMPILAN
        lay.addWidget(self._section_title("TAMPILAN", container))
        acard = Card(container, theme)
        al = QHBoxLayout(acard)
        al.setContentsMargins(16, 12, 16, 12)
        al.addWidget(self._lbl("TEMA", acard))
        self.theme_cb = QComboBox(acard)
        self.theme_cb.addItems(["dark", "light"])
        self.theme_cb.setCurrentText(self.app.config.get("theme") or "dark")
        al.addWidget(self.theme_cb)
        al.addStretch()
        lay.addWidget(acard)

        # -- CLIPPER
        lay.addWidget(self._section_title("CLIPPER", container))
        ccard = Card(container, theme)
        ccl = QVBoxLayout(ccard)
        ccl.setContentsMargins(16, 14, 16, 14)
        ccl.setSpacing(8)
        row_c1 = QHBoxLayout()
        row_c1.addWidget(self._lbl("DURASI DEFAULT (detik)", ccard))
        self.clip_dur = QSpinBox(ccard)
        self.clip_dur.setRange(5, 600)
        self.clip_dur.setValue(int(self.app.config.get("clip_default_duration") or 30))
        row_c1.addWidget(self.clip_dur)
        row_c1.addWidget(self._lbl("OVERLAP (detik)", ccard))
        self.clip_overlap = QSpinBox(ccard)
        self.clip_overlap.setRange(0, 60)
        self.clip_overlap.setValue(int(self.app.config.get("clip_overlap") or 0))
        row_c1.addWidget(self.clip_overlap)
        row_c1.addStretch()
        ccl.addLayout(row_c1)

        row_c2 = QHBoxLayout()
        row_c2.addWidget(self._lbl("ASPECT", ccard))
        self.clip_aspect = QComboBox(ccard)
        self.clip_aspect.addItems(["original", "9:16", "1:1", "16:9", "4:5"])
        self.clip_aspect.setCurrentText(self.app.config.get("clip_aspect") or "9:16")
        row_c2.addWidget(self.clip_aspect)
        row_c2.addWidget(self._lbl("QUALITY", ccard))
        self.clip_quality = QComboBox(ccard)
        self.clip_quality.addItems(["fast", "balanced", "high"])
        self.clip_quality.setCurrentText(self.app.config.get("clip_quality") or "balanced")
        row_c2.addWidget(self.clip_quality)
        row_c2.addWidget(self._lbl("PARALEL RENDER", ccard))
        self.clip_parallel = QSpinBox(ccard)
        self.clip_parallel.setRange(1, 3)
        self.clip_parallel.setValue(int(self.app.config.get("clip_parallel") or 1))
        row_c2.addWidget(self.clip_parallel)
        row_c2.addStretch()
        ccl.addLayout(row_c2)

        row_c3 = QHBoxLayout()
        row_c3.addWidget(self._lbl("OUTPUT CLIPS", ccard))
        self.clip_out = QLineEdit(ccard)
        self.clip_out.setPlaceholderText("Kosong = <projects>/<id>/clips")
        self.clip_out.setText(self.app.config.get("clip_output_dir") or "")
        row_c3.addWidget(self.clip_out, 1)
        btn_c = QPushButton("Pilih", ccard)
        btn_c.setObjectName("ghost")
        btn_c.clicked.connect(lambda: self._pick_dir(self.clip_out))
        row_c3.addWidget(btn_c)
        ccl.addLayout(row_c3)
        lay.addWidget(ccard)

        # -- TRANSCRIPTION
        lay.addWidget(self._section_title("TRANSCRIPTION", container))
        tcard = Card(container, theme)
        tl = QVBoxLayout(tcard)
        tl.setContentsMargins(16, 14, 16, 14)
        tl.setSpacing(8)
        row_t1 = QHBoxLayout()
        row_t1.addWidget(self._lbl("MODEL", tcard))
        self.tr_model = QComboBox(tcard)
        self.tr_model.addItems(["tiny", "base", "small", "medium", "large-v2", "large-v3"])
        self.tr_model.setCurrentText(self.app.config.get("transcribe_model") or "tiny")
        row_t1.addWidget(self.tr_model)
        row_t1.addWidget(self._lbl("BAHASA", tcard))
        self.tr_lang = QComboBox(tcard)
        self.tr_lang.addItems(["auto", "id", "en", "ja", "ko", "ar", "zh"])
        self.tr_lang.setCurrentText(self.app.config.get("transcribe_language") or "auto")
        row_t1.addWidget(self.tr_lang)
        row_t1.addWidget(self._lbl("DEVICE", tcard))
        self.tr_device = QComboBox(tcard)
        self.tr_device.addItems(["auto", "cpu", "cuda"])
        self.tr_device.setCurrentText(self.app.config.get("transcribe_device") or "auto")
        row_t1.addWidget(self.tr_device)
        self.tr_gpu_status = QLabel("Auto = gunakan GPU hanya jika cuDNN 9 + cuBLAS CUDA 12 dan symbol yang diperlukan benar-benar tersedia; jika tidak, otomatis CPU agar aplikasi tidak force close.", tcard)
        self.tr_gpu_status.setWordWrap(True)
        self.tr_gpu_status.setStyleSheet(f"color: {C['FOOT']}; font-size:10px;")
        tl.addWidget(self.tr_gpu_status)
        gpu_btn = QPushButton("🧪 Test GPU Safe", tcard)
        gpu_btn.setObjectName("ghost")
        gpu_btn.clicked.connect(self._test_cuda_safe)
        tl.addWidget(gpu_btn)
        row_t1.addStretch()
        tl.addLayout(row_t1)
        row_t2 = QHBoxLayout()
        row_t2.addWidget(self._lbl("MODEL DIR", tcard))
        self.tr_model_dir = QLineEdit(tcard)
        self.tr_model_dir.setPlaceholderText("Kosong = cache default")
        self.tr_model_dir.setText(self.app.config.get("transcribe_model_dir") or "")
        row_t2.addWidget(self.tr_model_dir, 1)
        btn_t = QPushButton("Pilih", tcard)
        btn_t.setObjectName("ghost")
        btn_t.clicked.connect(lambda: self._pick_dir(self.tr_model_dir))
        row_t2.addWidget(btn_t)
        tl.addLayout(row_t2)
        self.tr_word = QCheckBox("Aktifkan word-level timestamps (untuk karaoke highlight)", tcard)
        self.tr_word.setChecked(bool(self.app.config.get("word_timestamps")))
        tl.addWidget(self.tr_word)
        lay.addWidget(tcard)

        # -- AI
        lay.addWidget(self._section_title("AI & MODEL CONNECTION", container))
        aicard = Card(container, theme)
        al2 = QVBoxLayout(aicard)
        al2.setContentsMargins(16, 14, 16, 14)
        al2.setSpacing(8)
        al2.addWidget(QLabel("AI global (Settings). Dipakai Content Clipper AI untuk analisis teks, viral analysis, judul/caption/deskripsi, B-roll, dan reasoning konten; tugas visual memakainya hanya bila model terverifikasi mendukung gambar, selebihnya memakai Local Vision/Tracking.", aicard))
        row_ai1 = QHBoxLayout()
        row_ai1.addWidget(self._lbl("PROVIDER", aicard))
        self.ai_provider = QComboBox(aicard)
        self.ai_provider.addItem("Tidak digunakan", "none")
        self.ai_provider.addItem("Gateway OpenAI-compatible", "gateway")
        self.ai_provider.addItem("Google AI Studio / Gemini", "google")
        try:
            from chopster.ai.config_helpers import normalize_provider
            current_provider = normalize_provider(self.app.config.get("ai_provider"))
        except Exception:
            current_provider = "gateway" if str(self.app.config.get("ai_provider") or "none") not in ("", "none", "google") else str(self.app.config.get("ai_provider") or "none")
        idx = self.ai_provider.findData(current_provider)
        self.ai_provider.setCurrentIndex(idx if idx >= 0 else 0)
        self.ai_provider.currentIndexChanged.connect(lambda _i: self._ai_provider_changed())
        row_ai1.addWidget(self.ai_provider)
        row_ai1.addStretch()
        al2.addLayout(row_ai1)

        self._endpoint_label = self._lbl("BASE URL (Gateway saja)", aicard)
        al2.addWidget(self._endpoint_label)
        self.ai_endpoint = QLineEdit(aicard)
        self.ai_endpoint.setPlaceholderText("Contoh: https://provider.example/v1")
        self.ai_endpoint.setText(self.app.config.get("ai_endpoint") or "")
        self.ai_endpoint.editingFinished.connect(self._auto_discover_ai)
        al2.addWidget(self.ai_endpoint)

        al2.addWidget(self._lbl("API KEY", aicard))
        self.ai_key = QLineEdit(aicard)
        self.ai_key.setEchoMode(QLineEdit.Password)
        self.ai_key.setPlaceholderText("API key Gateway atau Google AI Studio — disimpan terenkripsi di Windows")
        self.ai_key.setText(self.app.config.get("ai_api_key") or "")
        self.ai_key.editingFinished.connect(self._auto_discover_ai)
        al2.addWidget(self.ai_key)
        show_key = QCheckBox("Tampilkan API key", aicard)
        show_key.toggled.connect(lambda checked: self.ai_key.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password))
        al2.addWidget(show_key)

        row_actions = QHBoxLayout()
        self.ai_test_btn = QPushButton("🔌 Test Connection", aicard); self.ai_test_btn.setObjectName("primary"); self.ai_test_btn.clicked.connect(self._test_ai_connection); row_actions.addWidget(self.ai_test_btn)
        self.ai_refresh_btn = QPushButton("🔄 Refresh Models", aicard); self.ai_refresh_btn.setObjectName("ghost"); self.ai_refresh_btn.clicked.connect(self._refresh_ai_models); row_actions.addWidget(self.ai_refresh_btn)
        self.ai_infer_btn = QPushButton("🧪 Test AI", aicard); self.ai_infer_btn.setObjectName("ghost"); self.ai_infer_btn.clicked.connect(self._test_ai_inference); row_actions.addWidget(self.ai_infer_btn)
        self.ai_vision_btn = QPushButton("🖼 Test Vision", aicard); self.ai_vision_btn.setObjectName("ghost"); self.ai_vision_btn.clicked.connect(self._test_ai_vision); row_actions.addWidget(self.ai_vision_btn)
        row_actions.addStretch()
        al2.addLayout(row_actions)
        self.ai_status = QLabel("Belum terhubung", aicard); self.ai_status.setObjectName("muted"); al2.addWidget(self.ai_status)
        self.ai_provider_hint = QLabel("Pastikan model AI mendukung teks dan gambar agar semua fitur AI bekerja maksimal.", aicard)
        self.ai_provider_hint.setWordWrap(True); self.ai_provider_hint.setStyleSheet(f"color:{C['GOLD']};font-size:10px;font-weight:700;"); al2.addWidget(self.ai_provider_hint)
        self.ai_local_status = QLabel("🧠 Local Vision/Tracking: READY • Face/Body/Composition • selalu tersedia tanpa API", aicard)
        self.ai_local_status.setWordWrap(True); self.ai_local_status.setStyleSheet(f"color:{C['GREEN']};font-size:10px;font-weight:700;"); al2.addWidget(self.ai_local_status)
        self.ai_embedded_status = QLabel("✨ Visual AI global: memakai AI di Settings bila model terverifikasi Teks+gambar; jika tidak, memakai Local Vision/Tracking.", aicard)
        self.ai_embedded_status.setWordWrap(True); self.ai_embedded_status.setStyleSheet(f"color:{C['BLUE']};font-size:10px;font-weight:800;"); al2.addWidget(self.ai_embedded_status)
        self.ai_embedded_test_btn = QPushButton("🧠 Test AI Vision (global)", aicard); self.ai_embedded_test_btn.setObjectName("ghost"); self.ai_embedded_test_btn.clicked.connect(self._test_embedded_gemini_vision); al2.addWidget(self.ai_embedded_test_btn)
        self._ai_provider_changed()
        self._refresh_ai_visual_status()

        al2.addWidget(self._lbl("MODEL DARI PROVIDER", aicard))
        manual_row = QHBoxLayout()
        manual_row.addWidget(self._lbl("MODEL ID MANUAL", aicard))
        self.ai_manual_model = QLineEdit(aicard)
        self.ai_manual_model.setPlaceholderText("Opsional — dipakai jika /models sedang gagal")
        self.ai_manual_model.setText(str(self.app.config.get("ai_model") or ""))
        manual_row.addWidget(self.ai_manual_model, 1)
        al2.addLayout(manual_row)

        vision_row = QHBoxLayout()
        vision_row.addWidget(self._lbl("VISION MODEL ID", aicard))
        self.ai_vision_model = QLineEdit(aicard)
        self.ai_vision_model.setPlaceholderText("Kosong = Auto berdasarkan capability provider")
        self.ai_vision_model.setText(str(self.app.config.get("ai_vision_model") or ""))
        vision_row.addWidget(self.ai_vision_model, 1)
        al2.addLayout(vision_row)
        self.ai_models_list = QListWidget(aicard)
        self.ai_models_list.setMinimumHeight(130)
        al2.addWidget(self.ai_models_list)
        row_add = QHBoxLayout()
        add_model = QPushButton("＋ Tambahkan Model Terpilih", aicard); add_model.setObjectName("ghost"); add_model.clicked.connect(self._add_selected_ai_model); row_add.addWidget(add_model)
        use_model = QPushButton("▶ Gunakan Model Terpilih", aicard); use_model.setObjectName("primary"); use_model.clicked.connect(self._use_selected_ai_model); row_add.addWidget(use_model)
        row_add.addStretch(); al2.addLayout(row_add)

        al2.addWidget(self._lbl("AI YANG DISIMPAN", aicard))
        self.ai_saved_list = QListWidget(aicard); self.ai_saved_list.setMinimumHeight(90); al2.addWidget(self.ai_saved_list)
        row_saved = QHBoxLayout()
        use_saved = QPushButton("Gunakan", aicard); use_saved.clicked.connect(self._use_saved_ai_model); row_saved.addWidget(use_saved)
        del_saved = QPushButton("Hapus", aicard); del_saved.setObjectName("danger"); del_saved.clicked.connect(self._delete_saved_ai_model); row_saved.addWidget(del_saved)
        row_saved.addStretch(); al2.addLayout(row_saved)

        self.ai_auto_model = QCheckBox("Auto pilih model untuk tiap tugas (mengikuti capability)", aicard)
        self.ai_auto_model.setChecked(bool(self.app.config.get("ai_auto_model", True))); al2.addWidget(self.ai_auto_model)
        self.ai_auto_visual = QCheckBox("Auto pilih model Vision untuk membaca frame video saat analisis visual", aicard)
        self.ai_auto_visual.setChecked(bool(self.app.config.get("ai_auto_visual", True))); al2.addWidget(self.ai_auto_visual)
        self.ai_failover_local = QCheckBox("AUTO FALLBACK: jika AI API gagal/crash, lanjutkan dengan Local Engine", aicard)
        self.ai_failover_local.setChecked(bool(self.app.config.get("ai_failover_local", True)))
        self.ai_failover_local.setToolTip("AI online hanya enhancement. Saat timeout/error/rate-limit, Chopster otomatis berpindah ke analisis lokal dan tidak mengulang pekerjaan yang sudah berhasil.")
        al2.addWidget(self.ai_failover_local)
        fail_info = QLabel("Local fallback: Viral/Highlight → heuristic transcript · Camera → local tracking/lock · Content → local generator · Export/FFmpeg tetap lokal", aicard)
        fail_info.setWordWrap(True); fail_info.setStyleSheet(f"color: {C['FOOT']}; font-size:10px;"); al2.addWidget(fail_info)
        row_ai2 = QHBoxLayout()
        row_ai2.addWidget(self._lbl("TIMEOUT (detik)", aicard))
        self.ai_timeout = QSpinBox(aicard); self.ai_timeout.setRange(10, 600); self.ai_timeout.setValue(int(self.app.config.get("ai_timeout") or 60)); row_ai2.addWidget(self.ai_timeout); row_ai2.addStretch(); al2.addLayout(row_ai2)
        warn = QLabel("AI global: Gateway OpenAI-compatible dan Google AI Studio / Gemini adalah dua adapter berbeda. Tugas teks memakai provider yang dipilih; tugas visual hanya dikirim ke provider bila model terverifikasi mendukung gambar, selain itu memakai Local Vision/Tracking. Tanpa API, semua fitur tetap berjalan via local fallback tanpa force close. Key tidak pernah dibundel ke EXE; di Windows key disimpan terenkripsi dengan DPAPI, terpisah dari settings.json.", aicard)
        warn.setWordWrap(True); warn.setStyleSheet(f"color: {C['FOOT']}; font-size:10px;"); al2.addWidget(warn)
        lay.addWidget(aicard)
        self.ai_models_cache=self.app.config.get("ai_models_cache", []) or []
        self._render_ai_model_lists()
        # -- STORAGE
        lay.addWidget(self._section_title("STORAGE", container))
        scard = Card(container, theme)
        sl = QVBoxLayout(scard)
        sl.setContentsMargins(16, 14, 16, 14)
        sl.setSpacing(8)
        for key, label, placeholder in [
            ("project_dir", "PROJECT DIR", "Kosong = AppData/ChopsterByAris/projects"),
            ("cache_dir", "CACHE DIR", "Kosong = default"),
            ("temp_dir", "TEMP DIR", "Kosong = system temp"),
        ]:
            sl.addWidget(self._lbl(label, scard))
            row_s = QHBoxLayout()
            le = QLineEdit(scard)
            le.setPlaceholderText(placeholder)
            le.setText(self.app.config.get(key) or "")
            setattr(self, f"storage_{key}", le)
            row_s.addWidget(le, 1)
            btn_s = QPushButton("Pilih", scard)
            btn_s.setObjectName("ghost")
            btn_s.clicked.connect(lambda _=False, w=le: self._pick_dir(w))
            row_s.addWidget(btn_s)
            sl.addLayout(row_s)
        # disk info
        self.disk_label = QLabel("", scard)
        self.disk_label.setStyleSheet(f"color: {C['FOOT']}; font-size:10px;")
        sl.addWidget(self.disk_label)
        self._refresh_disk()
        clear_btn = QPushButton("Bersihkan cache (hapus file sementara)", scard)
        clear_btn.setObjectName("ghost")
        clear_btn.clicked.connect(self._clear_cache)
        sl.addWidget(clear_btn)
        lay.addWidget(scard)

        # Save
        save = QPushButton("💾  Simpan Pengaturan", container)
        save.setObjectName("primary")
        save.clicked.connect(self._save)
        lay.addWidget(save)

        hint = QLabel("Ekstensi browser: hapus/nonaktifkan ekstensi Chopster versi lama, lalu buka chrome://extensions → Mode pengembang → Muat tidak terpakai → pilih folder browser_extension dari folder Chopster build ini. Muat ulang ekstensi setelah mengganti build.\nPastikan popup menampilkan “Chopster terhubung” saat aplikasi terbaru berjalan.", container)
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {C['MUTED']}; font-size:10px;")
        lay.addWidget(hint)
        lay.addStretch()
        self._bind_download_settings_dirty()

    def _bind_download_settings_dirty(self) -> None:
        """Track unsaved download-only edits so page refreshes don't discard them."""
        bindings = (
            (self.quality, "currentTextChanged", "quality"),
            (self.format_cb, "currentTextChanged", "output_format"),
            (self.audio_language, "currentTextChanged", "audio_language"),
            (self.h264, "toggled", "h264"),
            (self.tmpl, "currentTextChanged", "filename_template"),
            (self.speed, "currentTextChanged", "speed_limit"),
            (self.parallel, "valueChanged", "parallel"),
            (self.cb_subfolders, "toggled", "subfolders"),
            (self.cb_subtitles, "toggled", "subtitles"),
            (self.cb_thumbnail, "toggled", "thumbnail"),
            (self.cb_ask_duplicates, "toggled", "ask_duplicates"),
            (self.cb_download_playlist, "toggled", "download_playlist"),
            (self.browser_cookie, "currentTextChanged", "browser_cookie_source"),
            (self.out_dir, "textEdited", "out_dir"),
            (self.playlist_items, "textEdited", "playlist_items"),
            (self.cookies, "textEdited", "cookies_file"),
            (self.proxy, "textEdited", "proxy"),
        )
        for widget, signal_name, key in bindings:
            getattr(widget, signal_name).connect(
                lambda *_args, setting_key=key: self._mark_download_settings_dirty(setting_key)
            )

    def _mark_download_settings_dirty(self, key: str) -> None:
        self._download_settings_dirty_keys.add(key)
        self._download_settings_dirty = True

    def refresh_download_settings(self) -> None:
        """Refresh untouched download controls without discarding unsaved edits."""
        config = self.app.config
        values = (
            ("out_dir", self.out_dir, config.get("out_dir") or ""),
            ("quality", self.quality, config.get("quality") or "Terbaik yang tersedia"),
            ("output_format", self.format_cb, config.get("output_format") or "MP4 (Video + Audio)"),
            ("audio_language", self.audio_language, config.get("audio_language") or "Auto — Indonesia jika tersedia"),
            ("filename_template", self.tmpl, config.get("filename_template") or "[Platform] Judul"),
            ("speed_limit", self.speed, config.get("speed_limit") or "Tidak terbatas"),
            ("playlist_items", self.playlist_items, config.get("playlist_items") or ""),
            ("cookies_file", self.cookies, config.get("cookies_file") or ""),
            ("proxy", self.proxy, config.get("proxy") or ""),
            ("browser_cookie_source", self.browser_cookie, config.get("browser_cookie_source") or "Auto"),
        )
        for key, widget, value in values:
            if key in self._download_settings_dirty_keys:
                continue
            was_blocked = widget.blockSignals(True)
            if isinstance(widget, QLineEdit):
                widget.setText(str(value))
            else:
                widget.setCurrentText(str(value))
            widget.blockSignals(was_blocked)
        checkbox_widgets = {
            "subfolders": self.cb_subfolders,
            "subtitles": self.cb_subtitles,
            "thumbnail": self.cb_thumbnail,
            "ask_duplicates": self.cb_ask_duplicates,
            "download_playlist": self.cb_download_playlist,
            "h264": self.h264,
        }
        for key, widget in checkbox_widgets.items():
            if key not in self._download_settings_dirty_keys:
                widget.setChecked(bool(config.get(key, True) if key == "h264" else config.get(key)))
        if "parallel" not in self._download_settings_dirty_keys:
            self.parallel.setValue(int(config.get("parallel") or 2))

    def _lbl(self, t, parent):
        from PySide6.QtWidgets import QLabel
        from chopster.ui.theme import THEMES
        C = THEMES.get(self._theme, THEMES["dark"])
        lbl = QLabel(t, parent)
        lbl.setStyleSheet(f"color: {C['MUTED']}; font-weight:700; font-size:10px;")
        return lbl

    def _section_title(self, t, parent):
        lbl = QLabel(t, parent)
        C = THEMES.get(self._theme, THEMES["dark"])
        lbl.setStyleSheet(f"color: {C['GOLD']}; font-weight:800; font-size:11px; letter-spacing:1px;")
        return lbl

    def _test_cuda_safe(self):
        from chopster.clipper.transcript_manager import _cuda_preflight
        ok, detail = _cuda_preflight()
        if ok:
            self.tr_gpu_status.setText(f"✅ CUDA/cuDNN aman: {detail}")
        else:
            self.tr_gpu_status.setText(f"⚠️ GPU tidak aman: {detail}\nChopster akan memakai CPU agar tetap stabil.")


    def _pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Pilih folder", self.out_dir.text() or str(Path.home()))
        if d:
            self.out_dir.setText(d)

    def _pick_dir(self, widget):
        d = QFileDialog.getExistingDirectory(self, "Pilih folder", widget.text() or str(Path.home()))
        if d:
            widget.setText(d)

    def _pick_cookies(self):
        p, _ = QFileDialog.getOpenFileName(self, "Pilih cookies", "", "Cookies (*.txt *.cookies);;All (*.*)")
        if p:
            self.cookies.setText(p)

    def _refresh_disk(self):
        try:
            out = self.app.config.get("out_dir") or str(Path.home() / "Downloads")
            free = shutil.disk_usage(out).free
            total = shutil.disk_usage(out).total
            self.disk_label.setText(f"Disk {out}: {free/(1024**3):.1f} GB free / {total/(1024**3):.1f} GB total")
        except Exception:
            self.disk_label.setText("Info disk tidak tersedia")

    def _clear_cache(self):
        from chopster.app.paths import user_data_dir
        cache = user_data_dir() / "cache"
        if not cache.exists():
            QMessageBox.information(self, "Cache", "Cache sudah kosong.")
            return
        if QMessageBox.question(self, "Bersihkan cache", f"Hapus isi {cache} ?") != QMessageBox.Yes:
            return
        import shutil as _sh
        try:
            for child in cache.iterdir():
                if child.is_file():
                    child.unlink(missing_ok=True)
                elif child.is_dir():
                    _sh.rmtree(child, ignore_errors=True)
            QMessageBox.information(self, "Cache", "Cache dibersihkan.")
        except Exception as exc:
            QMessageBox.warning(self, "Cache", str(exc))

    def _ai_provider_changed(self):
        """Show/hide fields based on explicit provider choice."""
        provider = self._ai_provider_value()
        try:
            if hasattr(self, "ai_endpoint"):
                self.ai_endpoint.setVisible(provider == "gateway")
                self.ai_endpoint.setEnabled(provider == "gateway")
            if hasattr(self, "_endpoint_label"):
                self._endpoint_label.setVisible(provider == "gateway")
        except Exception:
            pass
        self._refresh_ai_visual_status()

    def _ai_provider_value(self) -> str:
        value = self.ai_provider.currentData()
        return str(value or self.ai_provider.currentText() or "none")

    def _refresh_ai_visual_status(self):
        try:
            provider = self._ai_provider_value()
            if provider == "none":
                text = "✨ Visual AI global: tidak digunakan. Tugas visual memakai Local Vision/Tracking."
                color = self._theme_gold_hex()
            else:
                verified = self._selected_model_vision_status()
                if verified == "vision":
                    text = "✨ Visual AI global: model terverifikasi Teks + gambar — tugas visual dapat memakai AI ini."
                elif provider == "google":
                    text = "✨ Visual AI global: model Google belum diverifikasi gambar. Klik Test AI Vision untuk verifikasi; tanpa verifikasi visual memakai Local Vision/Tracking."
                else:
                    text = "✨ Visual AI global: model belum terverifikasi mendukung gambar. Tugas visual memakai Local Vision/Tracking sampai Test Vision berhasil."
                color = self._theme_green_hex() if verified == "vision" else self._theme_gold_hex()
            if hasattr(self, "ai_embedded_status"):
                self.ai_embedded_status.setText(text)
                self.ai_embedded_status.setStyleSheet(f"color:{color};font-size:10px;font-weight:800;")
        except Exception:
            pass

    def _theme_gold_hex(self):
        from chopster.ui.theme import THEMES
        return THEMES.get(self._theme, THEMES["dark"]).get("GOLD", "#E3B653")

    def _theme_green_hex(self):
        from chopster.ui.theme import THEMES
        return THEMES.get(self._theme, THEMES["dark"]).get("GREEN", "#65CDA8")

    def _selected_model_vision_status(self) -> str:
        """Vision capability of the currently selected model, from verified metadata only."""
        try:
            from chopster.ai.config_helpers import explicit_vision_status, model_matches_scope
            model_id = self._selected_model_id()
            provider = self._ai_provider_value()
            endpoint = self.ai_endpoint.text().strip() if provider == "gateway" else ""
            for row in (self.app.config.get("ai_models_cache") or []):
                if str(row.get("id") or "") == model_id and model_matches_scope(row, provider, endpoint):
                    return explicit_vision_status(row)
        except Exception:
            pass
        return "unknown"

    def _selected_model_id(self) -> str:
        model = (self.ai_manual_model.text().strip() if hasattr(self, "ai_manual_model") and self.ai_manual_model.text().strip() else str(self.app.config.get("ai_model") or ""))
        return model or ""

    def _build_provider(self, endpoint: str, key: str, timeout: int, manual_model: str = "", vision_model: str = ""):
        provider = self._ai_provider_value()
        manual_model = manual_model or str(self.app.config.get("ai_model") or "")
        vision_model = vision_model or str(self.app.config.get("ai_vision_model") or "")
        if provider == "google":
            from chopster.ai.google_gemini import GoogleGeminiProvider
            return GoogleGeminiProvider(api_key=key, model=manual_model, timeout=timeout)
        from chopster.ai.api_provider import APIProvider
        return APIProvider(endpoint, key, manual_model, timeout, vision_model)

    def _provider_from_fields(self):
        return self._build_provider(self.ai_endpoint.text().strip(), self.ai_key.text().strip(), int(self.ai_timeout.value()))

    def _ai_fields_configured(self) -> bool:
        provider = self._ai_provider_value()
        key = self.ai_key.text().strip()
        if provider == "google":
            return bool(key)
        return bool(key and self.ai_endpoint.text().strip())

    def _auto_discover_ai(self):
        provider = self._ai_provider_value()
        endpoint = self.ai_endpoint.text().strip(); key = self.ai_key.text().strip()
        if provider == "google":
            if not key:
                return
            sig = (provider, key)
        else:
            if not endpoint or not key:
                return
            sig = (provider, endpoint, key)
        if getattr(self, "_last_ai_signature", None) == sig and getattr(self, "ai_models_cache", None):
            return
        self._last_ai_signature = sig
        self._test_ai_connection(auto=True)

    def _test_ai_connection(self, auto: bool = False):
        provider=self._ai_provider_value()
        endpoint=self.ai_endpoint.text().strip(); key=self.ai_key.text().strip(); timeout=int(self.ai_timeout.value())
        if provider == "google":
            if not key:
                QMessageBox.information(self, "AI Connection", "Isi API Key Google AI Studio / Gemini terlebih dahulu."); return
        else:
            if not endpoint or not key:
                QMessageBox.information(self, "AI Connection", "Isi Base URL dan API Key terlebih dahulu."); return
        saved = self.app.config.update({"ai_provider": provider, "ai_endpoint": endpoint, "ai_api_key": key, "ai_timeout": timeout})
        if not saved:
            QMessageBox.warning(self, "Penyimpanan aman gagal", "Key tidak dapat disimpan ke penyimpanan aman Windows. Key tidak dikirim ke pengaturan teks biasa.")
            return
        self.ai_status.setText("⏳ Menguji koneksi & daftar model…")
        self.ai_test_btn.setEnabled(False); self.ai_refresh_btn.setEnabled(False)
        manual_model=str(self.ai_manual_model.text().strip()) if hasattr(self,"ai_manual_model") else str(self.app.config.get("ai_model") or "")
        if manual_model:
            self.app.config.set("ai_model", manual_model)
        vision_model=str(self.ai_vision_model.text().strip()) if hasattr(self,"ai_vision_model") else str(self.app.config.get("ai_vision_model") or "")
        def worker(*,signals,cancel_check):
            return self._build_provider(endpoint,key,timeout,manual_model,vision_model).connection_probe(manual_model)
        def done(result):
            models=list(result.get("models") or [])
            if models:
                self.ai_models_cache=models; self.app.config.set("ai_models_cache", models)
            self._render_ai_model_lists(); self._refresh_ai_visual_status()
            if result.get("ok"):
                vision_count=self._verified_vision_model_count(models)
                via="model list" if result.get("model_route_ok") else "direct inference"
                self.ai_status.setText(f"✅ AI ONLINE • {len(models) if models else 'cached/manual'} model • Teks+gambar terverifikasi: {vision_count} • via {via} • Local fallback: {'ON' if self.app.config.get('ai_failover_local', True) else 'OFF'}")
                self.app.config.set("ai_last_connection", "connected")
            else:
                err=str(result.get("message") or result.get("model_error") or "Remote AI tidak tersedia")
                self.ai_status.setText(f"⚠️ AI REMOTE OFFLINE • {err[:260]} • LOCAL FALLBACK SIAP")
                self.app.config.set("ai_last_connection", "degraded_remote")
            try:self.app.config.save()
            except Exception:pass
            self.ai_test_btn.setEnabled(True); self.ai_refresh_btn.setEnabled(True); self.ai_settings_changed.emit()
        def failed(tid,error):
            self.ai_status.setText(f"⚠️ AI REMOTE OFFLINE: {error[:220]} • LOCAL FALLBACK SIAP")
            self.app.config.set("ai_last_connection", "degraded_remote")
            self.ai_test_btn.setEnabled(True); self.ai_refresh_btn.setEnabled(True)
        self._ai_task_callbacks=getattr(self,"_ai_task_callbacks",{})
        self._ai_task_callbacks["settings:ai_connect"]=done
        if not hasattr(self,"_ai_task_hooked"):
            self._ai_task_hooked=True
            self.app.tasks.task_result.connect(self._on_ai_task_result)
            self.app.tasks.task_failed.connect(self._on_ai_task_failed)
        self.app.tasks.submit("settings:ai_connect","AI Connection",worker)

    def _verified_vision_model_count(self, models) -> int:
        try:
            from chopster.ai.config_helpers import explicit_vision_status, model_matches_scope
            provider = self._ai_provider_value()
            endpoint = self.ai_endpoint.text().strip() if provider == "gateway" else ""
            return sum(1 for m in models if model_matches_scope(m, provider, endpoint) and explicit_vision_status(m) == "vision")
        except Exception:
            return 0

    def _refresh_ai_models(self):
        self._test_ai_connection()

    def _selected_ai_probe_model(self, preferred: str = "") -> str:
        """Choose a single explicit model for the user-triggered Vision probe."""
        if preferred:
            return str(preferred).strip()
        provider = self._ai_provider_value()
        endpoint = self.ai_endpoint.text().strip() if provider == "gateway" else ""
        try:
            from chopster.ai.config_helpers import model_matches_scope
            for widget_name in ("ai_models_list", "ai_saved_list"):
                widget = getattr(self, widget_name, None)
                item = widget.currentItem() if widget is not None else None
                model = item.data(32) if item is not None else None
                model_id = str((model or {}).get("id") or (model or {}).get("model") or "").strip()
                if model_id and model_matches_scope(model, provider, endpoint):
                    return model_id
            for row in (self.app.config.get("ai_models_cache") or []):
                if model_matches_scope(row, provider, endpoint):
                    model_id = str(row.get("id") or row.get("model") or "").strip()
                    if model_id:
                        return model_id
        except Exception:
            pass
        return ""

    def _test_ai_vision(self):
        provider=self._ai_provider_value()
        endpoint=self.ai_endpoint.text().strip(); key=self.ai_key.text().strip(); timeout=int(self.ai_timeout.value())
        if not self._ai_fields_configured():
            QMessageBox.information(self,"Test Vision","Hubungkan AI terlebih dahulu (isi API key; untuk Gateway juga Base URL)."); return
        self.ai_status.setText("⏳ Menguji kemampuan gambar AI dengan frame uji kecil… (aman, satu request kecil)")
        self.ai_vision_btn.setEnabled(False)
        manual_model=str(self.ai_manual_model.text().strip()) if hasattr(self,"ai_manual_model") and self.ai_manual_model.text().strip() else str(self.app.config.get("ai_model") or "")
        vision_model=str(self.ai_vision_model.text().strip()) if hasattr(self,"ai_vision_model") and self.ai_vision_model.text().strip() else (manual_model or "")
        probe_model = self._selected_ai_probe_model(vision_model)
        def worker(*,signals,cancel_check):
            import tempfile
            from pathlib import Path
            try:
                from PIL import Image, ImageDraw
            except Exception as exc:
                raise RuntimeError(f"Pillow tidak tersedia: {exc}")
            img=Path(tempfile.gettempdir())/"chopster_vision_probe.png"
            im=Image.new("RGB",(96,96),(238,238,238))
            d=ImageDraw.Draw(im); d.rectangle((8,8,88,88),outline=(20,20,20),width=3); d.text((18,40),"CHOPSTER",fill=(10,10,10))
            im.save(img)
            p=self._build_provider(endpoint,key,timeout,manual_model,vision_model)
            probe_options = {"allow_unverified": True} if provider == "gateway" else {}
            resp=p.generate_vision(
                'Balas JSON saja: {"ok":true,"description":"jelaskan gambar secara singkat"}.',
                [str(img)],
                system="Uji kemampuan vision.",
                model=probe_model,
                temperature=0,
                max_tokens=256,
                timeout=timeout,
                **probe_options,
            )
            raw=resp.raw if isinstance(resp.raw,dict) else {}
            return {"text":resp.text,"provider":raw.get("provider") or "remote","model":str(raw.get("model") or probe_model or "auto")}
        def done(res):
            used_model=str(res.get("model") or "")
            verified = not str(res.get("provider") or "").startswith("local")
            self._record_vision_test(used_model, verified, provider=provider, endpoint=endpoint)
            if verified:
                self.ai_status.setText(f"✅ Model terverifikasi Teks + gambar • model: {used_model or 'Auto'}. Tugas visual kini dapat memakai AI ini.")
            else:
                self.ai_status.setText("⚠️ Vision remote belum berhasil diverifikasi. Tugas visual memakai Local Vision/Tracking.")
            self._refresh_ai_visual_status()
            self.ai_vision_btn.setEnabled(True)
        self._ai_task_callbacks=getattr(self,"_ai_task_callbacks",{})
        self._ai_task_callbacks["settings:ai_vision"]=done
        if not hasattr(self,"_ai_task_hooked"):
            self._ai_task_hooked=True
            self.app.tasks.task_result.connect(self._on_ai_task_result)
            self.app.tasks.task_failed.connect(self._on_ai_task_failed)
        self.app.tasks.submit("settings:ai_vision","AI Vision Test",worker)

    def _record_vision_test(self, model_id: str, verified: bool, *, provider: str | None = None, endpoint: str | None = None):
        """Persist a safe vision result in the matching provider/endpoint scope."""
        model_id = str(model_id or "").strip()
        if not model_id:
            return
        provider = provider or self._ai_provider_value()
        if endpoint is None:
            endpoint = self.ai_endpoint.text().strip() if provider == "gateway" else ""
        from chopster.ai.config_helpers import model_scope
        scope = model_scope(provider, endpoint)
        cache = list(self.app.config.get("ai_models_cache") or [])
        matching = None
        for row in cache:
            if (str(row.get("id") or "") == model_id
                    and row.get("_chopster_scope") == scope):
                matching = row
                break
        if matching is None:
            matching = {"id": model_id, "name": model_id, "_chopster_scope": scope}
            cache.append(matching)
        matching["vision_checked"] = True
        matching["vision_verified"] = bool(verified)
        if verified:
            matching["capabilities"] = sorted(set(matching.get("capabilities") or []) | {"vision"})
        self.app.config.set("ai_models_cache", cache)
        self.ai_models_cache = cache

    def _test_embedded_gemini_vision(self):
        self.ai_embedded_test_btn.setEnabled(False)
        self.ai_status.setText("⏳ Menguji AI Vision (global) dengan frame uji kecil…")
        def worker(*,signals,cancel_check):
            import tempfile
            from pathlib import Path
            from PIL import Image, ImageDraw
            provider=self._ai_provider_value()
            endpoint=self.ai_endpoint.text().strip(); key=self.ai_key.text().strip(); timeout=int(self.ai_timeout.value())
            if not self._ai_fields_configured():
                raise RuntimeError("AI belum dikonfigurasi (set provider, isi API key; untuk Gateway juga Base URL). Visual akan memakai Local Vision/Tracking.")
            manual_model=str(self.ai_manual_model.text().strip()) if hasattr(self,"ai_manual_model") and self.ai_manual_model.text().strip() else str(self.app.config.get("ai_model") or "")
            vision_model=str(self.ai_vision_model.text().strip()) if hasattr(self,"ai_vision_model") and self.ai_vision_model.text().strip() else (manual_model or "")
            vision_model = self._selected_ai_probe_model(vision_model)
            img=Path(tempfile.gettempdir())/"chopster_embedded_gemini_probe.png"
            im=Image.new("RGB",(320,200),(38,44,58)); d=ImageDraw.Draw(im); d.rectangle((70,45,250,155),outline=(255,190,34),width=4); d.text((85,85),"CHOPSTER",fill=(255,255,255)); im.save(img)
            p=self._build_provider(endpoint,key,timeout,manual_model,vision_model)
            probe_options = {"allow_unverified": True} if provider == "gateway" else {}
            r=p.generate_vision("Jelaskan singkat isi frame ini dan apakah komposisinya aman untuk video portrait. Balas maksimal 3 kalimat.",[str(img)],model=vision_model or manual_model,max_tokens=600,timeout=min(60,timeout),**probe_options)
            raw=r.raw if isinstance(r.raw,dict) else {}
            return {"model":str(raw.get("model") or vision_model or "auto"),"text":r.text,"provider":str(raw.get("provider") or p.name), "verified": True}
        def done(result):
            used_model=str(result.get("model") or "")
            self._record_vision_test(used_model, bool(result.get("verified")), provider=provider, endpoint=endpoint)
            self.ai_status.setText(f"✅ AI Vision (global) aktif • model: {used_model} • Teks + gambar terverifikasi")
            self.ai_embedded_test_btn.setEnabled(True)
            self._refresh_ai_visual_status()
        def fail(tid,error):
            self.ai_status.setText(f"⚠️ AI Vision (global) belum tersedia: {str(error)[:260]} • Local Vision/Tracking tetap siap")
            self.ai_embedded_test_btn.setEnabled(True)
        self._ai_task_callbacks=getattr(self,"_ai_task_callbacks",{})
        self._ai_task_callbacks["settings:embedded_gemini_vision"]=done
        self._ai_task_callbacks_fail=getattr(self,"_ai_task_callbacks_fail",{})
        self._ai_task_callbacks_fail["settings:embedded_gemini_vision"]=fail
        if not hasattr(self,"_ai_task_hooked"):
            self._ai_task_hooked=True
            self.app.tasks.task_result.connect(self._on_ai_task_result)
            self.app.tasks.task_failed.connect(self._on_ai_task_failed)
        self.app.tasks.submit("settings:embedded_gemini_vision","AI Vision Test (global)",worker)

    def _test_ai_inference(self):
        endpoint=self.ai_endpoint.text().strip(); key=self.ai_key.text().strip(); model=(str(self.ai_manual_model.text().strip()) if hasattr(self,"ai_manual_model") and self.ai_manual_model.text().strip() else str(self.app.config.get("ai_model") or "")); timeout=int(self.ai_timeout.value())
        if not self._ai_fields_configured():
            QMessageBox.information(self,"Test AI","Hubungkan AI terlebih dahulu."); return
        def worker(*,signals,cancel_check):
            p=self._build_provider(endpoint,key,timeout,model)
            return p.test_inference(model or None)
        def done(resp): self.ai_status.setText(f"✅ AI merespons • {resp.text[:80] or 'OK'}")
        self._ai_task_callbacks=getattr(self,"_ai_task_callbacks",{})
        self._ai_task_callbacks["settings:ai_infer"]=done
        if not hasattr(self,"_ai_task_hooked"):
            self._ai_task_hooked=True; self.app.tasks.task_result.connect(self._on_ai_task_result); self.app.tasks.task_failed.connect(self._on_ai_task_failed)
        self.app.tasks.submit("settings:ai_infer","AI Inference Test",worker)

    def _on_ai_task_result(self,tid,result):
        cb=getattr(self,"_ai_task_callbacks",{}).pop(tid,None)
        if cb: cb(result)

    def _on_ai_task_failed(self,tid,error):
        self._ai_task_callbacks=getattr(self,"_ai_task_callbacks",{})
        self._ai_task_callbacks_fail=getattr(self,"_ai_task_callbacks_fail",{})
        cb_fail=self._ai_task_callbacks_fail.pop(tid,None)
        if cb_fail:
            self._ai_task_callbacks.pop(tid,None)
            cb_fail(tid,error)
            return
        if tid in self._ai_task_callbacks: self._ai_task_callbacks.pop(tid,None)
        if tid == "settings:ai_connect":
            self.app.config.set("ai_last_connection", "degraded_remote")
            # Keep the last known-good model cache so the app can still resume with
            # a manually selected model when the provider comes back.
            self.ai_models_cache=self.app.config.get("ai_models_cache", []) or getattr(self,"ai_models_cache", []) or []
            self._render_ai_model_lists()
        if tid.startswith("settings:ai_"):
            self.ai_status.setText(f"⚠️ Remote AI tidak tersedia: {error[:220]} • LOCAL FALLBACK SIAP")
            self.ai_test_btn.setEnabled(True); self.ai_refresh_btn.setEnabled(True)
            if tid == "settings:ai_vision" and hasattr(self, "ai_vision_btn"):
                self.ai_vision_btn.setEnabled(True)
            if tid == "settings:ai_infer" and hasattr(self, "ai_infer_btn"):
                self.ai_infer_btn.setEnabled(True)

    def _render_ai_model_lists(self):
        models=getattr(self,"ai_models_cache", None) or self.app.config.get("ai_models_cache", []) or []
        provider=self._ai_provider_value() if hasattr(self,"ai_provider") else "none"
        from chopster.ai.config_helpers import model_picker_label, model_matches_scope
        endpoint = self.ai_endpoint.text().strip() if provider == "gateway" and hasattr(self, "ai_endpoint") else ""
        self.ai_models_list.clear()
        for m in models:
            if not model_matches_scope(m, provider, endpoint):
                continue
            label=model_picker_label(provider, m, endpoint=endpoint)
            it=QListWidgetItem(f"{m.get('id')}  •  {label}")
            it.setData(32,m); self.ai_models_list.addItem(it)
        self.ai_saved_list.clear()
        profiles=self.app.config.get("ai_profiles", []) or []
        active=self.app.config.get("ai_model") or ""
        for m in profiles:
            mid=str(m.get("id") or m.get("model") or "")
            label=model_picker_label(provider, m, endpoint=endpoint)
            mark="✅ AKTIF" if mid==active else ""
            it=QListWidgetItem(f"{mid}  •  {label}  {mark}")
            it.setData(32,m); self.ai_saved_list.addItem(it)
        self._refresh_ai_visual_status()

    def _add_selected_ai_model(self):
        item=self.ai_models_list.currentItem()
        if not item: return
        model=item.data(32) or {}
        profiles=list(self.app.config.get("ai_profiles", []) or [])
        mid=str(model.get("id") or "")
        if not mid: return
        if not any(str(x.get("id") or x.get("model") or "") == mid for x in profiles): profiles.append(model)
        self.app.config.set("ai_profiles",profiles)
        self._render_ai_model_lists(); self.ai_status.setText(f"✅ {mid} ditambahkan ke AI tersimpan")

    def _use_selected_ai_model(self):
        item=self.ai_models_list.currentItem()
        if not item: return
        self._activate_ai_model(item.data(32) or {})

    def _use_saved_ai_model(self):
        item=self.ai_saved_list.currentItem()
        if item: self._activate_ai_model(item.data(32) or {})

    def _activate_ai_model(self,model):
        mid=str(model.get("id") or model.get("model") or "").strip()
        if not mid:return
        self.app.config.set("ai_model",mid)
        profiles=list(self.app.config.get("ai_profiles",[]) or [])
        if not any(str(x.get("id") or x.get("model") or "") == mid for x in profiles):
            profiles.append(model); self.app.config.set("ai_profiles",profiles)
        self._render_ai_model_lists(); self.ai_status.setText(f"✅ Model aktif: {mid}"); self._refresh_ai_visual_status(); self.ai_settings_changed.emit()

    def _delete_saved_ai_model(self):
        item=self.ai_saved_list.currentItem()
        if not item:return
        mid=str((item.data(32) or {}).get("id") or "")
        profiles=[x for x in (self.app.config.get("ai_profiles",[]) or []) if str(x.get("id") or x.get("model") or "") != mid]
        self.app.config.set("ai_profiles",profiles)
        if self.app.config.get("ai_model") == mid:self.app.config.set("ai_model","")
        self._render_ai_model_lists(); self.ai_settings_changed.emit()

    def _save(self):
        if hasattr(self,"ai_manual_model") and self.ai_manual_model.text().strip():
            self.app.config.set("ai_model", self.ai_manual_model.text().strip())
        manual_endpoint = self.ai_endpoint.text().strip()
        manual_key = self.ai_key.text().strip()
        manual_provider = self._ai_provider_value()
        saved = self.app.config.update({
            "out_dir": self.out_dir.text().strip(),
            "quality": self.quality.currentText(),
            "output_format": self.format_cb.currentText(),
            "audio_language": self.audio_language.currentText(),
            "h264": self.h264.isChecked(),
            "filename_template": self.tmpl.currentText(),
            "speed_limit": self.speed.currentText(),
            "parallel": int(self.parallel.value()),
            "subfolders": self.cb_subfolders.isChecked(),
            "subtitles": self.cb_subtitles.isChecked(),
            "thumbnail": self.cb_thumbnail.isChecked(),
            "ask_duplicates": self.cb_ask_duplicates.isChecked(),
            "download_playlist": self.cb_download_playlist.isChecked(),
            "playlist_items": self.playlist_items.text().strip(),
            "cookies_file": self.cookies.text().strip(),
            "browser_cookie_source": self.browser_cookie.currentText(),
            "proxy": self.proxy.text().strip(),
            "theme": self.theme_cb.currentText(),
            "clip_default_duration": int(self.clip_dur.value()),
            "clip_overlap": int(self.clip_overlap.value()),
            "clip_aspect": self.clip_aspect.currentText(),
            "clip_quality": self.clip_quality.currentText(),
            "clip_output_dir": self.clip_out.text().strip(),
            "clip_parallel": int(self.clip_parallel.value()),
            "transcribe_model": self.tr_model.currentText(),
            "transcribe_model_dir": self.tr_model_dir.text().strip(),
            "transcribe_language": self.tr_lang.currentText(),
            "transcribe_device": self.tr_device.currentText(),
            "transcribe_cuda_preflight": True,
            "word_timestamps": bool(self.tr_word.isChecked()),
            "ai_provider": manual_provider,
            "ai_endpoint": manual_endpoint,
            "ai_api_key": manual_key,
            "ai_settings_user_configured": bool(
                manual_provider == "google" and bool(manual_key)
            ) or bool(
                manual_provider not in ("", "none", "local", "google") and manual_endpoint and manual_key
            ),
            "ai_model": self.app.config.get("ai_model") or "",
            "ai_vision_model": self.ai_vision_model.text().strip() if hasattr(self,"ai_vision_model") else str(self.app.config.get("ai_vision_model") or ""),
            "ai_vision_preference": "",
            "ai_local_enabled": True,
            "ai_local_vision": True,
            "ai_timeout": int(self.ai_timeout.value()),
            "ai_auto_model": bool(self.ai_auto_model.isChecked()),
            "ai_auto_visual": bool(self.ai_auto_visual.isChecked()),
            "ai_failover_local": bool(self.ai_failover_local.isChecked()),
            "project_dir": self.storage_project_dir.text().strip(),
            "cache_dir": self.storage_cache_dir.text().strip(),
            "temp_dir": self.storage_temp_dir.text().strip(),
            "ai_models_cache": getattr(self, "ai_models_cache", []),
        })
        if not saved:
            QMessageBox.warning(self, "Penyimpanan gagal", "Pengaturan tidak tersimpan aman. Key API tidak ditulis ke settings.json.")
            return
        self._refresh_disk()
        self._download_settings_dirty_keys.clear()
        self._download_settings_dirty = False
        self.download_settings_changed.emit()
        self.ai_settings_changed.emit()
        QMessageBox.information(self, "Tersimpan", "Pengaturan disimpan. AI yang sudah terhubung akan dipakai otomatis oleh Content Clipper AI.")
