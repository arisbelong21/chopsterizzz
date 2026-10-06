"""Chopster — unified Content Clipper AI workspace.

This page intentionally keeps the complete production chain in one workspace:
import/downloaded media -> transcript -> analyzer -> clips -> reframe -> subtitles -> branding -> audio -> export -> publish queue.
Cloud AI is optional; every core workflow has a local fallback.
"""
from __future__ import annotations

import html, json, os, re, shutil, subprocess, time
from pathlib import Path

from PySide6.QtCore import Qt, Signal, Slot, QUrl, QTimer, QEvent, QRect
from PySide6.QtGui import QPixmap, QFont, QImage, QFontDatabase, QColor
from PySide6.QtWidgets import QGraphicsBlurEffect, QGraphicsDropShadowEffect, QGraphicsOpacityEffect
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput, QVideoSink
from PySide6.QtWidgets import (
    QWidget,QVBoxLayout,QHBoxLayout,QGridLayout,QLabel,QPushButton,QFrame,QFileDialog,QSizePolicy,
    QComboBox,QSpinBox,QDoubleSpinBox,QCheckBox,QTableWidget,QTableWidgetItem,QInputDialog,
    QHeaderView,QAbstractItemView,QTextEdit,QLineEdit,QMessageBox,QProgressBar,QSplitter,
    QTabWidget,QListWidget,QListWidgetItem,QSlider,QGroupBox,QFormLayout,QDialog,QDialogButtonBox,
    QScrollArea
)

from chopster.ui.theme import THEMES
from chopster.ui.components.cards import Card, StatCard
from chopster.clipper.media_probe import probe, format_duration
from chopster.clipper.project_manager import create_project, save_project, load_project, list_projects, ClipEntry
from chopster.clipper.clip_planner import plan_fixed_duration, plan_fixed_overlap, plan_fixed_interval, plan_from_timestamps
from chopster.clipper.transcript_manager import transcribe, load_cached_transcript, save_cached_transcript, delete_cached_transcript, transcript_to_srt
from chopster.clipper.analysis_cache import load_artifact, save_artifact, status as analysis_status
from chopster.clipper.subtitle_ai import prepare_transcript_and_subtitles, transcript_prepare_signature
from chopster.clipper.highlight_analyzer import analyze_with_ai
from chopster.clipper.caption_generator import generate_captions
from chopster.clipper.clip_renderer import export_batch
from chopster.clipper.scene_detector import detect_scenes
from chopster.clipper.gemini_scene_vision import analyze_scene_visuals
from chopster.clipper.advanced_features import (
    format_timecode, parse_timecode, remove_filler_segments, search_transcript,
    estimate_speakers, broll_suggestions, transcript_excerpt, center_focus
)
from chopster.clipper.face_tracking import detect_face_focus
from chopster.clipper.camera_director import interpolate_focus
from chopster.clipper.adaptive_framing import decide_framing
from chopster.clipper.scheduler import add_job, list_jobs, remove_job
from chopster.clipper.analytics import record as record_analytics, summary as analytics_summary
from chopster.clipper.master_analysis import ensure_master_analysis
from chopster.clipper.voiceover import generate_voiceover


class ScrollSafeComboBox(QComboBox):
    """Let a parent scroll area receive wheel input instead of changing selection."""

    def wheelEvent(self, event):
        event.ignore()


class ScrollSafeSpinBox(QSpinBox):
    """Prevent page scrolling from incrementing a setting under the pointer."""

    def wheelEvent(self, event):
        event.ignore()


class ScrollSafeDoubleSpinBox(QDoubleSpinBox):
    """Prevent page scrolling from incrementing a setting under the pointer."""

    def wheelEvent(self, event):
        event.ignore()


class ScrollSafeSlider(QSlider):
    """Keep scroll gestures from changing slider values while browsing the page."""

    def wheelEvent(self, event):
        event.ignore()


ACS_WORD_STYLES = {
    "viral_pop": {"label": "Viral Pop", "sample": "VIRAL  POP", "accent": "#FFE600", "tagline": "Yellow Glow"},
    "beast_punch": {"label": "Beast Punch", "sample": "BEAST  PUNCH", "accent": "#00FF66", "tagline": "High Impact Green"},
    "cyber_violet": {"label": "Cyber Violet", "sample": "CYBER  VIOLET", "accent": "#D946EF", "tagline": "Neon Purple Glow"},
    "fire_red": {"label": "Fire Crimson", "sample": "FIRE  CRIMSON", "accent": "#FF2E2E", "tagline": "High Energy Red"},
    "electric_cyan": {"label": "Electric Cyan", "sample": "ELECTRIC  CYAN", "accent": "#00F0FF", "tagline": "Ice Blue Glow"},
    "golden_aura": {"label": "Golden Aura", "sample": "GOLDEN  AURA", "accent": "#FFB800", "tagline": "Luxury Warm Gold"},
    "clean_minimal": {"label": "Clean Minimal", "sample": "Clean Minimal", "accent": "#E0E0E0", "tagline": "Soft Dark Box"},
    "none": {"label": "None", "sample": "×  NONE", "accent": "#FFFFFF", "tagline": "Burn No Captions"},
}
ACS_CAPTION_FONTS = ("Outfit", "Montserrat", "Inter", "Impact", "Bebas Neue", "Anton", "Poppins", "Arial Black")
ACS_CAPTION_SIZES = {"small": 65, "medium": 78, "big": 94}
_ACS_CAPTION_FONTS_REGISTERED = False


def _register_acs_caption_fonts():
    """Load the same bundled font assets used by Auto Clip Studio into Qt."""
    global _ACS_CAPTION_FONTS_REGISTERED
    if _ACS_CAPTION_FONTS_REGISTERED:
        return
    font_dir = Path(__file__).resolve().parents[2] / "auto_clip_studio" / "engine" / "fonts"
    for font_name in ("Outfit.ttf", "Montserrat.ttf", "Inter.ttf", "Bebas Neue.ttf", "Anton.ttf", "Poppins.ttf"):
        font_path = font_dir / font_name
        if font_path.is_file():
            try:
                QFontDatabase.addApplicationFont(str(font_path))
            except Exception:
                pass
    _ACS_CAPTION_FONTS_REGISTERED = True


class ClipperPage(QWidget):
    navigate = Signal(str)
    sig_log = Signal(str)
    sig_progress = Signal(int, str)

    def __init__(self, app, theme: str = "dark", parent: QWidget | None = None):
        super().__init__(parent)
        self.app=app; self.theme=theme; self.C=THEMES.get(theme,THEMES["dark"])
        self.setAcceptDrops(True)
        self.project=None; self.media=None; self.transcript=None; self.candidates=[]; self.clips=[]; self.focus_points=[]
        self._task_callbacks={}; self._task_errors={}; self._last_error=""; self._export_running=False
        self._subtitle_ready=False; self._subtitle_preview_time=None; self._preview_canvas_rect=None; self._fullscreen_preview=None; self._preview_muted=False; self._preview_volume=0.80
        self._acs_words_cache_owner=None; self._acs_words_cache=None
        self._last_frame_image=None; self._last_frame_clock=0.0; self._preview_current_sec=0.0; self._pending_seek_sec=0.0; self._preview_quick_focus=[]; self._last_scene_preview_progress=-1.0; self._scene_segments=[]; self._scene_preview_mode=False
        self._build_ui()
        self.app.tasks.task_progress.connect(self._task_progress)
        self.app.tasks.task_result.connect(self._task_result)
        self.app.tasks.task_failed.connect(self._task_failed)
        self._restore_latest()

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        C=self.C; root=QVBoxLayout(self); root.setContentsMargins(16,14,16,14); root.setSpacing(10)
        head=QHBoxLayout()
        title=QLabel("Content Clipper AI"); title.setObjectName("h1"); head.addWidget(title)
        self.project_label=QLabel("Belum ada project"); self.project_label.setObjectName("gold"); head.addWidget(self.project_label); head.addStretch()
        self._head_buttons=[]
        for text,fn,obj in [("＋ Project Baru",self._new_project,"ghost"),("📂 Buka",self._open_project,"ghost"),("💾 Simpan",self._save_project,"ghost"),("🧠 MASTER ANALYSIS",self._master_analysis,"primary"),("⚡ AUTO CREATE SHORTS",self._smart_pipeline,"primary")]:
            b=QPushButton(text); b.setObjectName(obj); b.clicked.connect(fn); head.addWidget(b); self._head_buttons.append(b)
        root.addLayout(head)

        stats=QHBoxLayout(); stats.setSpacing(8)
        self.stat_video=StatCard("VIDEO","—",C["BLUE"],self.theme,self)
        self.stat_clips=StatCard("CLIPS","0",C["GOLD"],self.theme,self)
        self.stat_high=StatCard("HIGHLIGHT","0",C["GREEN"],self.theme,self)
        self.stat_status=StatCard("STATUS","Siap",C["TEXT"],self.theme,self)
        self._stat_cards=(self.stat_video,self.stat_clips,self.stat_high,self.stat_status)
        for x in self._stat_cards: stats.addWidget(x)
        self._stats_layout=stats
        root.addLayout(stats)

        bar=QHBoxLayout()
        self.import_btn=QPushButton("🎬 Import Video"); self.import_btn.setObjectName("primary"); self.import_btn.clicked.connect(self._import_video); bar.addWidget(self.import_btn)
        self.download_btn=QPushButton("⬇ Download"); self.download_btn.setObjectName("ghost"); self.download_btn.clicked.connect(lambda:self.navigate.emit("download")); bar.addWidget(self.download_btn)
        self.aspect=ScrollSafeComboBox(); self.aspect.addItems(["Original","9:16","3:4","4:5","1:1","4:3","16:9"]); self.aspect.currentTextChanged.connect(self._on_aspect_changed); bar.addWidget(QLabel("Canvas / Frame")); bar.addWidget(self.aspect)
        self.reframe=ScrollSafeComboBox(); self.reframe.addItems(["Smart","AI Camera Director","Speaker Focus","Two Person","Manual","Center"]); self.reframe.currentTextChanged.connect(self._on_reframe_changed); bar.addWidget(QLabel("Reframe")); bar.addWidget(self.reframe)
        self.quality=ScrollSafeComboBox(); self.quality.addItems(["fast","balanced","high"]); self.quality.setCurrentText(str(self.app.config.get("clip_quality") or "balanced")); bar.addWidget(QLabel("Quality")); bar.addWidget(self.quality)
        self.output_edit=QLineEdit(str(self.app.config.get("clip_output_dir") or "")); self.output_edit.setPlaceholderText("Folder export — kosong = folder Chopster_Exports"); bar.addWidget(self.output_edit,1)
        pick=QPushButton("Pilih Folder"); pick.setObjectName("ghost"); pick.clicked.connect(self._pick_output); bar.addWidget(pick)
        self._toolbar_layout=bar
        root.addLayout(bar)
        self.aspect_help=self._hint("Canvas mengikuti rasio yang dipilih dan otomatis memakai Face/Person Safe Framing. 9:16 / 3:4 / 4:5 / 1:1 / 4:3 / 16:9 akan menyesuaikan posisi orang agar wajah tidak terpotong. Group yang terlalu lebar memakai komposisi fit.")
        root.addWidget(self.aspect_help)

        self.progress=QProgressBar(); self.progress.setRange(0,100); self.progress.setValue(0); root.addWidget(self.progress)
        self.status=QLabel("Siap — masukkan video untuk memulai."); self.status.setObjectName("muted"); root.addWidget(self.status)

        split=QSplitter(Qt.Vertical); root.addWidget(split,1)
        upper=QSplitter(Qt.Horizontal); split.addWidget(upper)
        # preview
        pv=QFrame(); pv.setObjectName("card"); pl=QVBoxLayout(pv); pl.setContentsMargins(8,8,8,8)
        ph=QHBoxLayout(); ph.addWidget(QLabel("LIVE PREVIEW")); ph.addStretch(); self.preview_status=QLabel("Belum ada video"); self.preview_status.setObjectName("muted"); ph.addWidget(self.preview_status); self.time_label=QLabel("00:00 / 00:00"); ph.addWidget(self.time_label); pl.addLayout(ph)
        self.preview_host=QFrame(); self.preview_host.setStyleSheet("background:#050505; border-radius:10px;"); self.preview_host.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Expanding); pl.addWidget(self.preview_host,1)
        # QVideoWidget is excellent for basic playback, but native video surfaces can
        # sit above normal child widgets on Windows. For a real editor preview (watermark
        # + subtitle + aspect crop), use QVideoSink -> QLabel so overlays are guaranteed
        # to render above the decoded frame.
        self.preview_canvas=QFrame(self.preview_host); self.preview_canvas.setStyleSheet("background:#000; border:2px solid rgba(255,190,34,190); border-radius:10px;"); self.preview_canvas.installEventFilter(self)
        self.preview_bg=QLabel(self.preview_canvas); self.preview_bg.setAlignment(Qt.AlignCenter); self.preview_bg.setStyleSheet("background:#060606; border:none;"); self.preview_bg.hide()
        self.preview_bg_blur=QGraphicsBlurEffect(self.preview_bg); self.preview_bg_blur.setBlurRadius(18); self.preview_bg.setGraphicsEffect(self.preview_bg_blur)
        self.video=QLabel(self.preview_canvas); self.video.setAlignment(Qt.AlignCenter); self.video.setStyleSheet("background:transparent; color:#9AA4B2; border:none;"); self.video.setText("Import video lalu klik Play untuk melihat preview."); self.video.setAttribute(Qt.WA_TransparentForMouseEvents,True)
        self.video_sink=QVideoSink(self)
        self.video_sink.videoFrameChanged.connect(self._on_video_frame)
        # Keep exactly one QMediaPlayer for the lifetime of the editor.
        # Recreating/deleting players during import can leave queued Qt callbacks
        # targeting a deleted QObject ("QMediaPlayer already deleted").
        self.audio_output=QAudioOutput(self)
        self.audio_output.setVolume(self._preview_volume)
        self.video_player=QMediaPlayer(self)
        self.video_player.setAudioOutput(self.audio_output)
        self.video_player.setVideoSink(self.video_sink)
        self.video_player.positionChanged.connect(self._position_changed)
        self.video_player.durationChanged.connect(self._on_duration_changed)
        self.video_player.errorOccurred.connect(self._on_media_error)
        self.video_player.mediaStatusChanged.connect(self._on_media_status)
        self.subtitle_overlay=QLabel(self.preview_canvas); self.subtitle_overlay.setAlignment(Qt.AlignCenter); self.subtitle_overlay.setWordWrap(True); self.subtitle_overlay.setAttribute(Qt.WA_TransparentForMouseEvents,True); self.subtitle_overlay.hide()
        self.watermark_overlay=QLabel(self.preview_canvas); self.watermark_overlay.setAlignment(Qt.AlignCenter); self.watermark_overlay.setAttribute(Qt.WA_TransparentForMouseEvents,True); self.watermark_overlay.hide()
        self.preview_badge=QLabel(self.preview_canvas); self.preview_badge.setAlignment(Qt.AlignCenter); self.preview_badge.setAttribute(Qt.WA_TransparentForMouseEvents,True); self.preview_badge.setStyleSheet(f"color:{C['ON_GOLD']}; background:{C['GOLD']}; border-radius:7px; padding:5px 9px; font-weight:800;"); self.preview_badge.hide()
        controls=QHBoxLayout(); self.play=QPushButton("▶ Play"); self.play.setObjectName("ghost"); self.play.clicked.connect(self._toggle_play); controls.addWidget(self.play); self.rewind=QPushButton("↺ 0:00"); self.rewind.setObjectName("ghost"); self.rewind.clicked.connect(lambda:self._seek_to(0)); controls.addWidget(self.rewind); self.seek=ScrollSafeSlider(Qt.Horizontal); self.seek.sliderMoved.connect(self._seek); controls.addWidget(self.seek,1); self.volume_btn=QPushButton("🔊"); self.volume_btn.setObjectName("ghost"); self.volume_btn.setToolTip("Mute / unmute preview"); self.volume_btn.clicked.connect(self._toggle_preview_mute); controls.addWidget(self.volume_btn); self.volume_slider=ScrollSafeSlider(Qt.Horizontal); self.volume_slider.setRange(0,100); self.volume_slider.setValue(80); self.volume_slider.setFixedWidth(92); self.volume_slider.setToolTip("Atur volume preview"); self.volume_slider.valueChanged.connect(self._set_preview_volume); controls.addWidget(self.volume_slider); self.fullscreen_btn=QPushButton("⛶"); self.fullscreen_btn.setObjectName("ghost"); self.fullscreen_btn.setToolTip("Fullscreen preview"); self.fullscreen_btn.clicked.connect(self._toggle_fullscreen_preview); controls.addWidget(self.fullscreen_btn); pl.addLayout(controls)
        upper.addWidget(pv)

        inspector=QTabWidget(); upper.addWidget(inspector)
        inspector.addTab(self._build_clip_tools(),"✂ Clips")
        inspector.addTab(self._build_subtitle_tools(),"💬 Subtitle")
        inspector.addTab(self._build_branding_tools(),"💧 Branding")
        inspector.addTab(self._build_ai_tools(),"🤖 AI")
        inspector.addTab(self._build_audio_tools(),"🔊 Audio")

        self.bottom=QTabWidget(); self.bottom.setUsesScrollButtons(True); self.bottom.setDocumentMode(True); split.addWidget(self.bottom); split.setSizes([520,360])
        self._build_transcript_tab(); self._build_highlight_tab(); self._build_clip_queue_tab(); self._build_caption_tab(); self._build_publish_tab(); self._build_analytics_tab()
        self.log=QTextEdit(); self.log.setReadOnly(True); self.log.setFixedHeight(70); root.addWidget(self.log)
        self.sig_log.connect(self._log)
        self._resize_preview()

    def _card(self,title):
        f=QFrame(); f.setObjectName("card"); l=QVBoxLayout(f); l.setContentsMargins(12,12,12,12); lab=QLabel(title); lab.setObjectName("h2"); l.addWidget(lab); return f,l

    def _hint(self, text):
        lab=QLabel(text)
        lab.setObjectName("muted")
        lab.setWordWrap(True)
        lab.setStyleSheet("font-size:11px; line-height:1.25; padding:2px 0 4px 0;")
        return lab

    def _build_clip_tools(self):
        f,l=self._card("Auto Clip & Timeline")
        l.addWidget(self._hint("Chopster membagi video berdasarkan aturan yang kamu pilih. Waktu selalu ditulis sebagai MM:SS. Jika durasi lebih dari 59:59, otomatis berubah menjadi HH:MM:SS."))
        form=QFormLayout(); form.setVerticalSpacing(9)
        self.clip_mode=ScrollSafeComboBox(); self.clip_mode.addItems(["Fixed Duration","Fixed Overlap","Fixed Interval","Scene Detection","AI Highlight"]); self.clip_mode.currentTextChanged.connect(self._update_clip_mode_help); form.addRow("Mode pembagian",self.clip_mode)
        self.clip_duration=QLineEdit("00:30"); self.clip_duration.setMinimumWidth(150); self.clip_duration.setPlaceholderText("MM:SS — contoh 00:30 = 30 detik"); self.clip_duration.setToolTip("Durasi target tiap clip. 00:30 = 30 detik; 01:00 = 1 menit; 01:30 = 1 menit 30 detik."); form.addRow("Durasi tiap clip (MM:SS)",self.clip_duration)
        self.clip_overlap=QLineEdit("00:05"); self.clip_overlap.setMinimumWidth(150); self.clip_overlap.setPlaceholderText("MM:SS — contoh 00:05 = 5 detik"); self.clip_overlap.setToolTip("Bagian video yang diulang antar clip. 00:05 berarti 5 detik terakhir clip sebelumnya masuk lagi ke clip berikutnya."); form.addRow("Overlap antar clip (MM:SS)",self.clip_overlap)
        self.clip_interval=QLineEdit("00:30"); self.clip_interval.setMinimumWidth(150); self.clip_interval.setPlaceholderText("MM:SS — contoh 00:45 = mulai tiap 45 detik"); self.clip_interval.setToolTip("Jarak antara waktu mulai clip. 00:45 berarti clip baru dimulai setiap 45 detik."); form.addRow("Jarak mulai clip (MM:SS)",self.clip_interval)
        self.clip_count=ScrollSafeSpinBox(); self.clip_count.setRange(1,100); self.clip_count.setValue(10); self.clip_count.setToolTip("Jumlah highlight yang dipakai saat mode AI Highlight."); form.addRow("Jumlah highlight AI",self.clip_count)
        l.addLayout(form)
        self.plan_preview=self._hint("Belum ada video. Import video untuk melihat contoh pembagian clip.")
        self.plan_preview.setWordWrap(True); self.plan_preview.setMinimumHeight(54); l.addWidget(self.plan_preview)
        self.clip_mode_help=self._hint("")
        l.addWidget(self.clip_mode_help)
        for field in (self.clip_duration,self.clip_overlap,self.clip_interval): field.textChanged.connect(self._refresh_plan_preview)
        self.clip_count.valueChanged.connect(self._refresh_plan_preview)
        self._update_clip_mode_help(self.clip_mode.currentText())
        b=QHBoxLayout(); x=QPushButton("⚡ Generate Clips"); x.setObjectName("primary"); x.setToolTip("Membuat daftar clip sesuai aturan di atas."); x.clicked.connect(self._generate_clips); b.addWidget(x)
        y=QPushButton("🔎 Scene Detect"); self.scene_btn=y; y.setObjectName("ghost"); y.setToolTip("Mencari pergantian scene tanpa membekukan jendela."); y.clicked.connect(self._scene_detect); b.addWidget(y)
        z=QPushButton("⏹ Cancel"); z.setObjectName("danger"); z.clicked.connect(self._cancel_tasks); b.addWidget(z); l.addLayout(b)
        return f

    def _update_clip_mode_help(self, mode):
        texts={
            "Fixed Duration":"Membagi video berurutan dari awal. Contoh 05:22 dengan 00:30 → 00:00–00:30, 00:30–01:00, …, 05:00–05:22.",
            "Fixed Overlap":"Membagi video dengan durasi yang sama dan overlap. Contoh 00:30 + overlap 00:05 → Clip 1 00:00–00:30, Clip 2 00:25–00:55, Clip 3 00:50–01:20, dst.",
            "Fixed Interval":"Durasi clip tetap, titik Start berjarak tetap. Contoh 00:30 + 00:45 → 00:00–00:30, 00:45–01:15, 01:30–02:00, dst.",
            "Scene Detection":"FFmpeg mencari pergantian visual. Durasi clip ditentukan oleh scene, bukan oleh angka durasi di atas.",
            "AI Highlight":"Viral Analyzer memilih bagian yang paling menarik. Jumlah kandidat diatur di 'Jumlah highlight AI'. Tanpa API pun mode ini tetap memakai heuristic lokal.",
        }
        if hasattr(self,"clip_mode_help"): self.clip_mode_help.setText(texts.get(mode,""))
        if hasattr(self,"clip_duration"):
            enabled=mode in ("Fixed Duration","Fixed Overlap","Fixed Interval")
            self.clip_duration.setEnabled(enabled); self.clip_overlap.setEnabled(mode=="Fixed Overlap"); self.clip_interval.setEnabled(mode=="Fixed Interval"); self.clip_count.setEnabled(mode=="AI Highlight")
            self._refresh_plan_preview()

    def _refresh_plan_preview(self, *_):
        if not hasattr(self, "plan_preview") or not getattr(self, "media", None):
            return
        try:
            total = float(self.media.duration)
            total_tc = format_timecode(total)
            mode = self.clip_mode.currentText()
            if mode == "Fixed Duration":
                secs = parse_timecode(self.clip_duration.text())
                if secs <= 0: raise ValueError
                count = int(total // secs) + (1 if total % secs > 0.5 else 0)
                last = total - max(0, count-1)*secs if count else 0
                self.plan_preview.setText(f"VIDEO {total_tc}  →  {count} CLIP ESTIMASI\nStart: 00:00 → {format_timecode(secs)} → {format_timecode(secs*2)} → …\nDurasi tiap clip: {format_timecode(secs)} • Clip terakhir: {format_timecode(last)}")
            elif mode == "Fixed Overlap":
                secs=parse_timecode(self.clip_duration.text()); ov=parse_timecode(self.clip_overlap.text()); step=secs-ov
                if secs<=0 or step<=0: raise ValueError
                count=max(1,int((max(0,total-secs)+step-1e-9)//step)+1)
                self.plan_preview.setText(f"VIDEO {total_tc}  →  kira-kira {count} CLIP\nDurasi: {format_timecode(secs)} • Overlap: {format_timecode(ov)} • Jarak mulai: {format_timecode(step)}\nContoh: 00:00–{format_timecode(secs)} lalu {format_timecode(step)}–{format_timecode(step+secs)}")
            elif mode == "Fixed Interval":
                secs=parse_timecode(self.clip_duration.text()); inter=parse_timecode(self.clip_interval.text())
                if secs<=0 or inter<=0: raise ValueError
                count=max(1,int((max(0,total-0.5))/inter)+1)
                self.plan_preview.setText(f"VIDEO {total_tc}  →  kira-kira {count} CLIP\nDurasi: {format_timecode(secs)} • Clip baru dimulai tiap {format_timecode(inter)}")
            elif mode == "Scene Detection":
                self.plan_preview.setText(f"VIDEO {total_tc}  →  jumlah clip ditentukan otomatis dari pergantian scene\nKlik 'Scene Detect' untuk menganalisis video.")
            else:
                self.plan_preview.setText(f"VIDEO {total_tc}  →  maksimal {self.clip_count.value()} highlight kandidat\nAI/heuristic akan memilih bagian yang paling menarik.")
        except Exception:
            self.plan_preview.setText("Format waktu belum valid. Gunakan MM:SS, contoh 00:30, 01:00, atau 01:05:20.")

    def _build_subtitle_tools(self):
        _register_acs_caption_fonts()
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame)
        frame, layout = self._card("Subtitle Engine")
        layout.setSpacing(10)
        layout.addWidget(self._hint("Animated word-level subtitles — gaya, warna sorot kata, font, dan preview mengikuti Auto Clip Studio. Kata aktif berubah sesuai timestamp; tekan Play untuk melihatnya bergerak."))

        ai_box = QGroupBox("AI Transcript & Subtitle")
        ai_layout = QVBoxLayout(ai_box); ai_layout.setSpacing(7)
        self.sub_ai_status = QLabel("AI Settings memprioritaskan transcript cleanup dan subtitle segmentation. Jika tidak tersedia, Chopster memakai engine lokal.")
        self.sub_ai_status.setObjectName("muted"); self.sub_ai_status.setWordWrap(True); ai_layout.addWidget(self.sub_ai_status)
        ai_actions = QHBoxLayout()
        self.sub_ai_btn = QPushButton("✨ Rapikan dengan AI"); self.sub_ai_btn.setObjectName("primary")
        self.sub_ai_btn.setToolTip("Rapikan transcript dan susun cue; timestamp tetap ditambatkan ke Whisper.")
        self.sub_ai_btn.clicked.connect(self._ai_prepare_subtitles); ai_actions.addWidget(self.sub_ai_btn, 1)
        self.sub_ai_local_btn = QPushButton("↺ Susun Lokal"); self.sub_ai_local_btn.setObjectName("ghost")
        self.sub_ai_local_btn.setToolTip("Susun cue subtitle secara lokal dari timestamp.")
        self.sub_ai_local_btn.clicked.connect(lambda: self._generate_subtitle(use_ai=False)); ai_actions.addWidget(self.sub_ai_local_btn)
        ai_layout.addLayout(ai_actions); layout.addWidget(ai_box)

        style_box = QGroupBox("Animated Word-Level Subtitles")
        style_layout = QVBoxLayout(style_box); style_layout.setContentsMargins(10, 12, 10, 10); style_layout.setSpacing(9)
        self.sub_preset = ScrollSafeComboBox(style_box)
        self.sub_preset.addItems(list(ACS_WORD_STYLES))
        stored_style = str(self.app.config.get("clipper_caption_style") or self.app.config.get("subtitle_preset") or "viral_pop")
        legacy_style_map = {"Bold Creator":"viral_pop", "Yellow Punch":"viral_pop", "Clean Podcast":"clean_minimal", "Minimal White":"clean_minimal", "Cyan Creator":"electric_cyan", "Gold Highlight":"golden_aura", "Karaoke Gold":"viral_pop"}
        stored_style = legacy_style_map.get(stored_style, stored_style)
        self.sub_preset.setCurrentText(stored_style if stored_style in ACS_WORD_STYLES else "viral_pop")
        self.sub_preset.hide()  # The style cards are the visible selector.
        self.sub_preset_cards = {}
        preset_grid = QGridLayout(); preset_grid.setHorizontalSpacing(7); preset_grid.setVerticalSpacing(7)
        for index, (style_id, item) in enumerate(ACS_WORD_STYLES.items()):
            card = QPushButton(f"{item['sample']}\n{item['label']}  ·  {item['tagline']}", style_box)
            card.setObjectName("acsSubtitleStyleCard"); card.setCheckable(True); card.setMinimumHeight(58)
            card.setToolTip(f"Pakai gaya subtitle {item['label']}")
            card.setStyleSheet(f"""
                QPushButton#acsSubtitleStyleCard {{ background:{self.C['INPUT']}; color:{item['accent']};
                    border:1px solid {self.C['BORDER']}; border-radius:8px; padding:7px 5px;
                    text-align:center; font-family:'Outfit'; font-size:10px; font-weight:800; }}
                QPushButton#acsSubtitleStyleCard:hover {{ border-color:{self.C['GOLD']}; background:{self.C['PANEL2']}; }}
                QPushButton#acsSubtitleStyleCard:checked {{ border:2px solid {self.C['GOLD']}; background:{self.C['PANEL2']}; }}
            """)
            card.clicked.connect(lambda _checked=False, key=style_id: self.sub_preset.setCurrentText(key))
            self.sub_preset_cards[style_id] = card
            preset_grid.addWidget(card, index // 4, index % 4)
        for col in range(4): preset_grid.setColumnStretch(col, 1)
        style_layout.addLayout(preset_grid)

        self.sub_font = ScrollSafeComboBox(style_box)
        self.sub_font.addItems(ACS_CAPTION_FONTS)
        stored_font=str(self.app.config.get("clipper_caption_font") or self.app.config.get("subtitle_font") or "Outfit")
        self.sub_font.setCurrentText(stored_font if stored_font in ACS_CAPTION_FONTS else "Outfit")
        self.sub_font.hide()
        self.sub_font_cards = {}
        font_box = QGroupBox("Font Family", style_box)
        font_grid = QGridLayout(font_box); font_grid.setContentsMargins(7, 8, 7, 7); font_grid.setHorizontalSpacing(5); font_grid.setVerticalSpacing(5)
        for index, font_name in enumerate(ACS_CAPTION_FONTS):
            button = QPushButton(font_name, font_box); button.setObjectName("ghost"); button.setCheckable(True)
            button.setStyleSheet(f"font-family:'{font_name}';font-size:11px;padding:5px 7px;")
            button.clicked.connect(lambda _checked=False, name=font_name: self.sub_font.setCurrentText(name))
            self.sub_font_cards[font_name] = button
            font_grid.addWidget(button, index // 4, index % 4)
        style_layout.addWidget(font_box)

        self.sub_size = ScrollSafeSpinBox(style_box); self.sub_size.setRange(14, 96); self.sub_size.hide()
        self.sub_size_preset = str(self.app.config.get("clipper_caption_size") or "medium").lower()
        if self.sub_size_preset not in {"small", "medium", "big"}: self.sub_size_preset = "medium"
        size_row = self._subtitle_pill_row(style_layout, "Font Size", (("small", "Small (36px)"), ("medium", "Medium (44px)"), ("big", "Big (54px)")))
        self.sub_size_cards = size_row
        self._set_subtitle_size(self.sub_size_preset, persist=False)

        self.sub_text_case = str(self.app.config.get("clipper_caption_text_case") or "uppercase").lower()
        if self.sub_text_case not in {"uppercase", "capitalize", "lowercase"}: self.sub_text_case = "uppercase"
        case_row = self._subtitle_pill_row(style_layout, "Letter Style", (("uppercase", "ABC (Caps)"), ("capitalize", "Abc (Title)"), ("lowercase", "abc (Lower)")))
        self.sub_case_cards = case_row

        self.sub_position_mode = str(self.app.config.get("clipper_caption_position_mode") or "bottom").lower()
        if self.sub_position_mode not in {"bottom", "center"}: self.sub_position_mode = "bottom"
        self.sub_bottom_percent = int(self.app.config.get("clipper_caption_bottom_percent") or 21)
        self.sub_center_percent = int(self.app.config.get("clipper_caption_center_percent") or 50)
        position_box = QGroupBox("Subtitle Placement", style_box)
        position_layout = QVBoxLayout(position_box); position_layout.setContentsMargins(8, 8, 8, 8); position_layout.setSpacing(6)
        position_buttons = QHBoxLayout(); position_buttons.setSpacing(6)
        self.sub_place_bottom = QPushButton("📌  Bottom (In Bar / Lower)", position_box); self.sub_place_bottom.setObjectName("ghost"); self.sub_place_bottom.setCheckable(True)
        self.sub_place_center = QPushButton("🎯  Center (Over Content)", position_box); self.sub_place_center.setObjectName("ghost"); self.sub_place_center.setCheckable(True)
        self.sub_place_bottom.clicked.connect(lambda: self._set_subtitle_position("bottom"))
        self.sub_place_center.clicked.connect(lambda: self._set_subtitle_position("center"))
        position_buttons.addWidget(self.sub_place_bottom); position_buttons.addWidget(self.sub_place_center); position_buttons.addStretch()
        position_layout.addLayout(position_buttons)
        position_header = QHBoxLayout(); position_header.addWidget(QLabel("Subtitle Position", position_box)); position_header.addStretch()
        self.sub_position_label = QLabel("", position_box); self.sub_position_label.setObjectName("muted"); position_header.addWidget(self.sub_position_label)
        position_layout.addLayout(position_header)
        self.sub_position_slider = ScrollSafeSlider(Qt.Horizontal, position_box)
        self.sub_position_slider.valueChanged.connect(self._on_subtitle_position_value_changed)
        position_layout.addWidget(self.sub_position_slider)
        style_layout.addWidget(position_box)
        self.sub_position_slider.blockSignals(True)
        self._set_subtitle_position(self.sub_position_mode, persist=False)
        self.sub_position_slider.blockSignals(False)
        self._sync_subtitle_preset_cards(); self._sync_subtitle_font_cards(); self._sync_subtitle_size_cards(); self._sync_subtitle_case_cards()
        self.sub_preset.currentTextChanged.connect(self._sync_subtitle_preset_cards)
        self.sub_preset.currentTextChanged.connect(self._on_subtitle_style_changed)
        self.sub_font.currentTextChanged.connect(self._sync_subtitle_font_cards)
        self.sub_font.currentTextChanged.connect(self._on_subtitle_style_changed)
        style_layout.addWidget(self._hint("Sorotan kata mengikuti word timestamp. Bila transcript hanya punya timestamp per kalimat, Chopster membagi waktu per kata seperti Auto Clip Studio."))
        layout.addWidget(style_box)

        # Existing export behavior is intentionally kept separate from the new style engine.
        export_box = QGroupBox("Export", frame)
        export_layout = QHBoxLayout(export_box)
        self.sub_burn = QCheckBox("Burn subtitle saat export")
        self.sub_burn.setChecked(bool(self.app.config.get("clip_burn_subtitle", True)))
        self.sub_burn.toggled.connect(self._on_subtitle_style_changed)
        export_layout.addWidget(self.sub_burn); export_layout.addStretch(); layout.addWidget(export_box)

        self.sub_max = ScrollSafeSpinBox(frame); self.sub_max.setRange(1, 15); self.sub_max.setValue(3); self.sub_max.hide()
        self.sub_mode = ScrollSafeComboBox(frame); self.sub_mode.addItems(["Baris", "Per kata", "Karaoke"]); self.sub_mode.setCurrentText("Per kata"); self.sub_mode.hide()
        self.sub_anim = ScrollSafeComboBox(frame); self.sub_anim.addItems(["Karaoke", "Word Highlight"]); self.sub_anim.setCurrentText("Word Highlight"); self.sub_anim.hide()
        self.sub_align = ScrollSafeComboBox(frame); self.sub_align.addItems(["Bawah tengah", "Tengah"]); self.sub_align.hide()
        self.sub_outline = ScrollSafeSpinBox(frame); self.sub_outline.setRange(0, 8); self.sub_outline.setValue(5); self.sub_outline.hide()
        self.sub_shadow = ScrollSafeSpinBox(frame); self.sub_shadow.setRange(0, 8); self.sub_shadow.setValue(2); self.sub_shadow.hide()
        self.sub_info = self._hint("Kata aktif disorot warna preset; subtitle tetap satu baris dan ukuran teks stabil.")
        layout.addWidget(self.sub_info)
        actions = QHBoxLayout()
        generate = QPushButton("Generate Subtitle"); generate.setObjectName("primary")
        generate.clicked.connect(self._generate_subtitle); actions.addWidget(generate, 1)
        remove_fillers = QPushButton("Remove Filler Words (reviewable)"); remove_fillers.setObjectName("ghost")
        remove_fillers.clicked.connect(self._remove_fillers); actions.addWidget(remove_fillers)
        layout.addLayout(actions); layout.addStretch(1)
        scroll.setWidget(frame)
        return scroll

    def _subtitle_pill_row(self, parent_layout, title, options):
        row_widget=QWidget(); row=QHBoxLayout(row_widget); row.setContentsMargins(0,0,0,0); row.setSpacing(8)
        label=QLabel(title); label.setObjectName("muted"); label.setMinimumWidth(105); row.addWidget(label)
        buttons={}
        for key,text in options:
            button=QPushButton(text,row_widget); button.setObjectName("ghost"); button.setCheckable(True); button.setMinimumHeight(30)
            buttons[key]=button; row.addWidget(button)
        row.addStretch(1); parent_layout.addWidget(row_widget)
        for key,button in buttons.items():
            if title=="Font Size": button.clicked.connect(lambda _=False,k=key:self._set_subtitle_size(k))
            else: button.clicked.connect(lambda _=False,k=key:self._set_subtitle_text_case(k))
        return buttons

    def _set_subtitle_size(self, size, persist=True):
        if size not in {"small", "medium", "big"}: return
        self.sub_size_preset=size
        if hasattr(self,"sub_size_cards"): self._sync_subtitle_size_cards()
        if hasattr(self,"sub_size"): self.sub_size.setValue(ACS_CAPTION_SIZES[size])
        if persist: self._on_subtitle_style_changed()

    def _set_subtitle_text_case(self, mode, persist=True):
        if mode not in {"uppercase", "capitalize", "lowercase"}: return
        self.sub_text_case=mode
        if hasattr(self,"sub_case_cards"): self._sync_subtitle_case_cards()
        if persist: self._on_subtitle_style_changed()

    def _set_subtitle_position(self, mode, persist=True):
        if mode not in {"bottom", "center"}: return
        self.sub_position_mode=mode
        self.sub_place_bottom.setChecked(mode=="bottom"); self.sub_place_center.setChecked(mode=="center")
        self.sub_place_bottom.setObjectName("primary" if mode=="bottom" else "ghost")
        self.sub_place_center.setObjectName("primary" if mode=="center" else "ghost")
        self.sub_place_bottom.style().unpolish(self.sub_place_bottom); self.sub_place_bottom.style().polish(self.sub_place_bottom)
        self.sub_place_center.style().unpolish(self.sub_place_center); self.sub_place_center.style().polish(self.sub_place_center)
        if mode=="bottom": self.sub_position_slider.setRange(8,42); self.sub_position_slider.setValue(max(8,min(42,self.sub_bottom_percent)))
        else: self.sub_position_slider.setRange(10,90); self.sub_position_slider.setValue(max(10,min(90,self.sub_center_percent)))
        self._update_subtitle_position_label()
        if persist: self._on_subtitle_style_changed()

    def _on_subtitle_position_value_changed(self, value):
        if self.sub_position_mode=="bottom": self.sub_bottom_percent=int(value)
        else: self.sub_center_percent=int(value)
        self._update_subtitle_position_label(); self._on_subtitle_style_changed()

    def _update_subtitle_position_label(self):
        if self.sub_position_mode=="bottom": self.sub_position_label.setText(f"{self.sub_bottom_percent}% from Bottom")
        else: self.sub_position_label.setText(f"{self.sub_center_percent}% from Top")

    def _sync_subtitle_preset_cards(self, *_args):
        selected=self.sub_preset.currentText() if hasattr(self,"sub_preset") else ""
        for key,button in getattr(self,"sub_preset_cards",{}).items(): button.setChecked(key==selected)

    def _sync_subtitle_font_cards(self, *_args):
        selected=self.sub_font.currentText() if hasattr(self,"sub_font") else ""
        for key,button in getattr(self,"sub_font_cards",{}).items():
            active=key==selected; button.setChecked(active); button.setObjectName("primary" if active else "ghost")
            button.style().unpolish(button); button.style().polish(button)

    def _sync_subtitle_size_cards(self):
        for key,button in getattr(self,"sub_size_cards",{}).items():
            active=key==self.sub_size_preset; button.setChecked(active); button.setObjectName("primary" if active else "ghost")
            button.style().unpolish(button); button.style().polish(button)

    def _sync_subtitle_case_cards(self):
        for key,button in getattr(self,"sub_case_cards",{}).items():
            active=key==self.sub_text_case; button.setChecked(active); button.setObjectName("primary" if active else "ghost")
            button.style().unpolish(button); button.style().polish(button)

    @staticmethod
    def _clean_acs_caption_text(value):
        text=str(value or "")
        text=re.sub(r"\[.*?\]|\(.*?\)|\*.*?\*", " ", text)
        text=re.sub(r"\b(laughter|applause|music|cheering|snickering|giggle|cough)\b", " ", text, flags=re.IGNORECASE)
        text=re.sub(r"[^\w\s.,%&$?]|_", "", text)
        return re.sub(r"\s+", " ", text).strip()

    def _caption_case_word(self, value):
        text=str(value or "").strip()
        if self.sub_text_case=="uppercase": return text.upper()
        if self.sub_text_case=="lowercase": return text.lower()
        return text.title()

    @staticmethod
    def _qt_hex_to_ass(value):
        v=str(value or "#FFFFFF").lstrip("#")
        if len(v)!=6: v="FFFFFF"
        return f"&H00{v[4:6]}{v[2:4]}{v[0:2]}".upper()

    def _subtitle_style_from_ui(self):
        from chopster.clipper.subtitle_templates import SubtitleStyle
        style_id=self.sub_preset.currentText() if hasattr(self,"sub_preset") else "viral_pop"
        label=ACS_WORD_STYLES.get(style_id,ACS_WORD_STYLES["viral_pop"])
        palette={
            "viral_pop":("&H00000000","&H80000000",4.8,2.0),
            "beast_punch":("&H00111111","&HA0000000",5.0,2.2),
            "cyber_violet":("&H00330033","&H80500050",4.8,2.0),
            "fire_red":("&H00000020","&H90000060",4.8,2.0),
            "electric_cyan":("&H00102020","&H90002030",4.8,2.0),
            "golden_aura":("&H000A1220","&HA0001830",4.8,2.0),
            "clean_minimal":("&H00111111","&HB0000000",3.6,1.8),
            "none":("&H00111111","&HB0000000",3.6,1.8),
        }
        outline_color,back_color,outline,shadow=palette.get(style_id,palette["viral_pop"])
        style=SubtitleStyle(
            name=label["label"], font=self.sub_font.currentText() or "Outfit",
            font_size=ACS_CAPTION_SIZES.get(self.sub_size_preset,78), bold=True,
            text_color="&H00FFFFFF", outline_color=outline_color, outline=int(outline),
            shadow=int(shadow), back_color=back_color,
            alignment=5 if self.sub_position_mode=="center" else 2,
            margin_v=self.sub_center_percent if self.sub_position_mode=="center" else self.sub_bottom_percent,
            animation="word_highlight", subtitle_mode="word",
            highlight_color=self._qt_hex_to_ass(label["accent"]), max_words_per_line=3,
        )
        style.caption_style=style_id
        style.text_case=self.sub_text_case
        style.size_preset=self.sub_size_preset
        style.subtitle_position_mode=self.sub_position_mode
        style.subtitle_bottom_percent=self.sub_bottom_percent
        style.subtitle_center_percent=self.sub_center_percent
        return style

    def _acs_word_timestamps(self, transcript=None):
        transcript=transcript or self.transcript
        if not transcript: return []
        if transcript is self.transcript and self._acs_words_cache_owner==id(transcript) and self._acs_words_cache is not None:
            return self._acs_words_cache
        result=[]
        for seg in getattr(transcript,"segments",[]) or []:
            try: line_start=float(seg.start); line_end=max(line_start+0.2,float(seg.end))
            except Exception: line_start=0.0; line_end=0.2
            found=[]
            for raw in getattr(seg,"words",[]) or []:
                raw_text=raw.get("word",raw.get("text","")) if isinstance(raw,dict) else str(raw)
                word=self._clean_acs_caption_text(raw_text)
                if not word: continue
                try: start=max(0.0,float(raw.get("start",line_start))); end=max(start+0.08,float(raw.get("end",start+0.25)))
                except Exception: continue
                found.append({"word":word,"start":start,"end":end})
            if found:
                result.extend(found)
                continue
            cleaned=self._clean_acs_caption_text(getattr(seg,"text",""))
            line_words=cleaned.split()
            if not line_words: continue
            line_duration=max(0.2,line_end-line_start)
            char_count=max(1,sum(max(1,len(word)) for word in line_words))
            cursor=line_start
            for word in line_words:
                duration=max(0.15,(max(1,len(word))/char_count)*line_duration)
                result.append({"word":word,"start":round(cursor,2),"end":round(cursor+duration,2)})
                cursor+=duration
        if not result:
            for cue in getattr(transcript,"subtitle_cues",[]) or []:
                cleaned=self._clean_acs_caption_text(cue.get("text",""))
                line_words=cleaned.split()
                if not line_words: continue
                try: start=float(cue.get("start",0)); end=max(start+0.2,float(cue.get("end",start+1)))
                except Exception: continue
                total=max(1,sum(len(w) for w in line_words)); cursor=start
                for word in line_words:
                    duration=max(0.15,len(word)/total*(end-start))
                    result.append({"word":word,"start":round(cursor,2),"end":round(cursor+duration,2)})
                    cursor+=duration
        result=sorted(result,key=lambda item:item["start"])
        if transcript is self.transcript:
            self._acs_words_cache_owner=id(transcript); self._acs_words_cache=result
        return result

    def _acs_preview_caption(self, pos, is_playing):
        words=self._acs_word_timestamps()
        if not words: return None
        chunks=[]; chunk=[]; char_count=0
        for item in words:
            text=self._caption_case_word(item["word"]); length=len(text)
            if len(chunk)>=3 or (chunk and char_count+length>16):
                chunks.append(chunk); chunk=[{**item,"display":text}]; char_count=length
            else:
                chunk.append({**item,"display":text}); char_count+=length+1
        if chunk: chunks.append(chunk)
        bounds=[]
        for group in chunks:
            start=float(group[0]["start"]); end=max(start+0.25,float(group[-1]["end"]))
            if bounds:
                previous_end=bounds[-1][1]
                if start<previous_end: start=previous_end
                if end<=start: end=start+0.25
            bounds.append([start,end])
        for index in range(len(bounds)-1):
            start,end=bounds[index]
            if end>bounds[index+1][0]: bounds[index][1]=bounds[index+1][0]
        selected=next((i for i,(start,end) in enumerate(bounds) if start<=pos<end),-1)
        if selected<0:
            if is_playing: return None
            selected=0
        group=chunks[selected]; start,end=bounds[selected]
        if len(group)==1:
            slices=[(start,end)]
        else:
            points=[start]
            for index in range(1,len(group)):
                raw_start=float(group[index]["start"])
                minimum=points[-1]+0.08; maximum=end-0.08*(len(group)-index)
                point=points[-1]+(end-points[-1])/(len(group)-index+1) if minimum>maximum else max(minimum,min(maximum,raw_start))
                points.append(point)
            points.append(end); slices=[(points[i],points[i+1]) for i in range(len(group))]
        active=next((i for i,(start,end) in enumerate(slices) if start<=pos<end),0)
        return {"words":[item["display"] for item in group],"active":active,"style":self.sub_preset.currentText()}

    def _on_subtitle_style_changed(self,*args):
        try:
            style=self._subtitle_style_from_ui()
            self.app.config.set("clipper_caption_style",style.caption_style)
            self.app.config.set("clipper_caption_font",style.font)
            self.app.config.set("clipper_caption_size",style.size_preset)
            self.app.config.set("clipper_caption_text_case",style.text_case)
            self.app.config.set("clipper_caption_position_mode",style.subtitle_position_mode)
            self.app.config.set("clipper_caption_bottom_percent",style.subtitle_bottom_percent)
            self.app.config.set("clipper_caption_center_percent",style.subtitle_center_percent)
            # Preserve legacy project/workspace fields while storing the new ACS style.
            self.app.config.set("subtitle_preset",style.caption_style)
            self.app.config.set("subtitle_font",style.font)
            self.app.config.set("subtitle_font_size",style.font_size)
            self.app.config.set("subtitle_mode","word")
            self.app.config.set("subtitle_animation","word_highlight")
            self.app.config.set("subtitle_max_words",3)
            self.app.config.set("subtitle_alignment_name","Tengah" if style.subtitle_position_mode=="center" else "Bawah tengah")
            self.app.config.set("clip_burn_subtitle",self.sub_burn.isChecked() if hasattr(self,"sub_burn") else True)
            if hasattr(self,"sub_info"): self.sub_info.setText(f"Gaya aktif: {ACS_WORD_STYLES[style.caption_style]['label']} • sorotan kata bertimestamp • font {style.font}")
            self._refresh_preview_overlay()
        except Exception as exc:
            self._log(f"Subtitle style update: {exc}")

    def _build_branding_tools(self):
        f,l=self._card("Watermark & Reframe")
        l.addWidget(self._hint("Watermark langsung tampil di LIVE PREVIEW. Semua perubahan posisi, opacity, ukuran, dan file dipantau secara live."))
        form=QFormLayout()
        self.wm_enable=QCheckBox("Aktif"); self.wm_enable.setChecked(bool(self.app.config.get("watermark_enabled"))); form.addRow("Watermark",self.wm_enable)
        self.wm_type=ScrollSafeComboBox(); self.wm_type.addItems(["Text","Image"]); form.addRow("Jenis",self.wm_type)
        self.wm_text=QLineEdit(str(self.app.config.get("watermark_text") or "")); self.wm_text.setPlaceholderText("Contoh: @NamaChannel"); form.addRow("Teks",self.wm_text)
        self.wm_image=QLineEdit(str(self.app.config.get("watermark_image") or "")); self.wm_image.setPlaceholderText("PNG / JPG / WebP"); form.addRow("File gambar",self.wm_image)
        ib=QPushButton("Pilih File Watermark"); ib.clicked.connect(self._pick_watermark); form.addRow("Pilih",ib)
        self.wm_pos=ScrollSafeComboBox(); self.wm_pos.addItems(["Top left","Top right","Bottom left","Bottom right","Center"]); self.wm_pos.setCurrentText(str(self.app.config.get("watermark_position") or "Bottom right")); form.addRow("Posisi",self.wm_pos)
        self.wm_op=ScrollSafeDoubleSpinBox(); self.wm_op.setRange(0.05,1.0); self.wm_op.setSingleStep(.05); self.wm_op.setDecimals(2); self.wm_op.setValue(float(self.app.config.get("watermark_opacity") or .75)); form.addRow("Opacity",self.wm_op)
        self.wm_size=ScrollSafeSpinBox(); self.wm_size.setRange(8,120); self.wm_size.setValue(int(self.app.config.get("clip_watermark_font") or 28)); form.addRow("Ukuran teks",self.wm_size)
        self.wm_img_size=ScrollSafeSpinBox(); self.wm_img_size.setRange(1,100); self.wm_img_size.setValue(int(self.app.config.get("watermark_image_scale_percent") or 22)); self.wm_img_size.setSuffix(" %"); form.addRow("Ukuran gambar",self.wm_img_size)
        l.addLayout(form)
        l.addWidget(self._hint("Ukuran teks = ukuran tulisan. Ukuran gambar = lebar watermark foto sebagai persentase frame (1% sangat kecil, 100% memenuhi lebar frame). Posisi dan opacity berlaku untuk keduanya. Semua perubahan langsung terlihat di LIVE PREVIEW."))
        self.wm_status=self._hint("Status watermark: nonaktif"); l.addWidget(self.wm_status)
        for w,sig in ((self.wm_enable,'toggled'),(self.wm_type,'currentTextChanged'),(self.wm_text,'textChanged'),(self.wm_image,'textChanged'),(self.wm_pos,'currentTextChanged'),(self.wm_op,'valueChanged'),(self.wm_size,'valueChanged'),(self.wm_img_size,'valueChanged')):
            getattr(w,sig).connect(self._on_branding_changed)
        b=QPushButton("🔎 Detect Faces / Smart Reframe"); b.setObjectName("primary"); b.clicked.connect(self._detect_faces); l.addWidget(b)
        l.addWidget(self._hint("Smart = otomatis: jika 2 orang terlihat, Chopster mencari pembicara aktif lalu mengunci kamera ke wajahnya. Speaker Focus = prioritas pembicara aktif. Two Person = selalu menjaga dua orang tetap aman dalam frame. Jika sinyal bicara ambigu, sistem sengaja fallback ke dua wajah agar tidak memotong setengah muka."))
        return f

    def _build_ai_tools(self):
        f,l=self._card("AI Connection")
        self.ai_connection_status=QLabel(); self.ai_connection_status.setWordWrap(True); l.addWidget(self.ai_connection_status)
        l.addWidget(self._hint("AI global dari Settings dipakai untuk text analysis, viral analysis, caption/content dan reasoning konten; tugas visual (frame/scene/camera) memakainya hanya bila model terverifikasi Teks+gambar, selebihnya memakai Local Vision/Tracking. Tanpa AI, semua fitur tetap berjalan dengan fallback lokal."))
        b=QHBoxLayout(); settings=QPushButton("⚙ Buka Pengaturan AI"); settings.setObjectName("ghost"); settings.clicked.connect(lambda: self.navigate.emit("settings")); b.addWidget(settings)
        refresh=QPushButton("🔄 Refresh Status"); refresh.clicked.connect(self.refresh_ai_status); b.addWidget(refresh); b.addStretch(); l.addLayout(b)
        self.ai_instruction=QLineEdit(); self.ai_instruction.setPlaceholderText("Contoh: cari obrolan paling seru, hook paling kuat, bagian lucu, konflik, atau payoff."); l.addWidget(self.ai_instruction)
        self.ai_action_hint=self._hint("Instruksi di atas benar-benar dipakai saat analisis. Klik Analyze Highlights untuk menjalankan AI/heuristic dari awal sampai akhir video. Hasil akan muncul di Viral Analyzer."); l.addWidget(self.ai_action_hint)
        b2=QHBoxLayout(); x=QPushButton("🔥 ANALYZE HIGHLIGHTS"); x.setObjectName("primary"); x.clicked.connect(self._analyze); b2.addWidget(x); y=QPushButton("✍ Generate Captions"); y.setObjectName("ghost"); y.clicked.connect(self._generate_captions); b2.addWidget(y); l.addLayout(b2)
        b3=QHBoxLayout(); br=QPushButton("🎞 B-roll Ideas"); br.clicked.connect(self._broll); vo=QPushButton("🔊 AI/Local Voiceover"); vo.clicked.connect(self._voiceover); b3.addWidget(br); b3.addWidget(vo); l.addLayout(b3)
        self.refresh_ai_status()
        return f

    def refresh_ai_status(self):
        if not hasattr(self,"ai_connection_status"): return
        try:
            from chopster.ai.config_helpers import normalize_provider, settings_ai_enabled, explicit_vision_status
            provider = normalize_provider(self.app.config.get("ai_provider"))
            endpoint = str(self.app.config.get("ai_endpoint") or "")
            key = str(self.app.config.get("ai_api_key") or "")
            model = str(self.app.config.get("ai_model") or "")
            vision_model = str(self.app.config.get("ai_vision_model") or "")
            profiles = list(self.app.config.get("ai_profiles", []) or [])
            cache = self.app.config.get("ai_models_cache") or []
            configured = settings_ai_enabled(provider, endpoint, key)
            if not configured:
                self.ai_connection_status.setText("⚙ AI global di Settings: belum diatur (opsional)\n🛡 Local fallback siap: analisis teks lokal, Local Vision/Tracking untuk frame/camera\n✨ Visual AI global aktif hanya setelah AI dikonfigurasi dan model terverifikasi Teks+gambar")
                return
            masked = ("•" * 6 + key[-4:]) if len(key) >= 4 else "••••"
            connection_state = str(self.app.config.get("ai_last_connection") or "")
            connected = connection_state == "connected"
            degraded = connection_state == "degraded_remote"
            prefix = "✅ AI terhubung" if connected else ("⚠️ AI remote offline — Local Fallback siap" if degraded else "🟡 AI sudah dikonfigurasi, tetapi belum lolos Test Connection")
            cap_text = "capability belum dimuat"
            vis_state = "belum diverifikasi gambar"
            for m in list(cache) + profiles:
                if str(m.get("id") or "") == (vision_model or model):
                    status_val = explicit_vision_status(m)
                    vis_state = "Teks + gambar (terverifikasi)" if status_val == "vision" else ("Teks saja" if status_val == "text" else "belum diverifikasi gambar")
                    break
            provider_label = {"gateway": "Gateway OpenAI-compatible", "google": "Google AI Studio / Gemini"}.get(provider, provider)
            self.ai_connection_status.setText(f"{prefix}\nProvider: <b>{provider_label}</b>\nModel teks: <b>{model or 'Auto'}</b>\nModel Vision/Auto: <b>{vision_model or model or 'Auto'}</b>\nAPI Key: {masked}\nKemampuan visual: {vis_state} (tanpa verifikasi, visual memakai Local Vision/Tracking)\nText AI: {'ON' if configured else 'OFF'}")
        except Exception:
            self.ai_connection_status.setText("⚙ AI global: status tidak dapat dimuat • Local fallback siap")

    def _build_audio_tools(self):
        f,l=self._card("Audio Enhancement")
        self.audio_enable=QCheckBox("Aktifkan saat export"); self.audio_enable.setChecked(bool(self.app.config.get("audio_enhance"))); l.addWidget(self.audio_enable)
        self.audio_preset=ScrollSafeComboBox(); self.audio_preset.addItems(["Voice Clear","Podcast","Shorts"]); self.audio_preset.setCurrentText(str(self.app.config.get("audio_preset") or "Voice Clear")); l.addWidget(self.audio_preset)
        lbl=QLabel("Noise reduction • compressor • loudness normalization."); lbl.setObjectName("muted"); l.addWidget(lbl)
        return f

    def _build_transcript_tab(self):
        w=QWidget(); l=QVBoxLayout(w); tools=QHBoxLayout()
        self.model=ScrollSafeComboBox(); self.model.addItems(["tiny","base","small","medium","large-v3"]); self.model.setCurrentText(str(self.app.config.get("transcribe_model") or "tiny")); tools.addWidget(QLabel("Model")); tools.addWidget(self.model)
        self.lang=ScrollSafeComboBox(); self.lang.addItems(["auto","id","en","ms","ja","ko","zh"]); self.lang.setCurrentText(str(self.app.config.get("transcribe_language") or "auto")); tools.addWidget(QLabel("Bahasa")); tools.addWidget(self.lang)
        self.word_ts=QCheckBox("Word timestamps / Karaoke"); self.word_ts.setChecked(bool(self.app.config.get("word_timestamps"))); tools.addWidget(self.word_ts)
        self.transcribe_btn=QPushButton("🎙 Transcribe"); self.transcribe_btn.setObjectName("primary"); self.transcribe_btn.clicked.connect(self._transcribe); tools.addWidget(self.transcribe_btn)
        self.transcribe_cancel_btn=QPushButton("⏹ Cancel"); self.transcribe_cancel_btn.setObjectName("danger"); self.transcribe_cancel_btn.setEnabled(False); self.transcribe_cancel_btn.clicked.connect(lambda:self.app.tasks.cancel("clipper:transcribe")); tools.addWidget(self.transcribe_cancel_btn)
        self.search=QLineEdit(); self.search.setPlaceholderText("Cari transcript..."); self.search.textChanged.connect(self._search_transcript); tools.addWidget(self.search,1)
        self.delete_transcript_btn=QPushButton("🗑 Hapus Transcript Cache"); self.delete_transcript_btn.setObjectName("danger"); self.delete_transcript_btn.setToolTip("Hapus transcript cache untuk video + pengaturan transkripsi saat ini agar Transcribe menjalankan ulang proses."); self.delete_transcript_btn.clicked.connect(self._delete_transcript_cache); tools.addWidget(self.delete_transcript_btn); l.addLayout(tools)
        self.transcript_table=QTableWidget(0,5); self.transcript_table.setHorizontalHeaderLabels(["Start (MM:SS)","End (MM:SS)","Speaker","Text","Words"]); self.transcript_table.horizontalHeader().setSectionResizeMode(3,QHeaderView.Stretch); self.transcript_table.setEditTriggers(QAbstractItemView.DoubleClicked|QAbstractItemView.EditKeyPressed); l.addWidget(self.transcript_table)
        l.addWidget(self._hint("Transcribe selalu membuat ASR/timestamp mentah dengan Whisper lokal, lalu AI dari Settings menjadi pengolah utama untuk merapikan transcript + subtitle cues. Jika AI tidak tersedia, hasil lokal dipakai otomatis. Hapus Transcript Cache untuk memaksa proses ulang dari awal."))
        b=QHBoxLayout(); s=QPushButton("Save SRT"); s.clicked.connect(self._save_srt); a=QPushButton("Save JSON"); a.clicked.connect(self._save_transcript); sp=QPushButton("Estimate Speakers"); sp.clicked.connect(self._estimate_speakers); polish=QPushButton("✨ AI Polish Transcript"); polish.clicked.connect(self._ai_polish_transcript); b.addWidget(s); b.addWidget(a); b.addWidget(sp); b.addWidget(polish); b.addStretch(); l.addLayout(b)
        self.bottom.addTab(w,"📝 Transcript")

    def _build_highlight_tab(self):
        w=QWidget(); l=QVBoxLayout(w)
        l.addWidget(self._hint("Viral Analyzer membaca seluruh timeline transcript dari 00:00 sampai durasi terakhir. Tanpa AI = heuristic lokal per segmen. Dengan AI = analisis transcript bertahap agar video panjang tidak terpotong hanya pada bagian awal, lalu kandidat dapat diverifikasi secara visual."))
        l.addWidget(self._hint("Contoh hasil: 02:14–02:44 • Score 88/100 • Hook: pertanyaan mengejutkan • Alasan: hook kuat + fakta spesifik + konteks pendukung."))
        row=QHBoxLayout(); self.analyze_btn=QPushButton("🔥 Analisis Viral dari Transcript"); self.analyze_btn.setObjectName("primary"); self.analyze_btn.clicked.connect(self._analyze); row.addWidget(self.analyze_btn)
        self.preview_highlight_btn=QPushButton("▶ Preview Highlight"); self.preview_highlight_btn.setObjectName("ghost"); self.preview_highlight_btn.clicked.connect(self._preview_selected_highlight); row.addWidget(self.preview_highlight_btn)
        self.export_highlight_btn=QPushButton("🚀 Export Highlight Terpilih"); self.export_highlight_btn.setObjectName("primary"); self.export_highlight_btn.clicked.connect(self._export_selected_highlight); row.addWidget(self.export_highlight_btn)
        self.use_highlights_btn=QPushButton("✂ Semua → Clip Queue"); self.use_highlights_btn.setObjectName("ghost"); self.use_highlights_btn.clicked.connect(self._use_all_highlights); row.addWidget(self.use_highlights_btn); row.addStretch(); l.addLayout(row)
        self.highlight_list=QListWidget(); self.highlight_list.currentItemChanged.connect(lambda cur,prev:self._preview_selected_highlight()); self.highlight_list.itemDoubleClicked.connect(self._use_highlight); l.addWidget(self.highlight_list)
        self.bottom.addTab(w,"🔥 Viral Analyzer")

    def _build_clip_queue_tab(self):
        w=QWidget(); l=QVBoxLayout(w)
        l.addWidget(self._hint("Klik baris untuk melihat Start clip. Tombol Preview Clip memutar bagian tersebut langsung di LIVE PREVIEW."))
        self.clip_table=QTableWidget(0,6); self.clip_table.setHorizontalHeaderLabels(["#","Start","End","Durasi","Title","Status"]); self.clip_table.horizontalHeader().setSectionResizeMode(4,QHeaderView.Stretch); self.clip_table.itemSelectionChanged.connect(self._select_clip); l.addWidget(self.clip_table)
        b=QHBoxLayout(); self.preview_clip_btn=QPushButton("▶ Preview Clip"); self.preview_clip_btn.setObjectName("ghost"); self.preview_clip_btn.clicked.connect(self._preview_selected_clip); b.addWidget(self.preview_clip_btn); self.export_btn=QPushButton("🚀 Export Selected"); self.export_btn.setObjectName("primary"); self.export_btn.clicked.connect(self._export_selected); b.addWidget(self.export_btn); ea=QPushButton("Export All"); ea.setObjectName("ghost"); ea.clicked.connect(self._export_all); b.addWidget(ea); ca=QPushButton("Cancel Export"); ca.setObjectName("danger"); ca.clicked.connect(self._cancel_export); b.addWidget(ca); l.addLayout(b); self._clip_queue_tab=w; self.bottom.addTab(w,"✂ Clip Queue")

    def _build_caption_tab(self):
        w=QWidget(); l=QVBoxLayout(w)
        l.addWidget(self._hint("AI Content Pack menghasilkan beberapa opsi judul, caption, deskripsi pendek + panjang seperti YouTube, hashtag, keyword SEO, pinned comment, dan teks thumbnail. Hasil tetap berbasis transcript/Viral Analyzer dan tidak boleh mengarang fakta."))
        row=QHBoxLayout(); self.caption_generate_btn=QPushButton("✨ Generate AI Content Pack"); self.caption_generate_btn.setObjectName("primary"); self.caption_generate_btn.clicked.connect(self._generate_captions); row.addWidget(self.caption_generate_btn)
        self.caption_copy_btn=QPushButton("📋 Salin Semua"); self.caption_copy_btn.setObjectName("ghost"); self.caption_copy_btn.clicked.connect(self._copy_caption); row.addWidget(self.caption_copy_btn); row.addStretch(); l.addLayout(row)
        self.caption_status=self._hint("Belum ada content pack. Alur: Transcript → Viral Analyzer → Generate AI Content Pack."); l.addWidget(self.caption_status)
        self.caption_text=QTextEdit(); self.caption_text.setPlaceholderText("JUDUL OPSI\n\nDESKRIPSI PANJANG\n\nCAPTION\n\nHASHTAG\n\nKEYWORD SEO\n\nPINNED COMMENT\n\nTHUMBNAIL TEXT"); l.addWidget(self.caption_text)
        self.bottom.addTab(w,"📣 Captions")

    def _build_publish_tab(self):
        w=QWidget(); l=QVBoxLayout(w); form=QFormLayout(); self.pub_platform=ScrollSafeComboBox(); self.pub_platform.addItems(["YouTube Shorts","TikTok","Instagram Reels","YouTube"]); form.addRow("Platform",self.pub_platform); self.pub_title=QLineEdit(); form.addRow("Title",self.pub_title); self.pub_schedule=QLineEdit(); self.pub_schedule.setPlaceholderText("YYYY-MM-DD HH:MM (opsional)"); form.addRow("Schedule",self.pub_schedule); l.addLayout(form)
        l.addWidget(self._hint("Publish Queue saat ini adalah staging/scheduling lokal: menyimpan file + platform + caption + waktu. Ini BELUM mengunggah otomatis ke TikTok/Instagram/YouTube karena kredensial/API platform belum terhubung. Jadi Schedule = metadata jadwal, bukan jaminan auto-publish."))
        b=QPushButton("＋ Tambah ke Publish Queue"); b.setObjectName("primary"); b.clicked.connect(self._schedule_publish); l.addWidget(b); self.publish_list=QListWidget(); l.addWidget(self.publish_list); r=QPushButton("↻ Refresh Queue"); r.clicked.connect(self._refresh_publish); l.addWidget(r); self.bottom.addTab(w,"📤 Publish")
        self._refresh_publish()

    def _build_analytics_tab(self):
        w=QWidget(); l=QVBoxLayout(w)
        l.addWidget(self._hint("Analytics lokal menyimpan performa clip yang kamu masukkan sendiri. Chopster tidak mengklaim terhubung ke TikTok/Instagram/YouTube hanya karena ada Publish Queue. Masukkan views/likes/comments/shares/retention untuk melihat ringkasan dan pola performa."))
        self.analytics_label=QLabel(); self.analytics_label.setWordWrap(True); l.addWidget(self.analytics_label)
        self.analytics_table=QTableWidget(0,7); self.analytics_table.setHorizontalHeaderLabels(["Clip","Platform","Views","Likes","Comments","Shares","Retention"]); self.analytics_table.horizontalHeader().setSectionResizeMode(0,QHeaderView.Stretch); l.addWidget(self.analytics_table)
        row=QHBoxLayout(); add=QPushButton("＋ Catat Performa"); add.setObjectName("primary"); add.clicked.connect(self._record_analytics_dialog); row.addWidget(add); refresh=QPushButton("↻ Refresh"); refresh.clicked.connect(self._refresh_analytics); row.addWidget(refresh); clear=QPushButton("Bersihkan Data"); clear.setObjectName("danger"); clear.clicked.connect(self._clear_analytics); row.addWidget(clear); row.addStretch(); l.addLayout(row)
        self.bottom.addTab(w,"📊 Analytics"); self._refresh_analytics()

    # -------------------------------------------------------------- media
    def _import_video(self):
        path,_=QFileDialog.getOpenFileName(self,"Pilih video","","Video (*.mp4 *.mov *.mkv *.webm *.avi *.m4v *.flv *.wmv)")
        if path:self._set_source(path)

    def _set_source(self,path):
        try:
            meta=probe(path); self.media=meta
            # Reuse the persistent player instead of destroying/recreating it.
            try:
                self.video_player.stop()
                self.video_player.setSource(QUrl())
            except Exception:
                pass
            self.project=create_project(Path(path).stem,path,{"duration":meta.duration,"width":meta.width,"height":meta.height,"fps":meta.fps,"has_audio":meta.has_audio})
            # A fresh import starts a fresh editing state. Never leak transcript,
            # highlights, scenes or subtitles from the previously opened video.
            self.transcript=None; self.candidates=[]; self.clips=[]; self.focus_points=[]; self._scene_segments=[]; self._scene_preview_mode=False; self._preview_quick_focus=[]; self._subtitle_ready=False; self._subtitle_preview_time=None
            if hasattr(self,"transcript_table"): self.transcript_table.setRowCount(0)
            if hasattr(self,"clip_table"): self.clip_table.setRowCount(0)
            if hasattr(self,"highlight_list"): self.highlight_list.clear()
            self._last_frame_image=None; self._last_frame_clock=0.0; self._preview_current_sec=0.0; self._pending_seek_sec=0.0
            self.project_label.setText(self.project.name); self.stat_video.set_value(format_duration(meta.duration)); self.status.setText(f"Loaded {meta.width}×{meta.height} • {format_duration(meta.duration)}")
            self.video_player.setSource(QUrl.fromLocalFile(str(Path(path).resolve())))
            self._last_frame_image=None; self._last_frame_clock=0.0; self._preview_current_sec=0.0
            self.video.setPixmap(QPixmap()); self.video.setText("Memuat video…")
            self.preview_status.setText("Memuat video…")
            self.preview_status.setStyleSheet(f"color:{self.C['GOLD']};font-weight:700;")
            self._resize_preview(); self._save_project(); self._log(f"Media: {path}")
        except Exception as e: QMessageBox.critical(self,"Import gagal",str(e))

    def _restore_latest(self):
        try:
            projs=list_projects()
            if projs and projs[0].source_path and Path(projs[0].source_path).exists(): self._load_project_obj(projs[0])
        except Exception: pass

    def _new_project(self):
        try:
            self.video_player.stop()
            self.video_player.setSource(QUrl())
        except Exception:
            pass
        self.project=None; self.media=None; self.transcript=None; self.candidates=[]; self.clips=[]; self.focus_points=[]; self._scene_segments=[]; self._scene_preview_mode=False; self._preview_quick_focus=[]; self._subtitle_ready=False; self._subtitle_preview_time=None; self._last_frame_image=None
        self.video.clear(); self.video.setText("Import video lalu klik Play untuk melihat preview.")
        self.transcript_table.setRowCount(0); self.clip_table.setRowCount(0); self.highlight_list.clear(); self.subtitle_overlay.hide(); self.watermark_overlay.hide(); self.project_label.setText("Belum ada project"); self.status.setText("Project baru — import video.")

    def _open_project(self):
        items=list_projects()
        if not items: QMessageBox.information(self,"Project","Belum ada project tersimpan."); return
        names=[f"{p.name} — {p.updated_at}" for p in items]
        dlg=QDialog(self); dlg.setWindowTitle("Buka Project"); ll=QVBoxLayout(dlg); lb=QListWidget(); lb.addItems(names); ll.addWidget(lb); bb=QDialogButtonBox(QDialogButtonBox.Open|QDialogButtonBox.Cancel); bb.accepted.connect(dlg.accept); bb.rejected.connect(dlg.reject); ll.addWidget(bb)
        if dlg.exec() and lb.currentRow()>=0:self._load_project_obj(items[lb.currentRow()])

    def _load_project_obj(self,p):
        self.project=p
        if not Path(p.source_path).exists():
            QMessageBox.warning(self,"Source hilang",f"Video tidak ditemukan:\n{p.source_path}"); return
        self._set_source_without_project(p.source_path)
        extra=p.extra or {}; self.clips=list(extra.get("clips",[])); self.focus_points=list(extra.get("focus_points",[])); self._scene_segments=list(extra.get("scene_segments",[]) or []); self._scene_preview_mode=bool(self._scene_segments); self.candidates=list(extra.get("candidates",[]))
        settings=extra.get("settings") or {}
        try:
            if settings.get("aspect") in [self.aspect.itemText(i) for i in range(self.aspect.count())]: self.aspect.setCurrentText(settings["aspect"])
            if settings.get("reframe") in [self.reframe.itemText(i) for i in range(self.reframe.count())]: self.reframe.setCurrentText(settings["reframe"])
            stored_style=str(settings.get("clipper_caption_style") or settings.get("subtitle") or "viral_pop")
            legacy_style_map={"Bold Creator":"viral_pop","Yellow Punch":"viral_pop","Clean Podcast":"clean_minimal","Minimal White":"clean_minimal","Cyan Creator":"electric_cyan","Gold Highlight":"golden_aura","Karaoke Gold":"viral_pop"}
            stored_style=legacy_style_map.get(stored_style,stored_style)
            if stored_style in ACS_WORD_STYLES: self.sub_preset.setCurrentText(stored_style)
            if settings.get("subtitle_font") is not None:
                font_name=str(settings.get("subtitle_font"))
                self.sub_font.setCurrentText(font_name if font_name in ACS_CAPTION_FONTS else "Outfit")
            size_name=str(settings.get("clipper_caption_size") or "").lower()
            if size_name not in {"small","medium","big"}:
                old_size=int(settings.get("subtitle_font_size") or 34)
                size_name="small" if old_size<=28 else ("big" if old_size>=52 else "medium")
            self._set_subtitle_size(size_name,persist=False)
            case_name=str(settings.get("clipper_caption_text_case") or settings.get("subtitle_text_case") or "uppercase").lower()
            self._set_subtitle_text_case(case_name if case_name in {"uppercase","capitalize","lowercase"} else "uppercase",persist=False)
            position_mode=str(settings.get("clipper_caption_position_mode") or settings.get("subtitle_position_mode") or ("center" if settings.get("subtitle_alignment_name")=="Tengah" else "bottom")).lower()
            self.sub_bottom_percent=int(settings.get("clipper_caption_bottom_percent") or settings.get("subtitle_y_percent") or 21)
            self.sub_center_percent=int(settings.get("clipper_caption_center_percent") or settings.get("subtitle_center_y_percent") or 50)
            self._set_subtitle_position(position_mode if position_mode in {"bottom","center"} else "bottom",persist=False)
            self._sync_subtitle_preset_cards(); self._sync_subtitle_font_cards()
            if settings.get("clip_burn_subtitle") is not None: self.sub_burn.setChecked(bool(settings.get("clip_burn_subtitle")))
            if settings.get("watermark") is not None: self.wm_text.setText(str(settings.get("watermark") or ""))
            if settings.get("watermark_image") is not None: self.wm_image.setText(str(settings.get("watermark_image") or ""))
            if settings.get("watermark_position") in [self.wm_pos.itemText(i) for i in range(self.wm_pos.count())]: self.wm_pos.setCurrentText(settings["watermark_position"])
            if settings.get("watermark_type") in [self.wm_type.itemText(i) for i in range(self.wm_type.count())]: self.wm_type.setCurrentText(settings["watermark_type"])
            if settings.get("watermark_opacity") is not None: self.wm_op.setValue(float(settings["watermark_opacity"]))
            if settings.get("watermark_size") is not None: self.wm_size.setValue(int(settings["watermark_size"]))
            if settings.get("watermark_image_scale_percent") is not None: self.wm_img_size.setValue(max(1,min(100,int(settings["watermark_image_scale_percent"]))))
            if settings.get("watermark_enabled") is not None: self.wm_enable.setChecked(bool(settings["watermark_enabled"]))
            self._refresh_preview_canvas(); self._refresh_preview_overlay()
        except Exception: pass
        tr=extra.get("transcript")
        if tr:
            from chopster.clipper.transcript_manager import Transcript
            self.transcript=Transcript.from_dict(tr); self._render_transcript(); self._render_highlights()
        self._subtitle_ready=bool((self.project_dir()/"subtitles"/"subtitles.ass").exists())
        if self._subtitle_ready:
            self.sub_burn.setChecked(True)
        self._refresh_preview_overlay()
        self._render_clips(); self.project_label.setText(p.name); self.status.setText("Project dipulihkan — pengaturan preview ikut dipulihkan.")

    def _set_source_without_project(self,path):
        self.media=probe(path)
        try:
            self.video_player.stop()
            self.video_player.setSource(QUrl())
        except Exception:
            pass
        self.video_player.setSource(QUrl.fromLocalFile(str(Path(path).resolve())))
        self._last_frame_image=None; self._last_frame_clock=0.0; self._preview_current_sec=0.0; self._pending_seek_sec=0.0
        self.video.setPixmap(QPixmap()); self.video.setText("Memuat video…")
        self.stat_video.set_value(format_duration(self.media.duration)); self.preview_status.setText("Memuat video…"); self._resize_preview()

    def _save_project(self):
        if not self.project:return
        try:
            self._sync_transcript_from_table()
            self.project.clips=[ClipEntry(index=i+1,start=float(c["start"]),end=float(c["end"]),title=c.get("title",f"Clip {i+1}"),status=c.get("status","planned"),output=c.get("output",""),selected=bool(c.get("selected",True))) for i,c in enumerate(self.clips)]
            self.project.extra.update({"clips":self.clips,"candidates":[getattr(c,"__dict__",c) for c in self.candidates],"focus_points":self.focus_points,"scene_segments":self._scene_segments,"transcript":self.transcript.to_dict() if self.transcript else None,"settings":self._settings_snapshot(),"captions":self._captions_data()})
            save_project(self.project); self.app.config.set("clip_output_dir",self.output_edit.text().strip()); self.status.setText("Project tersimpan.")
        except Exception as e:self._log(f"Save project error: {e}")

    def _settings_snapshot(self):
        return {
            "aspect":self.aspect.currentText(),"reframe":self.reframe.currentText(),"quality":self.quality.currentText(),
            "subtitle":self.sub_preset.currentText(),"clipper_caption_style":self.sub_preset.currentText(),
            "subtitle_font":self.sub_font.currentText(),"clipper_caption_font":self.sub_font.currentText(),
            "subtitle_font_size":ACS_CAPTION_SIZES.get(self.sub_size_preset,78),"clipper_caption_size":self.sub_size_preset,
            "subtitle_text_case":self.sub_text_case,"clipper_caption_text_case":self.sub_text_case,
            "subtitle_mode":"word","subtitle_animation":"word_highlight","subtitle_max_words":3,
            "subtitle_position_mode":self.sub_position_mode,"clipper_caption_position_mode":self.sub_position_mode,
            "subtitle_y_percent":self.sub_bottom_percent,"clipper_caption_bottom_percent":self.sub_bottom_percent,
            "subtitle_center_y_percent":self.sub_center_percent,"clipper_caption_center_percent":self.sub_center_percent,
            "subtitle_alignment_name":"Tengah" if self.sub_position_mode=="center" else "Bawah tengah",
            "clip_burn_subtitle":self.sub_burn.isChecked(),
            "watermark":self.wm_text.text(),"watermark_image":self.wm_image.text(),"watermark_enabled":self.wm_enable.isChecked(),
            "watermark_type":self.wm_type.currentText(),"watermark_opacity":self.wm_op.value(),"watermark_position":self.wm_pos.currentText(),
            "watermark_size":self.wm_size.value(),"watermark_image_scale_percent":self.wm_img_size.value(),
        }
    def _captions_data(self): return {"text":self.caption_text.toPlainText()}

    def _pick_output(self):
        p=QFileDialog.getExistingDirectory(self,"Pilih folder export",self.output_edit.text() or str(Path.home()))
        if p:self.output_edit.setText(p); self.app.config.set("clip_output_dir",p)
    def _pick_watermark(self):
        p,_=QFileDialog.getOpenFileName(self,"Pilih watermark","","Images (*.png *.jpg *.jpeg *.webp)")
        if p:
            self.wm_image.setText(p); self.wm_type.setCurrentText("Image"); self.wm_enable.setChecked(True); self._on_branding_changed()
            self.status.setText("Watermark dipilih — preview diperbarui sekarang.")

    # -------------------------------------------------------------- tasks
    def _submit(self,tid,name,fn,callback):
        if self.app.tasks.is_active(tid): self.status.setText(f"{name} masih berjalan."); return False
        self._task_callbacks[tid]=callback
        self.app.tasks.submit(tid,name,fn)
        self.stat_status.set_value("Running"); self.progress.setValue(0)
        if tid=="clipper:transcribe":
            self.transcribe_btn.setEnabled(False); self.transcribe_cancel_btn.setEnabled(True)
        if tid=="clipper:analyze" and hasattr(self,"analyze_btn"): self.analyze_btn.setEnabled(False)
        if tid=="clipper:scene" and hasattr(self,"scene_btn"): self.scene_btn.setEnabled(False)
        return True
    def _task_progress(self,tid,pct,msg):
        if tid.startswith("clipper:"):
            self.progress.setValue(int(pct)); self.status.setText(msg)
            if tid == "clipper:scene":
                # Surface newly detected cuts in the LIVE PREVIEW while FFmpeg scans.
                # Throttled so a rapid stream of showinfo lines does not spam seeks.
                try:
                    import re as _re, time as _time
                    m=_re.search(r"di ([0-9]+(?:\.[0-9]+)?)s", str(msg or ""))
                    if m:
                        sec=float(m.group(1)); now=_time.monotonic()
                        if self._last_scene_preview_progress < 0 or abs(sec-self._last_scene_preview_progress) >= 0.8:
                            self._last_scene_preview_progress=sec
                            self._seek_to(sec)
                            self.preview_status.setText(f"Scene candidate • {format_timecode(sec)}")
                            self._show_preview_badge(f"Scene candidate • {format_timecode(sec)}")
                except Exception:
                    pass
    def _task_result(self,tid,result):
        cb=self._task_callbacks.pop(tid,None)
        if tid=="clipper:transcribe":
            self.transcribe_btn.setEnabled(True); self.transcribe_cancel_btn.setEnabled(False)
        if tid=="clipper:scene" and hasattr(self,"scene_btn"): self.scene_btn.setEnabled(True)
        if tid=="clipper:analyze" and hasattr(self,"analyze_btn"): self.analyze_btn.setEnabled(True)
        if cb:
            try: cb(result)
            except Exception as e:self._log(f"ERROR: Callback: {e}")
        self.stat_status.set_value("Siap")
    def _task_failed(self,tid,error):
        if tid=="clipper:transcribe":
            self.transcribe_btn.setEnabled(True); self.transcribe_cancel_btn.setEnabled(False)
        if tid=="clipper:scene" and hasattr(self,"scene_btn"): self.scene_btn.setEnabled(True)
        if tid=="clipper:analyze" and hasattr(self,"analyze_btn"): self.analyze_btn.setEnabled(True)
        cb=self._task_errors.pop(tid,None)
        self._task_callbacks.pop(tid,None)
        if tid=="clipper:export": self.export_btn.setEnabled(True)
        self._last_error=error; self.stat_status.set_value("Gagal"); self.status.setText(f"Gagal: {error[:160]}"); self._log(f"ERROR: {error}")
    def _cancel_tasks(self): self.app.tasks.cancel_all("clipper:"); self.status.setText("Membatalkan...")

    # -------------------------------------------------------------- transcript
    def _delete_transcript_cache(self):
        if not self.project or not self.media:
            QMessageBox.information(self, "Hapus Transcript", "Import video terlebih dahulu.")
            return
        path=self.project.source_path; model=self.model.currentText(); lang=self.lang.currentText(); wt=self.word_ts.isChecked()
        try:
            deleted_cache=delete_cached_transcript(path,model,lang,wt)
            artifact=self.project_dir()/"analysis"/"transcript.json"
            if artifact.exists(): artifact.unlink()
            transcript_file=self.project_dir()/"transcripts"/"transcript.json"
            if transcript_file.exists(): transcript_file.unlink()
            # Subtitle files depend directly on transcript content; remove only generated transcript-derived files.
            for f in (self.project_dir()/"subtitles"/"subtitles.srt", self.project_dir()/"subtitles"/"subtitles.ass"):
                if f.exists(): f.unlink()
            if self.project:
                self.project.extra["transcript"] = None
            self.transcript=None; self._subtitle_ready=False; self._subtitle_preview_time=None
            self.transcript_table.setRowCount(0)
            self._save_project()
            self._refresh_preview_overlay()
            self.sub_info.setText("Transcript cache dihapus. Klik Transcribe untuk menjalankan ulang.")
            self.status.setText("Transcript cache dihapus — transkripsi lama tidak akan dipakai lagi.")
            self.progress.setValue(0)
            return deleted_cache
        except Exception as exc:
            self._log(f"ERROR: Hapus transcript cache: {exc}")
            QMessageBox.critical(self,"Hapus Transcript",f"Gagal menghapus cache transcript: {exc}")
            return False

    def _transcribe(self):
        if not self.project or not self.media:
            QMessageBox.information(self,"Transcribe","Import video terlebih dahulu.")
            return
        from chopster.clipper.transcript_manager import Transcript
        model=self.model.currentText(); lang=self.lang.currentText(); wt=self.word_ts.isChecked(); path=self.project.source_path
        cfg=self._ai_config(); max_words=int(self.sub_max.value()); prep_sig=transcript_prepare_signature(cfg,max_words)

        def source_label(src: str) -> str:
            return "AI Settings" if src == "settings-ai" else ("AI Settings + Local fallback" if src == "mixed" else "Local fallback")

        project_cached=load_artifact(self.project_dir(),"transcript",path)
        cached=load_cached_transcript(path,model,lang,wt)
        if project_cached:
            project_tr=Transcript.from_dict(project_cached)
            if str(getattr(project_tr, "prepared_signature", "") or "") == prep_sig:
                self.transcript=project_tr; self._render_transcript(); self.progress.setValue(100); self.status.setText(f"Transcript dari project cache — {len(self.transcript.segments)} segmen."); return
            cached_base = cached if cached is not None else project_tr
            self.status.setText("Transcript cache ditemukan — merapikan ulang dengan AI Settings di background.")
            def worker_cached(*,signals,cancel_check):
                signals.progress.emit("clipper:transcribe",18,"Transcript cache ditemukan — merapikan ulang dengan AI Settings…")
                prepared, source = prepare_transcript_and_subtitles(cached_base, cfg, max_words_per_line=max_words)
                if cancel_check(): raise RuntimeError("Dibatalkan")
                signals.progress.emit("clipper:transcribe",96,"Transcript cache siap — menyimpan transcript + subtitle cues…")
                return {"prepared": prepared.to_dict(), "source": source}
            def done_cached(data):
                prepared=Transcript.from_dict(data["prepared"])
                self.transcript=prepared
                save_artifact(self.project_dir(),"transcript",prepared.to_dict(),path)
                self._render_transcript(); self.stat_video.set_value(format_duration(self.media.duration)); self._save_project()
                src=str(data.get("source") or "local")
                self.sub_info.setText(f"✓ Transcript selesai • {len(prepared.segments)} segmen • subtitle cues {len(getattr(prepared,'subtitle_cues',[]) or [])} • sumber: {src}")
                self.status.setText(f"Transkripsi siap dari cache: {len(prepared.segments)} segmen — {source_label(src)}.")
            self._submit("clipper:transcribe","Transcription",worker_cached,done_cached)
            return
        if cached:
            self.status.setText("Transcript cache ditemukan — menyusun subtitle cues sesuai setting saat ini.")
            def worker_global(*,signals,cancel_check):
                signals.progress.emit("clipper:transcribe",18,"Transcript global cache ditemukan — menyiapkan subtitle cues…")
                prepared, source = prepare_transcript_and_subtitles(cached, cfg, max_words_per_line=max_words)
                if cancel_check(): raise RuntimeError("Dibatalkan")
                signals.progress.emit("clipper:transcribe",96,"Transcript cache siap — menyimpan transcript + subtitle cues…")
                return {"prepared": prepared.to_dict(), "source": source}
            def done_global(data):
                prepared=Transcript.from_dict(data["prepared"])
                self.transcript=prepared
                save_artifact(self.project_dir(),"transcript",prepared.to_dict(),path)
                self._render_transcript(); self.progress.setValue(100); self.stat_video.set_value(format_duration(self.media.duration)); self._save_project()
                src=str(data.get("source") or "local")
                self.sub_info.setText(f"✓ Transcript selesai • {len(prepared.segments)} segmen • subtitle cues {len(getattr(prepared,'subtitle_cues',[]) or [])} • sumber: {src}")
                self.status.setText(f"Transkripsi siap dari cache: {len(prepared.segments)} segmen — {source_label(src)}.")
            self._submit("clipper:transcribe","Transcription",worker_global,done_global)
            return
        self.status.setText(f"Transcribe: {model} — analisis berjalan di background dan hasil mentah akan dicache.")
        def worker(*,signals,cancel_check):
            def prog(p,m):
                if cancel_check(): raise RuntimeError("Dibatalkan")
                signals.progress.emit("clipper:transcribe",int(p),m)
            raw=transcribe(path,model=model,language=lang,device=str(self.app.config.get("transcribe_device") or "auto"),word_timestamps=wt,progress_cb=prog)
            if cancel_check(): raise RuntimeError("Dibatalkan")
            signals.progress.emit("clipper:transcribe",92,"AI Transcript: merapikan hasil dan menyusun cue subtitle…")
            prepared, source = prepare_transcript_and_subtitles(raw, cfg, max_words_per_line=max_words)
            if cancel_check(): raise RuntimeError("Dibatalkan")
            signals.progress.emit("clipper:transcribe",97,"Transkripsi siap — menyimpan hasil transcript + subtitle cues…")
            return {"raw": raw.to_dict(), "prepared": prepared.to_dict(), "source": source}
        def done(data):
            raw=Transcript.from_dict(data["raw"])
            prepared=Transcript.from_dict(data["prepared"])
            self.transcript=prepared
            save_cached_transcript(path,model,lang,wt,raw)
            save_artifact(self.project_dir(),"transcript",prepared.to_dict(),path)
            self._render_transcript(); self.stat_video.set_value(format_duration(self.media.duration)); self._save_project()
            src=str(data.get("source") or "local")
            self.sub_info.setText(f"✓ Transcript selesai • {len(prepared.segments)} segmen • subtitle cues {len(getattr(prepared,'subtitle_cues',[]) or [])} • sumber: {src}")
            self.status.setText(f"Transkripsi selesai: {len(prepared.segments)} segmen — {source_label(src)}.")
        self._submit("clipper:transcribe","Transcription",worker,done)

    def _master_analysis(self):
        if not self.project or not self.media:
            QMessageBox.information(self,"Master Analysis","Import video terlebih dahulu."); return
        if self.app.tasks.is_active("clipper:master"):
            self.status.setText("Master Analysis masih berjalan."); return
        path=self.project.source_path; model=self.model.currentText(); lang=self.lang.currentText(); wt=self.word_ts.isChecked(); duration=float(self.media.duration)
        mode=self.reframe.currentText() if self.reframe.currentText() in ("Smart","AI Camera Director","Speaker Focus","Two Person") else "Smart"
        cfg=self._ai_config(); cfg["ai_camera_director"] = mode in ("Smart","AI Camera Director","Speaker Focus","Two Person"); cfg["target_aspect"] = self._preview_target_ratio()
        def worker(*,signals,cancel_check):
            return ensure_master_analysis(self.project_dir(),path,model=model,language=lang,word_timestamps=wt,device=str(self.app.config.get("transcribe_device") or "auto"),mode=mode,ai_config=cfg,progress_cb=lambda p,m: signals.progress.emit("clipper:master",p,m),cancel_check=cancel_check,force_camera=False,max_camera_samples=1800,target_aspect=self._preview_target_ratio())
        def done(res):
            if res.get("cancelled"): self.status.setText("Master Analysis dibatalkan."); return
            from chopster.clipper.transcript_manager import Transcript
            self.transcript=Transcript.from_dict(res["transcript"]); self.focus_points=res.get("focus",[]); self._render_transcript(); self._render_highlights(); self._save_project(); self.progress.setValue(100)
            master_obj = res.get("master") if isinstance(res.get("master"), dict) else {}
            adv = master_obj.get("ai_advice", {}) if isinstance(master_obj, dict) else {}
            vis = master_obj.get("visual_review", {}) if isinstance(master_obj, dict) else {}
            self.status.setText(f"Master Analysis selesai — transcript {len(self.transcript.segments)} • {len(res.get('scenes',[]))} scene • camera {len(self.focus_points)} point • AI Text: {adv.get('source','local')} • AI Vision: {vis.get('source','local')}. Semua fitur downstream membaca cache ini.")
        self._submit("clipper:master","Master Analysis",worker,done)

    def _render_transcript(self):
        self.transcript_table.setRowCount(0)
        if not self.transcript:return
        speakers=estimate_speakers(self.transcript)
        for i,s in enumerate(self.transcript.segments):
            r=self.transcript_table.rowCount(); self.transcript_table.insertRow(r)
            vals=[format_timecode(s.start),format_timecode(s.end),str(speakers[i]["speaker"] if i<len(speakers) else 1),s.text,str(len(s.words))]
            for c,v in enumerate(vals): self.transcript_table.setItem(r,c,QTableWidgetItem(v))
        self._refresh_preview_overlay()

    def _sync_transcript_from_table(self):
        if not self.transcript:return
        for r,s in enumerate(self.transcript.segments):
            try:s.start=parse_timecode(self.transcript_table.item(r,0).text()); s.end=parse_timecode(self.transcript_table.item(r,1).text()); s.text=self.transcript_table.item(r,3).text().strip()
            except Exception: pass
        self.transcript.raw_text=" ".join(s.text for s in self.transcript.segments)
        self._acs_words_cache_owner=None; self._acs_words_cache=None

    def _search_transcript(self,q):
        if not self.transcript:return
        q=q.strip().lower()
        for r in range(self.transcript_table.rowCount()): self.transcript_table.setRowHidden(r, bool(q and q not in (self.transcript_table.item(r,3).text() if self.transcript_table.item(r,3) else "").lower()))
    def _save_srt(self):
        if not self.transcript or not self.project:return
        self._sync_transcript_from_table(); p=self.project_dir()/"subtitles"/"subtitles.srt"; transcript_to_srt(self.transcript,p); self._log(f"SRT: {p}")
    def _save_transcript(self):
        if not self.transcript or not self.project:return
        self._sync_transcript_from_table(); p=self.project_dir()/"transcripts"/"transcript.json"; p.write_text(json.dumps(self.transcript.to_dict(),ensure_ascii=False,indent=2),encoding="utf-8"); self._log(f"Transcript: {p}")
    def _estimate_speakers(self):
        if self.transcript:self._render_transcript(); self.status.setText("Speaker turn estimation diperbarui.")
    def _remove_fillers(self):
        if not self.transcript:return
        tr,removed=remove_filler_segments(self.transcript); self.transcript=tr; self._render_transcript(); self._save_project(); self.status.setText(f"Menghapus {len(removed)} filler segment. Review transcript sebelum export.")

    def _ai_polish_transcript(self):
        if not self.transcript:
            QMessageBox.information(self,"AI Transcript","Jalankan Transcribe terlebih dahulu."); return
        self._ai_prepare_subtitles()

    # -------------------------------------------------------------- analyzer/clips
    def _analyze(self):
        if not self.transcript or not self.media:
            QMessageBox.information(self,"Viral Analyzer","Jalankan Transcribe terlebih dahulu agar Analyzer memiliki teks untuk dianalisis.")
            return
        config=self._ai_config(); config.update({"ai_instruction":self.ai_instruction.text().strip(),"source_path":self.project.source_path, "ai_auto_visual": bool(self.app.config.get("ai_auto_visual",True)), "target_aspect": self._preview_target_ratio()})
        try:
            master=load_artifact(self.project_dir(), "master", self.project.source_path) or {}
            if isinstance(master, dict):
                config["master_context"] = {
                    "ai_advice": master.get("ai_advice", {}),
                    "visual_review": master.get("visual_review", {}),
                    "camera_plan": master.get("ai_shots", []),
                    "target_aspect": master.get("target_aspect"),
                }
                config["master_camera_plan"] = master.get("ai_shots", [])
        except Exception:
            pass
        def worker(*,signals,cancel_check):
            signals.progress.emit("clipper:analyze",8,"Analyzer: membaca seluruh transcript dari awal sampai akhir…")
            result=analyze_with_ai(self.transcript,self.media.duration,config)
            signals.progress.emit("clipper:analyze",96,"Analyzer: menyusun dan mengurutkan kandidat…")
            return result
        def done(c): self.candidates=c; self._render_highlights(); self.stat_high.set_value(str(len(c))); self._save_project(); self.status.setText(f"Analyzer selesai: {len(c)} kandidat dari seluruh timeline. Buka Viral Analyzer untuk preview.")
        self._submit("clipper:analyze","Viral Analyzer",worker,done)

    def _render_highlights(self):
        self.highlight_list.clear()
        for n,c in enumerate(self.candidates,1):
            d=c.__dict__ if hasattr(c,"__dict__") else c
            text=(f"🔥 #{n} • Score {int(d.get('score',0))}/100\n"
                  f"⏱ {format_timecode(float(d.get('start',0)))} → {format_timecode(float(d.get('end',0)))} • Durasi {format_timecode(max(0,float(d.get('end',0))-float(d.get('start',0))))}\n"
                  f"Hook: {d.get('hook','')}\n"
                  f"Mengapa berpotensi: {d.get('reason','')}")
            item=QListWidgetItem(text); item.setData(Qt.UserRole,d); self.highlight_list.addItem(item)

    def _preview_selected_highlight(self):
        item=self.highlight_list.currentItem() if hasattr(self,'highlight_list') else None
        if not item:return
        d=item.data(Qt.UserRole) or {}; s=float(d.get('start',0)); e=float(d.get('end',0)); self._seek_to(s)
        label=f"Viral Preview • {format_timecode(s)}–{format_timecode(e)} • Score {int(d.get('score',0))}/100"; self.preview_status.setText(label); self._show_preview_badge(label)
        try:self.video_player.play(); self.play.setText("⏸ Pause")
        except Exception:pass

    def _use_highlight(self,item):
        d=item.data(Qt.UserRole); self._add_clips([(float(d["start"]),float(d["end"]),d.get("title","AI Highlight"))])
    def _use_all_highlights(self):
        rows=[]
        for i in range(self.highlight_list.count()):
            d=self.highlight_list.item(i).data(Qt.UserRole) or {}
            if d: rows.append((float(d.get("start",0)),float(d.get("end",0)),str(d.get("title") or f"Highlight {i+1:03d}")))
        if rows:self._add_clips(rows)

    def _generate_clips(self):
        if not self.media or not self.project:return
        mode=self.clip_mode.currentText(); dur=float(self.media.duration)
        if mode=="Scene Detection":
            self._scene_detect(); return
        try:
            if mode=="Fixed Duration": clips=plan_fixed_duration(dur,parse_timecode(self.clip_duration.text()))
            elif mode=="Fixed Overlap": clips=plan_fixed_overlap(dur,parse_timecode(self.clip_duration.text()),parse_timecode(self.clip_overlap.text()))
            elif mode=="Fixed Interval": clips=plan_fixed_interval(dur,parse_timecode(self.clip_duration.text()),parse_timecode(self.clip_interval.text()))
            else:
                if not self.candidates:
                    self._analyze();
                    return
                clips=plan_from_timestamps(dur,[(float(c.start if hasattr(c,"start") else c["start"]),float(c.end if hasattr(c,"end") else c["end"])) for c in self.candidates[:self.clip_count.value()]])
            self._add_clips([(c.start,c.end,c.title or f"Clip {c.index:03d}") for c in clips]); self.status.setText(f"{len(clips)} clip dibuat — cek Clip Queue.")
        except Exception as e: QMessageBox.warning(self,"Generate clips",str(e))
    def _scene_detect(self):
        if not self.project or not self.media:return
        if self.app.tasks.is_active("clipper:scene"):
            QMessageBox.information(self,"Scene Detect","Scene Detect masih berjalan."); return
        self.status.setText("Scene Detect + Gemini Camera: mencari cut, membaca frame, lalu membuat camera shot plan…"); self.progress.setValue(0)
        cached_scenes=load_artifact(self.project_dir(),"scenes",self.project.source_path)
        def worker(*,signals,cancel_check):
            segments=cached_scenes
            if segments is None:
                def prog(p,m): signals.progress.emit("clipper:scene",min(40,int(p*0.40)),m)
                segments=detect_scenes(self.project.source_path, progress_cb=prog, cancel_check=cancel_check)
                save_artifact(self.project_dir(),"scenes",segments,self.project.source_path)
            else:
                signals.progress.emit("clipper:scene",8,"Scene cache ✓ — melewati scan FFmpeg")
            if cancel_check(): return {"cancelled":True}

            # IMPORTANT: build local person/face evidence before asking Gemini so every
            # Gemini visual decision has real local subject IDs it can target.
            base_focus=[]; camera_evidence={}
            try:
                from chopster.clipper.face_tracking import detect_face_focus
                cfg=self._ai_config(); cfg["target_aspect"]=self._preview_target_ratio(); cfg["ai_camera_director"]=False
                signals.progress.emit("clipper:scene",45,"Person/Face tracking — menyiapkan subject map…")
                tr=self.transcript
                base_focus=detect_face_focus(
                    self.project.source_path,0.0,float(self.media.duration),1.0,1800,
                    mode="AI Camera Director", transcript=tr, ai_config=cfg, evidence_out=camera_evidence,
                    progress_cb=lambda p,m: signals.progress.emit("clipper:scene", min(57, 45 + int(max(0,min(100,p))*0.12)), m),
                )
            except Exception as exc:
                camera_evidence={"timeline":[]}
                base_focus=[]
                signals.progress.emit("clipper:scene",47,f"Local person tracking fallback: {str(exc)[:140]}")
            if cancel_check(): return {"cancelled":True}

            signals.progress.emit("clipper:scene",58,"Gemini Scene Vision — membaca representative frame + subject map…")
            vision={}
            try:
                from chopster.ai.orchestrator import orchestrator_from_config
                cfg=self._ai_config(); cfg["target_aspect"]=self._preview_target_ratio()
                orch=orchestrator_from_config(cfg)
                vision=analyze_scene_visuals(
                    orch,self.project.source_path,segments,
                    target_aspect=self._preview_target_ratio(),max_scenes=24,
                    timeout=min(60,int(cfg.get("ai_timeout") or 45)),
                    camera_timeline=camera_evidence.get("timeline") or [],
                )
            except Exception as exc:
                vision={"source":"local-fallback","scenes":[],"error":str(exc)[:500],"target_aspect":self._preview_target_ratio()}
            save_artifact(self.project_dir(),"scene_vision",vision,self.project.source_path)
            if cancel_check(): return {"cancelled":True}

            # Use the same representative frames for a frame-by-frame Gemini camera
            # decision. This is the step that turns visual AI into actual camera moves
            # even when FFmpeg reports only one continuous scene.
            focus_dicts=[getattr(p,"__dict__",p) for p in (base_focus or [])]
            ai_shots=[]
            try:
                from chopster.ai.orchestrator import orchestrator_from_config
                from chopster.clipper.visual_analyzer import select_visual_timestamps, extract_frame_images
                from chopster.clipper.camera_director import ai_camera_director_visual, apply_ai_shots, stabilize_camera_path
                cfg=self._ai_config(); cfg["target_aspect"]=self._preview_target_ratio(); cfg["ai_camera_director"]=True
                orch=orchestrator_from_config(cfg)
                timeline=camera_evidence.get("timeline") or []
                timestamps=select_visual_timestamps(self.project.source_path, scenes=segments, camera_timeline=timeline, max_frames=12, duration=float(self.media.duration))
                root=None; imgs=[]
                try:
                    root,imgs=extract_frame_images(self.project.source_path,timestamps,max_width=720)
                    signals.progress.emit("clipper:scene",78,f"Gemini Camera Director — {len(imgs)} frame representatif…")
                    ai_shots=ai_camera_director_visual(
                        orch,timeline," ".join(getattr(s,"text","") for s in getattr(tr,"segments",[]) or []),
                        [str(x) for x in imgs],timestamps,visual_context=vision or {},max_tokens=8500,
                    )
                finally:
                    if root:
                        import shutil; shutil.rmtree(root,ignore_errors=True)
                if ai_shots:
                    focus_dicts=apply_ai_shots(
                        focus_dicts,ai_shots,timeline=timeline,
                        source_aspect=float(self.media.width or 16)/max(1.0,float(self.media.height or 9)),
                        target_aspect=self._preview_target_ratio(),
                    )
                    focus_dicts=stabilize_camera_path(focus_dicts,min_shot_duration=1.5,transition_duration=.28,max_speed=.48,max_step=.10)
                else:
                    from chopster.clipper.camera_director import _local_motion_shots
                    local_shots=_local_motion_shots(timeline)
                    if local_shots:
                        focus_dicts=apply_ai_shots(
                            focus_dicts,local_shots,timeline=timeline,
                            source_aspect=float(self.media.width or 16)/max(1.0,float(self.media.height or 9)),
                            target_aspect=self._preview_target_ratio(),
                        )
                        focus_dicts=stabilize_camera_path(focus_dicts,min_shot_duration=2.0,transition_duration=.28,max_speed=.48,max_step=.10)
                        ai_shots=local_shots
            except Exception as exc:
                camera_evidence.setdefault("ai_errors",[]).append(f"Gemini Camera Director: {str(exc)[:350]}")
            save_artifact(self.project_dir(),"camera",{
                "points":focus_dicts,"target_aspect":float(self._preview_target_ratio()),"mode":"AI Camera Director",
                "timeline":camera_evidence.get("timeline") or [],"ai_shots":ai_shots,
                "sample_step":camera_evidence.get("sample_step"),
                "camera_algorithm_version":"8.5.0",
            },self.project.source_path)
            if cancel_check(): return {"cancelled":True}
            signals.progress.emit("clipper:scene",100,"Scene Detect + Gemini Vision + Camera Director selesai")
            return {"segments":segments,"vision":vision,"focus":focus_dicts,"camera_evidence":camera_evidence,"used_cache":cached_scenes is not None,"ai_shots":ai_shots}
        def done(result):
            if isinstance(result,dict) and result.get("cancelled"):
                self.status.setText("Scene Detect dibatalkan."); return
            segments=result.get("segments",[]) if isinstance(result,dict) else result
            vision=result.get("vision",{}) if isinstance(result,dict) else {}
            self._add_clips([(a,b,f"Scene {i+1:03d}") for i,(a,b) in enumerate(segments)])
            self._apply_scene_vision_titles(vision)
            raw_focus=list(result.get("focus",[]) or []) if isinstance(result,dict) else []
            self._scene_segments=list(segments or [])
            self._scene_preview_mode=True
            self.focus_points=self._normalize_scene_adaptive_style(raw_focus,self._scene_segments,self._preview_target_ratio())
            self._preview_quick_focus=[]
            try:
                self.project.extra["scene_segments"]=self._scene_segments
                self.project.extra["focus_points"]=self.focus_points
            except Exception: pass
            self._save_project()
            self._show_scene_preview(segments, source="cache" if result.get("used_cache") else "fresh")
            self._render_preview_frame()
            mode="cache" if result.get("used_cache") else "fresh"
            cam_count=len(self.focus_points)
            ai_shots=len(result.get("ai_shots") or []) if isinstance(result,dict) else 0
            self.status.setText(f"Scene Detect {mode} selesai — {len(segments)} scene • Camera plan {cam_count} point • {ai_shots} AI shot • Gemini Vision: {str(vision.get('source','local'))}. Live Preview kini memakai shot plan.")
        self._submit("clipper:scene","Scene Detection + Gemini Vision",worker,done)

    def _apply_scene_vision_titles(self, vision):
        """Annotate Clip Queue scene titles with Gemini shot information without changing timing."""
        try:
            rows=(vision or {}).get("scenes") or []
            by_scene={int(x.get("scene")):x for x in rows if isinstance(x,dict) and str(x.get("scene","")).isdigit()}
            for i,c in enumerate(self.clips,1):
                info=by_scene.get(i) or {}
                shot=str(info.get("shot_type") or "").strip()
                active=str(info.get("active_subject") or "").strip()
                if shot or active:
                    label=f"Scene {i:03d}"
                    if shot: label+=f" • {shot}"
                    if active: label+=f" • {active[:44]}"
                    c["title"]=label
            self._render_clips()
        except Exception as exc:
            self._log(f"Scene Vision title sync fallback: {exc}")

    def _normalize_scene_adaptive_style(self, points, segments, target_aspect):
        """Keep adaptive-fit presentation consistent within each detected scene.

        A scene can contain several camera targets. We do not want the preview/export
        to flip between plain crop and blurred-context presentation simply because one
        tracker sample crossed the adaptive threshold. If any reliable point in a scene
        needs breathing room at the active aspect ratio (or Gemini explicitly chose
        fit), all points in that scene use the same presentation style. The target
        coordinates still come from the individual point, so speaker changes remain
        intact.
        """
        try:
            from chopster.clipper.camera_director import normalize_points
            pts=[getattr(x,"__dict__",x) for x in (points or [])]
            if not pts or not segments or self.aspect.currentText()=="Original":
                return pts
            source_aspect=float(self.media.width or 16)/max(1.0,float(self.media.height or 9))
            out=[dict(p) for p in pts]
            for si,(ss,ee) in enumerate(segments,1):
                ss=float(ss); ee=float(ee)
                idxs=[i for i,p in enumerate(out) if ss-0.25 <= float(p.get("t",0.0)) <= ee+0.25]
                if not idxs:
                    continue
                adaptive_scene=False
                for i in idxs:
                    p=out[i]
                    if str(p.get("shot",""))=="fit":
                        adaptive_scene=True; break
                    try:
                        d=decide_framing(p,source_aspect=source_aspect,target_aspect=float(target_aspect))
                        if d.mode=="adaptive_fit":
                            adaptive_scene=True; break
                    except Exception:
                        continue
                if not adaptive_scene:
                    continue
                # Borrow missing bounds from the nearest point in the same scene.
                for i in idxs:
                    p=out[i]
                    if not any(p.get(k) is not None for k in ("subject_left","group_left")):
                        near=min((out[j] for j in idxs if any(out[j].get(k) is not None for k in ("subject_left","group_left"))),
                                 key=lambda q: abs(float(q.get("t",0.0))-float(p.get("t",0.0))), default=None)
                        if near:
                            for k in ("subject_left","subject_right","subject_top","subject_bottom","group_left","group_right","group_top","group_bottom"):
                                if p.get(k) is None and near.get(k) is not None:
                                    p[k]=near[k]
                    p["shot"]="fit"
                    p["adaptive_scene"] = si
                    p["adaptive_reason"] = "scene-consistent close-subject presentation"
            return out
        except Exception:
            return [dict(getattr(x,"__dict__",x)) for x in (points or [])]

    def _show_scene_preview(self, segments, source="fresh"):
        """Immediately surface Scene Detect output in the LIVE PREVIEW.

        QMediaPlayer can occasionally seek while paused without emitting a fresh
        video frame immediately, so we kick the decoder briefly and refresh twice.
        This is only used after Scene Detect and never changes camera/framing data.
        """
        try:
            if not segments or not hasattr(self, "clip_table"):
                return
            idx = 0
            self.clip_table.selectRow(idx)
            start, end = float(segments[idx][0]), float(segments[idx][1])
            label=f"Scene 001 • {format_timecode(start)}–{format_timecode(end)}"
            self.preview_status.setText(label)
            self._show_preview_badge(label)
            self._seek_to(start)
            def kick():
                try:
                    player=getattr(self,"video_player",None)
                    if player is None:return
                    was_playing=player.playbackState()==QMediaPlayer.PlayingState
                    if not was_playing:
                        player.play()
                        QTimer.singleShot(140, lambda:(player.pause(), self.play.setText("▶ Play")))
                    self._render_preview_frame(); self._refresh_preview_overlay(start)
                except Exception as exc:
                    self._log(f"ERROR: Scene preview refresh: {exc}")
            QTimer.singleShot(120,kick)
            QTimer.singleShot(420,lambda:self._render_preview_frame())
            # Keep Live Preview on Scene 001 after detection. Other scenes are
            # previewed explicitly by selecting their row in Clip Queue, so the
            # preview does not appear to jump away from the detected scene result.
        except Exception as exc:
            self._log(f"ERROR: Show scene preview: {exc}")

    def _add_clips(self,rows):
        self.clips=[]
        for i,(s,e,t) in enumerate(rows,1): self.clips.append({"index":i,"start":float(s),"end":float(e),"title":str(t),"status":"planned","output":"","selected":True})
        self._render_clips(); self._save_project(); self.progress.setValue(100)
    def _render_clips(self):
        self.clip_table.setRowCount(0)
        for c in self.clips:
            r=self.clip_table.rowCount(); self.clip_table.insertRow(r)
            vals=[str(c["index"]),format_timecode(c["start"]),format_timecode(c["end"]),format_timecode(c["end"]-c["start"]),c.get("title",f"Clip {c['index']:03d}"),c.get("status","planned")]
            for j,v in enumerate(vals): self.clip_table.setItem(r,j,QTableWidgetItem(v))
        self.stat_clips.set_value(str(len(self.clips)))
    def _select_clip(self):
        rows=self.clip_table.selectionModel().selectedRows() if self.clip_table.selectionModel() else []
        if rows:
            idx=rows[0].row(); c=self.clips[idx]; self._seek_to(c["start"]); label="Clip %03d • %s–%s" % (c["index"], format_timecode(c["start"]), format_timecode(c["end"])); self.preview_status.setText(label); self._show_preview_badge(label)
    def _preview_selected_clip(self):
        rows=self.clip_table.selectionModel().selectedRows() if self.clip_table.selectionModel() else []
        if not rows:
            self.status.setText("Pilih satu clip di Clip Queue terlebih dahulu."); return
        c=self.clips[rows[0].row()]; self._seek_to(c["start"])
        try:self.video_player.play(); self.play.setText("⏸ Pause")
        except Exception:pass
        label=f"Preview Clip {c['index']:03d} • {format_timecode(c['start'])}–{format_timecode(c['end'])}"; self.preview_status.setText(label); self._show_preview_badge(label)

    def _sync_clips_from_table(self):
        for r,c in enumerate(self.clips):
            try:c["start"]=parse_timecode(self.clip_table.item(r,1).text()); c["end"]=parse_timecode(self.clip_table.item(r,2).text()); c["title"]=self.clip_table.item(r,4).text()
            except Exception:pass

    # -------------------------------------------------------------- subtitle / branding / reframe
    def _finish_generate_subtitle(self, tr, source_label: str):
        style=self._subtitle_style_from_ui()
        if not style: return
        try:
            self.transcript=tr
            from chopster.auto_clip_studio.engine.video_engine import generate_ass_file
            words=[] if style.caption_style=="none" else self._acs_word_timestamps(self.transcript)
            aspect=self.aspect.currentText()
            if aspect=="Original":
                ratio=(float(self.media.width)/max(1.0,float(self.media.height))) if self.media else 9/16
                aspect="16:9_landscape" if ratio>1.25 else ("1:1" if 0.90<=ratio<=1.10 else "9:16")
            aspect_map={"16:9":"16:9_landscape","16:9_landscape":"16:9_landscape","4:3":"16:9_landscape","1:1":"1:1","9:16":"9:16","3:4":"9:16","4:5":"9:16"}
            aspect=aspect_map.get(aspect, "9:16")
            duration=float(getattr(self.media,"duration",0.0) or max((float(s.end) for s in self.transcript.segments),default=60.0))
            p=self.project_dir()/"subtitles"/"subtitles.ass"
            generate_ass_file(
                words=words, style_preset=style.caption_style, font_name=style.font,
                output_ass_path=str(p), target_aspect_ratio=aspect,
                font_size_preset=style.size_preset, text_case=style.text_case,
                subtitle_y_percent=style.subtitle_bottom_percent,
                subtitle_position_mode=style.subtitle_position_mode,
                subtitle_center_y_percent=style.subtitle_center_percent,
                duration_seconds=max(1.0,duration), skip_title=True, streamer_preset="none",
            )
            self.sub_burn.setChecked(True)
            srt=self.project_dir()/"subtitles"/"subtitles.srt"; transcript_to_srt(self.transcript,srt)
            self._subtitle_ready=True
            if self.transcript.segments:
                first=float(self.transcript.segments[0].start); self._subtitle_preview_time=first; self._seek_to(first)
            self._refresh_preview_overlay(self._subtitle_preview_time)
            cue_count=len(getattr(self.transcript,"subtitle_cues",[]) or [])
            friendly = "AI Settings" if source_label == "settings-ai" else ("AI Settings + Local fallback" if source_label == "mixed" else "Local fallback")
            label=ACS_WORD_STYLES[style.caption_style]["label"]
            self.sub_info.setText(f"✓ Subtitle siap • {label} • {len(words)} kata animasi • {len(self.transcript.segments)} segmen • sumber: {source_label}")
            self.status.setText(f"Subtitle siap dan tampil di LIVE PREVIEW • {friendly}."); self._save_project()
        except Exception as exc:
            self._log(f"ERROR: Generate Subtitle: {exc}"); QMessageBox.critical(self,"Generate Subtitle",str(exc))

    def _ai_prepare_subtitles(self):
        if not self.transcript or not self.project:
            QMessageBox.information(self,"AI Subtitle","Jalankan Transcribe terlebih dahulu."); return
        self._sync_transcript_from_table(); cfg=self._ai_config()
        def worker(*,signals,cancel_check):
            signals.progress.emit("clipper:subtitle_ai",8,"AI Subtitle: merapikan transcript dan menyusun cue…")
            tr, source=prepare_transcript_and_subtitles(self.transcript,cfg,max_words_per_line=int(self.sub_max.value()))
            if cancel_check(): raise RuntimeError("Dibatalkan")
            signals.progress.emit("clipper:subtitle_ai",96,"AI Subtitle: menyimpan hasil transcript + cue…")
            return {"transcript":tr.to_dict(),"source":source}
        def done(data):
            from chopster.clipper.transcript_manager import Transcript
            self._finish_generate_subtitle(Transcript.from_dict(data["transcript"]),str(data.get("source") or "local"))
        self._submit("clipper:subtitle_ai","AI Transcript + Subtitle",worker,done)

    def _generate_subtitle(self, use_ai: bool = True):
        if not self.transcript or not self.project:
            QMessageBox.information(self,"Subtitle","Jalankan Transcribe terlebih dahulu."); return
        self._sync_transcript_from_table()
        if use_ai:
            self._ai_prepare_subtitles()
        else:
            try:
                local_cfg=dict(self._ai_config()); local_cfg.update({"ai_provider":"none","ai_endpoint":"","ai_api_key":"","ai_model":""})
                tr, source=prepare_transcript_and_subtitles(self.transcript,local_cfg,max_words_per_line=int(self.sub_max.value()))
                self._finish_generate_subtitle(tr,source)
            except Exception as exc:
                self._log(f"ERROR: Local subtitle: {exc}"); QMessageBox.critical(self,"Generate Subtitle",str(exc))

    def _detect_faces(self):
        if not self.project or not self.media:
            QMessageBox.information(self,"Smart Reframe","Import video terlebih dahulu."); return
        mode=self.reframe.currentText() if self.reframe.currentText() in ("Smart","AI Camera Director","Speaker Focus","Two Person") else "Smart"
        cfg=self._ai_config(); cfg["ai_camera_director"] = mode in ("Smart","AI Camera Director","Speaker Focus","Two Person"); cfg["target_aspect"] = self._preview_target_ratio()
        self.status.setText("Detecting people/faces — camera locks stable subjects and uses AI only for editorial shot decisions.")
        def worker(*,signals,cancel_check):
            def run():
                return detect_face_focus(self.project.source_path,0,self.media.duration,0.9,1800,mode,self.transcript,cfg)
            pts=run()
            return [p.__dict__ for p in pts]
        def done(pts):
            self.focus_points=pts; self._preview_quick_focus=[]
            try: save_artifact(self.project_dir(),"camera",{"points":pts,"target_aspect":self._preview_target_ratio(),"mode":mode,"camera_algorithm_version":"8.5.0"},self.project.source_path)
            except Exception: pass
            self._save_project(); self._render_preview_frame(); self.status.setText(f"Face / Person Tracking selesai: {len(pts)} locked camera points. Semua aspect ratio sekarang memakai metadata subject-safe.")
        self._submit("clipper:faces","Person + Face Tracking",worker,done)

    # -------------------------------------------------------------- captions/AI extras
    def _ai_config(self):
        c=self.app.config.as_dict()
        c["ai_provider"] = self.app.config.get("ai_provider") or "none"
        c["ai_model"] = self.app.config.get("ai_model") or ""
        c["ai_vision_model"] = self.app.config.get("ai_vision_model") or ""
        c["ai_endpoint"] = self.app.config.get("ai_endpoint") or ""
        c["ai_api_key"] = self.app.config.get("ai_api_key") or ""
        c["ai_profiles"] = self.app.config.get("ai_profiles",[]) or []
        c["ai_auto_model"] = bool(self.app.config.get("ai_auto_model", True))
        c["ai_auto_visual"] = bool(self.app.config.get("ai_auto_visual", True))
        c["ai_timeout"] = int(self.app.config.get("ai_timeout") or 90)
        c["ai_temperature"] = float(self.app.config.get("ai_temperature") or .2)
        c["ai_instruction"] = self.ai_instruction.text().strip() if hasattr(self, "ai_instruction") else ""
        c["source_path"] = str(self.project.source_path) if self.project else ""
        c["target_aspect"] = self._preview_target_ratio() if hasattr(self, "aspect") and self.media else 9/16
        c["ai_camera_director"] = bool(c.get("ai_camera_director", False))
        if self.project and self.project.source_path:
            try:
                master=load_artifact(self.project_dir(), "master", self.project.source_path) or {}
                c["master_context"] = {
                    "ai_advice": master.get("ai_advice", {}) if isinstance(master, dict) else {},
                    "visual_review": master.get("visual_review", {}) if isinstance(master, dict) else {},
                    "camera_plan": master.get("ai_shots", []) if isinstance(master, dict) else [],
                    "target_aspect": master.get("target_aspect") if isinstance(master, dict) else self._preview_target_ratio(),
                }
                c["master_camera_plan"] = master.get("ai_shots", []) if isinstance(master, dict) else []
            except Exception:
                c["master_context"] = {}
                c["master_camera_plan"] = []
        c["speaker_diarization_enabled"] = bool(c.get("speaker_diarization_enabled", False))
        return c
    def _generate_captions(self):
        if not self.transcript:
            QMessageBox.information(self,"Content Pack","Jalankan Transcribe atau Master Analysis terlebih dahulu.")
            return
        context=[]
        for c in (self.candidates or [])[:5]:
            d=c.__dict__ if hasattr(c,"__dict__") else c
            context.append({"start":float(d.get("start",0)),"end":float(d.get("end",0)),"score":int(d.get("score",0)),"title":str(d.get("title", "")),"hook":str(d.get("hook", "")),"reason":str(d.get("reason", "")),"excerpt":str(d.get("excerpt", ""))})
        cfg=self._ai_config(); cfg["highlight_context"]=context
        platform=self.pub_platform.currentText(); language=self.lang.currentText() if self.lang.currentText()!="auto" else "id"
        def worker(*,signals,cancel_check):
            signals.progress.emit("clipper:captions",10,"Content Pack: menyusun title options + description + SEO…")
            return generate_captions(self.transcript,cfg,platform=platform,language=language,count=5,style="hooks")
        def done(d):
            titles=d.get("titles") or ([d.get("title")] if d.get("title") else [])
            caps=d.get("captions") or []
            hashtags=d.get("hashtags") or []
            keywords=d.get("keywords") or []
            lines=["JUDUL OPTIONS"]+[f"{i+1}. {x}" for i,x in enumerate(titles)]
            lines += ["", "DESKRIPSI PENDEK", str(d.get("description_short",d.get("description", "")))]
            lines += ["", "DESKRIPSI PANJANG — YOUTUBE STYLE", str(d.get("description_long",d.get("description", "")))]
            lines += ["", "CAPTION OPTIONS"]+[f"{i+1}. {x}" for i,x in enumerate(caps)]
            lines += ["", "HASHTAG", " ".join(hashtags)]
            lines += ["", "KEYWORD SEO", ", ".join(keywords)]
            lines += ["", "PINNED COMMENT", str(d.get("pinned_comment", ""))]
            lines += ["", "THUMBNAIL TEXT", ", ".join(d.get("thumbnail_text") or [])]
            if context:
                top=context[0]
                lines += ["", "VIRAL REFERENCE", f"{format_timecode(top['start'])}–{format_timecode(top['end'])} • Score {top['score']}/100", f"Hook: {top['hook']}", f"Alasan: {top['reason']}"]
            self.caption_text.setPlainText("\n".join(lines))
            self.pub_title.setText(titles[0] if titles else d.get("title", ""))
            self.caption_status.setText(f"✓ Content Pack selesai • {len(titles)} judul • {len(caps)} caption • deskripsi panjang • {d.get('source','local')}")
            self.status.setText("AI Content Pack selesai — lihat tab Captions.")
            self._save_project()
        self._submit("clipper:captions","AI Content Pack",worker,done)

    def _copy_caption(self):
        try:
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(self.caption_text.toPlainText())
        except Exception: pass
    def _broll(self):
        if not self.transcript:
            QMessageBox.information(self,"B-roll","Jalankan Transcribe terlebih dahulu."); return
        cfg=self._ai_config(); transcript_text=self.transcript.raw_text
        def worker(*,signals,cancel_check):
            from chopster.ai.content_tools import generate_broll_ai
            signals.progress.emit("clipper:broll",15,"B-roll: memilih mode AI/local…")
            try:
                rows=generate_broll_ai(transcript_text,cfg,10)
                if rows:
                    return {"rows":rows,"source":"ai-orchestrated"}
            except Exception as exc:
                return {"rows":broll_suggestions(transcript_text,10),"source":"local-fallback","error":str(exc)[:220]}
            return {"rows":broll_suggestions(transcript_text,10),"source":"local-fallback"}
        def done(res):
            rows=res.get("rows",[])
            self.caption_text.setPlainText("B-ROLL IDEAS\n\n"+"\n".join(f"• {x.get('keyword','B-roll')} — {x.get('suggestion','')}"+(f"\n  Kenapa: {x.get('why')}" if x.get('why') else "") for x in rows))
            self.status.setText(f"B-roll selesai ({res.get('source','local')}).")
        self._submit("clipper:broll","B-roll Ideas",worker,done)
    def _voiceover(self):
        text=self.caption_text.toPlainText().strip()
        if not text and self.transcript:text=self.transcript.raw_text[:1500]
        if not text:
            QMessageBox.information(self,"Voiceover","Masukkan caption atau jalankan transcript terlebih dahulu."); return
        out=self.project_dir()/"exports"/"voiceover.wav" if self.project else Path.home()/"voiceover.wav"
        cfg=self._ai_config()
        def worker(*,signals,cancel_check):
            signals.progress.emit("clipper:voiceover",10,"Voiceover: menyiapkan naskah…")
            script=text
            try:
                from chopster.ai.content_tools import generate_voiceover_script
                script=generate_voiceover_script(text,cfg,language=self.lang.currentText())
                signals.progress.emit("clipper:voiceover",45,"Voiceover: naskah AI siap, membuat audio lokal…")
            except Exception as exc:
                signals.progress.emit("clipper:voiceover",35,f"Voiceover: AI gagal, lanjut lokal ({str(exc)[:100]})")
            if cancel_check(): return str(out)
            generate_voiceover(script,out); signals.progress.emit("clipper:voiceover",100,"Voiceover selesai")
            return str(out)
        def done(p): QMessageBox.information(self,"Voiceover",f"Voiceover dibuat:\n{p}")
        self._submit("clipper:voiceover","Voiceover",worker,done)

    # -------------------------------------------------------------- export
    def _output_dir(self):
        if self.output_edit.text().strip():return Path(self.output_edit.text().strip())
        return self.project_dir()/"exports" if self.project else Path.cwd()/"Chopster_Exports"
    def project_dir(self):
        from chopster.clipper.project_manager import project_dir
        return project_dir(self.project.id)
    def _subtitle_path(self):
        p=self.project_dir()/"subtitles"/"subtitles.ass"
        return p if p.exists() else None
    def _export_selected(self): self._export(False)
    def _export_all(self): self._export(True)
    def _export_selected_highlight(self):
        item=self.highlight_list.currentItem() if hasattr(self,'highlight_list') else None
        if not item:
            QMessageBox.information(self,"Export Highlight","Pilih hasil Viral Analyzer terlebih dahulu."); return
        d=item.data(Qt.UserRole) or {}
        try:
            start=float(d.get("start",0)); end=float(d.get("end",0))
        except Exception:
            QMessageBox.warning(self,"Export Highlight","Timestamp kandidat tidak valid."); return
        title=str(d.get("title") or f"Viral Highlight {start:.0f}s")
        row={"index":1,"start":start,"end":end,"title":title,"status":"planned","output":"","selected":True}
        self._export(False, forced_rows=[row], dialog_title="Export Viral Highlight")
    def _export(self,all_items,forced_rows=None,dialog_title="Export"):
        if not self.project or (not self.clips and forced_rows is None):
            QMessageBox.information(self,"Export","Belum ada clip. Pilih mode clip lalu klik Generate Clips.")
            return
        if self.app.tasks.is_active("clipper:export"):
            QMessageBox.information(self,"Export","Export masih berjalan. Gunakan Cancel Export jika ingin menghentikannya.")
            return
        self._sync_clips_from_table(); self._generate_subtitle(use_ai=False) if self.sub_burn.isChecked() and self.transcript else None
        if forced_rows is not None:
            chosen=[dict(c) for c in forced_rows]
        else:
            chosen=[dict(c) for c in self.clips if all_items or c.get("selected",True)]
            if not all_items:
                rows={r.row() for r in self.clip_table.selectionModel().selectedRows()} if self.clip_table.selectionModel() else set()
                if rows:chosen=[dict(self.clips[r]) for r in sorted(rows)]
        if not chosen:return
        out=self._output_dir(); self.app.config.set("clip_output_dir",str(out)); out.mkdir(parents=True,exist_ok=True)
        focus=self.focus_points if self.reframe.currentText()!="Manual" and self.aspect.currentText()!="Original" else None
        audio_filter={"Voice Clear":"highpass=f=80,lowpass=f=12000,afftdn=nf=-25,loudnorm=I=-16:TP=-1.5:LRA=11","Podcast":"highpass=f=70,lowpass=f=14000,afftdn=nf=-22,acompressor=threshold=-18dB:ratio=3:attack=5:release=80,loudnorm=I=-16:TP=-1.5:LRA=11","Shorts":"highpass=f=80,afftdn=nf=-24,acompressor=threshold=-20dB:ratio=3,loudnorm=I=-14:TP=-1.5:LRA=8"}.get(self.audio_preset.currentText()) if self.audio_enable.isChecked() else None
        burn=str(self._subtitle_path()) if self.sub_burn.isChecked() and self._subtitle_path() else None
        wm_text=self.wm_text.text().strip() if self.wm_enable.isChecked() and self.wm_type.currentText()=="Text" else None
        wm_img=self.wm_image.text().strip() if self.wm_enable.isChecked() and self.wm_type.currentText()=="Image" else None
        # convert focus plan to clip-relative coordinates
        fp=focus
        self.export_btn.setEnabled(False)
        self.status.setText(f"Export dimulai — {len(chosen)} clip ke {out}")
        errors=[]
        def worker(*,signals,cancel_check):
            def prog(i,total,msg):signals.progress.emit("clipper:export",int(i/total*100),msg)
            def clip_cb(idx,title,state):
                total_n=max(1,len(chosen)); completed=max(0,int(idx)-1) if int(idx) > 0 else 0
                pct=min(99,int(completed/total_n*100))
                signals.progress.emit("clipper:export",pct,f"Clip {idx}: {state}" if int(idx)>0 else state)
                if "Gagal:" in state:
                    errors.append(f"Clip {idx} ({title}): {state}")
            ok,fail=export_batch(self.project.source_path,chosen,out,self.quality.currentText(),self.aspect.currentText().lower(),cancel_flag=cancel_check,on_progress=prog,on_clip=clip_cb,parallel=int(self.app.config.get("export_parallel") or 1),burn_subtitle=burn,watermark_text=wm_text,watermark_image=wm_img,watermark_position=self.wm_pos.currentText(),watermark_opacity=self.wm_op.value(),watermark_font_size=self.wm_size.value(),watermark_scale=float(self.wm_img_size.value())/100.0,focus_points=fp,audio_filter=audio_filter)
            return ok,fail,str(out),list(errors)
        def done(res):
            ok,fail,folder,errs=res
            self.export_btn.setEnabled(True)
            for c in self.clips:
                if c in chosen:
                    idx=int(c.get("index",0))
                    title=str(c.get("title") or f"Clip_{idx:03d}")
                    safe="".join(ch if ch.isalnum() or ch in " -_()" else "_" for ch in title)[:60] or f"Clip_{idx:03d}"
                    output_path=Path(folder)/f"Clip_{idx:03d}_{safe}.mp4"
                    if output_path.exists() and output_path.stat().st_size>0:
                        c["status"]="exported"; c["output"]=str(output_path)
                    else:
                        c["status"]="failed"
            self._render_clips(); self._save_project(); self.status.setText(f"Export selesai — {ok} berhasil, {fail} gagal"); self.stat_status.set_value("Selesai")
            detail=("\n\n"+"\n".join(errs[:5])) if errs else ""
            if fail:
                self._log("EXPORT ERROR: "+" | ".join(errs[:10]))
            QMessageBox.information(self,dialog_title,f"Selesai.\nBerhasil: {ok}\nGagal: {fail}\nFolder: {folder}{detail}")
        self._submit("clipper:export","Export Queue",worker,done)
    def _cancel_export(self): self.app.tasks.cancel("clipper:export"); self.status.setText("Membatalkan export...")

    # -------------------------------------------------------------- publish/analytics
    def _schedule_publish(self):
        rows=[c for c in self.clips if c.get("output") and Path(str(c.get("output"))).exists()]
        if not rows:
            rows=[c for c in self.clips if c.get("status") in ("exported","rendered/partial") and Path(str(c.get("output") or "")).exists()]
        if not rows:
            QMessageBox.information(self,"Publish Queue","Export clip terlebih dahulu. Publish Queue hanya menerima file hasil export yang benar-benar ada."); return
        platform=self.pub_platform.currentText(); schedule=self.pub_schedule.text().strip(); title=self.pub_title.text().strip()
        for c in rows:
            add_job(c.get("output"),platform,title or str(c.get("title") or f"Clip {c.get('index',0):03d}"),self.caption_text.toPlainText(),[],schedule)
        self._refresh_publish(); self.status.setText(f"{len(rows)} file ditambahkan ke Publish Queue lokal. Belum di-upload otomatis.")
    def _refresh_publish(self):
        if not hasattr(self,"publish_list"):return
        self.publish_list.clear()
        for j in list_jobs():self.publish_list.addItem(f"{j['status']} • {j['platform']} • {Path(j['file']).name} • {j.get('scheduled_at','')}")
    def _refresh_analytics(self):
        if not hasattr(self,"analytics_label"):return
        data=analytics_summary(); self.analytics_label.setText(f"<b>Ringkasan performa</b> &nbsp; Clips: {data.get('clips',0)} &nbsp; Views: {data.get('views',0):,} &nbsp; Likes: {data.get('likes',0):,} &nbsp; Comments: {data.get('comments',0):,} &nbsp; Shares: {data.get('shares',0):,} &nbsp; Engagement: {data.get('engagement_rate',0):.2f}%")
        if hasattr(self,"analytics_table"):
            from chopster.clipper import analytics as am
            rows=am.load(); self.analytics_table.setRowCount(0)
            for x in rows:
                r=self.analytics_table.rowCount(); self.analytics_table.insertRow(r)
                vals=[Path(str(x.get('clip_id',''))).name or str(x.get('clip_id','')),x.get('platform',''),f"{int(x.get('views',0)):,}",f"{int(x.get('likes',0)):,}",f"{int(x.get('comments',0)):,}",f"{int(x.get('shares',0)):,}",f"{float(x.get('retention',0)):.1f}%"]
                for j,v in enumerate(vals): self.analytics_table.setItem(r,j,QTableWidgetItem(str(v)))

    def _record_analytics_dialog(self):
        rows=self.clips or []
        labels=[f"Clip {int(c.get('index',i+1)):03d} — {c.get('title','')}" for i,c in enumerate(rows)]
        if not labels:
            QMessageBox.information(self,"Analytics","Belum ada clip. Buat/export clip terlebih dahulu."); return
        label,ok=QInputDialog.getItem(self,"Catat Performa","Pilih clip:",labels,0,False)
        if not ok:return
        i=labels.index(label); c=rows[i]; platform,ok=QInputDialog.getItem(self,"Platform","Platform:",["YouTube Shorts","TikTok","Instagram Reels","YouTube"],0,False)
        if not ok:return
        vals=[]
        for name,default in [("Views",0),("Likes",0),("Comments",0),("Shares",0)]:
            v,ok=QInputDialog.getInt(self,"Analytics",f"{name}:",default,0,2_000_000_000,1)
            if not ok:return
            vals.append(v)
        ret,ok=QInputDialog.getDouble(self,"Analytics","Retention (%):",0,0,100,1)
        if not ok:return
        record_analytics(str(c.get('output') or c.get('index')),platform,*vals,retention=ret)
        self._refresh_analytics()

    def _clear_analytics(self):
        from chopster.clipper import analytics as am
        if QMessageBox.question(self,"Bersihkan Analytics","Hapus seluruh data performa lokal?",QMessageBox.Yes|QMessageBox.No,QMessageBox.No)!=QMessageBox.Yes:return
        try:
            if am.PATH.exists(): am.PATH.unlink()
        except Exception as exc: QMessageBox.warning(self,"Analytics",str(exc))
        self._refresh_analytics()

    # -------------------------------------------------------------- preview
    def _on_aspect_changed(self, value):
        self._preview_quick_focus=[]
        if value != "Original" and not self.focus_points:
            self._update_preview_quick_focus()
        if self.focus_points and self._scene_segments and value != "Original":
            self.focus_points=self._normalize_scene_adaptive_style(self.focus_points,self._scene_segments,self._preview_target_ratio())
            try:self.project.extra["focus_points"]=self.focus_points
            except Exception:pass
        self._refresh_preview_canvas(); self._render_preview_frame(); self._refresh_preview_overlay(); self._save_preview_settings(); self.status.setText(f"Canvas {value} diterapkan — subject-safe framing {'aktif' if self.focus_points or self._preview_quick_focus else 'center fallback'}.")
    def _on_reframe_changed(self, value):
        if value in ("Smart","AI Camera Director","Speaker Focus","Two Person") and not self.focus_points:
            self._update_preview_quick_focus()
        self._refresh_preview_canvas(); self._render_preview_frame(); self.status.setText(f"Reframe {value} diterapkan ke LIVE PREVIEW.")
    def _save_preview_settings(self):
        try:self.app.config.set("clip_aspect",self.aspect.currentText()); self.app.config.set("reframe_mode",self.reframe.currentText())
        except Exception:pass
    def _on_branding_changed(self,*_):
        try:
            self.app.config.set("watermark_enabled",self.wm_enable.isChecked()); self.app.config.set("watermark_text",self.wm_text.text()); self.app.config.set("watermark_image",self.wm_image.text()); self.app.config.set("watermark_position",self.wm_pos.currentText()); self.app.config.set("watermark_opacity",self.wm_op.value()); self.app.config.set("clip_watermark_font",self.wm_size.value()); self.app.config.set("watermark_image_scale_percent",self.wm_img_size.value()); self.app.config.set("watermark_scale",self.wm_img_size.value()/100.0)
        except Exception:pass
        self._refresh_preview_overlay()
    def _on_duration_changed(self,ms):
        try:
            fallback=int(float(self.media.duration)*1000) if self.media else 1
            duration=max(1,int(ms or fallback))
            self.seek.setRange(0,duration)
            self.time_label.setText(f"{format_timecode(self._preview_current_sec)} / {format_timecode(duration/1000.0)}")
            self._apply_pending_seek()
        except Exception as exc:
            self._log(f"ERROR: Duration update: {exc}")

    def _on_media_error(self,error,message=""):
        text=str(message or "Media player gagal membuka video.")
        try:
            enum_name=getattr(error,"name",None)
            if enum_name:
                text=f"{enum_name}: {text}"
        except Exception:
            pass
        self._media_error(text)
    def _on_media_status(self,status):
        names={QMediaPlayer.MediaStatus.LoadingMedia:"Memuat video…",QMediaPlayer.MediaStatus.LoadedMedia:"Video siap — tekan Play",QMediaPlayer.MediaStatus.BufferedMedia:"Video siap — tekan Play",QMediaPlayer.MediaStatus.BufferingMedia:"Buffering…",QMediaPlayer.MediaStatus.EndOfMedia:"Selesai — tekan Play untuk ulang",QMediaPlayer.MediaStatus.InvalidMedia:"Media tidak valid",QMediaPlayer.MediaStatus.StalledMedia:"Playback tertahan — coba Play lagi"}
        msg=names.get(status)
        if msg:
            self.preview_status.setText(msg)
            color=self.C["GREEN"] if status in (QMediaPlayer.MediaStatus.LoadedMedia,QMediaPlayer.MediaStatus.BufferedMedia) else self.C["GOLD"]
            self.preview_status.setStyleSheet(f"color:{color};font-weight:700;")
        if status in (QMediaPlayer.MediaStatus.LoadedMedia,QMediaPlayer.MediaStatus.BufferedMedia):
            self._apply_pending_seek()
            QTimer.singleShot(100, self._apply_pending_seek)
            QTimer.singleShot(350, self._apply_pending_seek)
            self.seek.setRange(0,max(1,int((self.media.duration if self.media else 1)*1000)))
            self._refresh_preview_canvas(); self._refresh_preview_overlay()
    def _media_error(self,msg):
        text=str(msg or "Media player gagal membuka video."); self.preview_status.setText("Preview gagal dibuka"); self.preview_status.setStyleSheet(f"color:{self.C['RED']};font-weight:700;"); self.status.setText(f"Import/playback gagal: {text[:180]}"); self._log(f"ERROR: Media preview: {text}")
    def _set_preview_volume(self, value):
        try:
            v=max(0,min(100,int(value)))
            self._preview_volume=v/100.0
            self.audio_output.setVolume(self._preview_volume)
            if v > 0 and self._preview_muted:
                self._preview_muted=False
                self.audio_output.setMuted(False)
            if hasattr(self,"volume_btn"):
                self.volume_btn.setText("🔇" if self._preview_muted or v==0 else "🔊")
                self.volume_btn.setToolTip("Unmute preview" if self._preview_muted else "Mute preview")
            dlg=getattr(self,"_fullscreen_preview",None)
            if dlg is not None and getattr(dlg,"volume_slider",None) is not None and dlg.volume_slider.value()!=v:
                dlg.volume_slider.blockSignals(True); dlg.volume_slider.setValue(v); dlg.volume_slider.blockSignals(False)
        except Exception as exc:
            self._log(f"ERROR: Preview volume level: {exc}")

    def _toggle_preview_mute(self):
        try:
            self._preview_muted = not bool(getattr(self, "_preview_muted", False))
            self.audio_output.setMuted(self._preview_muted)
            if hasattr(self, "volume_btn"):
                self.volume_btn.setText("🔇" if self._preview_muted else "🔊")
                self.volume_btn.setToolTip("Unmute preview" if self._preview_muted else "Mute preview")
            dlg=getattr(self,"_fullscreen_preview",None)
            if dlg is not None and getattr(dlg,"mute_btn",None) is not None:
                dlg.mute_btn.setText("🔇" if self._preview_muted else "🔊")
        except Exception as exc:
            self._log(f"ERROR: Preview volume: {exc}")

    def _toggle_fullscreen_preview(self):
        try:
            dlg = getattr(self, "_fullscreen_preview", None)
            if dlg is not None and dlg.isVisible():
                dlg.close(); return
            dlg=QDialog(self); dlg.setWindowTitle("Chopster — Fullscreen Preview"); dlg.setObjectName("fullscreenPreview")
            dlg.setStyleSheet("QDialog#fullscreenPreview{background:#000;} QLabel{background:#000;color:#9AA4B2;} QPushButton{padding:6px 12px;}")
            lay=QVBoxLayout(dlg); lay.setContentsMargins(10,10,10,10); lay.setSpacing(8)
            lab=QLabel(); lab.setAlignment(Qt.AlignCenter); lab.setAttribute(Qt.WA_TransparentForMouseEvents, False); lab.installEventFilter(self); lay.addWidget(lab,1)
            dlg.preview_label=lab
            bar=QHBoxLayout(); bar.setSpacing(8)
            bar.addStretch()
            dlg.mute_btn=QPushButton("🔇" if self._preview_muted else "🔊"); dlg.mute_btn.setToolTip("Mute / unmute preview"); dlg.mute_btn.clicked.connect(self._toggle_preview_mute); bar.addWidget(dlg.mute_btn)
            dlg.volume_slider=ScrollSafeSlider(Qt.Horizontal); dlg.volume_slider.setRange(0,100); dlg.volume_slider.setValue(int(round(self._preview_volume*100))); dlg.volume_slider.setFixedWidth(130); dlg.volume_slider.setToolTip("Atur volume preview"); dlg.volume_slider.valueChanged.connect(self._set_preview_volume); bar.addWidget(dlg.volume_slider)
            exit_btn=QPushButton("⤢ Keluar Fullscreen"); exit_btn.setToolTip("Kembali ke preview biasa"); exit_btn.clicked.connect(dlg.close); bar.addWidget(exit_btn)
            lay.addLayout(bar)
            dlg.finished.connect(lambda *_: self._on_fullscreen_preview_closed())
            self._fullscreen_preview=dlg
            dlg.showFullScreen()
            self._sync_fullscreen_preview()
        except Exception as exc:
            self._log(f"ERROR: Fullscreen preview: {exc}")

    def _on_fullscreen_preview_closed(self):
        self._fullscreen_preview=None

    def _sync_fullscreen_preview(self):
        dlg=getattr(self, "_fullscreen_preview", None)
        if dlg is None or not dlg.isVisible() or self._last_frame_image is None:
            return
        try:
            shot=self.preview_canvas.grab()
            if shot.isNull():
                return
            label=getattr(dlg, "preview_label", None)
            if label is None:
                return
            pix=shot.scaled(max(1,label.width()-8), max(1,label.height()-8), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            label.setPixmap(pix)
        except Exception as exc:
            self._log(f"ERROR: Fullscreen frame: {exc}")

    def eventFilter(self, obj, event):
        if obj is getattr(self, "preview_canvas", None) and event.type()==QEvent.Type.MouseButtonRelease and event.button()==Qt.MouseButton.LeftButton:
            self._toggle_play()
            return True
        dlg=getattr(self, "_fullscreen_preview", None)
        if dlg is not None and obj is getattr(dlg, "preview_label", None) and event.type()==QEvent.Type.MouseButtonRelease and event.button()==Qt.MouseButton.LeftButton:
            self._toggle_play()
            return True
        if dlg is not None and obj is dlg and event.type()==QEvent.Type.KeyPress and event.key()==Qt.Key.Key_Escape:
            dlg.close(); return True
        return super().eventFilter(obj, event)

    def _toggle_play(self):
        if not hasattr(self,"video_player"):self.status.setText("Import video terlebih dahulu."); return
        try:
            if self.video_player.playbackState()==QMediaPlayer.PlayingState:self.video_player.pause(); self.play.setText("▶ Play")
            else:self.video_player.play(); self.play.setText("⏸ Pause")
        except Exception as exc:self._media_error(str(exc))
    def _seek(self,v):
        try:self.video_player.setPosition(int(v))
        except Exception:pass
    def _seek_to(self,sec):
        try:
            target=max(0.0,float(sec)); self._pending_seek_sec=target; ms=int(target*1000)
            self.seek.setValue(ms)
            player=getattr(self,"video_player",None)
            if player is None: return
            # QMediaPlayer may report duration=0 briefly on local MP4 despite having a valid FFprobe duration.
            # Queue the seek until media is loaded and retry once after buffering.
            player.setPosition(ms)
            player.pause(); self.play.setText("▶ Play")
            QTimer.singleShot(80,lambda:self._apply_pending_seek())
            QTimer.singleShot(350,lambda:self._apply_pending_seek())
        except Exception as exc:
            self._log(f"ERROR: Seek: {exc}")
    def _apply_pending_seek(self):
        try:
            player=getattr(self,"video_player",None)
            if player is None:return
            ms=int(max(0.0,float(getattr(self,"_pending_seek_sec",0.0)))*1000)
            if ms <= 0:
                player.setPosition(0); return
            player.setPosition(ms)
            self._refresh_preview_overlay(ms/1000.0)
        except Exception:
            pass
    def _position_changed(self,ms):
        self._preview_current_sec=float(ms)/1000; self.seek.blockSignals(True); self.seek.setValue(int(ms)); self.seek.blockSignals(False); fallback_dur=float(self.media.duration if self.media else 0); player_dur=float(self.video_player.duration()/1000) if getattr(self,"video_player",None) and self.video_player.duration()>0 else fallback_dur; self.time_label.setText(f"{format_timecode(ms/1000)} / {format_timecode(player_dur)}");
        self._refresh_scene_preview_badge(self._preview_current_sec)
        self._refresh_preview_overlay(ms/1000)
        if self._last_frame_image is not None and getattr(self,"video_player",None) is not None and self.video_player.playbackState()!=QMediaPlayer.PlayingState:
            self._render_preview_frame()
    def _preview_target_ratio(self):
        if self.aspect.currentText()=="Original" and self.media and self.media.width and self.media.height:return max(.1,float(self.media.width)/float(self.media.height))
        return {"9:16":9/16,"3:4":3/4,"4:5":4/5,"1:1":1.0,"4:3":4/3,"16:9":16/9}.get(self.aspect.currentText(),16/9)
    def _canvas_rect(self):
        host=self.preview_host.rect(); hw=max(1,host.width()); hh=max(1,host.height()); ratio=self._preview_target_ratio()
        if hw/hh>ratio: ch=hh; cw=max(1,int(ch*ratio)); x=(hw-cw)//2; y=0
        else: cw=hw; ch=max(1,int(cw/ratio)); x=0; y=(hh-ch)//2
        return x,y,cw,ch
    def _on_video_frame(self, frame):
        """Receive frames from QMediaPlayer and render the editor canvas safely."""
        try:
            if not frame.isValid():
                return
            now=time.monotonic()
            # Cap UI painting to ~30 fps so 60-fps source video does not starve the editor.
            if now-self._last_frame_clock < (1/30):
                return
            self._last_frame_clock=now
            image=frame.toImage()
            if image.isNull():
                return
            self._last_frame_image=image.convertToFormat(QImage.Format_RGB32)
            if not self._preview_quick_focus and not self.focus_points and self.aspect.currentText() != "Original":
                self._update_preview_quick_focus()
            self._render_preview_frame()
        except Exception as exc:
            self._log(f"ERROR: Preview frame: {exc}")

    def _update_preview_quick_focus(self):
        """Cheap one-frame subject lock used before full Detect Faces runs."""
        if self._last_frame_image is None or not self.media or self.aspect.currentText()=="Original":
            self._preview_quick_focus=[]; return
        try:
            import numpy as np, cv2
            from chopster.clipper.face_tracking import _detect_subjects
            img=self._last_frame_image.convertToFormat(QImage.Format_RGB888)
            ptr=img.bits(); ptr.setsize(img.sizeInBytes())
            arr=np.frombuffer(ptr, dtype=np.uint8).reshape((img.height(), img.width(), 3))
            frame=cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
            from pathlib import Path as _Path
            face_cascades=[cv2.CascadeClassifier(str(_Path(cv2.data.haarcascades)/"haarcascade_frontalface_alt2.xml")), cv2.CascadeClassifier(str(_Path(cv2.data.haarcascades)/"haarcascade_profileface.xml"))]
            upper=cv2.CascadeClassifier(str(_Path(cv2.data.haarcascades)/"haarcascade_upperbody.xml"))
            dets,_=_detect_subjects(frame,face_cascades,upper)
            if not dets:
                self._preview_quick_focus=[]; return
            sw,sh=frame.shape[1],frame.shape[0]
            # Before the expensive full tracking pass, prefer a two-person group
            # when two faces are clearly present. This prevents a close UmeTV-style
            # split screen from immediately zooming into only one giant face.
            ranked=sorted(dets, key=lambda d: (d["face"][2]*d["face"][3]), reverse=True)
            if len(ranked) >= 2:
                pair=ranked[:2]
                left=min(float(d["subject"][0]) for d in pair)/sw
                right=max(float(d["subject"][0]+d["subject"][2]) for d in pair)/sw
                top=min(float(d["subject"][1]) for d in pair)/sh
                bottom=max(float(d["subject"][1]+d["subject"][3]) for d in pair)/sh
                gx=(left+right)*.5; gy=(top+bottom)*.5
                tmp={"t":self._preview_current_sec,"x":gx,"y":gy,"group_left":left,"group_right":right,"group_top":top,"group_bottom":bottom,"shot":"two","target_id":"preview-1,preview-2"}
                d=decide_framing(tmp, source_aspect=sw/max(1,sh), target_aspect=self._preview_target_ratio())
                fp={"t":self._preview_current_sec,"x":d.center_x,"y":d.center_y,"source":"quick-preview-group","confidence":.78,"group_left":left,"group_right":right,"group_top":top,"group_bottom":bottom,"target_id":"preview-1,preview-2","shot":"fit" if d.mode=="adaptive_fit" else "two","reason":d.reason or "quick group safe framing","hold":True}
                self._preview_quick_focus=[fp]
                return
            chosen=ranked[0]
            subject=chosen["subject"]; left,right,top,bottom=subject[0]/sw,(subject[0]+subject[2])/sw,subject[1]/sh,(subject[1]+subject[3])/sh
            tmp={"t":self._preview_current_sec,"x":chosen["face_center"][0],"y":chosen["face_center"][1],"subject_left":left,"subject_right":right,"subject_top":top,"subject_bottom":bottom,"shot":"solo"}
            d=decide_framing(tmp, source_aspect=sw/max(1,sh), target_aspect=self._preview_target_ratio())
            fp={"t":self._preview_current_sec,"x":d.center_x,"y":d.center_y,"source":"quick-preview","confidence":.72,"subject_left":left,"subject_right":right,"subject_top":top,"subject_bottom":bottom,"target_id":"preview-1","shot":"fit" if d.mode=="adaptive_fit" else "solo","reason":d.reason,"hold":True}
            self._preview_quick_focus=[fp]
        except Exception:
            self._preview_quick_focus=[]

    def _preview_focus_point(self, sec: float):
        if self.reframe.currentText() not in ("Smart","AI Camera Director","Speaker Focus","Two Person"):
            return None
        try:
            from chopster.clipper.camera_director import normalize_points
            pts = normalize_points(self.focus_points or self._preview_quick_focus)
            if not pts:
                return None
            t = max(0.0, float(sec))
            if t <= pts[0].t: return pts[0]
            if t >= pts[-1].t: return pts[-1]
            lo, hi = 0, len(pts)-1
            while lo + 1 < hi:
                mid=(lo+hi)//2
                if pts[mid].t <= t: lo=mid
                else: hi=mid
            a,b=pts[lo],pts[hi]
            if b.t <= a.t:return a
            u=max(0.0,min(1.0,(t-a.t)/(b.t-a.t))); u=u*u*(3-2*u)
            return type(a)(
                t=t, x=a.x+(b.x-a.x)*u, y=a.y+(b.y-a.y)*u, source=b.source,
                confidence=max(a.confidence,b.confidence),
                subject_left=a.subject_left if a.subject_left is not None else b.subject_left,
                subject_right=a.subject_right if a.subject_right is not None else b.subject_right,
                subject_top=a.subject_top if a.subject_top is not None else b.subject_top,
                subject_bottom=a.subject_bottom if a.subject_bottom is not None else b.subject_bottom,
                group_left=a.group_left if a.group_left is not None else b.group_left,
                group_right=a.group_right if a.group_right is not None else b.group_right,
                group_top=a.group_top if a.group_top is not None else b.group_top,
                group_bottom=a.group_bottom if a.group_bottom is not None else b.group_bottom,
                target_id=a.target_id if a.target_id==b.target_id else b.target_id,
                shot=b.shot, hold=True, reason=b.reason)
        except Exception:
            return None

    def _preview_focus(self, sec: float) -> tuple[float,float]:
        p=self._preview_focus_point(sec)
        if p is None:return .5,.5
        return max(0,min(1,p.x)),max(0,min(1,p.y))

    def _crop_preview_image(self, image):
        if image is None or image.isNull():
            return None
        try:
            sw,sh=image.width(),image.height()
            target=self._preview_target_ratio()
            source=sw/max(1,sh)
            if abs(target-source) < .01:
                return image
            p=self._preview_focus_point(self._preview_current_sec)
            fx,fy=(p.x,p.y) if p is not None else (.5,.5)
            if target < source:
                crop_h=sh; crop_w=max(1,min(sw,int(round(sh*target))))
            else:
                crop_w=sw; crop_h=max(1,min(sh,int(round(sw/target))))

            half_x=(crop_w/sw)*.5; half_y=(crop_h/sh)*.5
            if p is not None:
                # Person-safe crop: clamp the crop center so the selected face stays
                # inside the visible canvas rather than merely centering on its eyes.
                l,r=p.subject_left,p.subject_right
                t,b=p.subject_top,p.subject_bottom
                group_w = None if p.group_left is None or p.group_right is None else float(p.group_right)-float(p.group_left)
                group_h = None if p.group_top is None or p.group_bottom is None else float(p.group_bottom)-float(p.group_top)
                group_needed = p.shot in ("group","two") or (p.shot == "fit" and ((group_w is not None and group_w > (crop_w/sw)*.92) or (group_h is not None and group_h > (crop_h/sh)*.92)))
                if group_needed:
                    l,r=p.group_left if p.group_left is not None else l, p.group_right if p.group_right is not None else r
                    t,b=p.group_top if p.group_top is not None else t, p.group_bottom if p.group_bottom is not None else b
                if l is not None and r is not None:
                    lo=max(half_x,float(r)-half_x); hi=min(1-half_x,float(l)+half_x)
                    fx=(lo+hi)/2 if lo>hi else max(lo,min(hi,fx))
                if t is not None and b is not None:
                    lo=max(half_y,float(b)-half_y); hi=min(1-half_y,float(t)+half_y)
                    fy=(lo+hi)/2 if lo>hi else max(lo,min(hi,fy))

            left=int(round(fx*sw-crop_w/2)); top=int(round(fy*sh-crop_h/2))
            left=max(0,min(sw-crop_w,left)); top=max(0,min(sh-crop_h,top))
            return image.copy(left,top,crop_w,crop_h)
        except Exception:
            return image

    def _render_preview_frame(self):
        if not hasattr(self,"preview_canvas") or self._last_frame_image is None:
            return
        try:
            x,y,w,h=self._canvas_rect(); self._preview_canvas_rect=(x,y,w,h); self.preview_canvas.setGeometry(x,y,w,h)
            self.preview_bg.setGeometry(0,0,w,h); self.video.setGeometry(0,0,w,h)
            p=self._preview_focus_point(self._preview_current_sec)
            adaptive=False
            if p is not None and self.aspect.currentText() != "Original":
                try:
                    target=self._preview_target_ratio(); source=self._last_frame_image.width()/max(1,self._last_frame_image.height())
                    adaptive=decide_framing(p, source_aspect=source, target_aspect=target).mode == "adaptive_fit"
                except Exception:
                    adaptive=False
            if adaptive and p is not None:
                d=decide_framing(p, source_aspect=self._last_frame_image.width()/max(1,self._last_frame_image.height()), target_aspect=self._preview_target_ratio())
                bg_pix=QPixmap.fromImage(self._last_frame_image).scaled(max(1,w),max(1,h),Qt.KeepAspectRatioByExpanding,Qt.SmoothTransformation)
                self.preview_bg.setPixmap(bg_pix); self.preview_bg.show(); self.preview_bg.lower(); self.preview_bg.setGraphicsEffect(self.preview_bg_blur)
                self.video.raise_()
                if d.context_left is not None and d.context_right is not None and d.context_top is not None and d.context_bottom is not None:
                    sw,sh=self._last_frame_image.width(),self._last_frame_image.height()
                    left=int(round(d.context_left*sw)); top=int(round(d.context_top*sh))
                    cw=max(1,int(round((d.context_right-d.context_left)*sw))); ch=max(1,int(round((d.context_bottom-d.context_top)*sh)))
                    left=max(0,min(sw-cw,left)); top=max(0,min(sh-ch,top))
                    crop=self._last_frame_image.copy(left,top,cw,ch)
                    pix=QPixmap.fromImage(crop).scaled(max(1,w-12),max(1,h-12),Qt.KeepAspectRatio,Qt.SmoothTransformation)
                    self.video.setPixmap(pix); self.video.setStyleSheet("background:transparent; border:1px solid rgba(255,255,255,28); border-radius:8px;"); self.video.setText("")
            else:
                self.preview_bg.clear(); self.preview_bg.hide()
                cropped=self._crop_preview_image(self._last_frame_image)
                if cropped is not None and not cropped.isNull():
                    pix=QPixmap.fromImage(cropped).scaled(max(1,w-4),max(1,h-4),Qt.IgnoreAspectRatio,Qt.SmoothTransformation)
                    self.video.setPixmap(pix); self.video.setText("")
            self._position_preview_overlays()
            self._sync_fullscreen_preview()
        except Exception as exc:
            self._log(f"ERROR: Render preview: {exc}")

    def _refresh_preview_canvas(self):
        try:
            x,y,w,h=self._canvas_rect(); self._preview_canvas_rect=(x,y,w,h); self.preview_canvas.setGeometry(x,y,w,h); self.preview_bg.setGeometry(0,0,w,h); self.video.setGeometry(0,0,w,h); self._position_preview_overlays()
        except Exception:
            pass

    def _refresh_scene_preview_badge(self, sec):
        """Keep the scene badge synced across the full timeline after Scene Detect."""
        if not self._scene_preview_mode or not self._scene_segments or not hasattr(self,"preview_badge"):
            return
        try:
            t=max(0.0,float(sec)); idx=None; st=0.0; en=0.0
            for i,(a,b) in enumerate(self._scene_segments,1):
                if float(a) <= t < float(b) or (i==len(self._scene_segments) and t<=float(b)):
                    idx=i; st=float(a); en=float(b); break
            if idx is None:
                return
            label=f"Scene {idx:03d} • {format_timecode(st)}–{format_timecode(en)}"
            self.preview_status.setText(label)
            self._show_preview_badge(label)
        except Exception as exc:
            self._log(f"Scene badge sync fallback: {exc}")

    def _show_preview_badge(self,text):
        if not hasattr(self,"preview_badge"):return
        self.preview_badge.setText(str(text)); self.preview_badge.adjustSize(); self.preview_badge.show(); self.preview_badge.raise_(); self._position_preview_overlays()
    def _position_preview_overlays(self):
        if not self._preview_canvas_rect:return
        if not hasattr(self, "subtitle_overlay") or not hasattr(self, "watermark_overlay"):
            return
        _,_,w,h=self._preview_canvas_rect
        if hasattr(self, "preview_bg"):
            self.preview_bg.lower()
        self.video.raise_()
        # Subtitle geometry is controlled by the Auto Clip Studio placement
        # settings in _refresh_acs_subtitle_overlay; don't reset it here.
        self.subtitle_overlay.raise_(); self._place_watermark()
        if self.watermark_overlay.isVisible():self.watermark_overlay.raise_()
        if self.preview_badge.isVisible():
            bw=min(max(150,self.preview_badge.sizeHint().width()),max(150,w-20)); self.preview_badge.resize(bw,self.preview_badge.sizeHint().height()); self.preview_badge.move(10,10); self.preview_badge.raise_()

    def set_compact(self, compact: bool):
        """Responsive editor layout for smaller window sizes.

        The editor keeps every core control available, but compresses labels,
        hides secondary guidance, and gives the preview/tabs the majority of the
        viewport instead of letting widgets keep their fullscreen geometry.
        """
        self._compact = bool(compact)
        try:
            lay = self.layout()
            if lay:
                m = 8 if compact else 16
                lay.setContentsMargins(m, 8 if compact else 14, m, 10 if compact else 14)
                lay.setSpacing(7 if compact else 10)
            for i,b in enumerate(getattr(self, '_head_buttons', [])):
                labels = ["＋", "📂", "💾", "🧠", "⚡"]
                full = ["＋ Project Baru", "📂 Buka", "💾 Simpan", "🧠 MASTER ANALYSIS", "⚡ AUTO CREATE SHORTS"]
                b.setText(labels[i] if compact else full[i])
                b.setToolTip(full[i] if compact else "")
            self.import_btn.setText("🎬" if compact else "🎬 Import Video")
            self.download_btn.setText("⬇" if compact else "⬇ Download")
            self.aspect_help.setVisible(not compact)
            self.output_edit.setPlaceholderText("Export folder" if compact else "Folder export — kosong = folder Chopster_Exports")
            if compact:
                self.output_edit.setMaximumWidth(220)
            else:
                self.output_edit.setMaximumWidth(16777215)
            for card in getattr(self, '_stat_cards', []):
                card.setMinimumWidth(0)
            try:
                self.bottom.tabBar().setUsesScrollButtons(True)
            except Exception:
                pass
            self._resize_preview()
        except Exception:
            pass

    def _resize_preview(self):
        # Qt can deliver resize events while this widget is still being constructed.
        # Never let a partially-initialized preview tear down the whole application.
        if not hasattr(self, "preview_canvas") or not hasattr(self, "video"):
            return
        self._refresh_preview_canvas(); self._render_preview_frame(); self._refresh_preview_overlay()
    def resizeEvent(self,e):super().resizeEvent(e); self._resize_preview()
    @staticmethod
    def _ass_color_to_qt(value,default="#FFFFFF"):
        try:
            v=str(value or "").replace("&H","")
            if len(v)==8:v=v[2:]
            if len(v)==6:return f"#{v[4:6]}{v[2:4]}{v[0:2]}"
        except Exception:pass
        return default
    def _refresh_acs_subtitle_overlay(self, pos):
        if self.sub_preset.currentText()=="none":
            self.subtitle_overlay.hide(); return
        try: playing=self.video_player.playbackState()==QMediaPlayer.PlayingState
        except Exception: playing=False
        caption=self._acs_preview_caption(float(pos),playing)
        if not caption:
            self.subtitle_overlay.hide(); return
        style_info=ACS_WORD_STYLES.get(caption["style"],ACS_WORD_STYLES["viral_pop"])
        accent=style_info["accent"]
        html_words=[]
        for index,word in enumerate(caption["words"]):
            safe=html.escape(word)
            if index==caption["active"]:
                html_words.append(f'<span style="color:{accent};font-weight:800;">{safe}</span>')
            else:
                html_words.append(f'<span style="color:#FFFFFF;font-weight:800;">{safe}</span>')
        self.subtitle_overlay.setText(" ".join(html_words)); self.subtitle_overlay.setTextFormat(Qt.RichText)
        _register_acs_caption_fonts()
        font=QFont(self.sub_font.currentText() or "Outfit")
        selected_aspect=self.aspect.currentText()
        source_ratio=(float(self.media.width)/max(1.0,float(self.media.height))) if self.media else 9/16
        landscape=(selected_aspect in {"16:9","4:3"}) or (selected_aspect=="Original" and source_ratio>1.25)
        font_px=(40 if self.sub_size_preset=="small" else 48 if self.sub_size_preset=="medium" else 58) if landscape else ACS_CAPTION_SIZES.get(self.sub_size_preset,78)
        scale=self.preview_canvas.height()/(1080 if landscape else 1920)
        font.setPixelSize(max(10,int(font_px*scale))); font.setBold(True)
        self.subtitle_overlay.setFont(font); self.subtitle_overlay.setWordWrap(False)
        self.subtitle_overlay.setStyleSheet("color:#FFFFFF; background:transparent; padding:0; border:none; border-radius:0;")
        effect=self.subtitle_overlay.graphicsEffect()
        if not isinstance(effect,QGraphicsDropShadowEffect):
            effect=QGraphicsDropShadowEffect(self.subtitle_overlay); self.subtitle_overlay.setGraphicsEffect(effect)
        effect.setColor(QColor(0,0,0,235)); effect.setOffset(0,2); effect.setBlurRadius(max(2,int(font_px*scale*.10)))
        canvas_w=max(80,self.preview_canvas.width()-16)
        overlay_h=max(24,int(font_px*scale*1.45))
        if self.sub_position_mode=="center":
            y=int(self.preview_canvas.height()*self.sub_center_percent/100.0-overlay_h/2)
        else:
            y=int(self.preview_canvas.height()*(1.0-self.sub_bottom_percent/100.0)-overlay_h)
        y=max(0,min(max(0,self.preview_canvas.height()-overlay_h),y))
        self.subtitle_overlay.setGeometry(8,y,canvas_w,overlay_h)
        self.subtitle_overlay.show(); self.subtitle_overlay.raise_()

    def _refresh_preview_overlay(self,*args):
        if not hasattr(self,"preview_canvas"):return
        if not hasattr(self,"subtitle_overlay") or not hasattr(self,"watermark_overlay") or not hasattr(self,"wm_enable"):
            return
        try:
            if args and args[0] is not None:self._preview_current_sec=float(args[0])
        except Exception:pass
        # Subtitle word-highlighting follows Auto Clip Studio's timed 1–3 word chunks.
        if self.transcript and self._subtitle_ready and self.transcript.segments:
            self._refresh_acs_subtitle_overlay(float(self._preview_current_sec or 0.0))
        else:
            self.subtitle_overlay.hide()

        if self.wm_enable.isChecked():
            try:
                self.watermark_overlay.setGraphicsEffect(None)
                self.watermark_overlay.clear()
                if self.wm_type.currentText()=="Image":
                    ip=Path(self.wm_image.text()).expanduser()
                    if ip.exists():
                        scale=max(0.01,min(1.0,float(self.wm_img_size.value())/100.0)); maxw=max(16,int(self.preview_canvas.width()*scale)); maxh=max(16,int(self.preview_canvas.height()*scale*0.75))
                        pix=QPixmap(str(ip))
                        if not pix.isNull():
                            self.watermark_overlay.setPixmap(pix.scaled(maxw,maxh,Qt.KeepAspectRatio,Qt.SmoothTransformation)); self.watermark_overlay.setText("")
                        else:
                            self.watermark_overlay.setText("Watermark tidak dapat dibaca")
                    else:
                        self.watermark_overlay.setText("Watermark image belum ditemukan")
                else:
                    text=self.wm_text.text().strip() or "@NamaChannel"
                    f=QFont("Segoe UI",max(12,min(72,int(self.wm_size.value()*max(.55,self.preview_canvas.width()/1080))))); f.setBold(True)
                    self.watermark_overlay.setFont(f); self.watermark_overlay.setText(text); self.watermark_overlay.setStyleSheet("color:white; background:rgba(0,0,0,160); padding:7px 11px; border-radius:7px; border:1px solid rgba(255,255,255,35);")
                op=QGraphicsOpacityEffect(self.watermark_overlay); op.setOpacity(max(.05,min(1,float(self.wm_op.value())))); self.watermark_overlay.setGraphicsEffect(op)
                self.watermark_overlay.adjustSize(); self.watermark_overlay.show(); self.watermark_overlay.raise_(); self._place_watermark(); self.wm_status.setText("✓ Watermark tampil langsung di LIVE PREVIEW")
            except Exception as exc:
                self.watermark_overlay.hide(); self.wm_status.setText(f"Watermark preview gagal: {exc}")
        else:
            self.watermark_overlay.hide(); self.wm_status.setText("Watermark nonaktif — centang Aktif untuk menampilkannya.")
        self._position_preview_overlays()

    def _place_watermark(self):
        if not self.watermark_overlay.isVisible() or not self._preview_canvas_rect:return
        _,_,w,h=self._preview_canvas_rect
        sw=max(1,self.watermark_overlay.sizeHint().width()); sh=max(1,self.watermark_overlay.sizeHint().height())
        m=max(10,int(min(w,h)*.025)); pos=self.wm_pos.currentText().lower()
        x=m if 'left' in pos else (w-sw-m if 'right' in pos else max(0,(w-sw)//2))
        y=m if 'top' in pos else (h-sh-m if 'bottom' in pos else max(0,(h-sh)//2))
        self.watermark_overlay.move(max(0,min(w-sw,x)),max(0,min(h-sh,y)))

    # -------------------------------------------------------------- pipeline
    def _smart_pipeline(self):
        if not self.project:
            return self._import_video()
        path=self.project.source_path
        model=self.model.currentText(); lang=self.lang.currentText(); wt=self.word_ts.isChecked()
        total_duration=float(self.media.duration)
        config=self._ai_config(); count=self.clip_count.value(); publish_platform=str(self.app.config.get("publish_platform") or "YouTube Shorts")
        analysis_mode=self.reframe.currentText() if self.reframe.currentText() in ("Smart","AI Camera Director","Speaker Focus","Two Person") else "Smart"
        config["ai_camera_director"] = analysis_mode in ("Smart","AI Camera Director","Speaker Focus","Two Person")
        config["target_aspect"] = self._preview_target_ratio()
        def worker(*,signals,cancel_check):
            signals.progress.emit("clipper:smart",2,"Auto Create Shorts — cek Master Analysis cache…")
            analysis=ensure_master_analysis(self.project_dir(),path,model=model,language=lang,word_timestamps=wt,device=str(self.app.config.get("transcribe_device") or "auto"),mode=analysis_mode,ai_config=config,progress_cb=lambda p,m: signals.progress.emit("clipper:smart",min(68,p),m),cancel_check=cancel_check,max_camera_samples=1800,target_aspect=self._preview_target_ratio())
            if analysis.get("cancelled"): return analysis
            from chopster.clipper.transcript_manager import Transcript
            tr=Transcript.from_dict(analysis["transcript"])
            master=analysis.get("master") or {}
            config["master_context"] = {
                "ai_advice": master.get("ai_advice", {}) if isinstance(master, dict) else {},
                "visual_review": master.get("visual_review", {}) if isinstance(master, dict) else {},
                "camera_plan": master.get("ai_shots", []) if isinstance(master, dict) else [],
                "target_aspect": master.get("target_aspect") if isinstance(master, dict) else self._preview_target_ratio(),
            }
            config["master_camera_plan"] = master.get("ai_shots", []) if isinstance(master, dict) else []
            config["target_aspect"] = self._preview_target_ratio()
            signals.progress.emit("clipper:smart",72,"Auto Create Shorts — AI Editor Brain menilai kandidat berdasarkan Master Analysis…")
            cands=analyze_with_ai(tr,total_duration,config)
            if cancel_check(): return {"cancelled":True}
            clips=[{"index":i+1,"start":float(c.start),"end":float(c.end),"title":c.title,"status":"planned","output":"","selected":True} for i,c in enumerate(cands[:count])]
            signals.progress.emit("clipper:smart",84,"Generating caption package — Auto Create Shorts membuat AI Content Pack…")
            caption_pkg={}
            try:
                ctx=[c.__dict__ if hasattr(c,"__dict__") else c for c in cands[:5]]
                ccfg=dict(config); ccfg["highlight_context"]=ctx
                caption_pkg=generate_captions(tr,ccfg,platform=publish_platform,language=lang if lang!="auto" else "id",count=5,style="hooks")
            except Exception as exc:
                caption_pkg={"source":"local-fallback","ai_error":str(exc)[:220]}
            signals.progress.emit("clipper:smart",94,"Auto Create Shorts — menyusun hasil dan cache…")
            return {"transcript":tr.to_dict(),"candidates":[c.__dict__ for c in cands],"clips":clips,"captions":caption_pkg,"focus":analysis.get("focus",[]),"scenes":analysis.get("scenes",[]),"master":analysis.get("master",{})}
        def done(res):
            if res.get("cancelled"):
                self.status.setText("Dibatalkan")
                return
            from chopster.clipper.transcript_manager import Transcript
            self.transcript=Transcript.from_dict(res["transcript"]); self.candidates=res["candidates"]; self.clips=res["clips"]; self.focus_points=res.get("focus",[])
            self._preview_quick_focus=[]
            self._render_transcript(); self._render_highlights(); self._render_clips()
            try:
                self.bottom.setCurrentWidget(self._clip_queue_tab)
                if self.clips:
                    self.clip_table.selectRow(0)
            except Exception:
                pass
            if self.transcript and not self._subtitle_ready:
                try: self._generate_subtitle()
                except Exception: pass
            pkg=res.get("captions") or {}
            if isinstance(pkg,dict) and pkg:
                titles=pkg.get("titles") or ([pkg.get("title")] if pkg.get("title") else [])
                caps=pkg.get("captions") or []
                lines=["JUDUL OPTIONS"]+[f"{i+1}. {x}" for i,x in enumerate(titles)]
                lines += ["", "DESKRIPSI PENDEK", str(pkg.get("description_short",pkg.get("description", "")))]
                lines += ["", "DESKRIPSI PANJANG — YOUTUBE STYLE", str(pkg.get("description_long",pkg.get("description", "")))]
                lines += ["", "CAPTION OPTIONS"]+[f"{i+1}. {x}" for i,x in enumerate(caps)]
                lines += ["", "HASHTAG", " ".join(pkg.get("hashtags") or [])]
                lines += ["", "KEYWORD SEO", ", ".join(pkg.get("keywords") or [])]
                lines += ["", "PINNED COMMENT", str(pkg.get("pinned_comment", ""))]
                lines += ["", "THUMBNAIL TEXT", ", ".join(pkg.get("thumbnail_text") or [])]
                self.caption_text.setPlainText("\n".join(lines)); self.pub_title.setText(titles[0] if titles else pkg.get("title", ""))
            ai_src = str((res.get("master") or {}).get("ai_advice", {}).get("source", "local"))
            self._save_project(); self.status.setText(f"AUTO CREATE SHORTS siap — Master + AI Editor Brain ✓ ({ai_src}) • {len(self.candidates)} highlight • {len(self.clips)} clip plan • framing {len(self.focus_points)} point • AI content pack {'✓' if pkg else 'fallback'}"); self.progress.setValue(100)
        self._submit("clipper:smart","Auto Create Shorts",worker,done)

    # -------------------------------------------------------------- drag/drop/log
    def dragEnterEvent(self,e):
        if e.mimeData().hasUrls():e.acceptProposedAction()
    def dropEvent(self,e):
        urls=e.mimeData().urls()
        if urls:self._set_source(urls[0].toLocalFile())
    def _log(self,msg):
        text=str(msg or "").strip(); low=text.lower()
        important=("error" in low or "gagal" in low or "berhasil" in low or "selesai" in low or "dibatalkan" in low or text.startswith("✓") or text.startswith("✗"))
        if hasattr(self,"log") and important:self.log.append(text)

