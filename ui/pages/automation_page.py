"""Automation page — overview, create wizard, accounts, queue, recipes, history.

Design follows spec §109-§137: existing Chopster dark/gold theme, compact
desktop cards with subtle depth, progressive disclosure, no fake features,
no exposed secrets, and no chain-of-thought from the AI browser agent.
All heavy work runs through app.tasks (Qt TaskManager) or the Automation
manager's own worker threads; UI updates arrive via queued signals.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import (
    QDate, QEasingCurve, QObject, QPoint, QPropertyAnimation, QRunnable, Qt,
    QThreadPool, QTime, QTimer, Signal,
)
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QFrame, QGraphicsOpacityEffect, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QProgressBar, QPushButton,
    QRadioButton, QScrollArea, QSpinBox, QStackedWidget, QTableWidget,
    QTableWidgetItem, QTabWidget, QTextEdit, QTimeEdit, QVBoxLayout, QWidget,
)

from chopster.automation.auth import validate_redirect_uri
from chopster.automation.autopilot import SOURCE_LABELS
from chopster.automation.models import (
    DELETABLE_JOB_STATUSES, PLATFORM_LABELS, PLATFORMS, human_job_status,
)
from chopster.automation.planner import ALL_DAYS, WEEKDAY_LABELS, describe_slots
from chopster.automation.utils import humanize_when, parse_local_time
from chopster.ui.theme import THEMES

TERMINAL = {"PUBLISHED", "SCHEDULED", "FAILED", "CANCELLED"}
RUN_ACTIVE = {"QUEUED", "RUNNING", "READY", "DRAFT"}
# Short labels for the machine-readable reason codes stored on a job (spec §8/§9).
# A row shows the chip only when a reason is present, so nothing is invented.
REASON_LABELS = {
    "draft_ready": "⚠ Draft siap — publish di app",
    "verify_unknown": "⚠ Hasil belum pasti",
    "captcha": "⚠ Captcha/verifikasi",
    "login_required": "⚠ Perlu login",
    "soft_cap": "⚠ Soft cap tercapai",
    "test_mode": "⚠ Test Mode (simulasi)",
    "policy_blocked": "⚠ Dibatasi kebijakan",
    "scheduled_live": "🗓 Tayang terjadwal di platform",
}
MEDIA_SOURCE_CHOICES = (("Semua sumber", ""), ("Auto Clip Studio", "acs"),
                        ("Content Clipper AI", "cca"), ("Manual Import", "manual"))
SOURCE_KEYS = dict(MEDIA_SOURCE_CHOICES)
# Statuses the user may cancel from the Queue (spec: cooperative cancellation).
# PUBLISHED / CANCELLED / FAILED are terminal — FAILED is retried, not cancelled.
CANCELLABLE_JOB_STATUSES = {
    "QUEUED", "SCHEDULED", "PAUSED", "RETRYING", "ACTION_REQUIRED",
    "AUTH_CHECK", "PREPARING", "UPLOADING", "PROCESSING", "VERIFYING",
}
# Explains every entry of the media-source combo: where the videos come from
# and what the user must do first (the #1 confusion when opening Studio).
SOURCE_HINTS = {
    "Semua sumber": "Semua clip siap dari Auto Clip Studio, Content Clipper AI, "
                    "dan import manual — centang yang mau dijadwalkan.",
    "Auto Clip Studio": "Klip hasil render Auto Clip Studio (sudah ada di komputer, "
                        "tidak perlu download): buka tab Auto Clip Studio → scan/render "
                        "video, hasilnya otomatis masuk ke sini. Belum muncul? Klik "
                        "«Muat dari media siap».",
    "Content Clipper AI": "Clip hasil export Content Clipper AI. Transkrip analisisnya "
                          "ikut terbawa, jadi «Generate AI Content Pack» bisa membaca "
                          "isi video yang kamu centang.",
    "Manual Import": "File video dari folder komputer kamu — klik «Pilih file…» untuk "
                     "menambahkan ke daftar ini.",
}


def _thumb_cache_dir() -> Path:
    d = Path(tempfile.gettempdir()) / "chopster_thumbs"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def _extract_thumbnail(path: str, dest: Path) -> bool:
    """Grab one poster frame with ffmpeg; never raises (thumbs are optional)."""
    try:
        from chopster.downloader.ffmpeg import find_ffmpeg
        ff = find_ffmpeg()
        src = Path(path)
        if not ff or not src.exists():
            return False
        for seek in ("1", "0"):     # very short clips have no frame at 1s
            cmd = [ff, "-hide_banner", "-loglevel", "error", "-ss", seek,
                   "-i", str(src), "-frames:v", "1",
                   "-vf", "scale='min(240,iw)':-2", "-q:v", "6", "-y", str(dest)]
            cp = subprocess.run(cmd, capture_output=True, timeout=25)
            if cp.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
                return True
        return False
    except Exception:
        return False


class _ThumbSignals(QObject):
    ready = Signal(str, str)        # media path, cache file


class _ThumbTask(QRunnable):
    """Background poster-frame extraction (QThreadPool, like app tasks)."""

    def __init__(self, path: str, dest: Path, signals: _ThumbSignals):
        super().__init__()
        self.setAutoDelete(True)
        self._path = path
        self._dest = dest
        self._signals = signals

    def run(self) -> None:
        if _extract_thumbnail(self._path, self._dest):
            try:
                self._signals.ready.emit(self._path, str(self._dest))
            except RuntimeError:
                pass        # page closed while the frame was extracting


class AutomationBridge(QObject):
    """Thread-safe signal bridge: manager threads → queued UI slots."""
    job = Signal(dict)
    run = Signal(dict)
    media = Signal(dict)
    auth = Signal(dict)
    action = Signal(dict)
    failed = Signal(dict)


# Extensions accepted from the OS file picker AND from drag-and-drop, so the
# two paths can never disagree about what counts as a publishable clip.
VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}


class _MediaListWidget(QListWidget):
    """Video list that also accepts clip files dragged from the file manager.

    The Source hint has always told users to add files with «Pilih file…», but
    dragging a rendered clip from Explorer/Finder is what people try first.
    Drop handling mirrors `clipper_page.ClipperPage.dropEvent` (accept URLs,
    take local files) but forwards them as a signal so the page keeps owning
    the list bookkeeping (counters, pack sync, thumbnails).
    """

    dropped = Signal(list)      # list of local video paths

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)

    def _video_paths(self, event) -> list[str]:
        try:
            urls = event.mimeData().urls()
        except Exception:
            return []
        paths: list[str] = []
        for url in urls or []:
            try:
                if not url.isLocalFile():
                    continue
                path = url.toLocalFile()
            except Exception:
                continue
            if Path(path).suffix.lower() in VIDEO_SUFFIXES:
                paths.append(path)
        return paths

    def dragEnterEvent(self, event) -> None:
        if self._video_paths(event):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:
        if self._video_paths(event):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = self._video_paths(event)
        if paths:
            event.acceptProposedAction()
            self.dropped.emit(paths)


def _status_style(status: str, C: dict[str, str]) -> tuple[str, str]:
    s = str(status or "").upper()
    if s in ("PUBLISHED", "COMPLETED", "CONNECTED"):
        return s, C["GREEN"]
    if s in ("FAILED", "CANCELLED", "DISCONNECTED", "EXPIRED"):
        return s, C["RED"]
    if s in ("ACTION_REQUIRED", "PAGE_SELECTION_REQUIRED", "SESSION_REQUIRED", "RETRYING"):
        return s, C["GOLD"]
    if s in ("RUNNING", "UPLOADING", "PROCESSING", "AUTH_CHECK", "PREPARING", "VERIFYING"):
        return s, C["BLUE"]
    return s, C["MUTED"]


def _mix_color(a: str, b: str, t: float) -> str:
    """Blend two theme colours — tiny adaptive depth without hardcoding."""
    try:
        ca, cb = QColor(a), QColor(b)
        r = int(ca.red() + (cb.red() - ca.red()) * t)
        g = int(ca.green() + (cb.green() - ca.green()) * t)
        bl = int(ca.blue() + (cb.blue() - ca.blue()) * t)
        return QColor(max(0, min(255, r)), max(0, min(255, g)),
                      max(0, min(255, bl))).name()
    except Exception:
        return a


def _job_pack(row: dict) -> dict:
    """Parsed AI Content Pack snapshot stored alongside the job.

    History and Queue read this instead of live UI state, so the detail view
    always shows exactly what was (or will be) published.
    """
    raw = row.get("content_pack")
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    pack = data.get("pack")
    if not isinstance(pack, dict):
        return data
    merged = dict(pack)
    # Envelope keys live next to "pack" in the snapshot and are authoritative
    # for the publish settings (audience/privacy), not inside the pack itself.
    for key in ("made_for_kids", "privacy", "category_id", "pack_source"):
        if key in data:
            merged[key] = data.get(key)
    return merged


class AutomationPage(QWidget):
    # Worker thread → UI: per-video AI Content Pack progress text.
    pack_status = Signal(str)
    # Sidebar jumps (Settings / Auto Clip Studio / Content Clipper AI) so the
    # readiness checklist can take the user straight to the missing switch.
    # Wired by main_window for every page exposing `navigate`.
    navigate = Signal(str)

    def __init__(self, app, theme: str = "dark", parent: QWidget | None = None):
        super().__init__(parent)
        self.app = app
        self._theme = theme
        C = THEMES.get(theme, THEMES["dark"])
        self.C = C
        self._remove_listener = None
        self._pending_refresh = False
        # One generated pack per media path (spec §6.6: a pack never bleeds
        # onto a different video's job).
        self._pack: dict = {}
        self._packs: dict[str, dict] = {}
        self._connect_results: dict[str, dict] = {}
        self._thumb_pixmaps: dict[str, QPixmap] = {}
        self._thumb_pending: set[str] = set()
        self._thumb_signals = _ThumbSignals(self)
        self._thumb_signals.ready.connect(self._on_thumb_ready, Qt.QueuedConnection)
        self.pack_status.connect(self._on_pack_status, Qt.QueuedConnection)

        self.setObjectName("automationPage")
        self.setStyleSheet(self._page_css(C))

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        # ---- header -----------------------------------------------------
        head = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Automation", self)
        title.setObjectName("h1")
        title_box.addWidget(title)
        subtitle = QLabel("Publish hasil render kamu ke banyak platform — sekarang atau terjadwal.", self)
        subtitle.setObjectName("muted")
        title_box.addWidget(subtitle)
        head.addLayout(title_box)
        head.addStretch()
        self.health_lbl = QLabel("", self)
        self.health_lbl.setObjectName("muted")
        self.health_lbl.hide()  # superseded by the live status strip below
        head.addWidget(self.health_lbl)
        self.create_btn = QPushButton("+ Create Automation", self)
        self.create_btn.setObjectName("primary")
        self.create_btn.setCursor(Qt.PointingHandCursor)
        self.create_btn.clicked.connect(lambda: (self.tabs.setCurrentIndex(self.TAB_STUDIO), self._reset_wizard()))
        head.addWidget(self.create_btn)
        root.addLayout(head)
        root.addWidget(self._build_status_strip())

        # ---- tabs -------------------------------------------------------
        self.tabs = QTabWidget(self)
        root.addWidget(self.tabs, 1)

        # Named indices: the previous hardcoded 1/2/3 indexes would silently
        # mis-route buttons the moment a tab is inserted.
        self.TAB_OVERVIEW, self.TAB_STUDIO, self.TAB_AUTOPILOT = 0, 1, 2
        self.TAB_ACCOUNTS, self.TAB_QUEUE, self.TAB_PRESETS, self.TAB_HISTORY = 3, 4, 5, 6

        self.tab_overview = self._build_overview()
        self.tab_create = self._build_create()
        self.tab_autopilot = self._build_autopilot()
        self.tab_accounts = self._build_accounts()
        self.tab_queue = self._build_queue()
        self.tab_recipes = self._build_recipes()
        self.tab_history = self._build_history()
        # Tab labels get a glossary tooltip: "Studio" was constantly confused
        # with the separate "Auto Clip Studio" product, so the create tab is
        # now called "Create" and every term (Preset / Auto-Pilot / Pack) is
        # explained on hover.
        tabs = (("Overview", self.tab_overview,
                 "Ringkasan: checklist siap publish, automation aktif, antrean, dan media siap."),
                ("Create", self.tab_create,
                 "Create Automation: pilih clip → platform → waktu → review, lalu Start. "
                 "(Dulu bernama «Studio», bukan Auto Clip Studio.)"),
                ("Auto-Pilot", self.tab_autopilot,
                 "Auto-Pilot: rule yang otomatis menjadwalkan klip BARU dari Auto Clip Studio / "
                 "Content Clipper AI tanpa kamu klik apa pun."),
                ("Accounts", self.tab_accounts,
                 "Accounts: sambungkan akun YouTube / TikTok / Facebook / Instagram yang akan dipakai publish."),
                ("Queue", self.tab_queue,
                 "Queue: antrean upload — 1 baris = 1 clip × 1 platform. Batalkan, jeda, retry, atau hapus job di sini."),
                ("Presets", self.tab_recipes,
                 "Preset: template publish siap pakai (platform + jam + aturan review) yang bisa "
                 "dijalankan sekali klik atau diaktifkan berulang."),
                ("History & Analytics", self.tab_history,
                 "History: riwayat publish beserta link yang sudah tayang dan catatan kegagalan."))
        for name, widget, tip in tabs:
            self.tabs.addTab(widget, name)
            self.tabs.setTabToolTip(self.tabs.count() - 1, tip)
        self.tabs.currentChanged.connect(lambda _i: self.refresh_current())
        self.tabs.currentChanged.connect(lambda _i: self._tab_entered())

        # ---- events -----------------------------------------------------
        self.bridge = AutomationBridge(self)
        self.bridge.job.connect(self._on_job_event, Qt.QueuedConnection)
        self.bridge.run.connect(self._on_run_event, Qt.QueuedConnection)
        self.bridge.media.connect(self._on_media_event, Qt.QueuedConnection)
        self.bridge.auth.connect(self._on_auth_event, Qt.QueuedConnection)
        self.bridge.action.connect(self._on_action_event, Qt.QueuedConnection)
        self.bridge.failed.connect(self._on_failed_event, Qt.QueuedConnection)
        mgr = self._manager()
        if mgr is not None:
            self._remove_listener = mgr.add_listener(self._thread_event)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(350)
        self._debounce.timeout.connect(self.refresh_current)
        self._strip_pulse = QTimer(self)
        self._strip_pulse.setInterval(650)
        self._strip_pulse.timeout.connect(self._strip_pulse_tick)
        self._pulse_state = False
        self._tick = QTimer(self)
        self._tick.setInterval(8000)
        self._tick.timeout.connect(self.refresh_current)

        # Populating all seven tabs plus the token probe on page construction
        # used to stall the main window before it ever appeared. The page is
        # populated on first show (showEvent); _first_populate only catches
        # hosts that never send showEvent (e.g. offscreen tests).
        self._populated = False
        QTimer.singleShot(0, self, self._first_populate)

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _page_css(self, C: dict[str, str]) -> str:
        line = _mix_color(C["BORDER"], C["PANEL2"], 0.5)
        return f"""
        QWidget#automationPage {{ background: {C['BG']}; }}
        QFrame#autCard {{
            background: {C['PANEL']};
            border: 1px solid {C['BORDER']};
            border-radius: 14px;
        }}
        QFrame#autStrip {{
            background: {C['PANEL']};
            border: 1px solid {C['BORDER']};
            border-radius: 14px;
        }}
        QFrame#autChip {{
            background: {C['PANEL2']};
            border: 1px solid {C['BORDER']};
            border-radius: 10px;
        }}
        QLabel#autChipVal {{ color: {C['TEXT']}; font-size: 15px; font-weight: 800; }}
        QLabel#autChipCap {{ color: {C['MUTED']}; font-size: 9px; font-weight: 800; }}
        QLabel#autLive {{ font-size: 10px; font-weight: 800; color: {C['MUTED']}; }}
        QFrame#autFlow {{
            background: {C['PANEL']};
            border: 1px solid {C['BORDER']};
            border-radius: 14px;
        }}
        QLabel#autStep {{
            color: {C['TEXT']}; background: {C['PANEL2']};
            border: 1px solid {C['BORDER']}; border-radius: 10px;
            padding: 6px 10px; font-weight: 700; font-size: 11px;
        }}
        QFrame#autFlowLine {{ background: {line}; border: none; border-radius: 1px; }}
        QLabel#autComet {{ color: {C['GOLD']}; font-size: 12px; background: transparent; }}
        QLabel#autSteps {{ color: {C['GOLD']}; font-size: 12px; font-weight: 800; letter-spacing: 1px; }}
        QProgressBar#autStepsBar {{
            border: none; border-radius: 2px; background: {C['INPUT']};
            max-height: 4px; text-align: center; color: transparent;
        }}
        QProgressBar#autStepsBar::chunk {{
            border-radius: 2px;
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                        stop:0 {C['GOLD']}, stop:1 {C['GOLD2']});
        }}
        QLabel#autSection {{ color: {C['GOLD']}; font-size: 13px; font-weight: 700; letter-spacing: 1px; }}
        QLabel#autStatus {{ font-size: 12px; font-weight: 700; }}
        QLabel#muted {{ font-size: 13px; }}
        QLabel#autBig {{ color: {C['TEXT']}; font-size: 22px; font-weight: 800; }}
        QPushButton#autGhost {{
            background: transparent; border: 1px solid {C['BORDER']};
            border-radius: 10px; padding: 7px 14px; color: {C['TEXT']};
        }}
        QPushButton#autGhost:hover {{ background: {C['HOVER']}; border-color: {C['GOLD']}; }}
        QListWidget, QTableWidget {{
            background: {C['INPUT']}; border: 1px solid {C['BORDER']};
            border-radius: 12px; gridline-color: {C['BORDER']};
        }}
        QTableWidget::item {{ padding: 6px; }}
        QTableWidget::item:hover {{ background: {C['HOVER']}; }}
        QListWidget::item:hover {{ background: {C['HOVER']}; border-radius: 8px; }}
        QProgressBar#autBusy {{ border: none; border-radius: 4px; background: {C['INPUT']}; max-height: 6px; text-align: center; color: transparent; }}
        QProgressBar#autBusy::chunk {{ border-radius: 4px; background: {C['GOLD']}; }}
        QHeaderView::section {{ background: {C['PANEL']}; color: {C['MUTED']}; border: none; border-bottom: 1px solid {C['BORDER']}; padding: 8px; font-size: 11px; }}
        QTabWidget::pane {{ border: 1px solid {C['BORDER']}; border-radius: 14px; background: {C['BG']}; top: -1px; }}
        QTabBar::tab {{
            background: transparent; color: {C['MUTED']}; border: none;
            border-bottom: 2px solid transparent;
            padding: 10px 18px; margin-right: 4px;
            border-top-left-radius: 10px; border-top-right-radius: 10px;
            font-weight: 700; font-size: 12px;
        }}
        QTabBar::tab:hover {{ color: {C['TEXT']}; background: {C['HOVER']}; }}
        QTabBar::tab:selected {{
            color: {C['GOLD']}; background: {C['PANEL']};
            border-bottom: 2px solid {C['GOLD']};
        }}
        """

    def _manager(self):
        mgr = getattr(self.app, "automation", None)
        return mgr

    def _lbl(self, text: str, parent=None, muted: bool = False) -> QLabel:
        w = QLabel(text, parent or self)
        if muted:
            w.setObjectName("muted")
        w.setWordWrap(True)
        return w

    def _section(self, text: str) -> QLabel:
        w = QLabel(text, self)
        w.setObjectName("autSection")
        return w

    def _card(self) -> QFrame:
        f = QFrame(self)
        f.setObjectName("autCard")
        lay = QVBoxLayout(f)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)
        return f

    def _btn(self, text: str, slot, primary: bool = False) -> QPushButton:
        b = QPushButton(text, self)
        if primary:
            b.setObjectName("primary")
        else:
            b.setObjectName("autGhost")
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(slot)
        return b

    def _empty(self, title: str, body: str, action_text: str = "", action=None) -> QFrame:
        card = self._card()
        t = QLabel(title, card)
        t.setObjectName("h1")
        card.layout().addWidget(t)
        card.layout().addWidget(self._lbl(body, card, muted=True))
        if action_text and action:
            card.layout().addWidget(self._btn(action_text, action, primary=True))
        return card

    def _slide_in(self, widget: QWidget | None) -> None:
        """Small slide-in so switching wizard steps is actually visible."""
        if widget is None:
            return
        end = widget.pos()
        anim = QPropertyAnimation(widget, b"pos", widget)
        anim.setStartValue(QPoint(end.x() + 26, end.y()))
        anim.setEndValue(end)
        anim.setDuration(180)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start(QPropertyAnimation.DeleteWhenStopped)

    def _build_status_strip(self) -> QFrame:
        """Always-visible command bar: live indicator + one chip per headline."""
        strip = QFrame(self)
        strip.setObjectName("autStrip")
        lay = QHBoxLayout(strip)
        lay.setContentsMargins(12, 7, 12, 7)
        lay.setSpacing(8)

        self.strip_live_lbl = QLabel("○ Idle", strip)
        self.strip_live_lbl.setObjectName("autLive")
        lay.addWidget(self.strip_live_lbl)

        self.strip_values: dict[str, QLabel] = {}
        for key, cap in (("siap", "MEDIA SIAP"), ("antrean", "ANTREAN"),
                         ("jadwal", "TERJADWAL"), ("perhatian", "PERHATIAN"),
                         ("akun", "AKUN SEHAT")):
            chip = QFrame(strip)
            chip.setObjectName("autChip")
            cl = QVBoxLayout(chip)
            cl.setContentsMargins(10, 4, 10, 4)
            cl.setSpacing(0)
            val = QLabel("0", chip)
            val.setObjectName("autChipVal")
            cap_lbl = QLabel(cap, chip)
            cap_lbl.setObjectName("autChipCap")
            cl.addWidget(val)
            cl.addWidget(cap_lbl)
            self.strip_values[key] = val
            lay.addWidget(chip)
        lay.addStretch()
        return strip

    def _refresh_status_strip(self) -> None:
        """Refresh the header chips from the live manager (cheap, never raises)."""
        if not hasattr(self, "strip_values"):
            return
        mgr = self._manager()
        try:
            counts = mgr.job_counts() if mgr else {}
        except Exception:
            counts = {}
        try:
            media = mgr.media_counts() if mgr else {}
        except Exception:
            media = {}
        try:
            statuses = mgr.account_statuses() if mgr else {}
        except Exception:
            statuses = {}
        healthy = sum(1 for p in statuses.values()
                      if str(p.get("state") or "") == "connected")
        running = (int(counts.get("UPLOADING", 0)) + int(counts.get("PREPARING", 0))
                   + int(counts.get("AUTH_CHECK", 0)) + int(counts.get("VERIFYING", 0))
                   + int(counts.get("PROCESSING", 0)))
        self.strip_values["siap"].setText(
            str(int(media.get("acs", 0)) + int(media.get("cca", 0))
                + int(media.get("manual", 0))))
        self.strip_values["antrean"].setText(str(int(counts.get("QUEUED", 0)) + running))
        self.strip_values["jadwal"].setText(str(int(counts.get("SCHEDULED", 0))))
        self.strip_values["perhatian"].setText(
            str(int(counts.get("FAILED", 0)) + int(counts.get("ACTION_REQUIRED", 0))))
        self.strip_values["akun"].setText(str(healthy))
        self._set_running_state(bool(int(counts.get("QUEUED", 0)) or running))

    def _set_running_state(self, running: bool) -> None:
        self._job_running = bool(running)
        live = self.strip_live_lbl
        if running:
            live.setText("● LIVE")
            if not self._strip_pulse.isActive():
                self._pulse_state = False
                self._strip_pulse.start()
        else:
            live.setText("○ Idle")
            self._strip_pulse.stop()
            live.setStyleSheet(f"color: {self.C['MUTED']};")

    def _strip_pulse_tick(self) -> None:
        self._pulse_state = not self._pulse_state
        color = self.C["GREEN"] if self._pulse_state else self.C["GOLD"]
        self.strip_live_lbl.setStyleSheet(f"color: {color};")

    def _build_flow_preview(self) -> QFrame:
        """Pipeline map Media → AI Pack → Jadwal → Publish with a travelling
        comet that runs while jobs are queued (visual "where am I" preview)."""
        flow = QFrame(self)
        flow.setObjectName("autFlow")
        flow.setFixedHeight(46)
        lay = QHBoxLayout(flow)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(8)
        steps = (("1", "📥 Media"), ("2", "🧠 AI Pack"),
                 ("3", "🗓 Jadwal"), ("4", "🚀 Publish"))
        for i, (num, label) in enumerate(steps):
            pill = QLabel(f"{num}  {label}", flow)
            pill.setObjectName("autStep")
            pill.setAlignment(Qt.AlignCenter)
            lay.addWidget(pill, 1)
            if i < len(steps) - 1:
                connector = QFrame(flow)
                connector.setObjectName("autFlowLine")
                connector.setFixedHeight(2)
                lay.addWidget(connector, 2)
        comet = QLabel("●", flow)
        comet.setObjectName("autComet")
        comet.adjustSize()
        comet.hide()
        self._flow_comet = comet
        self._flow_container = flow
        self._flow_running = False
        self._flow_anim = QPropertyAnimation(comet, b"pos", self)
        self._flow_anim.setLoopCount(-1)
        return flow

    def _update_flow_state(self, running: bool) -> None:
        """Start/stop the travelling comet based on live automation activity."""
        self._flow_running = bool(running)
        if self._flow_running:
            self._start_flow_anim()
        else:
            try:
                self._flow_anim.stop()
            except Exception:
                pass
            self._flow_comet.hide()

    def _start_flow_anim(self) -> None:
        try:
            c = self._flow_container
            comet = self._flow_comet
            if comet is None or c is None or not self._flow_running or c.width() < 80:
                if comet is not None:
                    comet.hide()
                return
            comet.adjustSize()
            y = max(2, (c.height() - comet.height()) // 2)
            self._flow_anim.stop()
            self._flow_anim.setDuration(2400)
            self._flow_anim.setEasingCurve(QEasingCurve.Linear)
            self._flow_anim.setStartValue(QPoint(6, y))
            self._flow_anim.setEndValue(
                QPoint(max(12, c.width() - comet.width() - 8), y))
            comet.setVisible(True)
            comet.raise_()
            self._flow_anim.start()
        except Exception:
            try:
                self._flow_comet.hide()
            except Exception:
                pass

    def _tab_entered(self) -> None:
        """Soft entrance for the tab that just became current."""
        w = self.tabs.currentWidget() if hasattr(self, "tabs") else None
        if w is not None:
            self._fade_in(w, delay_ms=40)

    # ------------------------------------------------------------------ #
    # Overview
    # ------------------------------------------------------------------ #
    def _build_overview(self) -> QWidget:
        w = QWidget()
        page_lay = QVBoxLayout(w)
        page_lay.setContentsMargins(0, 0, 0, 0)
        page_lay.setSpacing(0)
        scroll = QScrollArea(w)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        inner = QWidget(scroll)
        inner.setMinimumHeight(780)
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(6, 10, 6, 6)
        lay.setSpacing(10)
        page_lay.addWidget(scroll)
        scroll.setWidget(inner)

        lay.addWidget(self._build_flow_preview())

        health = self._card()
        hl = QHBoxLayout()
        hl.addWidget(self._section("AUTOMATION HEALTH"))
        hl.addStretch()
        self.health_items = QLabel("—", health)
        self.health_items.setObjectName("muted")
        hl.addWidget(self.health_items)
        health.layout().addLayout(hl)
        lay.addWidget(health)

        # Readiness checklist: one glance answering "why is nothing publishing
        # yet?" instead of leaving the user to hunt through four tabs. Every
        # unchecked row carries the button that fixes it.
        ready = self._card()
        rhead = QHBoxLayout()
        rhead.addWidget(self._section("SIAP PUBLISH?"))
        rhead.addStretch()
        self.ready_summary = QLabel("—", ready)
        self.ready_summary.setObjectName("muted")
        rhead.addWidget(self.ready_summary)
        ready.layout().addLayout(rhead)
        self.ready_box = QVBoxLayout()
        ready.layout().addLayout(self.ready_box)
        self.pipeline_lbl = self._lbl("", ready, muted=True)
        ready.layout().addWidget(self.pipeline_lbl)
        self.blocked_banner = self._lbl("", ready, muted=True)
        self.blocked_banner.hide()
        ready.layout().addWidget(self.blocked_banner)
        lay.addWidget(ready)

        lay.addWidget(self._section("ACTIVE AUTOMATIONS"))
        self.active_box = QVBoxLayout()
        active_wrap = QWidget()
        active_wrap.setLayout(self.active_box)
        lay.addWidget(active_wrap)

        lay.addWidget(self._section("QUEUE"))
        queue_card = self._card()
        self.counts_row = QHBoxLayout()
        self.count_labels: dict[str, QLabel] = {}
        for key in ("QUEUED", "RUNNING", "SCHEDULED", "ACTION_REQUIRED", "FAILED", "PUBLISHED"):
            box = QVBoxLayout()
            num = QLabel("0", queue_card)
            num.setObjectName("autBig")
            cap = QLabel(human_job_status(key), queue_card)
            cap.setObjectName("muted")
            box.addWidget(num)
            box.addWidget(cap)
            self.count_labels[key] = num
            self.counts_row.addLayout(box)
        self.counts_row.addStretch()
        queue_card.layout().addLayout(self.counts_row)
        lay.addWidget(queue_card)

        lay.addWidget(self._section("READY MEDIA (RENDER → AUTOMATION)"))
        # One stable line that explains where this content comes from, so the
        # Overview answers "how do ACS / CCA connect to Automation?" without
        # the user hunting through Settings.
        src_hint = self._lbl(
            "Hasil render Auto Clip Studio dan export Content Clipper AI masuk ke "
            "sini otomatis (lihat Settings → Automation). Centang clip di tab "
            "Studio untuk membuat automation.", self, muted=True)
        lay.addWidget(src_hint)
        self.media_card = self._card()
        lay.addWidget(self.media_card)
        lay.addStretch()
        return w

    def _refresh_overview(self) -> None:
        mgr = self._manager()
        C = self.C
        # health
        try:
            statuses = mgr.account_statuses() if mgr else {}
            healthy = sum(1 for p in statuses.values() if p.get("state") == "connected")
            counts = mgr.job_counts() if mgr else {}
            sched = int(counts.get("SCHEDULED", 0))
            failed = int(counts.get("FAILED", 0)) + int(counts.get("ACTION_REQUIRED", 0))
            self.health_items.setText(f"● {healthy} akun sehat   ● {sched} terjadwal   ● {failed} butuh perhatian")
        except Exception as exc:
            self.health_items.setText(f"Status: {exc}")
        # counts
        try:
            counts = mgr.job_counts() if mgr else {}
            running = int(counts.get("UPLOADING", 0)) + int(counts.get("PREPARING", 0)) + int(counts.get("AUTH_CHECK", 0)) + int(counts.get("VERIFYING", 0)) + int(counts.get("PROCESSING", 0))
            values = {"QUEUED": int(counts.get("QUEUED", 0)), "RUNNING": running, "SCHEDULED": int(counts.get("SCHEDULED", 0)), "ACTION_REQUIRED": int(counts.get("ACTION_REQUIRED", 0)), "FAILED": int(counts.get("FAILED", 0)), "PUBLISHED": int(counts.get("PUBLISHED", 0))}
            for key, lbl in self.count_labels.items():
                lbl.setText(str(values.get(key, 0)))
                _, color = _status_style(key, C)
                lbl.setStyleSheet(f"color: {color};")
            self._flow_active = bool(running or int(counts.get("QUEUED", 0)))
        except Exception:
            self._flow_active = False
        # active runs
        while self.active_box.count():
            item = self.active_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                sub = item.layout()
                while sub.count():
                    s = sub.takeAt(0)
                    if s.widget():
                        s.widget().deleteLater()
        try:
            runs = [r for r in (mgr.db.automation_list_runs(limit=60) if mgr else []) if str(r.get("status")) in RUN_ACTIVE or str(r.get("status")) in ("ACTION_REQUIRED", "PARTIAL_SUCCESS")][:6]
        except Exception:
            runs = []
        if not runs:
            self.active_box.addWidget(self._empty("Belum ada automation aktif", "Render clip kamu, lalu Create Automation untuk publish ke YouTube, TikTok, Facebook, atau Instagram.", "+ Create Automation", lambda: (self.tabs.setCurrentIndex(self.TAB_STUDIO), self._reset_wizard())))
            self.active_box.addWidget(self._empty("Belum ada media siap publish", "Render dari Auto Clip Studio otomatis muncul di sini sebagai media siap publish.", "", None))
        else:
            for run in runs:
                self.active_box.addWidget(self._run_card(run))
        # readiness checklist + "why is it not running?" banner
        self._refresh_readiness()
        # ready media
        self._refresh_media_card()
        # travelling comet = "sesuatu sedang berjalan" preview
        self._update_flow_state(getattr(self, "_flow_active", False))

    def _refresh_readiness(self) -> None:
        """"Siap Publish?" checklist and the "kenapa belum jalan?" banner.

        Answers the two questions the previous Overview could not: *what is
        still missing before my clips can publish* and *why is Auto-Pilot not
        doing anything*. Every failing row carries the button that fixes it, and
        the banner lists the real blockers (Settings switches + per-rule
        `blocking_reasons` from autopilot.blocking_reasons).
        """
        if getattr(self, "ready_box", None) is None:
            return
        data = self._readiness_data()
        while self.ready_box.count():
            item = self.ready_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                sub = item.layout()
                while sub.count():
                    s = sub.takeAt(0)
                    if s.widget():
                        s.widget().deleteLater()
        checks = data["checks"]
        ok_count = sum(1 for ok, *_ in checks if ok)
        self.ready_summary.setText(f"{ok_count}/{len(checks)} siap")
        self.ready_summary.setStyleSheet(
            f"color: {self.C['GREEN'] if ok_count == len(checks) else self.C['GOLD']};")
        for ok, text, action_text, action in checks:
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            mark = QLabel("✓" if ok else "✗", row)
            mark.setStyleSheet(
                f"color: {self.C['GREEN'] if ok else self.C['RED']}; font-weight: 700;")
            rl.addWidget(mark)
            rl.addWidget(self._lbl(text, row, muted=ok))
            rl.addStretch()
            if not ok:
                rl.addWidget(self._btn(action_text, action))
            self.ready_box.addWidget(row)
        self.pipeline_lbl.setText(
            f"Alur: Auto Clip Studio / Content Clipper AI → {data['ready_count']} media siap → "
            f"{data['rules_active']} rule aktif → {data['queued']} antrean → "
            f"{data['published']} terkirim.")
        reasons = data["reasons"]
        if reasons:
            self.blocked_banner.setText("Kenapa belum jalan?  •  " + "  •  ".join(reasons))
            self.blocked_banner.setStyleSheet(f"color: {self.C['GOLD']};")
            self.blocked_banner.show()
        else:
            self.blocked_banner.setText("")
            self.blocked_banner.hide()
        # Keep the Presets quick-start card's live line in sync too.
        if getattr(self, "preset_ready_lbl", None) is not None:
            self.preset_ready_lbl.setText(
                ("✅ Siap: " if ok_count == len(checks) else f"⚠ {ok_count}/{len(checks)} siap: ")
                + (data["summary"] or "semua syarat publish terpenuhi."))

    def _readiness_data(self) -> dict:
        """Single source of truth for "can this publish yet?" across tabs.

        Returns the checklist rows (with their jump buttons), the human
        blockers, and the pipeline counters used by both the Overview card and
        the Presets quick-start card.
        """
        mgr = self._manager()
        try:
            statuses = mgr.account_statuses() if mgr else {}
        except Exception:
            statuses = {}
        connected = sum(1 for p in statuses.values()
                        if str(p.get("state")) == "connected")
        try:
            ready_rows = mgr.list_media("ready") if mgr else []
        except Exception:
            ready_rows = []
        ready_count = len([r for r in ready_rows
                           if Path(str(r.get("media_path") or "")).exists()])
        try:
            rules = mgr.list_rules() if mgr else []
        except Exception:
            rules = []
        enabled_rules = [r for r in rules if r.get("enabled")]
        acs_on = bool(self.app.config.get("aut_acs_handoff", True))
        cca_on = bool(self.app.config.get("aut_cca_handoff", True))
        auto_on = True
        try:
            auto_on = bool(mgr.autopilot.automatic_enabled()) if mgr else True
        except Exception:
            auto_on = True
        try:
            counts = mgr.job_counts() if mgr else {}
        except Exception:
            counts = {}
        queued = int(counts.get("QUEUED", 0)) + int(counts.get("SCHEDULED", 0))
        published = int(counts.get("PUBLISHED", 0))

        # (ok, text) — the most-blocking checks first, so the top row is the
        # thing the user should fix first.
        checks = [
            (connected > 0,
             f"Akun platform terhubung ({connected})",
             "Buka Accounts", lambda: self.tabs.setCurrentIndex(self.TAB_ACCOUNTS)),
            (ready_count > 0,
             f"Media siap publish ({ready_count} clip)",
             "Buka Create", lambda: (self.tabs.setCurrentIndex(self.TAB_STUDIO),
                                     self._reset_wizard())),
            ((acs_on or cca_on),
             "Handoff otomatis aktif (Auto Clip Studio / Content Clipper AI)",
             "Buka Settings → Automation", lambda: self.navigate.emit("settings")),
            (auto_on,
             "Auto-Pilot global aktif (Settings → Automation)",
             "Buka Settings → Automation", lambda: self.navigate.emit("settings")),
            (bool(enabled_rules),
             f"Rule Auto-Pilot aktif ({len(enabled_rules)})",
             "Buka Auto-Pilot", lambda: self.tabs.setCurrentIndex(self.TAB_AUTOPILOT)),
        ]

        # "Kenapa belum jalan?" — only the things that actually stop publishing.
        reasons: list[str] = []
        if not acs_on and not cca_on:
            reasons.append("Handoff Auto Clip Studio & Content Clipper AI dimatikan "
                           "(Settings → Automation): klip baru tidak masuk daftar.")
        elif not acs_on:
            reasons.append("Handoff Auto Clip Studio dimatikan (Settings → Automation).")
        elif not cca_on:
            reasons.append("Handoff Content Clipper AI dimatikan (Settings → Automation).")
        if not auto_on:
            reasons.append("Auto-Pilot global DIMATIKAN (Settings → Automation) — "
                           "rule tidak berjalan otomatis.")
        if not connected:
            reasons.append("Belum ada akun platform terhubung (Accounts).")
        if enabled_rules and ready_count == 0:
            reasons.append("Belum ada media siap — rule Auto-Pilot menunggu klip baru.")
        for rule in enabled_rules:
            try:
                blocked = list((mgr.rule_status(rule["id"]) if mgr else {}).get("blocked") or [])
            except Exception:
                blocked = []
            if blocked:
                reasons.append(f"Rule «{rule.get('name') or 'Rule'}»: " + "; ".join(blocked))
        return {
            "checks": checks,
            "reasons": reasons,
            "summary": "; ".join(reasons),
            "ready_count": ready_count,
            "rules_active": len(enabled_rules),
            "queued": queued,
            "published": published,
        }

    def _run_card(self, run: dict) -> QFrame:
        card = self._card()
        mgr = self._manager()
        row = QHBoxLayout()
        name = QLabel(str(run.get("name") or "Automation"), card)
        name.setStyleSheet(f"color: {self.C['TEXT']}; font-weight: 700; font-size: 14px;")
        row.addWidget(name)
        meta = safe_json(run.get("metadata"))
        plats = ", ".join(PLATFORM_LABELS.get(p, p) for p in (meta.get("platforms") or []))
        row.addWidget(self._lbl(plats or "—", card, muted=True))
        row.addStretch()
        status = str(run.get("status") or "")
        pill = QLabel(status.replace("_", " ").title(), card)
        pill.setObjectName("autStatus")
        _, color = _status_style(status, self.C)
        pill.setStyleSheet(f"color: {color}; background: {self.C['INPUT']}; border: 1px solid {self.C['BORDER']}; border-radius: 8px; padding: 3px 10px;")
        row.addWidget(pill)
        card.layout().addLayout(row)
        try:
            jobs = mgr.db.automation_list_jobs(run_id=str(run["id"]), limit=500) if mgr else []
            total = len(jobs)
            done = sum(1 for j in jobs if str(j.get("status")) in ("PUBLISHED", "SCHEDULED", "CANCELLED"))
            info = f"{done}/{total} jobs"
            if run.get("scheduled_at"):
                info += f"  •  jadwal {run.get('scheduled_at')} ({humanize_when(str(run.get('scheduled_at')))})"
        except Exception:
            info = ""
        row2 = QHBoxLayout()
        row2.addWidget(self._lbl(info, card, muted=True))
        row2.addStretch()
        if status == "READY":
            row2.addWidget(self._btn("Start", lambda _=False, rid=str(run.get("id")): self._start_run(rid), primary=True))
        row2.addWidget(self._btn("Manage", lambda _=False: self.tabs.setCurrentIndex(self.TAB_QUEUE)))
        row2.addWidget(self._btn("✕ Batalkan", lambda _=False, rid=str(run.get("id")): self._cancel_run(rid)))
        card.layout().addLayout(row2)
        return card

    def _start_run(self, run_id: str) -> None:
        mgr = self._manager()
        if mgr and mgr.start_ready_run(run_id):
            self.refresh_all()

    def _cancel_run(self, run_id: str) -> None:
        if QMessageBox.question(self, "Batalkan Automation", "Batalkan automation beserta job yang belum selesai?") != QMessageBox.Yes:
            return
        mgr = self._manager()
        if mgr:
            mgr.cancel_run(run_id)
            self.refresh_all()

    def _refresh_media_card(self) -> None:
        # Every refresh used to drop the row *layouts* while leaving their
        # labels alive as children of the card, so old rows stayed painted at
        # their previous geometry and each tick drew a new row on top of them
        # ("tulisan numpuk"). Detach every row, then delete all leftover child
        # widgets in one pass before drawing the new content.
        lay = self.media_card.layout()
        while lay.count():
            item = lay.takeAt(0)
            sub = item.layout()
            if sub is not None:
                while sub.count():
                    sub.takeAt(0)
                sub.deleteLater()
        for child in list(self.media_card.findChildren(
                QWidget, options=Qt.FindDirectChildrenOnly)):
            child.deleteLater()
        mgr = self._manager()
        try:
            rows = mgr.list_media("ready") if mgr else []
            rows = [r for r in rows if Path(str(r.get("media_path") or "")).exists()]
        except Exception:
            rows = []
        if not rows:
            self.media_card.layout().addWidget(self._lbl("Belum ada. Render dari Auto Clip Studio akan otomatis muncul di sini, atau tambahkan file manual di tab Create.", self.media_card, muted=True))
            return
        for row in rows[:8]:
            item_row = QHBoxLayout()
            label = str(row.get("title") or Path(str(row.get("media_path"))).name)
            item_row.addWidget(self._lbl(label, self.media_card))
            item_row.addWidget(self._lbl(f"[{row.get('source')}] • {humanize_when(str(row.get('created_at') or ''))}", self.media_card, muted=True))
            item_row.addStretch()
            self.media_card.layout().addLayout(item_row)
        if len(rows) > 8:
            self.media_card.layout().addWidget(self._lbl(f"+{len(rows) - 8} lainnya…", self.media_card, muted=True))

    # ------------------------------------------------------------------ #
    # Create wizard
    # ------------------------------------------------------------------ #
    def _build_create(self) -> QWidget:
        outer = QWidget()
        o_lay = QVBoxLayout(outer)
        o_lay.setContentsMargins(6, 10, 6, 6)
        o_lay.setSpacing(8)

        head = QWidget(outer)
        h_lay = QVBoxLayout(head)
        h_lay.setContentsMargins(0, 0, 0, 0)
        h_lay.setSpacing(4)
        self.step_lbl = QLabel("", self)
        self.step_lbl.setObjectName("autSteps")
        h_lay.addWidget(self.step_lbl)
        self.step_progress = QProgressBar(head)
        self.step_progress.setObjectName("autStepsBar")
        self.step_progress.setRange(0, 100)
        self.step_progress.setValue(25)
        self.step_progress.setTextVisible(False)
        self.step_progress.setFixedHeight(4)
        h_lay.addWidget(self.step_progress)
        o_lay.addWidget(head)

        # The wizard steps (especially the AI Content Pack form) are taller
        # than the window on smaller screens — the whole step scrolls instead
        # of clipping the description / per-video pack list.
        scroll = QScrollArea(outer)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        inner = QWidget(scroll)
        inner.setMinimumHeight(780)
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, 0, 6, 0)
        lay.setSpacing(10)
        self.wizard = QStackedWidget(inner)
        lay.addWidget(self.wizard, 1)
        scroll.setWidget(inner)
        o_lay.addWidget(scroll, 1)

        nav = QHBoxLayout()
        nav.addStretch()
        self.back_btn = self._btn("Kembali", lambda: self._wizard_step(self.wizard.currentIndex() - 1))
        self.next_btn = self._btn("Lanjut", lambda: self._wizard_next(), primary=True)
        nav.addWidget(self.back_btn)
        nav.addWidget(self.next_btn)
        o_lay.addLayout(nav)

        self.wizard.addWidget(self._step_content())
        self.wizard.addWidget(self._step_platforms())
        self.wizard.addWidget(self._step_schedule())
        self.wizard.addWidget(self._step_review())
        self._wizard_step(0)
        return outer

    def _step_content(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        card = self._card()
        card.layout().addWidget(self._section("LANGKAH 1 — KONTEN"))
        card.layout().addWidget(self._lbl(
            "Cara pakai: pilih sumber di bawah → centang video yang mau dijadwalkan (atau "
            "tarik & lepas file video langsung ke daftar) → "
            "(opsional) Generate AI Content Pack → Next ke Platform & waktu → Start. "
            "Hanya video tercentang yang dibaca di langkah berikutnya.", card, muted=True))
        top = QHBoxLayout()
        top.addWidget(self._btn("Pilih file…", self._pick_files))
        top.addWidget(self._btn("Muat dari media siap", lambda: (self._reload_media_list(), self._sync_next())))
        top.addStretch()
        card.layout().addLayout(top)
        filt = QHBoxLayout()
        self.media_source_filter = QComboBox(card)
        self.media_source_filter.addItems([label for label, _key in MEDIA_SOURCE_CHOICES])
        self.media_source_filter.currentTextChanged.connect(
            lambda _t: (self._update_source_hint(), self._reload_media_list()))
        filt.addWidget(self.media_source_filter)
        self.media_search = QLineEdit(card)
        self.media_search.setPlaceholderText("Cari judul clip...")
        self.media_search.textChanged.connect(lambda _t: self._reload_media_list())
        filt.addWidget(self.media_search, 1)
        filt.addWidget(self._btn("Select All", lambda _=False: self._set_all_media_checks(True)))
        filt.addWidget(self._btn("Deselect All", lambda _=False: self._set_all_media_checks(False)))
        card.layout().addLayout(filt)
        self.media_source_hint = self._lbl("", card, muted=True)
        card.layout().addWidget(self.media_source_hint)
        self._update_source_hint()

        self.media_list = _MediaListWidget(card)
        try:
            self.media_list.setPlaceholderText(
                "Tarik & lepas file video di sini, atau klik «Pilih file…»")
        except Exception:
            pass
        self.media_list.setMinimumHeight(180)
        self.media_list.dropped.connect(self._on_files_dropped)
        self.media_list.itemChanged.connect(
            lambda _i: (self._update_media_count(), self._mark_pack_stale(),
                        self._sync_pack_form()))
        self.media_list.currentItemChanged.connect(
            lambda _c, _p: (self._mark_pack_stale(), self._sync_pack_form()))
        card.layout().addWidget(self.media_list)
        edit_row = QHBoxLayout()
        edit_row.addWidget(self._btn("🗑 Hapus Terpilih", self._remove_selected_media))
        edit_row.addWidget(self._btn("🗑 Bersihkan Daftar", self._clear_media_list))
        edit_row.addStretch()
        card.layout().addLayout(edit_row)
        foot = QHBoxLayout()
        self.media_count_lbl = self._lbl("0 video dipilih", card, muted=True)
        foot.addWidget(self.media_count_lbl)
        foot.addStretch()
        self.media_reject_lbl = self._lbl("", card, muted=True)
        # Clickable: "N konten ditolak validasi" opens the per-clip reasons
        # (the reason existed in the DB but was previously only a count).
        self.media_reject_lbl.setOpenExternalLinks(False)
        self.media_reject_lbl.setTextInteractionFlags(Qt.LinksAccessibleByMouse)
        self.media_reject_lbl.linkActivated.connect(self._show_rejected_media)
        foot.addWidget(self.media_reject_lbl)
        card.layout().addLayout(foot)
        lay.addWidget(card)

        pack_card = self._card()
        pack_head = QHBoxLayout()
        pack_head.addWidget(self._section("AI CONTENT PACK"))
        self.pack_state = self._lbl(
            "Belum ada — klik «Generate AI Content Pack» atau isi manual.",
            pack_card, muted=True)
        pack_head.addWidget(self.pack_state)
        pack_head.addStretch()
        self.pack_gen_btn = self._btn("Generate AI Content Pack", self._generate_pack, primary=True)
        self.pack_cancel_btn = self._btn("Batalkan Generate", self._cancel_pack)
        self.pack_cancel_btn.setEnabled(False)
        pack_head.addWidget(self.pack_gen_btn)
        pack_head.addWidget(self.pack_cancel_btn)
        pack_card.layout().addLayout(pack_head)
        self.pack_scope_lbl = self._lbl("", pack_card, muted=True)
        pack_card.layout().addWidget(self.pack_scope_lbl)
        self.pack_progress = QProgressBar(pack_card)
        self.pack_progress.setObjectName("autBusy")
        self.pack_progress.setRange(0, 0)
        self.pack_progress.setTextVisible(False)
        self.pack_progress.setFixedHeight(6)
        self.pack_progress.hide()
        pack_card.layout().addWidget(self.pack_progress)
        model_row = QHBoxLayout()
        model_row.addWidget(self._lbl("Model transkripsi lokal", pack_card, muted=True))
        self.pack_model_combo = QComboBox(pack_card)
        try:
            from chopster.automation.local_transcribe import PACK_MODELS, pack_whisper_model
            self.pack_model_combo.addItems(list(PACK_MODELS))
            self.pack_model_combo.setCurrentText(pack_whisper_model(self.app.config))
        except Exception:
            self.pack_model_combo.addItems(["small", "medium", "large-v2", "large-v3"])
            self.pack_model_combo.setCurrentText("small")
        self.pack_model_combo.currentTextChanged.connect(self._save_pack_model)
        model_row.addWidget(self.pack_model_combo)
        model_row.addWidget(self._lbl(
            "Dipakai hanya untuk video yang belum punya transkrip sendiri (mis. file dari "
            "komputer). Minimal small — tiny/base terlalu berisik untuk deskripsi AI.",
            pack_card, muted=True))
        model_row.addStretch()
        pack_card.layout().addLayout(model_row)
        lang_row = QHBoxLayout()
        lang_row.addWidget(self._lbl("Bahasa judul/deskripsi", pack_card, muted=True))
        self.pack_lang_combo = QComboBox(pack_card)
        for _lbl_lang, _val_lang in (("Ikuti bahasa video (deteksi otomatis)", "auto"),
                                     ("Indonesia", "id"),
                                     ("English (Inggris)", "en")):
            self.pack_lang_combo.addItem(_lbl_lang, _val_lang)
        _saved_lang = ""
        try:
            _saved_lang = str(self.app.config.get("aut_pack_language") or "auto").strip().lower()
        except Exception:
            _saved_lang = "auto"
        _idx_lang = self.pack_lang_combo.findData(_saved_lang if _saved_lang in ("auto", "id", "en") else "auto")
        self.pack_lang_combo.setCurrentIndex(max(0, _idx_lang))
        self.pack_lang_combo.currentIndexChanged.connect(self._save_pack_lang)
        lang_row.addWidget(self.pack_lang_combo)
        lang_row.addWidget(self._lbl(
            "«Ikuti bahasa video» memakai bahasa yang terdeteksi dari transkrip clip "
            "ini, jadi video Inggris mendapat judul/deskripsi Inggris.",
            pack_card, muted=True))
        lang_row.addStretch()
        pack_card.layout().addLayout(lang_row)
        form = QVBoxLayout()
        form.addWidget(self._lbl("Title", pack_card, muted=True))
        self.title_combo = QComboBox(pack_card)
        self.title_combo.setEditable(True)
        self.title_combo.currentTextChanged.connect(lambda _t: self._sync_next())
        form.addWidget(self.title_combo)
        form.addWidget(self._lbl("Description (YouTube style)", pack_card, muted=True))
        self.desc_edit = QTextEdit(pack_card)
        self.desc_edit.setAcceptRichText(False)
        self.desc_edit.setMinimumHeight(130)
        form.addWidget(self.desc_edit)
        form.addWidget(self._lbl("Caption (TikTok / Instagram / Facebook)", pack_card, muted=True))
        self.caption_edit = QLineEdit(pack_card)
        self.caption_edit.setPlaceholderText("Caption per platform — AI menyesuaikan…")
        form.addWidget(self.caption_edit)
        form.addWidget(self._lbl("Hashtags (spasi)", pack_card, muted=True))
        self.tags_edit = QLineEdit(pack_card)
        self.tags_edit.setPlaceholderText("#shorts #podcast #clip")
        form.addWidget(self.tags_edit)
        pack_card.layout().addLayout(form)
        # Per-video pack status: the form above edits ONE video at a time, so
        # this list answers "which of my checked clips already has pack?" and
        # lets the user jump straight to the video whose pack needs attention.
        pack_card.layout().addWidget(self._section("PACK PER VIDEO"))
        self.pack_video_box = QVBoxLayout()
        pack_card.layout().addLayout(self.pack_video_box)
        lay.addWidget(pack_card)
        lay.addStretch()
        return w

    def _step_platforms(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        card = self._card()
        card.layout().addWidget(self._section("LANGKAH 2 — PLATFORM TUJUAN"))
        self.platform_checks: dict[str, QCheckBox] = {}
        self.platform_status: dict[str, QLabel] = {}
        for platform in PLATFORMS:
            row = QHBoxLayout()
            cb = QCheckBox(PLATFORM_LABELS[platform], card)
            cb.setCursor(Qt.PointingHandCursor)
            st = QLabel("—", card)
            st.setObjectName("autStatus")
            connect = self._btn("Connect", lambda _=False, p=platform: (self.tabs.setCurrentIndex(self.TAB_ACCOUNTS), self._focus_connect(p)))
            row.addWidget(cb)
            row.addWidget(st)
            row.addStretch()
            row.addWidget(connect)
            card.layout().addLayout(row)
            self.platform_checks[platform] = cb
            self.platform_status[platform] = st
            cb.toggled.connect(lambda _c, p=platform: (self._update_platform_row(p), self._sync_next()))
        hint = self._lbl(
            "Di bawah ada privasi dan pilihan audience YouTube "
            "«ditonton untuk anak-anak atau tidak» — YouTube mewajibkan "
            "pilihan itu sebelum upload.", card, muted=True)
        card.layout().addWidget(hint)
        self.adv_check = QCheckBox(
            "Advanced — privasi & YouTube audience (made for kids)", card)
        self.adv_check.toggled.connect(lambda _v: self._toggle_advanced())
        card.layout().addWidget(self.adv_check)
        self.adv_row = QHBoxLayout()
        self.privacy_combo = QComboBox(card)
        self.privacy_combo.addItems(["Default platform", "Public", "Unlisted", "Private"])
        self.adv_row.addWidget(QLabel("Privacy", card))
        self.adv_row.addWidget(self.privacy_combo)
        self.mfk_combo = QComboBox(card)
        self.mfk_combo.addItems(["Made for kids: belum dipilih",
                                 "Ya — dibuat untuk anak-anak",
                                 "Tidak — bukan untuk anak-anak"])
        self.adv_row.addWidget(QLabel("YouTube audience", card))
        self.adv_row.addWidget(self.mfk_combo)
        self.adv_row.addStretch()
        self.adv_wrap = QWidget(card)
        self.adv_wrap.setLayout(self.adv_row)
        self.adv_wrap.hide()
        card.layout().addWidget(self.adv_wrap)
        # Visible by default: an unanswered audience question blocks every
        # YouTube upload, so it must not hide behind an unchecked box.
        self.adv_check.setChecked(True)
        lay.addWidget(card)
        lay.addStretch()
        return w

    def _toggle_advanced(self) -> None:
        self.adv_wrap.setVisible(self.adv_check.isChecked())

    def _made_for_kids_value(self):
        """Persisted YouTube audience declaration (None = not answered yet)."""
        text = self.mfk_combo.currentText() or ""
        if text.startswith("Ya"):
            return True
        if text.startswith("Tidak"):
            return False
        return None

    def _focus_connect(self, platform: str) -> None:
        self._wizard_step(1)
        self.tabs.setCurrentIndex(self.TAB_ACCOUNTS)
        row = self.account_rows.get(platform) or {}
        btn = row.get("connect")
        if btn:
            btn.setFocus()

    def _step_schedule(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        card = self._card()
        card.layout().addWidget(self._section("LANGKAH 3 — WAKTU"))
        self.now_radio = QRadioButton("Publish immediately", card)
        self.sched_radio = QRadioButton("Schedule", card)
        self.now_radio.setChecked(True)
        card.layout().addWidget(self.now_radio)
        card.layout().addWidget(self.sched_radio)
        dt_row = QHBoxLayout()
        self.date_edit = QDateEdit(card)
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDate(QDate.currentDate().addDays(1))
        self.time_edit = QTimeEdit(card)
        self.time_edit.setTime(QTime(19, 0))
        dt_row.addWidget(QLabel("Date", card))
        dt_row.addWidget(self.date_edit)
        dt_row.addWidget(QLabel("Time", card))
        dt_row.addWidget(self.time_edit)
        dt_row.addStretch()
        card.layout().addLayout(dt_row)
        self.stagger_radio = QRadioButton("Staggered (satu video per interval)", card)
        card.layout().addWidget(self.stagger_radio)
        st = QHBoxLayout()
        st.addWidget(QLabel("Interval", card))
        self.stagger_interval = QSpinBox(card)
        self.stagger_interval.setRange(1, 1440)
        self.stagger_interval.setValue(120)
        self.stagger_interval.setSuffix(" menit")
        st.addWidget(self.stagger_interval)
        st.addWidget(QLabel("Jam operasional", card))
        self.stagger_from = QTimeEdit(card)
        self.stagger_from.setTime(QTime(10, 0))
        self.stagger_to = QTimeEdit(card)
        self.stagger_to.setTime(QTime(23, 0))
        st.addWidget(self.stagger_from)
        st.addWidget(self.stagger_to)
        st.addStretch()
        card.layout().addLayout(st)
        self.stagger_preview = self._lbl("", card, muted=True)
        card.layout().addWidget(self.stagger_preview)
        self.stagger_interval.valueChanged.connect(lambda _v: self._preview_stagger())
        self.stagger_from.timeChanged.connect(lambda _t: self._preview_stagger())
        self.stagger_to.timeChanged.connect(lambda _t: self._preview_stagger())
        self.stagger_radio.toggled.connect(lambda _v: self._preview_stagger())

        note = self._lbl("Scheduler berjalan lokal di komputer kamu (local-first). Job bertahan setelah restart aplikasi.", card, muted=True)
        card.layout().addWidget(note)
        self.sched_radio.toggled.connect(lambda _v: self._sync_next())
        lay.addWidget(card)
        lay.addStretch()
        return w

    def _step_review(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        card = self._card()
        card.layout().addWidget(self._section("LANGKAH 4 — REVIEW & JALANKAN"))
        self.review_lines: list[QLabel] = []
        for _ in range(9):
            lbl = QLabel("—", card)
            lbl.setWordWrap(True)
            card.layout().addWidget(lbl)
            self.review_lines.append(lbl)
        self.review_media_lbl = QLabel("—", card)
        self.review_media_lbl.setWordWrap(True)
        card.layout().addWidget(self.review_media_lbl)
        self.review_pack_lbl = QLabel("—", card)
        self.review_pack_lbl.setWordWrap(True)
        card.layout().addWidget(self.review_pack_lbl)
        preset_row = QHBoxLayout()
        preset_row.addWidget(self._lbl("Preset", card, muted=True))
        self.preset_combo = QComboBox(card)
        preset_row.addWidget(self.preset_combo)
        preset_row.addWidget(self._btn("↧ Terapkan ke wizard", self._apply_preset))
        preset_row.addWidget(self._lbl(
            "Preset = platform + waktu + aturan yang tersimpan (kelola di tab "
            "Presets).", card, muted=True))
        preset_row.addStretch()
        card.layout().addLayout(preset_row)
        self.approval_review = QCheckBox(
            "Review sebelum publish (konten menunggu persetujuan kamu)", card)
        card.layout().addWidget(self.approval_review)
        self.acs_template_check = QCheckBox(
            "⚡ Aktifkan sebagai template publish 1-klik dari ACS", card)
        card.layout().addWidget(self.acs_template_check)
        tmpl_row = QHBoxLayout()
        tmpl_row.addWidget(QLabel("Privacy", card))
        self.template_privacy = QComboBox(card)
        self.template_privacy.addItems(["Public", "Unlisted", "Private"])
        self.template_privacy.setCurrentText("Public")
        tmpl_row.addWidget(self.template_privacy)
        tmpl_row.addWidget(QLabel("Made for Kids", card))
        self.template_mfk = QComboBox(card)
        self.template_mfk.addItems(["Tidak", "Ya"])
        self.template_mfk.setCurrentText("Tidak")
        tmpl_row.addWidget(self.template_mfk)
        tmpl_row.addWidget(QLabel("Bahasa AI", card))
        self.template_lang = QComboBox(card)
        for _lbl_lang, _val_lang in (("Indonesia", "id"),
                                     ("English (English)", "en"),
                                     ("Ikuti bahasa video", "auto")):
            self.template_lang.addItem(_lbl_lang, _val_lang)
        tmpl_row.addWidget(self.template_lang)
        card.layout().addLayout(tmpl_row)
        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel("Maksimal video per klik/jalan", card))
        self.template_max = QComboBox(card)
        for _lbl_max, _val_max in (("1 video", 1), ("2 video", 2), ("3 video", 3),
                                   ("5 video", 5), ("10 video", 10),
                                   ("Semua video siap", 0)):
            self.template_max.addItem(_lbl_max, _val_max)
        scope_row.addWidget(self.template_max)
        scope_row.addStretch()
        card.layout().addLayout(scope_row)
        self.template_ai_auto = QCheckBox(
            "Generate AI judul/deskripsi/hashtag saat publish", card)
        self.template_ai_auto.setChecked(True)
        card.layout().addWidget(self.template_ai_auto)
        lay.addWidget(card)
        self.recipe_check = QCheckBox("Simpan konfigurasi ini sebagai Preset", card)
        self.recipe_name = QLineEdit(card)
        self.recipe_name.setPlaceholderText("Nama preset (mis. Shorts Daily)")
        self.recipe_name.hide()
        self.recipe_check.toggled.connect(lambda v: self.recipe_name.setVisible(v))
        lay.addWidget(self.recipe_check)
        lay.addWidget(self.recipe_name)
        lay.addStretch()
        return w

    def _wizard_step(self, index: int) -> None:
        index = max(0, min(self.wizard.count() - 1, index))
        if index == 3:
            self._refresh_review()
        self.wizard.setCurrentIndex(index)
        self._slide_in(self.wizard.currentWidget())
        steps = ["1 Konten", "2 Platform", "3 Waktu", "4 Review"]
        pretty = "   ".join(f"{'▸' if i == index else '✓'} {s}" for i, s in enumerate(steps))
        self.step_lbl.setText(pretty)
        self.step_progress.setValue((index + 1) * 25)
        self.back_btn.setEnabled(index > 0)
        self.next_btn.setText("Mulai Automation ▶" if index == 3 else "Lanjut ▶")
        if index == 1:
            self._refresh_platform_rows()

    def _wizard_next(self) -> None:
        idx = self.wizard.currentIndex()
        if idx < 3:
            if idx == 0 and not self._selected_media():
                QMessageBox.information(self, "Content", "Pilih minimal satu file/media clip dulu.")
                return
            if idx == 1 and not self._selected_platforms():
                QMessageBox.information(self, "Platforms", "Pilih minimal satu platform yang sudah terhubung.")
                return
            self._wizard_step(idx + 1)
            return
        self._start_automation()

    def _reset_wizard(self) -> None:
        self._reload_media_list()
        self._refresh_platform_rows()
        self._wizard_step(0)

    def _pack_language(self, ctx) -> str:
        """Content-pack language from the Create-tab choice; "auto" = follow video.

        ``auto`` resolves to the clip's stored language, or a fresh detection
        from the clip's own transcript — an English video therefore gets an
        English pack instead of silently falling back to Indonesian.
        """
        try:
            choice = str(self.pack_lang_combo.currentData() or "auto").strip().lower()
        except Exception:
            choice = "auto"
        if choice not in ("auto", "video", "follow", "id", "en"):
            choice = "auto"
        from chopster.automation.context import resolve_pack_language
        return resolve_pack_language(ctx, choice)

    def _save_pack_lang(self, _index: int = 0) -> None:
        """Persist the Create-tab metadata language choice."""
        try:
            value = str(self.pack_lang_combo.currentData() or "auto").strip().lower()
        except Exception:
            return
        if value not in ("auto", "id", "en"):
            return
        try:
            config = self.app.config
            if hasattr(config, "update"):
                config.update({"aut_pack_language": value})
            else:
                config.set("aut_pack_language", value)
        except Exception:
            return

    def _selected_media(self) -> list[dict]:
        out = []
        for i in range(self.media_list.count()):
            item = self.media_list.item(i)
            if item.checkState() == Qt.Checked:
                data = item.data(Qt.UserRole)
                if isinstance(data, dict):
                    out.append(data)
        return out

    def _selected_platforms(self) -> list[dict]:
        return [{"platform": p} for p, cb in self.platform_checks.items() if cb.isChecked()]

    def _pick_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Pilih clip hasil render", str(self.app.config.get("out_dir") or ""), "Video (*.mp4 *.mov *.webm)")
        for f in files:
            self._add_media_row({"path": f, "title": Path(f).name, "source": "manual"})
        self._sync_next()

    def _on_files_dropped(self, paths: list[str]) -> None:
        """Drag-and-dropped clip files join the list exactly like «Pilih file…»,
        so counters, pack scope and pack bindings stay consistent either way.
        """
        added = 0
        for f in paths:
            if self._add_media_row({"path": f, "title": Path(f).name, "source": "manual"},
                                   refresh=False):
                added += 1
        if added:
            self._update_media_count()
            self._sync_pack_form()
            self._sync_next()
            if getattr(self, "media_source_hint", None) is not None:
                self.media_source_hint.setText(
                    f"{added} file video ditambahkan dari drag & drop — centang yang mau "
                    "dijadwalkan, atau langsung Generate AI Content Pack.")

    def _reload_media_list(self) -> None:
        """Ready content for the active source filter + search (spec 5.1/5.2)."""
        mgr = self._manager()
        source = self._media_source_key()
        try:
            rows = mgr.list_media("ready", source=source) if mgr else []
        except Exception:
            rows = []
        needle = (self.media_search.text() or "").strip().lower()
        self.media_list.blockSignals(True)
        self.media_list.clear()
        seen: set[str] = set()
        missing = 0
        for row in rows:
            path = str(row.get("media_path") or "")
            if not path or path in seen:
                continue
            seen.add(path)
            title = str(row.get("title") or Path(path).name)
            if needle and needle not in title.lower():
                continue
            if not Path(path).exists():
                missing += 1          # ingested earlier, file gone: never queueable
                continue
            data = {"path": path, "title": title,
                    "source": str(row.get("source") or "acs"),
                    "clip_id": str(row.get("clip_id") or ""),
                    "media_id": str(row.get("id") or "")}
            try:
                from chopster.automation.manager import split_media_content
                _, clip_ctx = split_media_content(row.get("content_pack"))
                if clip_ctx:
                    data["clip_context"] = clip_ctx
            except Exception:
                pass
            self._add_media_row(data, refresh=False)
        self.media_list.blockSignals(False)
        self._update_media_count()
        self._request_list_thumbs()
        if missing:
            self.media_reject_lbl.setText(
                self.media_reject_lbl.text() + f" | {missing} file hilang dari disk")
        self._sync_next()

    def _add_media_row(self, data: dict, refresh: bool = True) -> bool:
        """Insert one media row; returns True when a new row was added."""
        path = str(data.get("path") or "")
        for i in range(self.media_list.count()):
            existing = self.media_list.item(i).data(Qt.UserRole)
            if isinstance(existing, dict) and str(existing.get("path")) == path:
                return False
        label = f"{data.get('title') or Path(path).name}    [{data.get('source') or 'manual'}]"
        item = QListWidgetItem(label)
        item.setData(Qt.UserRole, data)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked)
        item.setToolTip(path)
        self.media_list.addItem(item)
        cached = self._thumb_pixmaps.get(path)
        if cached is not None:
            item.setIcon(QIcon(cached))
        if refresh:
            # Manually added rows (file picker, tests) must move the counters
            # and the AI-scope line right away; the bulk reload path updates
            # once at the end instead.
            self._update_media_count()
        return True

    def _refresh_platform_rows(self) -> None:
        mgr = self._manager()
        try:
            statuses = mgr.account_statuses() if mgr else {}
        except Exception:
            statuses = {}
        for platform, lbl in self.platform_status.items():
            info = statuses.get(platform) or {}
            state = str(info.get("state") or "not_connected")
            text = str(info.get("label") or "Not connected")
            lbl.setText(f"● {text}")
            _, color = _status_style(state.upper(), self.C)
            lbl.setStyleSheet(f"color: {color};")

    def _update_platform_row(self, platform: str) -> None:
        self._refresh_platform_rows()

    def _ordered_checked_rows(self) -> list[QListWidgetItem]:
        """Checked rows, focused row first — the order the AI reads them."""
        rows: list[QListWidgetItem] = []
        cur = self.media_list.currentItem()
        if cur is not None and cur.checkState() == Qt.Checked:
            rows.append(cur)
        rows += [self.media_list.item(i) for i in range(self.media_list.count())
                 if self.media_list.item(i) is not cur]
        return [it for it in rows
                if it is not None and it.checkState() == Qt.Checked]

    def _row_context(self, item: QListWidgetItem):
        """ClipContext stored with this row, or None (raw — no status check)."""
        data = item.data(Qt.UserRole) or {}
        raw = data.get("clip_context")
        if not isinstance(raw, dict) or not raw:
            return None
        try:
            from chopster.automation.context import ClipContext
            return ClipContext.from_dict(raw)
        except Exception:
            return None

    def _selected_context(self):
        """Evidence pinned to the focused clip, else the first checked clip
        whose own transcript is strong enough for the AI (spec 4.4)."""
        from chopster.automation.context import context_status
        for item in self._ordered_checked_rows():
            ctx = self._row_context(item)
            if ctx is not None and context_status(ctx)[0]:
                return ctx
        return None

    def _pack_targets(self) -> list[dict]:
        """Every checked video that can get its OWN AI pack (path/title/ctx).

        A checked video with no usable transcript is still a target: the worker
        transcribes *that* file locally (Whisper) instead of describing it with
        whichever video happens to be open in Content Clipper AI.
        """
        from chopster.automation.context import context_status
        out: list[dict] = []
        for item in self._ordered_checked_rows():
            data = item.data(Qt.UserRole) or {}
            path = str(data.get("path") or "")
            if not path:
                continue
            ctx = self._row_context(item)
            usable = bool(ctx is not None and context_status(ctx)[0])
            out.append({"path": path,
                        "title": str(data.get("title") or ""),
                        "ctx": ctx if usable else None,
                        "needs_transcribe": not usable})
        return out

    def _generate_pack(self) -> None:
        win = getattr(self.app, "window", None)
        clipper = getattr(win, "page_clipper", None) if win else None
        selected = self._selected_media()
        targets = self._pack_targets()
        if targets:
            # Every checked video gets its OWN pack: its handed-off transcript
            # when the renderer provided one, otherwise a local Whisper
            # transcription of that exact file. A pack is never written from
            # another video's words (spec §6.6/§4.4).
            self._submit_pack_generation(targets, clipper, selected)
            return
        # Nothing checked: the only remaining evidence is the project that is
        # open in Content Clipper AI, and the pack is bound to that file below.
        transcript = getattr(clipper, "transcript", None) if clipper else None
        if transcript is None:
            QMessageBox.information(
                self, "AI Content Pack",
                "Belum ada video yang bisa dibaca.\n\n"
                "Centang minimal satu video di daftar konten lalu klik «Generate AI "
                "Content Pack» lagi: video dari komputer ditranskripsi lokal (Whisper) "
                "supaya AI benar-benar membacanya, sedangkan clip hasil Auto Clip Studio "
                "atau Content Clipper AI memakai transkrip yang sudah ada.\n\n"
                "Alternatif: buka Content Clipper AI, jalankan Transcribe atau Master "
                "Analysis, lalu Generate dari sini.")
            return
        # Fallback path (no clip evidence): make sure the transcript we are
        # about to use actually belongs to the selected video. Otherwise the
        # pack would describe a completely different file.
        sel_path = str(selected[0].get("path") or "") if selected else ""
        proj_path = ""
        try:
            proj = getattr(clipper, "project", None)
            proj_path = str(getattr(proj, "source_path", "") or "")
        except Exception:
            proj_path = ""
        if sel_path and proj_path:
            try:
                same = Path(sel_path).resolve() == Path(proj_path).resolve()
            except Exception:
                same = sel_path.lower() == proj_path.lower()
            if not same:
                answer = QMessageBox.question(
                    self, "AI Content Pack",
                    "Clip terpilih tidak punya transkrip analisisnya sendiri, dan "
                    "video yang sedang terbuka di Content Clipper AI berbeda:\n\n"
                    f"  Clip terpilih : {Path(sel_path).name}\n"
                    f"  Sumber transkrip : {Path(proj_path).name}\n\n"
                    "Generate tetap akan memakai transkrip video di Content Clipper AI, "
                    "sehingga hasilnya bisa tidak sesuai clip terpilih. Lanjutkan?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if answer != QMessageBox.Yes:
                    return
        try:
            cfg = clipper._ai_config() if clipper else {}
        except Exception:
            cfg = {}
        from copy import deepcopy
        transcript = getattr(clipper, "transcript", None) if clipper else None
        tr = deepcopy(transcript) if transcript is not None else None
        from chopster.automation.content import primary_platform
        from chopster.automation.context import resolve_pack_language
        plat = primary_platform(self._selected_platforms())

        class _TranscriptCtx:
            """Minimal ctx so the shared language resolver can read a Transcript."""
            def __init__(self, tr):
                self.language = str(getattr(tr, "language", "") or "")
                self.title = ""
                self.segments = [{"text": str(s.text or "")}
                                 for s in (getattr(tr, "segments", []) or [])]

        try:
            fb_choice = str(self.pack_lang_combo.currentData() or "auto").strip().lower()
        except Exception:
            fb_choice = "auto"
        lang_fb = resolve_pack_language(_TranscriptCtx(tr), fb_choice)

        def worker(*, signals, cancel_check):
            signals.progress.emit("automation:pack", 20, "Content Pack: menyusun judul + deskripsi + hashtag…")
            from chopster.clipper.caption_generator import generate_captions
            # No clip evidence on this path: the Content Clipper transcript is
            # the only input (the user confirmed that above).
            return generate_captions(tr, cfg, platform=plat, language=lang_fb, count=5, style="hooks")

        def done(result):
            self._pack_running(False)
            if isinstance(result, dict) and (result.get("title") or result.get("titles")):
                self._pack = dict(result)
                # Remember which video this pack describes so switching the
                # selection can bring it back — and Start attaches it there.
                # Prefer the Content Clipper project path: this fallback path
                # describes THAT project, not whichever row happens to be first.
                primary = str(proj_path or (selected[0].get("path") if selected else "") or "")
                self._pack["_media_path"] = primary
                if primary:
                    self._packs[primary] = dict(result)
                self._apply_pack(result)
                self.pack_state.setText(f"✓ Ready • {result.get('source', 'local')}")
                self._update_pack_scope()
            else:
                self.pack_state.setText("Generate gagal — isi manual.")

        def fail(err):
            self._pack_running(False)
            if str(err) == "Dibatalkan":
                self.pack_state.setText("Generate dibatalkan.")
                return
            self.pack_state.setText(f"Generate gagal: {str(err)[:120]}")
        try:
            wkr = self.app.tasks.submit("automation:pack", "AI Content Pack", worker)
            wkr.signals.result.connect(lambda _tid, res: done(res), Qt.QueuedConnection)
            wkr.signals.failed.connect(lambda _tid, err: fail(err), Qt.QueuedConnection)
            self._pack_running(True)
            self.pack_state.setText("Menggenerate AI Content Pack… (bisa dibatalkan)")
        except Exception as exc:
            fail(exc)

    def _submit_pack_generation(self, targets: list[dict], clipper,
                                selected: list[dict]) -> None:
        """One background pass that generates a SEPARATE pack per checked video.

        Evidence per video (spec 4.4/7.4):

        1. the clip's own handed-off transcript, when the renderer carried one;
        2. otherwise the local Whisper transcription of *that* file -- never
           another video's transcript, which is what made two different videos
           receive the same description.

        Each pack is also compared with the packs already produced in this pass
        and regenerated once when a description repeats (spec 6.6).
        """
        from chopster.automation.context import caption_context, focused_transcript
        from chopster.automation.content import pack_signature, primary_platform
        from chopster.automation.local_transcribe import (
            media_context_from_file,
            pack_whisper_model,
        )
        try:
            cfg_base = clipper._ai_config() if clipper else {}
        except Exception:
            cfg_base = {}
        from copy import deepcopy
        n_total = max(1, len(targets))
        plat = primary_platform(self._selected_platforms())
        primary_path = str(targets[0].get("path") or "")
        binding = str(getattr(targets[0].get("ctx"), "source_asset_id", "") or "")
        model = pack_whisper_model(self.app.config)
        to_transcribe = len([t for t in targets if t.get("needs_transcribe")])

        def worker(*, signals, cancel_check):
            from chopster.clipper.caption_generator import generate_captions
            packs: dict[str, dict] = {}
            errors: list[str] = []
            used_descriptions: list[str] = []
            used_titles: list[str] = []
            done_local = 0
            for idx, tgt in enumerate(targets, start=1):
                if cancel_check():
                    raise RuntimeError("Dibatalkan")
                path = str(tgt.get("path") or "")
                name = str(tgt.get("title") or Path(str(tgt.get("path") or "")).name)
                try:
                    self.pack_status.emit(f"AI membaca «{name}» ({idx}/{n_total})…")
                except Exception:
                    pass
                signals.progress.emit("automation:pack", int(idx * 90 / n_total),
                                      f"Content Pack {idx}/{n_total}: {name}")
                try:
                    ctx = tgt.get("ctx")
                    if ctx is None:
                        # No transcript of its own: read THIS file locally with
                        # Whisper instead of borrowing another video's words.
                        self._pack_progress(f"Transkripsi lokal Whisper {model}: {name}")
                        ctx = media_context_from_file(
                            path, title=name, config=self.app.config,
                            progress_cb=lambda _pct, msg: self._pack_progress(str(msg)),
                            cancel_check=cancel_check)
                        done_local += 1
                    cfg2 = deepcopy(cfg_base)
                    # Focus the AI on THIS clip's window so a long podcast's
                    # opening can never describe a clip taken at minute 50.
                    cfg2.update(caption_context(ctx))
                    # Language follows the template choice ("id"/"en"/"auto");
                    # "auto" uses the clip's own analysed language.
                    pack_lang = self._pack_language(ctx)
                    # The renderer/import title is a label to REPLACE, never a
                    # title to reuse (spec 8.2).
                    cfg2["source_title"] = name
                    cfg2["working_title"] = name
                    # What earlier videos already received in this same pass, so
                    # one description can never be stamped on several videos.
                    cfg2["avoid_titles"] = list(used_titles)
                    cfg2["avoid_descriptions"] = list(used_descriptions)
                    focus = focused_transcript(ctx)
                    if focus is None or not getattr(focus, "segments", None):
                        raise RuntimeError("transkrip video ini kosong")
                    pack = generate_captions(focus, cfg2, platform=plat,
                                             language=pack_lang, count=5, style="hooks")
                    signature = pack_signature(pack)[0]
                    if signature and signature in used_descriptions:
                        # Same words for a different video: regenerate once with
                        # an explicit instruction, then report it honestly.
                        cfg2["force_distinct"] = True
                        retry = generate_captions(focus, cfg2, platform=plat,
                                                  language=pack_lang, count=5, style="hooks")
                        retry_sig = pack_signature(retry)[0]
                        if (isinstance(retry, dict) and (retry.get("title") or retry.get("titles"))
                                and retry_sig not in used_descriptions):
                            pack = retry
                        else:
                            errors.append(f"{name}: deskripsi masih sama dengan video lain")
                    if isinstance(pack, dict) and (pack.get("title") or pack.get("titles")):
                        pack["_clip_source"] = str(getattr(ctx, "source", "") or "")
                        pack["_evidence"] = str(getattr(ctx, "evidence", "") or "")
                        packs[path] = pack
                        sig_desc, sig_titles = pack_signature(pack)
                        if sig_desc:
                            used_descriptions.append(sig_desc)
                        used_titles.extend(sig_titles)
                    else:
                        errors.append(f"{name}: hasil kosong")
                except Exception as exc:
                    if str(exc) == "Dibatalkan":
                        raise
                    errors.append(f"{name}: {str(exc)[:120]}")
            return {"packs": packs, "errors": errors, "primary": primary_path,
                    "binding": binding, "transcribed": done_local}

        def done(result):
            self._pack_running(False)
            packs = {}
            if isinstance(result, dict):
                packs = {p: pk for p, pk in (result.get("packs") or {}).items()
                         if isinstance(pk, dict)}
            if not packs:
                errs = ""
                if isinstance(result, dict) and result.get("errors"):
                    errs = " (" + "; ".join(str(e) for e in result["errors"][:3]) + ")"
                self.pack_state.setText(f"Generate gagal — isi manual.{errs}")
                self._update_pack_scope()
                return
            self._packs.update(packs)
            primary = str(result.get("primary") or "")
            if primary not in packs:
                primary = next(iter(packs))
            self._pack = dict(packs[primary])
            self._pack["_clip_binding"] = str(result.get("binding") or "")
            self._pack["_media_path"] = primary
            self._apply_pack(packs[primary])
            errors = list(result.get("errors") or [])
            extra = f" • {len(errors)} gagal" if errors else ""
            total = len(selected) or len(packs)
            self.pack_state.setText(
                f"✓ Pack siap untuk {len(packs)}/{total} video terpilih{extra}")
            self._update_pack_scope()

        def fail(err):
            self._pack_running(False)
            if str(err) == "Dibatalkan":
                self.pack_state.setText("Generate dibatalkan.")
                return
            self.pack_state.setText(f"Generate gagal: {str(err)[:120]}")
        try:
            wkr = self.app.tasks.submit("automation:pack", "AI Content Pack", worker)
            wkr.signals.result.connect(lambda _tid, res: done(res), Qt.QueuedConnection)
            wkr.signals.failed.connect(lambda _tid, err: fail(err), Qt.QueuedConnection)
            self._pack_running(True)
            self.pack_state.setText(
                f"Membaca {n_total} video terpilih… (bisa dibatalkan)")
            if to_transcribe:
                # Be explicit that these videos are being read locally, not
                # described from another video's transcript.
                self.pack_state.setText(
                    self.pack_state.text()
                    + f" {to_transcribe} video ditranskripsi lokal (Whisper {model}).")
        except Exception as exc:
            fail(exc)

    def _pack_running(self, running: bool) -> None:
        """Keep Generate disabled and Batalkan Generate armed while the AI runs."""
        self.pack_gen_btn.setEnabled(not running)
        self.pack_cancel_btn.setEnabled(running)
        if hasattr(self, "pack_progress"):
            self.pack_progress.setVisible(running)

    def _pack_progress(self, message: str) -> None:
        """Forward one worker-side progress line to the pack status label."""
        try:
            self.pack_status.emit(str(message))
        except Exception:
            pass

    def _save_pack_model(self, name: str) -> None:
        """Persist the Whisper model used for videos without a transcript.

        The value is floored at ``small``: a pack written from ``tiny`` output is
        exactly the generic, bot-like description this workflow must not produce.
        """
        try:
            from chopster.automation.local_transcribe import normalize_pack_model
            model = normalize_pack_model(name)
        except Exception:
            return
        try:
            config = self.app.config
            if hasattr(config, "update"):
                config.update({"aut_pack_whisper_model": model})
            else:
                config.set("aut_pack_whisper_model", model)
        except Exception:
            return
        combo = getattr(self, "pack_model_combo", None)
        if combo is not None:
            combo.blockSignals(True)
            combo.setCurrentText(model)
            combo.blockSignals(False)
        self._update_pack_scope()

    def _cancel_pack(self) -> None:
        tasks = getattr(self.app, "tasks", None)
        if tasks is None:
            return
        tasks.cancel("automation:pack")
        self.pack_state.setText("Membatalkan generate…")

    def _mark_pack_stale(self) -> None:
        """Discard the focused pack once its clip binding no longer matches.
        Per-video packs of other still-checked videos remain valid (they are
        keyed by their own path and their context never changes)."""
        binding = self._pack.get("_clip_binding") if isinstance(self._pack, dict) else ""
        if not binding:
            return
        ctx = self._selected_context()
        current = ctx.source_asset_id if ctx is not None else ""
        if current != binding:
            self._pack = {}
            checked = {str(m.get("path") or "") for m in self._selected_media()}
            self._packs = {p: k for p, k in self._packs.items() if p in checked}
            self.pack_state.setText(
                "Pilihan berubah — pack yang lama dibuang (konteks clip berbeda). Generate ulang.")

    def _sync_pack_form(self) -> None:
        """Keep the pack form focused on the checked video the user sees first;
        edits made before switching away are stored back into _packs."""
        if getattr(self, "pack_scope_lbl", None) is None:
            return
        self._refresh_pack_video_rows()
        selected = self._selected_media()
        if not selected:
            return
        primary_path = str(selected[0].get("path") or "")
        old_path = str(self._pack.get("_media_path") or "")
        if old_path and old_path != primary_path and self._pack.get("title"):
            saved = self._current_pack()
            self._packs[old_path] = saved
        pack = self._packs.get(primary_path)
        if pack is not None and old_path != primary_path:
            self._pack = dict(pack)
            self._pack["_media_path"] = primary_path
            self._apply_pack(pack)
            self.pack_state.setText(
                f"✓ Pack untuk «{selected[0].get('title') or 'video'}» siap — "
                "judul bisa diedit di bawah.")

    def _refresh_pack_video_rows(self) -> None:
        """Live list of every checked clip and whether it already has its own
        pack. Clicking «Fokus» loads that clip into the editor above — the
        whole point of per-video packs is that the editing view follows.
        """
        box = getattr(self, "pack_video_box", None)
        if box is None:
            return
        while box.count():
            item = box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        checked = [it for i in range(self.media_list.count())
                   if (it := self.media_list.item(i)).checkState() == Qt.Checked]
        if not checked:
            box.addWidget(self._lbl(
                "Belum ada video tercentang — centang video di daftar konten, "
                "maka status pack-nya tampil satu per satu di sini.", muted=True))
            return
        for item in checked:
            data = item.data(Qt.UserRole) or {}
            path = str(data.get("path") or "")
            title = str(data.get("title") or Path(path).name)
            pack = self._packs.get(path) if path else None
            has_pack = bool(pack and str(pack.get("title") or "").strip())
            row_w = QWidget()
            row = QHBoxLayout(row_w)
            row.setContentsMargins(0, 0, 0, 0)
            mark = QLabel("✓" if has_pack else "○", row_w)
            mark.setStyleSheet(
                f"color: {self.C['GREEN'] if has_pack else self.C['MUTED']}; "
                "font-weight: 700;")
            row.addWidget(mark)
            state = (f"pack «{pack.get('title')}» siap" if has_pack
                     else "belum ada pack")
            row.addWidget(self._lbl(f"{title} — {state}", row_w, muted=True))
            row.addStretch()
            focus = self._btn(
                "Fokus" if item is self.media_list.currentItem() else "Edit",
                lambda _=False, it=item: self._focus_media_item(it))
            row.addWidget(focus)
            box.addWidget(row_w)

    def _focus_media_item(self, item) -> None:
        """Bring one clip to the front of the media list so currentItemChanged
        runs; _sync_pack_form() then swaps the form to that video's pack."""
        self.media_list.setCurrentItem(item)

    def _apply_pack(self, pack: dict) -> None:
        titles = [str(t) for t in (pack.get("titles") or []) if t]
        if pack.get("title") and str(pack["title"]) not in titles:
            titles.insert(0, str(pack["title"]))
        self.title_combo.blockSignals(True)
        self.title_combo.clear()
        self.title_combo.addItems(titles or [""])
        self.title_combo.blockSignals(False)
        self.desc_edit.setPlainText(str(pack.get("description_long") or pack.get("description_short") or pack.get("description") or ""))
        self.caption_edit.setText(str(pack.get("captions") or pack.get("caption") or ""))
        if isinstance(pack.get("captions"), list) and pack["captions"]:
            self.caption_edit.setText(str(pack["captions"][0]))
        self.tags_edit.setText(" ".join(str(h) for h in (pack.get("hashtags") or [])))
        self._sync_next()

    def _current_pack(self) -> dict:
        pack = dict(self._pack)
        pack.pop("_clip_binding", None)   # internal binding marker, never persisted
        pack.pop("_media_path", None)     # …and so is the focus-path marker
        pack["title"] = self.title_combo.currentText().strip()
        pack["titles"] = [self.title_combo.itemText(i) for i in range(self.title_combo.count()) if self.title_combo.itemText(i).strip()]
        pack["description_long"] = self.desc_edit.toPlainText().strip()
        pack["description_short"] = self.desc_edit.toPlainText().strip()[:300]
        pack["description"] = pack["description_short"]
        caption = self.caption_edit.text().strip()
        pack["caption"] = caption
        if caption and caption not in (pack.get("captions") or []):
            pack["captions"] = [caption] + [str(c) for c in (pack.get("captions") or [])]
        pack["hashtags"] = [t for t in self.tags_edit.text().replace(",", " ").split() if t.strip()]
        pack["source"] = str(pack.get("source") or "manual")
        return pack

    def _refresh_review(self) -> None:
        self._refresh_presets_combo()
        media = self._selected_media()
        platforms = self._selected_platforms()
        jobs = len(media) * len(platforms)
        when = "Sekarang (Publish immediately)"
        if self.stagger_radio.isChecked():
            when = "Staggered"
            try:
                _mgr = self._manager()
                if _mgr is not None and media:
                    slots = _mgr.preview_stagger(self._stagger_config(), len(media))
                    when = (f"Staggered {len(slots)} slot mulai "
                            f"{slots[0].strftime('%d %b %H:%M')} lokasi kamu")
            except Exception as exc:
                when = "Staggered tidak valid: " + str(
                    getattr(exc, "user_message", None) or exc)
        elif self.sched_radio.isChecked():
            dt = self.date_edit.date().toString("yyyy-MM-dd")
            tm = self.time_edit.time().toString("HH:mm")
            when = f"{dt} {tm} (lokal)"
        pack = self._current_pack()
        mgr = self._manager()
        ready = []
        missing = []
        try:
            statuses = mgr.account_statuses() if mgr else {}
        except Exception:
            statuses = {}
        for p in platforms:
            key = p["platform"]
            state = str((statuses.get(key) or {}).get("state") or "not_connected")
            (ready if state == "connected" else missing).append(PLATFORM_LABELS.get(key, key))
        lines = [
            f"🎬  {len(media)} clip  •  {len(platforms)} platform  •  {jobs} publication jobs",
            f"📍  Publish ke: {', '.join(PLATFORM_LABELS.get(p['platform'], p['platform']) for p in platforms) or '—'}",
            f"📝  Judul: {pack.get('title') or '(belum diisi)'}",
            f"⏰  Waktu: {when}",
            f"✓  Akun siap: {', '.join(ready) if ready else '—'}",
            (f"⚠  Belum siap: {', '.join(missing)} — sambungkan dulu di Accounts." if missing else "✓  Semua koneksi siap"),
        ]
        sources_in_batch = sorted({str(m.get("source") or "manual") for m in media})
        lines.append("Sumber: " + (", ".join(
            SOURCE_LABELS.get(s, s) for s in sources_in_batch) or "-"))
        lines.append("Approval: " + ("Review before publish"
                                       if self.approval_review.isChecked()
                                       else "Auto publish"))
        rejected = 0
        try:
            rejected = len(mgr.rejected_media(limit=200)) if mgr else 0
        except Exception:
            rejected = 0
        if rejected:
            lines.append(f"{rejected} konten ditolak validasi - tidak masuk antrean")
        lines.append("Siap dijalankan lewat satu Automation Engine yang sama.")
        for lbl, text in zip(self.review_lines, lines):
            lbl.setText(text)
            if text.startswith("⚠"):
                lbl.setStyleSheet(f"color: {self.C['GOLD']};")
            else:
                lbl.setStyleSheet("")
        # Exactly which videos will be published, and who has AI metadata —
        # answers users previously had to guess from a bare count.
        titles = [str(m.get("title") or Path(str(m.get("path") or "")).name)
                  for m in media]
        if media:
            shown = "  •  ".join(titles[:8])
            if len(titles) > 8:
                shown += f"  •  +{len(titles) - 8} lainnya"
            self.review_media_lbl.setText("🎬  Video tercentang: " + shown)
            covered = sum(1 for m in media
                          if str(m.get("path") or "") in self._packs)
            if covered == len(media):
                self.review_pack_lbl.setText(
                    f"🤖  AI Content Pack siap untuk semua {len(media)} video "
                    "(judul & deskripsi tiap video dipakai masing-masing).")
                self.review_pack_lbl.setStyleSheet("")
            elif covered:
                self.review_pack_lbl.setText(
                    f"🤖  AI Content Pack: {covered}/{len(media)} video — sisanya "
                    "pakai judul clip sendiri (deskripsi diambil dari transkrip).")
                self.review_pack_lbl.setStyleSheet(f"color: {self.C['GOLD']};")
            else:
                self.review_pack_lbl.setText(
                    "🤖  Belum ada AI Content Pack — video publish dengan judul "
                    "clip, deskripsi diambil dari transkrip klip. Ingin judul/"
                    "deskripsi AI? Kembali ke Step 1 lalu Generate AI Content Pack.")
                self.review_pack_lbl.setStyleSheet(f"color: {self.C['GOLD']};")
        else:
            self.review_media_lbl.setText("🎬  Video tercentang: (belum ada)")
            self.review_pack_lbl.setText("")
            self.review_pack_lbl.setStyleSheet("")

    def _start_automation(self) -> None:
        mgr = self._manager()
        if mgr is None:
            QMessageBox.warning(self, "Automation", "Automation manager belum tersedia.")
            return
        media = self._selected_media()
        platforms = self._selected_platforms()
        if not media or not platforms:
            QMessageBox.information(self, "Automation", "Pilih clip dan platform dulu.")
            return
        mode = ("stagger" if self.stagger_radio.isChecked()
                else ("schedule" if self.sched_radio.isChecked() else "now"))
        scheduled_at = ""
        stagger = None
        if mode == "schedule":
            d = self.date_edit.date().toString("yyyy-MM-dd")
            t = self.time_edit.time().toString("HH:mm")
            scheduled_at = f"{d} {t}"
            if parse_local_time(scheduled_at) is None:
                QMessageBox.information(self, "Automation", "Waktu jadwal tidak valid.")
                return
        if mode == "stagger":
            stagger = self._stagger_config()
            try:
                mgr.preview_stagger(stagger, len(media))
            except Exception as exc:
                QMessageBox.information(self, "Automation", str(
                    getattr(exc, "user_message", None) or exc))
                return
        privacy_map = {"Default platform": "", "Public": "public", "Unlisted": "unlisted", "Private": "private"}
        overrides = {
            "privacy": privacy_map.get(self.privacy_combo.currentText(), "") if self.adv_check.isChecked() else "",
            # Persisted into the job snapshot so YouTube's adapter transmits the
            # approved answer verbatim (never a silent default).
            "made_for_kids": self._made_for_kids_value(),
        }
        # Attach each video's OWN pack (spec §6.6). create_run drops a shared
        # pack when several videos are selected — that is exactly the "pack
        # tidak sesuai checklist" complaint — so every job gets per-media
        # metadata here, or none (its clip title) if no pack was generated.
        edited_pack = self._current_pack()
        form_path = str(self._pack.get("_media_path") or "")
        if form_path and not str(edited_pack.get("title") or "").strip():
            # The form is empty for this path (the focused pack belongs to
            # another video): fall back to the stored pack, never to a blank
            # title that would wipe the video's metadata.
            stored = self._packs.get(form_path)
            if stored:
                edited_pack = {k: v for k, v in stored.items()
                               if not str(k).startswith("_")}
        if not form_path and (edited_pack.get("title") or ""):
            cur = self.media_list.currentItem()
            data = cur.data(Qt.UserRole) if cur is not None else None
            if isinstance(data, dict):
                form_path = str(data.get("path") or "")
            elif media:
                form_path = str(media[0].get("path") or "")
        for m in media:
            p = str(m.get("path") or "")
            if p and p == form_path:
                m["content"] = edited_pack
            elif p in self._packs:
                m["content"] = {k: v for k, v in self._packs[p].items()
                                if not str(k).startswith("_")}
        try:
            run = mgr.create_run(name=f"Automation {len(media)} clip", media_items=media, platforms=platforms, mode=mode, scheduled_at=scheduled_at, content_pack=self._current_pack(), overrides=overrides,
                                 review=self.approval_review.isChecked(), source="studio",
                                 stagger=stagger)
            if self.recipe_check.isChecked() and self.recipe_name.text().strip():
                _scope = self.template_max.currentData()
                cfg = {"platforms": [p["platform"] for p in platforms], "mode": mode, "time": self.time_edit.time().toString("HH:mm"), "repeat": "", "privacy": overrides.get("privacy", ""), "review": bool(self.approval_review.isChecked()), "made_for_kids": self._made_for_kids_value(), "max_per_run": int(_scope if _scope is not None else max(1, len(media)))}
                cfg["acs_publish"] = bool(self.acs_template_check.isChecked())
                cfg["privacy"] = {"Public": "public", "Unlisted": "unlisted",
                                  "Private": "private"}.get(
                    self.template_privacy.currentText(), "public") if self.acs_template_check.isChecked() else (overrides.get("privacy", "") or "")
                cfg["made_for_kids"] = (self.template_mfk.currentText() == "Ya") if self.acs_template_check.isChecked() else self._made_for_kids_value()
                cfg["ai_auto"] = bool(self.template_ai_auto.isChecked())
                cfg["ai_language"] = str(self.template_lang.currentData() or "id")
                # Persist the staggered spacing so preset/auto-post keeps the
                # interval the user configured in the Waktu step.
                if mode == "stagger":
                    cfg["stagger"] = self._stagger_config()
                # Same name updates the existing preset in place instead of
                # creating a duplicate, so re-saving can change a template's
                # settings (platforms, privacy, language, …).
                _pname = self.recipe_name.text().strip()
                try:
                    _existing = next(
                        (r for r in (mgr.list_recipes() or [])
                         if str(r.get("name") or "").strip().lower() == _pname.lower()),
                        None)
                except Exception:
                    _existing = None
                mgr.save_recipe(_pname, cfg,
                                recipe_id=str(_existing.get("id")) if _existing else "")
        except Exception as exc:
            QMessageBox.warning(self, "Automation", str(getattr(exc, "user_message", None) or exc))
            return
        status = str(run.get("status") or "QUEUED")
        try:
            made = len(mgr.db.automation_list_jobs(run_id=str(run.get("id") or "")))
        except Exception:
            made = len(media) * len(platforms)
        skipped = len(media) * len(platforms) - made
        note = f"\n{skipped} job dilewati: videonya sudah ada di Queue." if skipped > 0 else ""
        QMessageBox.information(self, "Automation", f"Automation dibuat ({status}).\n{made} job dibuat.{note}")
        self.recipe_check.setChecked(False)
        self.recipe_name.clear()
        self._refresh_presets_combo()
        self.tabs.setCurrentIndex(self.TAB_QUEUE)
        self.refresh_all()

    def _sync_next(self) -> None:
        pass

    # ------------------------------------------------------------------ #
    # Accounts
    # ------------------------------------------------------------------ #
    def _build_accounts(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)
        outer.setContentsMargins(6, 10, 6, 6)
        # One tidy entry point: a single button opens the platform picker popup
        # instead of scattering connect buttons over the tab.
        head = QHBoxLayout()
        head.addWidget(self._lbl(
            "Sambungkan akun sekali di sini; Chopster memakai jalur API resmi bila "
            "tersedia, dan Browser Session (lanjutan) hanya untuk platform yang mengizinkannya.",
            w, muted=True), 1)
        self.connect_btn = self._btn("+ Sambungkan Akun", self._open_connect_dialog, primary=True)
        head.addWidget(self.connect_btn)
        outer.addLayout(head)
        scroll = QScrollArea(w)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        self.accounts_lay = QVBoxLayout(inner)
        self.accounts_lay.setContentsMargins(0, 0, 0, 0)
        self.accounts_lay.setSpacing(10)
        self.accounts_lay.addWidget(self._section("CONNECTED ACCOUNTS"))
        self.account_rows: dict[str, dict] = {}
        for platform in PLATFORMS:
            self.accounts_lay.addWidget(self._account_row(platform))
        self.accounts_lay.addWidget(self._section("DEVELOPER CONFIGURATION (DISIMPAN TERENKRIPTASI)"))
        adv = self._card()
        self.client_fields: dict[str, tuple[QLineEdit, QLineEdit]] = {}
        for platform in PLATFORMS:
            row = QHBoxLayout()
            row.addWidget(QLabel(PLATFORM_LABELS[platform], adv))
            cid = QLineEdit(adv)
            cid.setPlaceholderText("Client ID / Key")
            cid.setText(str(self.app.config.get(f"aut_{platform}_client_id") or ""))
            csec = QLineEdit(adv)
            csec.setEchoMode(QLineEdit.Password)
            saved = bool(str(self.app.config.get(f"aut_{platform}_client_secret") or ""))
            csec.setPlaceholderText("Client Secret (tersimpan DPAPI)" if saved else "Client Secret")
            row.addWidget(cid, 1)
            row.addWidget(csec, 1)
            self.client_fields[platform] = (cid, csec)
            self.accounts_lay.addLayout(row)
        red_row = QHBoxLayout()
        red_row.addWidget(QLabel("Redirect URI", adv))
        self.redirect_field = QLineEdit(adv)
        self.redirect_field.setPlaceholderText("http://localhost:51234/")
        self.redirect_field.setText(str(self.app.config.get("aut_oauth_redirect_uri") or ""))
        red_row.addWidget(self.redirect_field, 1)
        adv.layout().addLayout(red_row)
        save_row = QHBoxLayout()
        save_row.addStretch()
        save_row.addWidget(self._btn("Simpan Developer Config", self._save_client_config, primary=True))
        adv.layout().addLayout(save_row)
        tip = self._lbl(
            "Langkah: (1) buat developer app di console platform — Google Cloud (YouTube), "
            "Meta for Developers (Facebook), TikTok for Developers (TikTok); "
            "(2) daftarkan Redirect URI di kolom atas PERSIS seperti tertulis (host & port sama); "
            "(3) isi Client ID/Secret lalu klik Simpan Developer Config; (4) klik Connect. "
            "Instagram API memakai kredensial Facebook + Page terhubung; Instagram Browser Session "
            "tidak butuh kredensial (butuh playwright). Secret tidak pernah ditampilkan atau dicatat.",
            adv, muted=True)
        adv.layout().addWidget(tip)
        self.accounts_lay.addWidget(adv)
        self.accounts_lay.addWidget(self._section("BROWSER SESSION (INSTAGRAM FALLBACK)"))
        br = self._card()
        # Reuse the card's existing layout. _card() already installed one, so a
        # second setLayout() is ignored by Qt and the new layout would be orphaned
        # (widgets then float over the page title instead of stacking in the card).
        br_lay = br.layout()
        self.ig_mode_api = QRadioButton("Official API", br)
        self.ig_mode_browser = QRadioButton("Browser Session", br)
        self.ig_mode_api.setChecked(True)
        mode_row = QHBoxLayout()
        mode_row.addWidget(self.ig_mode_api)
        mode_row.addWidget(self.ig_mode_browser)
        mode_row.addStretch()
        br_lay.addLayout(mode_row)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self._btn("Open Instagram Login", self._ig_browser_login, primary=True))
        self.ig_session_lbl = QLabel("—", br)
        self.ig_session_lbl.setObjectName("muted")
        btn_row.addWidget(self.ig_session_lbl)
        btn_row.addStretch()
        br_lay.addLayout(btn_row)
        note = self._lbl("Jendela browser Chopster terbuka; login manual (termasuk MFA/CAPTCHA) di sana. Chopster tidak pernah mengumpulkan password atau membaca cookie browser utama kamu.", br, muted=True)
        br_lay.addWidget(note)
        self.accounts_lay.addWidget(br)
        # ---- Diagnostics (read-only) + optional safety switches -------------
        self.accounts_lay.addWidget(self._section("DIAGNOSTICS (READ-ONLY)"))
        dg = self._card()
        dg_lay = dg.layout()
        self.diag_summary = QLabel("—", dg)
        self.diag_summary.setWordWrap(True)
        dg_lay.addWidget(self.diag_summary)
        self.diag_body = QLabel("—", dg)
        self.diag_body.setWordWrap(True)
        dg_lay.addWidget(self.diag_body)
        opt_row = QHBoxLayout()
        self.diag_test_mode = QCheckBox("Test Mode (simulasi, tanpa posting nyata)", dg)
        self.diag_test_mode.setChecked(bool(self.app.config.get("aut_browser_test_mode")))
        opt_row.addWidget(self.diag_test_mode)
        opt_row.addWidget(QLabel("Soft cap / 24 jam (0 = nonaktif)", dg))
        self.diag_soft_cap = QSpinBox(dg)
        self.diag_soft_cap.setRange(0, 100)
        try:
            self.diag_soft_cap.setValue(int(self.app.config.get("aut_browser_soft_cap") or 0))
        except Exception:
            self.diag_soft_cap.setValue(0)
        opt_row.addWidget(self.diag_soft_cap)
        opt_row.addStretch()
        dg_lay.addLayout(opt_row)
        act_row = QHBoxLayout()
        act_row.addWidget(self._btn("↻ Cek Kesiapan", self._refresh_diagnostics))
        act_row.addWidget(self._btn("Simpan Opsi", self._save_diag_options))
        act_row.addWidget(QLabel("Tandai policy ditinjau:", dg))
        self.diag_review_platform = QComboBox(dg)
        for _p in PLATFORMS:
            self.diag_review_platform.addItem(PLATFORM_LABELS[_p], _p)
        act_row.addWidget(self.diag_review_platform)
        act_row.addWidget(self._btn("Tandai", self._mark_policy_reviewed))
        act_row.addStretch()
        dg_lay.addLayout(act_row)
        dg_lay.addWidget(self._lbl(
            "Diagnostics hanya membaca status: tidak membuka browser dan tidak memposting. "
            "'Test Mode' membuat job jalur browser berhenti sebagai simulasi. "
            "'Soft cap' adalah pengaman konservatif pembatas percobaan per akun dalam 24 jam.",
            dg, muted=True))
        self.accounts_lay.addWidget(dg)
        self.accounts_lay.addStretch()
        scroll.setWidget(inner)
        outer.addWidget(scroll)
        return w

    def _account_row(self, platform: str) -> QFrame:
        card = self._card()
        row = QHBoxLayout()
        name = QLabel(PLATFORM_LABELS[platform], card)
        name.setStyleSheet(f"color: {self.C['TEXT']}; font-weight: 700; font-size: 14px;")
        row.addWidget(name)
        self_lbl = QLabel("—", card)
        self_lbl.setObjectName("muted")
        row.addWidget(self_lbl)
        row.addStretch()
        status = QLabel("Not connected", card)
        status.setObjectName("autStatus")
        row.addWidget(status)
        connect = QPushButton("Connect", card)
        connect.setObjectName("primary")
        connect.setCursor(Qt.PointingHandCursor)
        connect.clicked.connect(lambda _=False, p=platform: self._connect(p))
        manage = QPushButton("Disconnect", card)
        manage.setObjectName("autGhost")
        manage.clicked.connect(lambda _=False, p=platform: self._disconnect(p))
        manage.setVisible(False)  # only shown once an account is connected
        row.addWidget(connect)
        row.addWidget(manage)
        card.layout().addLayout(row)
        self.account_rows[platform] = {"card": card, "name_lbl": self_lbl, "status": status, "connect": connect, "manage": manage}
        return card

    def _refresh_accounts(self) -> None:
        mgr = self._manager()
        try:
            statuses = mgr.account_statuses() if mgr else {}
        except Exception as statuses_exc:
            statuses = {}
            for row in self.account_rows.values():
                row["status"].setText(f"Error: {str(statuses_exc)[:60]}")
        for platform, row in self.account_rows.items():
            info = statuses.get(platform) or {}
            state = str(info.get("state") or "not_connected")
            accounts = info.get("accounts") or []
            display = ", ".join(str(a.get("display_name") or a.get("id")) for a in accounts) or "—"
            row["name_lbl"].setText(display)
            label = str(info.get("label") or "Not connected")
            row["status"].setText(f"● {label}")
            _, color = _status_style(state.upper(), self.C)
            row["status"].setStyleSheet(f"color: {color};")
            row["connect"].setText("Reconnect" if state in ("expired", "error") else ("Select Page" if state == "page_selection_required" else ("Open Login" if state == "session_required" else "Connect")))
            has_account = bool(accounts)
            needs_action = state in ("expired", "error", "session_required", "page_selection_required")
            # Not connected → only Connect. Connected → only Disconnect. Both are
            # shown only while a stored token exists but the session is stale.
            row["connect"].setVisible((not has_account) or needs_action)
            row["manage"].setVisible(has_account)
            row["manage"].setEnabled(has_account)
        try:
            from chopster.automation.browser import session as bsession
            # Prefer the Instagram Browser Session account's own profile; fall
            # back to the legacy per-platform folder when none exists yet.
            ig_aid = ""
            try:
                ig_aid = next((str(a.get("id")) for a in mgr.db.automation_list_accounts("instagram")
                               if str(a.get("auth_mode") or "") == "browser"
                               and str(a.get("status") or "") != "disconnected"), "")
            except Exception:
                ig_aid = ""
            if bsession.session_valid("instagram", ig_aid):
                self.ig_session_lbl.setText(
                    f"● Session active ({bsession.session_verified_at('instagram', ig_aid)})")
            else:
                self.ig_session_lbl.setText("● No browser session")
        except Exception:
            self.ig_session_lbl.setText("● Playwright not installed")
        self._refresh_diagnostics()

    def _refresh_diagnostics(self) -> None:
        if not hasattr(self, "diag_summary"):
            return                      # the Diagnostics card is not built yet
        mgr = self._manager()
        if mgr is None or not hasattr(mgr, "diagnostics"):
            self.diag_summary.setText("Automation belum aktif.")
            self.diag_body.setText("—")
            return
        try:
            data = mgr.diagnostics()
        except Exception as exc:
            self.diag_summary.setText(f"Diagnostics gagal: {str(exc)[:120]}")
            self.diag_body.setText("—")
            return
        engine = data.get("channel") or "bundled"
        pw = "terpasang" if data.get("playwright") else "BELUM terpasang (jalankan: python -m playwright install chromium)"
        self.diag_summary.setText(
            f"Playwright: {pw} · Browser engine: {engine} · "
            f"TikTok path: {data.get('tiktok_mode')} · "
            f"Test Mode: {'AKTIF' if data.get('test_mode') else 'nonaktif'} · "
            f"Soft cap: {data.get('soft_cap') or 'nonaktif'}")
        soft_cap = int(data.get("soft_cap") or 0)
        lines: list[str] = []
        for entry in data.get("platforms") or []:
            platform = str(entry.get("platform") or "")
            review = entry.get("policy_reviewed") or {}
            reviewed = (f"ditinjau {review.get('date')}" if review.get("date") else "belum ditinjau")
            lines.append(
                f"• {PLATFORM_LABELS.get(platform, platform)}: {entry.get('adapter')} · "
                f"akun: {entry.get('accounts')} · policy: {reviewed}")
            for sess in entry.get("sessions") or []:
                extra = ""
                if soft_cap > 0 and sess.get("cap_remaining") is not None:
                    extra = f" · sisa cap 24 jam: {sess.get('cap_remaining')}"
                lines.append(
                    f"    ↳ browser: {sess.get('display_name') or sess.get('account_id')} — "
                    f"{sess.get('session')}{extra}")
            lf = entry.get("last_failure") or {}
            if lf:
                reason = lf.get("reason") or lf.get("error_code") or "error"
                lines.append(f"    ↳ gagal terakhir: {reason}")
        self.diag_body.setText("\n".join(lines) or "—")

    def _save_diag_options(self) -> None:
        values = {
            "aut_browser_test_mode": bool(self.diag_test_mode.isChecked()),
            "aut_browser_soft_cap": int(self.diag_soft_cap.value()),
        }
        try:
            self.app.config.update(values)
        except Exception as exc:
            QMessageBox.warning(self, "Diagnostics", f"Gagal menyimpan: {exc}")
            return
        QMessageBox.information(self, "Diagnostics", "Opsi disimpan.")
        self._refresh_diagnostics()

    def _mark_policy_reviewed(self) -> None:
        platform = str(self.diag_review_platform.currentData() or "")
        if not platform:
            return
        try:
            review = dict(self.app.config.get("aut_policy_review") or {})
        except Exception:
            review = {}
        if not isinstance(review, dict):
            review = {}
        review[platform] = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "scope": "API resmi + Browser Session (additive, default OFF)",
            "source": "Peninjauan pengguna di Automation → Diagnostics",
        }
        try:
            self.app.config.update({"aut_policy_review": review})
        except Exception as exc:
            QMessageBox.warning(self, "Diagnostics", f"Gagal menyimpan: {exc}")
            return
        self._refresh_diagnostics()

    def _save_client_config(self) -> None:
        ok_uri, uri_result = validate_redirect_uri(self.redirect_field.text())
        if not ok_uri:
            QMessageBox.warning(self, "Developer Config", uri_result)
            return
        self.redirect_field.setText(uri_result)
        values: dict[str, str] = {}
        values["aut_oauth_redirect_uri"] = uri_result
        for platform, (cid, csec) in self.client_fields.items():
            values[f"aut_{platform}_client_id"] = cid.text().strip()
            secret = csec.text().strip()
            if secret:
                values[f"aut_{platform}_client_secret"] = secret
        ok = self.app.config.update(values)
        if ok:
            for csec in [f[1] for f in self.client_fields.values()]:
                csec.clear()
            QMessageBox.information(self, "Developer Config", "Disimpan (client secret terenkripsi DPAPI).")
        else:
            QMessageBox.warning(self, "Developer Config", "Gagal menyimpan — secret store tidak tersedia.")

    def _connect(self, platform: str) -> None:
        mgr = self._manager()
        if mgr is None:
            return
        row = self.account_rows.get(platform)
        if row:
            row["status"].setText("● Connecting — selesaikan di browser…")
            row["status"].setStyleSheet(f"color: {self.C['BLUE']};")
            row["connect"].setEnabled(False)
        mode = "api"
        if platform == "instagram":
            mode = "browser" if self.ig_mode_browser.isChecked() else "api"
        task_id = f"automation:connect:{platform}"

        def worker(*, signals, cancel_check):
            signals.progress.emit(task_id, 30, f"Membuka otorisasi {PLATFORM_LABELS[platform]}…")
            try:
                # Forward the cancellation flag so closing Chopster (or
                # cancelling the task) breaks the OAuth wait immediately instead
                # of leaving the window disabled until the 5-minute timeout.
                return mgr.connect_platform(platform, mode=mode, cancel_check=cancel_check)
            except Exception as exc:
                # Surface the actionable user_message (task manager only
                # forwards str(exc)); keep the technical detail behind it.
                friendly = str(getattr(exc, "user_message", "") or "")
                detail = str(exc)
                if friendly and friendly != detail:
                    raise RuntimeError(f"{friendly}\n\nDetail: {detail}") from exc
                raise

        def done(result):
            if row:
                row["connect"].setEnabled(True)
            try:
                if platform == "facebook" and isinstance(result, dict) and result.get("pages") and result.get("needs_page_selection"):
                    pages = result["pages"]
                    names = [str(p.get("name") or p.get("id")) for p in pages]
                    from PySide6.QtWidgets import QInputDialog
                    choice, ok = QInputDialog.getItem(self, "Pilih Page", "Facebook Page:", names, 0, False)
                    if ok:
                        idx = names.index(choice)
                        mgr.auth.for_platform("facebook").select_page(str(result["account"]["id"]), str(pages[idx].get("id")))
            except Exception as exc:
                QMessageBox.warning(self, "Facebook", str(exc))
            self.refresh_all()

        def fail(err):
            if row:
                row["connect"].setEnabled(True)
            msg = str(err)
            main, _, detail = msg.partition("\n\nDetail: ")
            if "user_message" in msg:
                main = main.split("user_message=")[-1].strip(" )'\"")
            box = QMessageBox(QMessageBox.Warning, f"Hubungkan {PLATFORM_LABELS[platform]}",
                              "", QMessageBox.Ok, self)
            box.setText(f"Gagal menyambungkan.\n\n{main[:900].strip()}" if main.strip()
                        else "Gagal menyambungkan.")
            if detail:
                box.setDetailedText(detail[:4000])
            box.exec()
            self.refresh_all()
        try:
            wkr = self.app.tasks.submit(task_id, f"Connect {PLATFORM_LABELS[platform]}", worker)
            wkr.signals.result.connect(lambda _tid, res: done(res), Qt.QueuedConnection)
            wkr.signals.failed.connect(lambda _tid, err: fail(err), Qt.QueuedConnection)
        except Exception as exc:
            fail(exc)

    def _disconnect(self, platform: str) -> None:
        mgr = self._manager()
        if mgr is None:
            return
        accounts = [a for a in mgr.db.automation_list_accounts(platform) if a.get("status") != "disconnected"]
        if not accounts:
            return
        # Browser Session accounts have no token to revoke: disconnecting deletes
        # their whole profile folder, so say that instead of only "token dihapus".
        browser = any(str(a.get("auth_mode") or "") == "browser" for a in accounts)
        extra = ("\nSesi Browser Session (profil login) juga dihapus — login harus diulang."
                 if browser else "")
        if QMessageBox.question(self, "Disconnect", f"Putuskan {PLATFORM_LABELS[platform]}?\nToken dihapus; riwayat publish tetap tersimpan.{extra}") != QMessageBox.Yes:
            return
        for acc in accounts:
            mgr.disconnect_account(str(acc["id"]))
        self.refresh_all()

    def _ig_browser_login(self) -> None:
        """Legacy card button: same flow as the generic Browser Session login."""
        self._browser_login("instagram")

    @staticmethod
    def _supports_browser(platform: str) -> bool:
        """Browser Session is only offered where the platform allows it."""
        try:
            from chopster.automation.models import capabilities_for
            return bool(capabilities_for(platform, "browser").browser_fallback)
        except Exception:
            return False

    def _browser_login(self, platform: str) -> None:
        """Open the Chopster browser for manual login, then register the account.

        The user completes login / MFA / CAPTCHA themselves in that window;
        Chopster never collects credentials and never reads the main profile.
        """
        mgr = self._manager()
        if mgr is None:
            return
        task_id = f"automation:browserlogin:{platform}"

        def worker(*, signals, cancel_check):
            signals.progress.emit(task_id, 20, "Menunggu login manual di browser...")
            # Connect through the manager so the browser profile folder and the
            # created account id match (per-account isolation).
            res = mgr.connect_platform(platform, mode="browser")
            acc = (res or {}).get("account") if isinstance(res, dict) else None
            return str((acc or {}).get("status") or "") == "connected"

        def done(ok):
            if not ok:
                QMessageBox.information(
                    self, "Browser Session",
                    "Login belum terdeteksi. Ulangi setelah menyelesaikan login di jendela browser Chopster.")
            self.refresh_all()

        def fail(err):
            QMessageBox.warning(self, "Browser Session", str(err)[:500])
            self.refresh_all()

        try:
            wkr = self.app.tasks.submit(task_id, f"{platform} browser login", worker)
            wkr.signals.result.connect(lambda _tid, res: done(res), Qt.QueuedConnection)
            wkr.signals.failed.connect(lambda _tid, err: fail(err), Qt.QueuedConnection)
        except Exception as exc:
            fail(exc)

    def _dialog_then(self, dlg: QDialog, action) -> None:
        """Close the picker first, then run the chosen action."""
        try:
            dlg.accept()
        except Exception:
            pass
        action()

    def _open_connect_dialog(self) -> None:
        """Platform picker popup (one tidy entry point for connecting accounts)."""
        mgr = self._manager()
        if mgr is None:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Sambungkan Akun")
        dlg.setMinimumWidth(560)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)
        lay.addWidget(self._lbl(
            "Pilih platform yang ingin disambungkan. Jalur API resmi disarankan; "
            "Browser Session hanya tersedia untuk platform yang mendukungnya dan "
            "memiliki risiko akun (mode lanjutan).", dlg, muted=True))
        try:
            accounts = {str(a.get("platform")): a for a in mgr.db.automation_list_accounts()}
        except Exception:
            accounts = {}
        for platform in PLATFORMS:
            acc = accounts.get(platform)
            if acc and str(acc.get("status") or "") == "disconnected":
                acc = None
            row = QFrame(dlg)
            row.setObjectName("autCard")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(12, 10, 12, 10)
            label = PLATFORM_LABELS.get(platform, platform)
            if acc:
                label += " - Terhubung"
                if str(acc.get("auth_mode") or "") == "browser":
                    label += " (Browser Session)"
            else:
                label += " - Belum tersambung"
            rl.addWidget(self._lbl(label, row), 1)
            if acc:
                rl.addWidget(self._btn(
                    "Kelola",
                    lambda _=False, p=platform, a=dict(acc): self._manage_account_dialog(p, a)))
            else:
                rl.addWidget(self._btn(
                    "Connect (API)",
                    lambda _=False, p=platform: self._dialog_then(dlg, lambda: self._connect(p)),
                    primary=True))
                browser_btn = self._btn(
                    "Browser Session",
                    lambda _=False, p=platform: self._dialog_then(dlg, lambda: self._browser_login(p)))
                supports = self._supports_browser(platform)
                browser_btn.setEnabled(supports)
                browser_btn.setToolTip(
                    "Login manual di jendela browser Chopster (risiko akun)."
                    if supports else "Belum tersedia untuk platform ini.")
                rl.addWidget(browser_btn)
            lay.addWidget(row)
        bb = QDialogButtonBox(QDialogButtonBox.Close, dlg)
        bb.rejected.connect(dlg.reject)
        bb.accepted.connect(dlg.accept)
        lay.addWidget(bb)
        dlg.exec()

    def _manage_account_dialog(self, platform: str, acc: dict) -> None:
        """Per-account panel: browser login/verification + disconnect."""
        mgr = self._manager()
        if mgr is None:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Kelola " + str(PLATFORM_LABELS.get(platform, platform)))
        dlg.setMinimumWidth(460)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)
        mode = str(acc.get("auth_mode") or "oauth")
        lay.addWidget(self._lbl(
            "Akun: " + str(acc.get("display_name") or acc.get("id") or "-") + " - jalur: "
            + ("Browser Session" if mode == "browser" else "API resmi"), dlg))
        if self._supports_browser(platform):
            lay.addWidget(self._btn(
                "Login / Verifikasi browser",
                lambda _=False, p=platform: self._dialog_then(dlg, lambda: self._browser_login(p))))
        if str(acc.get("credential_ref") or "").startswith("browser:"):
            lay.addWidget(self._lbl(
                "Sesi browser tersimpan di profil Chopster (bukan profil Chrome kamu). "
                "Logout/hapus sesi lewat kartu Browser Session di tab ini.", dlg, muted=True))
        lay.addWidget(self._btn(
            "Putuskan akun",
            lambda _=False, p=platform: self._dialog_then(dlg, lambda: self._disconnect(p))))
        bb = QDialogButtonBox(QDialogButtonBox.Close, dlg)
        bb.rejected.connect(dlg.reject)
        bb.accepted.connect(dlg.accept)
        lay.addWidget(bb)
        dlg.exec()

    # ------------------------------------------------------------------ #
    # Queue
    # ------------------------------------------------------------------ #
    def _build_queue(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 10, 6, 6)
        lay.setSpacing(8)
        lay.addWidget(self._lbl(
            "Antrean upload: setiap baris = 1 clip × 1 platform. Status berjalan "
            "dari Queued → Running → Published. Double-click baris untuk detail, "
            "klik ✕ Batal di barisnya (atau pilih baris + tombol ✕ Batalkan) untuk "
            "membatalkan job, dan 🗑 Hapus di barisnya untuk menghapus job yang "
            "sudah selesai. Hasil yang sudah tayang ada di tab History & Analytics.", w, muted=True))
        bar = QHBoxLayout()
        self.q_refresh_btn = self._btn("↻ Muat Ulang", self._refresh_queue, primary=True)
        self.q_pause_btn = self._btn("⏸ Jeda", self._pause_selected)
        self.q_resume_btn = self._btn("▶ Lanjut", self._resume_selected)
        self.q_retry_btn = self._btn("↻ Ulangi", self._retry_selected)
        self.q_cancel_btn = self._btn("✕ Batalkan", self._cancel_selected)
        self.q_cancel_all_btn = self._btn("✕ Batalkan Antrean", self._cancel_all_queued)
        self.q_retry_failed_btn = self._btn("↻ Ulangi Gagal", self._retry_failed_bulk)
        self.q_delete_finished_btn = self._btn(
            "🗑 Hapus Gagal/Batal", self._delete_finished_bulk)
        for b in (self.q_refresh_btn, self.q_pause_btn, self.q_resume_btn,
                  self.q_retry_btn, self.q_cancel_btn, self.q_cancel_all_btn,
                  self.q_retry_failed_btn, self.q_delete_finished_btn):
            bar.addWidget(b)
        bar.addStretch()
        # Action buttons act on the selected row; arming them without one made
        # every click look dead ("tombolnya tidak ada fungsinya").
        for b in (self.q_pause_btn, self.q_resume_btn,
                  self.q_retry_btn, self.q_cancel_btn):
            b.setEnabled(False)
        self.queue_source_filter = QComboBox(w)
        self.queue_source_filter.addItem("Semua sumber")
        for label, _key in MEDIA_SOURCE_CHOICES[1:]:
            self.queue_source_filter.addItem(label)
        self.queue_source_filter.addItem("Auto-Pilot")
        self.queue_source_filter.currentTextChanged.connect(lambda _t: self._refresh_queue())
        bar.addWidget(self.queue_source_filter)
        self.queue_filter = QComboBox(w)
        self.queue_filter.addItems(["Semua status", "Queued", "Running", "Scheduled", "Action Required", "Failed", "Published"])
        self.queue_filter.currentTextChanged.connect(lambda _t: self._refresh_queue())
        bar.addWidget(self.queue_filter)
        lay.addLayout(bar)
        self.queue_table = QTableWidget(0, 9, w)
        self.queue_table.setHorizontalHeaderLabels(["Status", "Clip", "Source", "Platform", "Account", "Schedule", "Progress", "Detail", "Aksi"])
        hdr = self.queue_table.horizontalHeader()
        for col in (0, 2, 3, 4, 5, 6, 8):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        hdr.setSectionResizeMode(7, QHeaderView.Stretch)
        self.queue_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.queue_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.queue_table.verticalHeader().setVisible(False)
        self.queue_table.cellDoubleClicked.connect(self._open_queue_row)
        self.queue_table.itemSelectionChanged.connect(self._sync_queue_buttons)
        lay.addWidget(self.queue_table, 1)
        self.queue_empty_lbl = QLabel(
            "Belum ada job di antrean. Buat automation dulu di tab Create "
            "(pilih clip → platform → Start), atau jalankan rule di tab Auto-Pilot.", w)
        self.queue_empty_lbl.setObjectName("muted")
        self.queue_empty_lbl.setWordWrap(True)
        lay.addWidget(self.queue_empty_lbl)
        return w

    def _queue_filter_status(self) -> list[str]:
        text = self.queue_filter.currentText()
        return {"Queued": ["QUEUED", "RETRYING"], "Running": ["AUTH_CHECK", "PREPARING", "UPLOADING", "PROCESSING", "VERIFYING"], "Scheduled": ["SCHEDULED"], "Action Required": ["ACTION_REQUIRED"], "Failed": ["FAILED"], "Published": ["PUBLISHED"]}.get(text, [])

    def _refresh_queue(self) -> None:
        mgr = self._manager()
        statuses = self._queue_filter_status()
        try:
            rows = mgr.db.automation_list_jobs(statuses=statuses or None, limit=300) if mgr else []
        except Exception:
            rows = []
        filtered = bool(statuses) or \
            self.queue_source_filter.currentText() != "Semua sumber"
        source_key = SOURCE_KEYS.get(self.queue_source_filter.currentText(), "")
        if self.queue_source_filter.currentText() == "Auto-Pilot":
            rows = [j for j in rows if str(j.get("source") or "") == "autopilot"]
        elif source_key:
            rows = [j for j in rows if str(j.get("source") or "") == source_key]
        account_names = {}
        try:
            for acc in (mgr.db.automation_list_accounts() if mgr else []):
                account_names[str(acc["id"])] = str(acc.get("display_name") or acc["id"])
        except Exception:
            pass
        self.queue_table.setRowCount(0)
        if rows:
            self.queue_empty_lbl.setVisible(False)
        else:
            self.queue_empty_lbl.setVisible(True)
            if filtered:
                self.queue_empty_lbl.setText(
                    "Tidak ada job yang cocok dengan filter ini. Pilih "
                    "«Semua sumber» + «Semua status» untuk melihat seluruh antrean.")
            else:
                self.queue_empty_lbl.setText(
                    "Belum ada job di antrean. Buat automation dulu di tab Create "
                    "(pilih clip → platform → Start), atau jalankan rule di tab Auto-Pilot.")
        queued_thumbs = 0
        for job in rows:
            r = self.queue_table.rowCount()
            self.queue_table.insertRow(r)
            status = str(job.get("status") or "")
            reason = str(job.get("reason") or "")
            st_text = human_job_status(status)
            if reason:
                # Surface the machine reason (draft_ready / captcha / ...) right
                # in the status cell so the user sees what is needed at a glance.
                st_text = f"{st_text} · {REASON_LABELS.get(reason, reason)}"
            st_item = QTableWidgetItem(st_text)
            _, color = _status_style(status, self.C)
            st_item.setForeground(QColor(color))
            self.queue_table.setItem(r, 0, st_item)
            self.queue_table.setItem(r, 1, QTableWidgetItem(str(job.get("media_title") or Path(str(job.get("media_path") or "")).name)))
            self.queue_table.setItem(r, 2, QTableWidgetItem(_source_label(job.get("source"))))
            self.queue_table.setItem(r, 3, QTableWidgetItem(PLATFORM_LABELS.get(str(job.get("platform")), str(job.get("platform")))))
            self.queue_table.setItem(r, 4, QTableWidgetItem(account_names.get(str(job.get("account_id")), "-")))
            when = str(job.get("scheduled_at") or "")
            self.queue_table.setItem(r, 5, QTableWidgetItem(when or "-"))
            self.queue_table.setItem(r, 6, QTableWidgetItem(f"{job.get('progress') or 0}%"))
            detail = str(job.get("error_message") or job.get("last_activity") or "")
            if job.get("attempt"):
                detail = f"[try {job.get('attempt')}] {detail}"
            self.queue_table.setItem(r, 7, QTableWidgetItem(detail[:200]))
            # Full job row travels with the Status cell: detail dialogs and
            # selection-driven actions read it from there.
            self.queue_table.item(r, 0).setData(Qt.UserRole, dict(job))
            # Per-row actions: cancel + delete, right where the user looks.
            cell = QWidget(self.queue_table)
            cell_l = QHBoxLayout(cell)
            cell_l.setContentsMargins(4, 2, 4, 2)
            cell_l.setSpacing(4)
            cancel_btn = self._btn(
                "✕ Batal",
                lambda _=False, jid=str(job.get("id") or ""): self._cancel_job_row(jid))
            cancel_btn.setEnabled(status.upper() in CANCELLABLE_JOB_STATUSES)
            cancel_btn.setToolTip(
                "Batalkan job ini" if cancel_btn.isEnabled()
                else "Status ini sudah final — pakai ↻ Ulangi Gagal untuk job gagal")
            delete_btn = self._btn(
                "🗑 Hapus",
                lambda _=False, jid=str(job.get("id") or ""): self._delete_job_row(jid))
            delete_btn.setEnabled(status.upper() in DELETABLE_JOB_STATUSES)
            delete_btn.setToolTip(
                "Hapus job ini dari Queue & History (file video tetap di disk)"
                if delete_btn.isEnabled()
                else "Job masih berjalan — batalkan dulu dengan ✕ Batal")
            cell_l.addWidget(cancel_btn)
            cell_l.addWidget(delete_btn)
            if status.upper() == "ACTION_REQUIRED":
                resume_btn = self._btn(
                    "⟳ Cek & Lanjut",
                    lambda _=False, jid=str(job.get("id") or ""): self._verify_resume_job(jid))
                resume_btn.setToolTip(
                    "Cek sesi/verifikasi lalu masukkan kembali job ini ke antrean")
                cell_l.addWidget(resume_btn)
            cell_l.addStretch()
            self.queue_table.setCellWidget(r, 8, cell)
            # Poster frames so the row shows WHICH video this job uploads.
            jpath = str(job.get("media_path") or "")
            if (jpath and queued_thumbs < 60
                    and jpath not in self._thumb_pixmaps
                    and jpath not in self._thumb_pending):
                queued_thumbs += 1
                self._queue_thumb(jpath)
                cached = self._thumb_pixmaps.get(jpath)
                if cached is not None:
                    self.queue_table.item(r, 1).setIcon(
                        QIcon(cached.scaledToHeight(44, Qt.SmoothTransformation)))
        self._sync_queue_buttons()

    def _retry_failed_bulk(self) -> None:
        """Retry every failed/action-required job after an explicit confirmation."""
        mgr = self._manager()
        if mgr is None:
            return
        try:
            candidates = [j for j in mgr.db.automation_list_jobs(
                statuses=["FAILED", "ACTION_REQUIRED"], limit=500)]
        except Exception:
            candidates = []
        if not candidates:
            QMessageBox.information(self, "Retry", "Tidak ada job Failed / Action Required.")
            return
        if QMessageBox.question(
                self, "Retry Failed",
                f"Coba ulang {len(candidates)} job yang gagal?") != QMessageBox.Yes:
            return
        done = sum(1 for job in candidates if mgr.retry_job(str(job["id"])))
        QMessageBox.information(self, "Retry", f"{done} dari {len(candidates)} job dimasukkan kembali ke antrean.")
        self._refresh_queue()

    def _selected_job_id(self) -> str:
        row = self.queue_table.currentRow()
        if row < 0:
            return ""
        item = self.queue_table.item(row, 0)
        data = item.data(Qt.UserRole) if item else None
        return str(data.get("id")) if isinstance(data, dict) else ""

    def _sync_queue_buttons(self) -> None:
        """Arm the selection-driven actions only when a row is actually selected."""
        has_row = bool(self._selected_job_id())
        for b in (self.q_pause_btn, self.q_resume_btn,
                  self.q_retry_btn, self.q_cancel_btn):
            b.setEnabled(has_row)

    def _pause_selected(self):
        mgr = self._manager()
        jid = self._selected_job_id()
        if not mgr or not jid:
            QMessageBox.information(self, "Jeda", "Pilih dulu satu baris job di tabel.")
            return
        mgr.pause_job(jid)
        self._refresh_queue()

    def _resume_selected(self):
        mgr = self._manager()
        jid = self._selected_job_id()
        if not mgr or not jid:
            QMessageBox.information(self, "Lanjut", "Pilih dulu satu baris job di tabel.")
            return
        if not mgr.resume_job(jid):
            # A refused resume explains itself (e.g. browser session not logged in).
            job = mgr.db.automation_get_job(jid) or {}
            QMessageBox.information(
                self, "Lanjut",
                str(job.get("error_message") or "") or "Job ini tidak dalam status Jeda.")
        self._refresh_queue()

    def _retry_selected(self):
        mgr = self._manager()
        jid = self._selected_job_id()
        if not mgr or not jid:
            QMessageBox.information(self, "Ulangi", "Pilih dulu satu baris job di tabel.")
            return
        if mgr.retry_job(jid):
            self._refresh_queue()
            return
        # Explain the real reason: a delivered draft must be finished in the app
        # and must never be re-sent blindly (that would duplicate the draft).
        job = mgr.db.automation_get_job(jid) or {}
        detail = str(job.get("error_message") or "")
        if str(job.get("verification_status") or "") == "DRAFT_READY":
            detail = detail or ("Draft sudah dikirim ke aplikasi platform. Publish dari aplikasi; "
                               "untuk mengirim ulang, hapus draft itu dulu.")
        QMessageBox.information(
            self, "Ulangi",
            detail or "Hanya job Failed / Action Required / Cancelled yang bisa diulang.")

    def _cancel_selected(self):
        mgr = self._manager()
        jid = self._selected_job_id()
        if not mgr or not jid:
            QMessageBox.information(
                self, "Batalkan",
                "Pilih dulu satu baris job di tabel, atau klik ✕ Batal di barisnya.")
            return
        self._cancel_job_row(jid)

    def _cancel_job_row(self, job_id: str) -> None:
        mgr = self._manager()
        if not mgr or not job_id:
            return
        if QMessageBox.question(
                self, "Batalkan Job",
                "Batalkan job ini dari antrean?\n\n"
                "Job yang sedang berjalan dihentikan secepat mungkin; job yang "
                "sudah Published/CANCELLED tidak bisa dibatalkan.") != QMessageBox.Yes:
            return
        mgr.cancel_job(job_id)
        self._refresh_queue()

    def _verify_resume_job(self, job_id: str) -> None:
        """'Cek & Lanjut' for an ACTION_REQUIRED row (draft / login / captcha)."""
        mgr = self._manager()
        if not mgr or not job_id or not hasattr(mgr, "verify_and_resume_job"):
            return
        try:
            ok, message = mgr.verify_and_resume_job(job_id)
        except Exception as exc:
            QMessageBox.warning(self, "Cek & Lanjut", str(exc))
            return
        (QMessageBox.information if ok else QMessageBox.warning)(
            self, "Cek & Lanjut", message)
        self._refresh_queue()

    def _cancel_all_queued(self) -> None:
        """Bulk-cancel every job that is still cancellable, after confirmation."""
        mgr = self._manager()
        if mgr is None:
            return
        try:
            candidates = [
                j for j in mgr.db.automation_list_jobs(limit=500)
                if str(j.get("status") or "").upper() in CANCELLABLE_JOB_STATUSES]
        except Exception:
            candidates = []
        if not candidates:
            QMessageBox.information(
                self, "Batalkan Antrean",
                "Tidak ada job aktif yang bisa dibatalkan.")
            return
        if QMessageBox.question(
                self, "Batalkan Antrean",
                f"Batalkan semua {len(candidates)} job yang masih berjalan / "
                "terantre?\n\nJob yang sudah Published, Failed, atau Cancelled "
                "tetap tersimpan.") != QMessageBox.Yes:
            return
        for job in candidates:
            try:
                mgr.cancel_job(str(job.get("id")))
            except Exception:
                pass
        self._refresh_queue()

    def _delete_job_row(self, job_id: str) -> None:
        """Delete one finished job (never touches the video file on disk)."""
        mgr = self._manager()
        if not mgr or not job_id:
            return
        if QMessageBox.question(
                self, "Hapus Job",
                "Hapus job ini dari antrean?\n\n"
                "Baris ini juga hilang dari History & Analytics. File video "
                "TIDAK dihapus dari disk.") != QMessageBox.Yes:
            return
        try:
            mgr.delete_job(job_id)
        except Exception as exc:
            QMessageBox.information(
                self, "Hapus Job",
                str(getattr(exc, "user_message", None) or exc))
            return
        self._refresh_queue()

    def _delete_finished_bulk(self) -> None:
        """Clear out Failed/Cancelled rows after an explicit confirmation."""
        mgr = self._manager()
        if mgr is None:
            return
        try:
            candidates = list(mgr.db.automation_list_jobs(
                statuses=["FAILED", "CANCELLED"], limit=500))
        except Exception:
            candidates = []
        if not candidates:
            QMessageBox.information(
                self, "Hapus Job Selesai",
                "Tidak ada job Failed / Cancelled untuk dihapus.")
            return
        if QMessageBox.question(
                self, "Hapus Job Selesai",
                f"Hapus {len(candidates)} job Failed / Cancelled dari antrean?\n\n"
                "Job yang sedang berjalan dan yang sudah Published tetap "
                "tersimpan. File video tidak dihapus dari disk."
                ) != QMessageBox.Yes:
            return
        removed = 0
        for job in candidates:
            try:
                if mgr.delete_job(str(job.get("id"))):
                    removed += 1
            except Exception:
                pass
        QMessageBox.information(self, "Hapus Job Selesai",
                                f"{removed} job dihapus dari antrean.")
        self._refresh_queue()

    def _open_queue_row(self, row: int, _col: int) -> None:
        item = self.queue_table.item(row, 0)
        data = item.data(Qt.UserRole) if item else None
        if not isinstance(data, dict):
            return
        url = str(data.get("external_url") or "")
        if url:
            from PySide6.QtGui import QDesktopServices
            from PySide6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl(url))
            return
        QMessageBox.information(self, "Job Detail", json.dumps({k: data.get(k) for k in ("id", "platform", "status", "attempt", "verification_status", "reason", "error_code", "error_message", "last_activity")}, ensure_ascii=False, indent=2))

    # ------------------------------------------------------------------ #
    # Recipes
    # ------------------------------------------------------------------ #
    def _build_recipes(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 10, 6, 6)
        lay.setSpacing(10)
        lay.addWidget(self._section("PRESET — TEMPLATE PUBLISH SEKALI KLIK"))
        how = self._card()
        how.layout().addWidget(self._lbl(
            "Preset menyimpan platform + jam publish + aturan review pilihanmu. "
            "Tekan «▶ Jalankan sekarang» untuk langsung membuat automation dari "
            "preset itu, atau «● Aktifkan» supaya job-nya dibuat otomatis berulang. "
            "Simpan preset baru dari tab Create → langkah Review → centang "
            "«Simpan konfigurasi ini sebagai Preset».", how, muted=True))
        lay.addWidget(how)

        # Two-column grid: the previous one-row-per-card list left the bottom
        # half of the tab empty and hid what each preset actually does.
        self.recipes_grid = QGridLayout()
        self.recipes_grid.setSpacing(10)
        self.recipes_grid.setColumnStretch(0, 1)
        self.recipes_grid.setColumnStretch(1, 1)
        wrap = QWidget()
        wrap.setLayout(self.recipes_grid)
        scroll = QScrollArea(w)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(wrap)
        lay.addWidget(scroll, 1)

        tips = self._card()
        tips.layout().addWidget(self._section("CARA PAKAI CEPAT"))
        self.preset_ready_lbl = self._lbl("", tips, muted=True)
        tips.layout().addWidget(self.preset_ready_lbl)
        for line in (
            "1. Pilih preset yang paling dekat dengan kebutuhanmu (mis. Multi-Platform Daily).",
            "2. Klik «▶ Jalankan sekarang» → automation + job-nya dibuat; kamu "
            "langsung diarahkan ke tab Queue.",
            "3. Di Queue terlihat video & platform tiap job — batalkan (✕ Batal) "
            "atau hapus (🗑 Hapus) yang tidak jadi.",
            "4. Mau preset sendiri? Susun di tab Create, lalu simpan sebagai "
            "Preset di langkah Review.",
        ):
            tips.layout().addWidget(self._lbl(line, tips, muted=True))
        jump = QHBoxLayout()
        jump.addWidget(self._btn("⚙ Perbaiki yang belum siap", self.refresh_all))
        jump.addStretch()
        tips.layout().addLayout(jump)
        lay.addWidget(tips)
        return w

    def _refresh_recipes(self) -> None:
        while self.recipes_grid.count():
            item = self.recipes_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        mgr = self._manager()
        try:
            recipes = mgr.list_recipes() if mgr else []
        except Exception:
            recipes = []
        if not recipes:
            self.recipes_grid.addWidget(self._empty(
                "Belum ada preset",
                "Preset adalah konfigurasi publish yang bisa dipakai ulang. Simpan "
                "dari wizard Studio (langkah Review → «Simpan konfigurasi ini "
                "sebagai Preset»), atau tunggu preset bawaan muncul setelah "
                "Automation Manager berjalan.",
                "Buka Studio",
                lambda: (self.tabs.setCurrentIndex(self.TAB_STUDIO),
                         self._reset_wizard())), 0, 0, 1, 2)
            return
        for index, recipe in enumerate(recipes):
            card = self._recipe_card(recipe)
            self.recipes_grid.addWidget(card, index // 2, index % 2)
            # Cap the cascade so a long preset list never leaves the last cards
            # waiting more than ~300 ms before they fade in.
            self._fade_in(card, delay_ms=min(index, 5) * 60)

    @staticmethod
    def _recipe_icon(name: str) -> str:
        n = str(name or "").lower()
        if "multi" in n:
            return "🌐"
        if "daily" in n or "harian" in n:
            return "📅"
        if "short" in n:
            return "⚡"
        if "review" in n or "preview" in n or "manual" in n:
            return "👁"
        if "now" in n:
            return "🚀"
        return "🧩"

    def _edit_recipe(self, rid: str) -> None:
        """Edit an existing preset/template in place (same recipe id)."""
        mgr = self._manager()
        if mgr is None:
            return
        recipe = next((r for r in (mgr.list_recipes() or [])
                       if str(r.get("id")) == str(rid)), None)
        if not recipe:
            QMessageBox.information(self, "Preset", "Preset tidak ditemukan.")
            return
        cfg = dict(recipe.get("config_obj") or {})

        dlg = QDialog(self)
        dlg.setWindowTitle("Edit Preset / Template")
        dlg.setMinimumWidth(580)
        outer = QVBoxLayout(dlg)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(10)
        form = QFormLayout()

        name_edit = QLineEdit(str(recipe.get("name") or ""), dlg)
        form.addRow("Nama preset", name_edit)

        plat_box = QWidget(dlg)
        pgrid = QGridLayout(plat_box)
        pgrid.setContentsMargins(0, 0, 0, 0)
        pgrid.setSpacing(6)
        plat_checks: dict[str, QCheckBox] = {}
        cur_plats = [str(p) for p in (cfg.get("platforms") or [])]
        for i, plat in enumerate(PLATFORMS):
            cb = QCheckBox(PLATFORM_LABELS.get(plat, plat), plat_box)
            cb.setChecked(plat in cur_plats)
            plat_checks[plat] = cb
            pgrid.addWidget(cb, i // 2, i % 2)
        form.addRow("Platform", plat_box)

        mode_combo = QComboBox(dlg)
        for lbl, val in (("Sekarang (langsung)", "now"),
                         ("Terjadwal (jam tertentu)", "schedule"),
                         ("Staggered (satu video per interval)", "stagger")):
            mode_combo.addItem(lbl, val)
        _mi = mode_combo.findData(str(cfg.get("mode") or "now"))
        mode_combo.setCurrentIndex(_mi if _mi >= 0 else 0)
        form.addRow("Waktu", mode_combo)

        time_edit = QTimeEdit(dlg)
        try:
            _hh, _mm = (int(x) for x in str(cfg.get("time") or "19:00").split(":"))
            time_edit.setTime(QTime(_hh, _mm))
        except Exception:
            time_edit.setTime(QTime(19, 0))
        form.addRow("Jam publish", time_edit)

        _st = cfg.get("stagger") if isinstance(cfg.get("stagger"), dict) else {}
        interval = QSpinBox(dlg)
        interval.setRange(1, 1440)
        interval.setSuffix(" menit")
        interval.setValue(int(_st.get("interval_minutes") or 120))
        form.addRow("Interval (stagger)", interval)

        hours_box = QWidget(dlg)
        hrow = QHBoxLayout(hours_box)
        hrow.setContentsMargins(0, 0, 0, 0)
        _hours = _st.get("active_hours") if isinstance(_st.get("active_hours"), list) else [10, 23]
        from_edit = QTimeEdit(hours_box)
        to_edit = QTimeEdit(hours_box)
        try:
            from_edit.setTime(QTime(int(_hours[0]), 0))
            to_edit.setTime(QTime(int(_hours[1]), 0))
        except Exception:
            from_edit.setTime(QTime(10, 0))
            to_edit.setTime(QTime(23, 0))
        hrow.addWidget(from_edit)
        hrow.addWidget(QLabel("s/d", hours_box))
        hrow.addWidget(to_edit)
        hrow.addStretch()
        form.addRow("Jam operasional", hours_box)

        privacy = QComboBox(dlg)
        for lbl, val in (("Public", "public"), ("Unlisted", "unlisted"), ("Private", "private")):
            privacy.addItem(lbl, val)
        _pi = privacy.findData(str(cfg.get("privacy") or "public"))
        privacy.setCurrentIndex(_pi if _pi >= 0 else 0)
        mfk = QComboBox(dlg)
        mfk.addItems(["Tidak", "Ya"])
        mfk.setCurrentText("Ya" if cfg.get("made_for_kids") else "Tidak")
        lang_combo = QComboBox(dlg)
        for lbl, val in (("Indonesia", "id"), ("English", "en"), ("Ikuti bahasa video", "auto")):
            lang_combo.addItem(lbl, val)
        _li = lang_combo.findData(str(cfg.get("ai_language") or "id"))
        lang_combo.setCurrentIndex(_li if _li >= 0 else 0)
        scope = QComboBox(dlg)
        for lbl, val in (("1 video", 1), ("2 video", 2), ("3 video", 3), ("5 video", 5),
                         ("10 video", 10), ("Semua video siap", 0)):
            scope.addItem(lbl, val)
        _si = scope.findData(int(cfg.get("max_per_run") or 0))
        scope.setCurrentIndex(_si if _si >= 0 else 0)

        opts = QWidget(dlg)
        og = QFormLayout(opts)
        og.setContentsMargins(0, 0, 0, 0)
        og.addRow("Privacy", privacy)
        og.addRow("Made for Kids", mfk)
        og.addRow("Bahasa AI", lang_combo)
        og.addRow("Maksimal video per klik/jalan", scope)
        form.addRow(opts)

        review_cb = QCheckBox("Review sebelum publish", dlg)
        review_cb.setChecked(bool(cfg.get("review")))
        acs_cb = QCheckBox("Aktifkan sebagai template publish 1-klik dari ACS", dlg)
        acs_cb.setChecked(bool(cfg.get("acs_publish")))
        ai_cb = QCheckBox("Generate AI judul/deskripsi/hashtag saat publish", dlg)
        ai_cb.setChecked(bool(cfg.get("ai_auto", True)))
        form.addRow("", review_cb)
        form.addRow("", acs_cb)
        form.addRow("", ai_cb)
        outer.addLayout(form)

        def _sync_mode() -> None:
            m = str(mode_combo.currentData() or "now")
            time_edit.setEnabled(m == "schedule")
            interval.setEnabled(m == "stagger")
            hours_box.setEnabled(m == "stagger")

        mode_combo.currentIndexChanged.connect(lambda _i: _sync_mode())
        _sync_mode()

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel, dlg)
        try:
            bb.button(QDialogButtonBox.Save).setText("Simpan")
            bb.button(QDialogButtonBox.Cancel).setText("Batal")
        except Exception:
            pass
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        outer.addWidget(bb)

        if dlg.exec() != QDialog.Accepted:
            return
        new_name = name_edit.text().strip()
        if not new_name:
            QMessageBox.information(self, "Preset", "Nama preset tidak boleh kosong.")
            return
        new_cfg = dict(cfg)          # preserve any keys this dialog doesn't show
        new_cfg["platforms"] = [p for p, cb in plat_checks.items() if cb.isChecked()]
        new_cfg["mode"] = str(mode_combo.currentData() or "now")
        new_cfg["time"] = time_edit.time().toString("HH:mm")
        new_cfg["review"] = bool(review_cb.isChecked())
        new_cfg["max_per_run"] = int(scope.currentData())
        new_cfg["acs_publish"] = bool(acs_cb.isChecked())
        new_cfg["privacy"] = str(privacy.currentData() or "public")
        new_cfg["made_for_kids"] = (mfk.currentText() == "Ya")
        new_cfg["ai_language"] = str(lang_combo.currentData() or "id")
        new_cfg["ai_auto"] = bool(ai_cb.isChecked())
        if new_cfg["mode"] == "stagger":
            lo = int(from_edit.time().hour())
            hi = int(to_edit.time().hour())
            new_cfg["stagger"] = {
                "start": datetime.now().replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M"),
                "interval_minutes": int(interval.value()),
                "active_hours": [lo, hi] if lo < hi else [],
            }
        else:
            new_cfg.pop("stagger", None)
        try:
            mgr.save_recipe(new_name, new_cfg, recipe_id=str(rid))
        except Exception as exc:
            QMessageBox.warning(self, "Preset", f"Gagal menyimpan preset: {exc}")
            return
        self._refresh_recipes()
        self._refresh_presets_combo()
        self._refresh_status_strip()

    def _recipe_card(self, recipe: dict) -> QFrame:
        """One preset as a self-explaining card: what it publishes, when, and
        the actions that act on it."""
        card = self._card()
        cfg = recipe.get("config_obj") or {}
        rid = str(recipe.get("id") or "")
        enabled = int(recipe.get("enabled") or 0)
        plats = [PLATFORM_LABELS.get(p, p) for p in (cfg.get("platforms") or [])]
        plats_txt = ", ".join(plats) or "semua platform yang sudah terhubung"
        repeat = str(cfg.get("repeat") or "")
        mode = str(cfg.get("mode") or "now")
        if repeat:
            when_txt = f"setiap {repeat}, jam {cfg.get('time') or '19:00'}"
        elif mode == "schedule":
            when_txt = f"terjadwal {cfg.get('time') or cfg.get('scheduled_at') or '—'}"
        elif mode == "stagger":
            _st = cfg.get("stagger") if isinstance(cfg.get("stagger"), dict) else {}
            when_txt = (f"bertahap tiap {int(_st.get('interval_minutes') or 120)} menit "
                        "(staggered)")
        else:
            when_txt = "langsung saat dijalankan"
        review_txt = ("menunggu persetujuanmu dulu (review before publish)"
                      if cfg.get("review") else "auto publish")

        head = QHBoxLayout()
        icon = QLabel(self._recipe_icon(str(recipe.get("name") or "")), card)
        icon.setStyleSheet("font-size: 22px;")
        head.addWidget(icon)
        title = QLabel(str(recipe.get("name") or "Preset"), card)
        title.setStyleSheet(
            f"color: {self.C['TEXT']}; font-weight: 800; font-size: 15px;")
        head.addWidget(title)
        head.addStretch()
        state = QLabel("● Aktif" if enabled else "○ Nonaktif", card)
        state.setObjectName("autStatus")
        state.setStyleSheet(
            f"color: {self.C['GREEN'] if enabled else self.C['MUTED']};")
        head.addWidget(state)
        card.layout().addLayout(head)

        card.layout().addWidget(self._lbl(
            f"Fungsi: publish otomatis ke {plats_txt} — {when_txt}, {review_txt}.",
            card, muted=True))
        _max = int(cfg.get("max_per_run") or 0)
        _cakupan = "semua video siap" if _max <= 0 else f"maksimal {_max} video"
        card.layout().addWidget(self._lbl(
            f"Cakupan: {_cakupan} per jalan/klik • "
            + ("preset aktif berjalan sendiri" if enabled
               else "preset nonaktif hanya jalan saat kamu klik Jalankan"),
            card, muted=True))
        if cfg.get("acs_publish"):
            lang_lbl = {"id": "Indonesia", "en": "English"}.get(
                str(cfg.get("ai_language") or "id"), "ikuti bahasa video")
            card.layout().addWidget(self._lbl(
                f"⚡ Template 1-klik ACS: tersedia sebagai opsi publish di Auto Clip Studio "
                f"• bahasa konten: {lang_lbl}.",
                card, muted=True))
        if "youtube" in (cfg.get("platforms") or []):
            _aud = cfg.get("made_for_kids")
            _aud_txt = ("Ya — dibuat untuk anak-anak" if _aud is True
                        else "Tidak — bukan untuk anak-anak" if _aud is False
                        else "belum diatur (upload YouTube ditahan sampai diisi)")
            card.layout().addWidget(self._lbl(
                f"YouTube audience (made for kids): {_aud_txt}.", card, muted=True))

        row = QHBoxLayout()
        row.addWidget(self._btn(
            "▶ Jalankan sekarang",
            lambda _=False, r=rid: self._use_recipe(r), primary=True))
        row.addWidget(self._btn(
            "✎ Edit", lambda _=False, r=rid: self._edit_recipe(r)))
        row.addWidget(self._btn(
            "○ Nonaktifkan" if enabled else "● Aktifkan",
            lambda _=False, r=rid, en=enabled: self._toggle_recipe(r, not bool(en))))
        row.addWidget(self._btn(
            "🗑 Hapus", lambda _=False, r=rid: self._delete_recipe(r)))
        row.addStretch()
        card.layout().addLayout(row)
        return card

    def _fade_in(self, widget: QWidget, delay_ms: int = 0) -> None:
        """Subtle entrance animation — the tab used to be completely static.

        Both timers are bound to ``widget`` with the context-object overload of
        ``QTimer.singleShot``. The preset grid is rebuilt on every refresh
        (``currentChanged`` and ``showEvent`` both reach ``_refresh_recipes``),
        and a card that disappears while its entrance animation is still
        pending used to call ``start()`` on the ``QPropertyAnimation`` that was
        destroyed together with it:

            RuntimeError: libshiboken: Internal C++ object
            (PySide6.QtCore.QPropertyAnimation) already deleted.

        Keeping the timer tied to the card makes that call impossible: Qt drops
        the callback as soon as the context object is gone.
        """
        effect = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", widget)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(220)
        anim.setEasingCurve(QEasingCurve.OutCubic)

        def _start() -> None:
            anim.start(QPropertyAnimation.DeleteWhenStopped)

        def _cleanup() -> None:
            # Drop the effect once it finished, so normal stylesheet painting
            # resumes (the animation object is already gone by then). Bound to
            # the card, so a card deleted right after fading is never touched.
            QTimer.singleShot(0, widget, lambda: widget.setGraphicsEffect(None))

        anim.finished.connect(_cleanup)
        if delay_ms > 0:
            QTimer.singleShot(delay_ms, widget, _start)
        else:
            _start()

    def _use_recipe(self, recipe_id: str) -> None:
        mgr = self._manager()
        if mgr is None:
            return
        try:
            run = mgr.run_recipe(recipe_id, trigger="manual")
        except Exception as exc:
            QMessageBox.information(
                self, "Preset",
                str(getattr(exc, "user_message", None) or exc))
            return
        if not run:
            QMessageBox.information(
                self, "Preset",
                "Preset ini belum menghasilkan automation — biasanya karena "
                "belum ada video siap. Render dulu di Auto Clip Studio / Content "
                "Clipper AI, lalu coba lagi.")
            return
        QMessageBox.information(
            self, "Preset dijalankan",
            f"Preset «{run.get('name') or 'ini'}» membuat automation baru "
            f"({run.get('status')}).\n\nDi tab Queue kamu bisa melihat video dan "
            "platform tiap job — batalkan (✕ Batal) atau hapus (🗑 Hapus) yang "
            "tidak jadi.")
        self.tabs.setCurrentIndex(self.TAB_QUEUE)
        self.refresh_all()

    def _toggle_recipe(self, recipe_id: str, enabled: bool) -> None:
        mgr = self._manager()
        if mgr:
            mgr.set_recipe_enabled(recipe_id, enabled)
            self._refresh_recipes()

    def _delete_recipe(self, recipe_id: str) -> None:
        if QMessageBox.question(self, "Recipe", "Hapus recipe ini?") != QMessageBox.Yes:
            return
        mgr = self._manager()
        if mgr:
            mgr.delete_recipe(recipe_id)
            self._refresh_recipes()

    # ------------------------------------------------------------------ #
    # History
    # ------------------------------------------------------------------ #
    def _build_history(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 10, 6, 6)
        lay.setSpacing(8)
        bar = QHBoxLayout()
        bar.addWidget(self._btn("Refresh", lambda: self._refresh_history(), primary=True))
        bar.addStretch()
        self.hist_filter = QComboBox(w)
        self.hist_filter.addItems(["All", "Published", "Scheduled", "Failed", "Action Required"])
        self.hist_filter.currentTextChanged.connect(lambda _t: self._refresh_history())
        bar.addWidget(self.hist_filter)
        lay.addLayout(bar)
        self.hist_table = QTableWidget(0, 7, w)
        self.hist_table.setHorizontalHeaderLabels(
            ["Date", "Automation", "Platform", "Status", "Judul (AI Content Pack)", "Clip", "Link"])
        hdr = self.hist_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.Stretch)
        hdr.setSectionResizeMode(5, QHeaderView.Stretch)
        hdr.setSectionResizeMode(6, QHeaderView.Stretch)
        self.hist_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.hist_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.hist_table.verticalHeader().setVisible(False)
        self.hist_table.cellDoubleClicked.connect(self._open_history_row)
        lay.addWidget(self.hist_table, 1)
        self.hist_empty = QLabel("Belum ada riwayat automation.", w)
        self.hist_empty.setObjectName("muted")
        lay.addWidget(self.hist_empty)
        return w

    def _refresh_history(self) -> None:
        mgr = self._manager()
        try:
            rows = mgr.history(300) if mgr else []
        except Exception:
            rows = []
        filt = self.hist_filter.currentText()
        want = {"Published": ["PUBLISHED"], "Scheduled": ["SCHEDULED"], "Failed": ["FAILED"], "Action Required": ["ACTION_REQUIRED"]}.get(filt, [])
        self.hist_table.setRowCount(0)
        shown = 0
        for row in rows:
            status = str(row.get("status") or "")
            if want and status not in want:
                continue
            r = self.hist_table.rowCount()
            self.hist_table.insertRow(r)
            st_item = QTableWidgetItem(human_job_status(status))
            _, color = _status_style(status, self.C)
            st_item.setForeground(QColor(color))
            self.hist_table.setItem(r, 0, QTableWidgetItem(str(row.get("created_at") or "")))
            self.hist_table.setItem(r, 1, QTableWidgetItem(str(row.get("run_name") or "—")))
            self.hist_table.setItem(r, 2, QTableWidgetItem(PLATFORM_LABELS.get(str(row.get("platform")), str(row.get("platform")))))
            self.hist_table.setItem(r, 3, st_item)
            pack = _job_pack(row)
            published_title = str(pack.get("title") or "").strip() \
                or str((pack.get("titles") or [""])[0] or "").strip()
            self.hist_table.setItem(r, 4, QTableWidgetItem(published_title or "—"))
            self.hist_table.setItem(r, 5, QTableWidgetItem(str(row.get("media_title") or "")))
            self.hist_table.setItem(r, 6, QTableWidgetItem(str(row.get("external_url") or row.get("external_post_id") or "")))
            self.hist_table.item(r, 0).setData(Qt.UserRole, dict(row))
            shown += 1
        self.hist_empty.setVisible(shown == 0)

    def _open_history_row(self, row: int, _col: int) -> None:
        item = self.hist_table.item(row, 0)
        data = item.data(Qt.UserRole) if item else None
        if not isinstance(data, dict):
            return
        pack = _job_pack(data)
        desc = str(pack.get("description_long") or pack.get("description")
                   or pack.get("description_short") or "").strip()
        tags = " ".join(str(t) for t in (pack.get("hashtags") or []))
        mfk = pack.get("made_for_kids")
        audience = ("Ya — untuk anak-anak" if mfk is True
                    else "Tidak — bukan untuk anak-anak" if mfk is False
                    else "belum dipilih")
        url = str(data.get("external_url") or "")
        lines = [
            f"Judul      : {pack.get('title') or '(tanpa judul)'}",
            f"Platform   : {PLATFORM_LABELS.get(str(data.get('platform')), data.get('platform'))}",
            f"Status     : {human_job_status(str(data.get('status') or ''))}",
            f"Clip       : {data.get('media_title') or '—'}",
            f"Jadwal     : {data.get('scheduled_at') or '—'}",
            f"Audience   : {audience}",
            f"Privasi    : {str(pack.get('privacy') or 'default platform')}",
            "",
            "Deskripsi:",
            (desc[:900] + ("…" if len(desc) > 900 else "")) if desc else "(kosong)",
            "",
            f"Hashtag    : {tags or '(tidak ada)'}",
            "",
            f"Link       : {url or '—'}",
        ]
        if data.get("error_message"):
            lines += ["", f"Catatan    : {data.get('error_message')}"]
        box = QMessageBox(self)
        box.setWindowTitle("Detail Publish")
        box.setText("\n".join(lines))
        link_btn = box.addButton("Buka link", QMessageBox.AcceptRole) if url else None
        box.addButton("Tutup", QMessageBox.RejectRole)
        box.exec()
        if link_btn is not None and box.clickedButton() is link_btn:
            from PySide6.QtGui import QDesktopServices
            from PySide6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl(url))

    # ------------------------------------------------------------------ #
    # events / refresh
    # ------------------------------------------------------------------ #
    def _thread_event(self, event: str, payload: dict) -> None:
        # Called from manager worker threads → hop to Qt main thread via signals.
        try:
            if event == "job":
                self.bridge.job.emit(dict(payload))
            elif event == "run":
                self.bridge.run.emit(dict(payload))
            elif event == "media":
                self.bridge.media.emit(dict(payload))
            elif event == "auth":
                self.bridge.auth.emit(dict(payload))
            elif event == "action_required":
                self.bridge.action.emit(dict(payload))
            elif event == "publish_failed":
                self.bridge.failed.emit(dict(payload))
            elif event == "rules":
                self.bridge.run.emit({"run_id": "", "kind": "rules"})
        except Exception:
            pass

    def _schedule_refresh(self) -> None:
        if not self._debounce.isActive():
            self._debounce.start()

    def _on_job_event(self, payload: dict) -> None:
        self._schedule_refresh()

    def _on_run_event(self, payload: dict) -> None:
        self._schedule_refresh()

    def _on_media_event(self, payload: dict) -> None:
        self._schedule_refresh()
        # Immediate "event received" feedback for ACS/CCA handoffs: the user
        # watching Auto Clip Studio render should SEE the clip land here
        # instead of wondering whether the handoff fired at all.
        try:
            status = str(payload.get("status") or "")
            if not status:
                return
            if status == "ready":
                src = SOURCE_LABELS.get(str(payload.get("source") or ""), "sumber")
                title = str(payload.get("title") or "")[:60]
                self.health_lbl.setText(f"📥 Clip baru dari {src} siap publish"
                                        + (f": «{title}»" if title else "."))
                QTimer.singleShot(8_000, self._clear_health_status)
            elif status == "rejected":
                reason = str(payload.get("reason") or "ditolak validasi")[:80]
                self.health_lbl.setText(f"⚠ 1 clip {SOURCE_LABELS.get(str(payload.get('source') or ''), 'konten')} "
                                        f"ditolak: {reason}")
                QTimer.singleShot(8_000, self._clear_health_status)
        except Exception:
            pass

    def _clear_health_status(self) -> None:
        try:
            self.health_lbl.setText("")
        except Exception:
            pass

    def _on_auth_event(self, payload: dict) -> None:
        self._schedule_refresh()

    def _on_action_event(self, payload: dict) -> None:
        self._schedule_refresh()
        if self.isVisible():
            msg = str(payload.get("message") or "Butuh tindakan pengguna.")
            QMessageBox.information(self, "Action Required", f"{msg}\n\nBuka tab Queue untuk melanjutkan atau membatalkan job.")

    def _on_failed_event(self, payload: dict) -> None:
        self._schedule_refresh()

    def refresh_current(self) -> None:
        if not self.isVisible():
            return
        idx = self.tabs.currentIndex()
        try:
            if idx == self.TAB_OVERVIEW:
                self._refresh_overview()
            elif idx == self.TAB_STUDIO:
                self._refresh_platform_rows()
                self._update_media_count()
            elif idx == self.TAB_AUTOPILOT:
                self._refresh_autopilot()
            elif idx == self.TAB_ACCOUNTS:
                self._refresh_accounts()
            elif idx == self.TAB_QUEUE:
                self._refresh_queue()
            elif idx == self.TAB_PRESETS:
                self._refresh_recipes()
            elif idx == self.TAB_HISTORY:
                self._refresh_history()
        except Exception:
            pass
        self._refresh_status_strip()

    def refresh_all(self) -> None:
        try:
            self._refresh_overview()
            self._refresh_platform_rows()
            self._refresh_accounts()
            self._refresh_queue()
            self._refresh_recipes()
            self._refresh_autopilot()
            self._refresh_history()
            if self.media_list.count() == 0:
                self._reload_media_list()
            self._refresh_status_strip()
        except Exception:
            pass

    def _first_populate(self) -> None:
        if self._populated:
            return
        self._populated = True
        if self.isVisible():
            self.refresh_all()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh_all()
        if not self._tick.isActive():
            self._tick.start()

    def hideEvent(self, event) -> None:
        self._tick.stop()
        super().hideEvent(event)

    def shutdown(self) -> None:
        try:
            self._tick.stop()
            self._debounce.stop()
        except Exception:
            pass
        if self._remove_listener:
            try:
                self._remove_listener()
            except Exception:
                pass
            self._remove_listener = None


    # ------------------------------------------------------------------ #
    # Media source filter / bulk selection (spec 5.1-5.2)
    # ------------------------------------------------------------------ #
    def _media_source_key(self) -> str:
        return SOURCE_KEYS.get(self.media_source_filter.currentText(), "")

    def _update_source_hint(self) -> None:
        """Keep the 'where do these videos come from?' line visible at all times."""
        if getattr(self, "media_source_hint", None) is None:
            return
        text = SOURCE_HINTS.get(self.media_source_filter.currentText(),
                                SOURCE_HINTS["Semua sumber"])
        self.media_source_hint.setText(text)

    # ------------------------------------------------------------------ #
    # Media list removal (complaint: "riwayat tidak bisa dihapus")
    # ------------------------------------------------------------------ #
    def _delete_media_rows(self, items: list[QListWidgetItem]) -> None:
        """Remove rows from the list — and from the ready-media registry, so a
        reload cannot resurrect them. The video file on disk is never touched."""
        mgr = self._manager()
        removed = 0
        for item in items:
            data = item.data(Qt.UserRole)
            media_id = str(data.get("media_id") or "") if isinstance(data, dict) else ""
            path = str(data.get("path") or "") if isinstance(data, dict) else ""
            if media_id and mgr is not None:
                try:
                    mgr.db.automation_delete_media(media_id)
                except Exception:
                    pass
            if path:
                self._packs.pop(path, None)
            row = self.media_list.row(item)
            self.media_list.takeItem(row)
            removed += 1
        if removed:
            self._update_media_count()
            self._mark_pack_stale()
            self._sync_pack_form()

    def _remove_selected_media(self) -> None:
        items = [self.media_list.item(i) for i in range(self.media_list.count())
                 if self.media_list.item(i).checkState() == Qt.Checked]
        if not items:
            QMessageBox.information(
                self, "Hapus", "Centang dulu video yang mau dihapus dari daftar.")
            return
        if QMessageBox.question(
                self, "Hapus Terpilih",
                f"Hapus {len(items)} video tercentang dari daftar siap?\n\n"
                "File video TIDAK ikut terhapus dari disk — hanya entri di "
                "Automation yang dilepas.") != QMessageBox.Yes:
            return
        self._delete_media_rows(items)

    def _clear_media_list(self) -> None:
        count = self.media_list.count()
        if not count:
            QMessageBox.information(self, "Daftar Kosong", "Tidak ada video untuk dihapus.")
            return
        if QMessageBox.question(
                self, "Bersihkan Daftar",
                f"Hapus SEMUA {count} video dari daftar siap?\n\n"
                "File video tetap aman di disk. Kamu bisa memuat ulang lewat "
                "«Muat dari media siap» atau render baru dari Auto Clip Studio."
                ) != QMessageBox.Yes:
            return
        self._delete_media_rows(
            [self.media_list.item(i) for i in range(count)])

    # ------------------------------------------------------------------ #
    # Poster-frame thumbnails (show *which* video is being scheduled)
    # ------------------------------------------------------------------ #
    def _request_list_thumbs(self, limit: int = 60) -> None:
        queued = 0
        for i in range(self.media_list.count()):
            data = self.media_list.item(i).data(Qt.UserRole)
            path = str(data.get("path") or "") if isinstance(data, dict) else ""
            if not path or self._load_cached_thumb(path):
                continue
            if queued >= limit:
                break
            self._queue_thumb(path)
            queued += 1

    def _queue_thumb(self, path: str) -> None:
        if path in self._thumb_pending or path in self._thumb_pixmaps:
            return
        try:
            st = Path(path).stat()
            key = hashlib.sha1(
                f"{Path(path).resolve()}|{st.st_mtime_ns}|{st.st_size}".encode()
            ).hexdigest()
        except Exception:
            return
        dest = _thumb_cache_dir() / f"{key}.jpg"
        if dest.exists():
            if self._on_thumb_ready(path, str(dest)):
                return
            try:
                dest.unlink()
            except Exception:
                pass
        self._thumb_pending.add(path)
        QThreadPool.globalInstance().start(_ThumbTask(path, dest, self._thumb_signals))

    def _on_thumb_ready(self, path: str, file: str) -> bool:
        self._thumb_pending.discard(path)
        pix = QPixmap(file)
        if pix.isNull():
            return False
        pix = pix.scaledToWidth(220, Qt.SmoothTransformation)
        # Keep memory bounded: oldest entries drop out first.
        while len(self._thumb_pixmaps) > 150:
            self._thumb_pixmaps.pop(next(iter(self._thumb_pixmaps)))
        self._thumb_pixmaps[path] = pix
        self._apply_thumb(path, pix)
        return True

    def _load_cached_thumb(self, path: str) -> bool:
        """True when the pixmap is already in memory (cache file checked by caller)."""
        return path in self._thumb_pixmaps

    def _apply_thumb(self, path: str, pix: QPixmap) -> None:
        icon = QIcon(pix)
        small = QIcon(pix.scaledToHeight(44, Qt.SmoothTransformation))
        for i in range(self.media_list.count()):
            item = self.media_list.item(i)
            data = item.data(Qt.UserRole)
            if isinstance(data, dict) and str(data.get("path") or "") == path:
                item.setIcon(icon)
        for r in range(self.queue_table.rowCount()):
            data = self.queue_table.item(r, 0).data(Qt.UserRole) \
                if self.queue_table.item(r, 0) else None
            if isinstance(data, dict) and str(data.get("media_path") or "") == path:
                cell = self.queue_table.item(r, 1)
                if cell is not None:
                    cell.setIcon(small)

    def _set_all_media_checks(self, checked: bool) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        self.media_list.blockSignals(True)
        for i in range(self.media_list.count()):
            self.media_list.item(i).setCheckState(state)
        self.media_list.blockSignals(False)
        self._update_media_count()

    def _update_media_count(self) -> None:
        selected = len(self._selected_media())
        total = self.media_list.count()
        self.media_count_lbl.setText(f"{selected} video dipilih dari {total} tersedia")
        rejected = 0
        mgr = self._manager()
        try:
            rejected = len(mgr.rejected_media(limit=200)) if mgr else 0
        except Exception:
            rejected = 0
        self.media_reject_lbl.setText(
            (f'<a href="rejected" style="color:{self.C["GOLD"]};">'
             f"⚠ {rejected} konten ditolak validasi — lihat alasan</a>")
            if rejected else "")
        self._update_pack_scope()
        self._refresh_pack_video_rows()
        self._sync_next()

    def _show_rejected_media(self, *_args) -> None:
        """Per-clip reject reasons, so a rejected render never leaves the user
        guessing which clip was dropped and why."""
        mgr = self._manager()
        try:
            rows = mgr.rejected_media(limit=100) if mgr else []
        except Exception:
            rows = []
        if not rows:
            QMessageBox.information(self, "Konten Ditolak", "Tidak ada konten ditolak.")
            return
        lines = []
        for row in rows:
            title = str(row.get("title") or Path(str(row.get("media_path") or "")).name)
            reason = str(row.get("reject_reason") or row.get("reason") or "tanpa alasan")
            when = str(row.get("created_at") or "")
            lines.append(f"• {title} — {reason}" + (f" ({when})" if when else ""))
        shown = "\n".join(lines[:40])
        if len(lines) > 40:
            shown += f"\n… dan {len(lines) - 40} lagi."
        QMessageBox.information(
            self, "Konten Ditolak Validasi",
            f"{len(lines)} konten ditolak masuk antrean publish:\n\n{shown}\n\n"
            "Render ulang atau perbaiki video di Auto Clip Studio / Content "
            "Clipper AI, lalu muat kembali daftar media.")

    def _on_pack_status(self, text: str) -> None:
        """Pack worker progress shown live (thread-safe via queued signal)."""
        self.pack_state.setText(str(text))

    def _update_pack_scope(self) -> None:
        """Spell out exactly which videos the AI reads and who gets a pack."""
        try:
            from chopster.automation.local_transcribe import pack_whisper_model
            model = pack_whisper_model(self.app.config)
        except Exception:
            model = "small"
        if getattr(self, "pack_scope_lbl", None) is None:
            return
        selected = self._selected_media()
        if not selected:
            self.pack_scope_lbl.setText(
                "Belum ada video tercentang. Centang minimal satu video di daftar: "
                "video dari komputer akan ditranskripsi lokal (Whisper), sedangkan "
                "clip dari Auto Clip Studio / Content Clipper AI memakai transkrip "
                "yang sudah ada.")
            return

        def has_ctx(m: dict) -> bool:
            raw = m.get("clip_context")
            if not isinstance(raw, dict) or not raw:
                return False
            try:
                from chopster.automation.context import ClipContext, context_status
                ctx = ClipContext.from_dict(raw)
                return ctx is not None and context_status(ctx)[0]
            except Exception:
                return False

        ready = [m for m in selected if has_ctx(m)]
        focus = ready[0] if ready else selected[0]
        total = len(selected)
        if len(ready) == total:
            self.pack_scope_lbl.setText(
                f"🤖 AI membaca {total} video terpilih (mulai dari "
                f"«{focus.get('title') or 'video'}») — tiap video dapat pack-nya "
                "sendiri, jadi judul tidak pernah tertukar.")
        else:
            self.pack_scope_lbl.setText(
                f"🤖 AI membaca «{focus.get('title') or 'video'}» • "
                f"{len(ready)}/{total} video punya transkrip sendiri — "
                f"{total - len(ready)} video lain ditranskripsi lokal lebih dulu "
                f"(Whisper {model}) supaya tiap video tetap dapat judul, deskripsi, "
                "caption, dan hashtag sendiri.")
        self._refresh_pack_video_rows()

    # ------------------------------------------------------------------ #
    # Staggered publishing (spec 5.6C)
    # ------------------------------------------------------------------ #
    def _stagger_config(self) -> dict:
        lo = int(self.stagger_from.time().hour())
        hi = int(self.stagger_to.time().hour())
        return {
            "start": datetime.now().replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M"),
            "interval_minutes": int(self.stagger_interval.value()),
            "active_hours": [lo, hi] if lo < hi else [],
        }

    def _preview_stagger(self) -> None:
        if not self.stagger_radio.isChecked():
            self.stagger_preview.setText("")
            return
        count = max(1, len(self._selected_media()))
        mgr = self._manager()
        if mgr is None:
            self.stagger_preview.setText("Automation manager belum tersedia.")
            return
        try:
            slots = mgr.preview_stagger(self._stagger_config(), count)
            self.stagger_preview.setText(f"{count} jadwal: {describe_slots(slots)}")
        except Exception as exc:
            self.stagger_preview.setText(str(getattr(exc, "user_message", None) or exc))

    # ------------------------------------------------------------------ #
    # Presets (saved configurations reused in Studio)
    # ------------------------------------------------------------------ #
    def _refresh_presets_combo(self) -> None:
        mgr = self._manager()
        try:
            recipes = mgr.list_recipes() if mgr else []
        except Exception:
            recipes = []
        current = str(self.preset_combo.currentData() or "")
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem("(tanpa preset)", "")
        for recipe in recipes:
            self.preset_combo.addItem(str(recipe.get("name") or "Preset"), str(recipe.get("id")))
        index = self.preset_combo.findData(current)
        self.preset_combo.setCurrentIndex(max(0, index))
        self.preset_combo.blockSignals(False)

    def _apply_preset(self) -> None:
        mgr = self._manager()
        rid = str(self.preset_combo.currentData() or "")
        if mgr is None or not rid:
            QMessageBox.information(
                self, "Preset",
                "Pilih preset di dropdown lalu klik «Terapkan preset».\n\n"
                "Belum punya preset? Centang «Simpan konfigurasi ini sebagai Preset» "
                "di langkah Review saat membuat automation.")
            return
        recipes = {str(r.get("id")): r for r in mgr.list_recipes()}
        recipe = recipes.get(rid) or {}
        cfg = recipe.get("config_obj") or {}
        applied: list[str] = []
        wanted = [str(p) for p in (cfg.get("platforms") or [])]
        if wanted:
            for platform, cb in self.platform_checks.items():
                cb.setChecked(platform in wanted)
            applied.append("Platform: " + ", ".join(
                PLATFORM_LABELS.get(p, p) for p in wanted))
        mode = str(cfg.get("mode") or "now")
        self.sched_radio.setChecked(mode == "schedule")
        self.now_radio.setChecked(mode != "schedule")
        applied.append("Waktu: " + ("terjadwal" if mode == "schedule"
                                    else "langsung (publish now)"))
        if cfg.get("time"):
            hh, mm = _split_time(str(cfg["time"]))
            self.time_edit.setTime(QTime(hh, mm))
            applied.append(f"Jam mulai {hh:02d}:{mm:02d}")
        if "review" in cfg:
            self.approval_review.setChecked(bool(cfg.get("review")))
            applied.append("Review sebelum publish: " + ("ya" if cfg.get("review") else "tidak"))
        privacy_key = str(cfg.get("privacy") or "")
        privacy_label = {"public": "Public", "unlisted": "Unlisted",
                         "private": "Private"}.get(privacy_key, "")
        if privacy_label:
            self.privacy_combo.setCurrentText(privacy_label)
            self.adv_check.setChecked(True)
            applied.append(f"Privasi: {privacy_label}")
        mfk = cfg.get("made_for_kids")
        if isinstance(mfk, bool):
            self.mfk_combo.setCurrentIndex(1 if mfk else 2)
            self.adv_check.setChecked(True)
            applied.append("YouTube audience: " + ("untuk anak-anak" if mfk
                                                   else "bukan untuk anak-anak"))
        self._refresh_review()
        QMessageBox.information(
            self, "Preset diterapkan",
            f"Preset «{recipe.get('name') or rid}» diterapkan ke wizard:\n\n• "
            + "\n• ".join(applied)
            + "\n\nPeriksa kembali langkah Review, lalu klik Start.")

    # ------------------------------------------------------------------ #
    # Auto-Pilot (spec 6)
    # ------------------------------------------------------------------ #
    def _build_autopilot(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)
        outer.setContentsMargins(6, 10, 6, 6)
        outer.setSpacing(8)
        bar = QHBoxLayout()
        bar.addWidget(self._section("AUTO-PILOT"))
        bar.addStretch()
        self.autopilot_state = QLabel("", w)
        self.autopilot_state.setObjectName("muted")
        bar.addWidget(self.autopilot_state)
        bar.addWidget(self._btn("Refresh", self._refresh_autopilot))
        bar.addWidget(self._btn("+ New Rule", self._new_rule, primary=True))
        outer.addLayout(bar)
        hint = self._lbl("Rule berlaku untuk klip BARU yang selesai setelah rule diaktifkan. "
                         "Untuk klip lama, pakai tombol Run now (backfill eksplisit).", w, muted=True)
        outer.addWidget(hint)
        scroll = QScrollArea(w)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        self.rules_box = QVBoxLayout(inner)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)
        return w

    def _refresh_autopilot(self) -> None:
        while self.rules_box.count():
            item = self.rules_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        mgr = self._manager()
        try:
            rules = mgr.list_rules() if mgr else []
        except Exception:
            rules = []
        enabled = sum(1 for r in rules if r["enabled"])
        jobs_today = 0
        for rule in rules:
            try:
                jobs_today += int((mgr.rule_status(rule["id"]) if mgr else {}).get("jobs_today") or 0)
            except Exception:
                pass
        automatic = True
        try:
            automatic = bool(mgr.autopilot.automatic_enabled()) if mgr else True
        except Exception:
            automatic = True
        state_text = f"{enabled}/{len(rules)} aktif - {jobs_today} job hari ini"
        if not automatic:
            state_text += " - Auto-Pilot global DIMATIKAN (Settings -> Automation)"
        self.autopilot_state.setText(state_text)
        if not rules:
            self.rules_box.addWidget(self._empty(
                "Belum ada rule Auto-Pilot",
                "Buat rule supaya klip baru dari Auto Clip Studio atau Content Clipper AI "
                "langsung masuk antrean publish otomatis.",
                "+ New Rule", self._new_rule))
            self.rules_box.addStretch()
            return
        for rule in rules:
            self.rules_box.addWidget(self._rule_card(rule))
        self.rules_box.addStretch()

    def _rule_card(self, rule: dict) -> QFrame:
        mgr = self._manager()
        card = self._card()
        status: dict = {}
        try:
            status = mgr.rule_status(rule["id"]) if mgr else {}
        except Exception:
            status = {}
        row = QHBoxLayout()
        name = QLabel(str(rule.get("name") or "Rule"), card)
        name.setStyleSheet(f"color: {self.C['TEXT']}; font-weight: 700; font-size: 14px;")
        row.addWidget(name)
        sources = ", ".join(SOURCE_LABELS.get(s, s) for s in (rule.get("sources_list") or []))
        plats = ", ".join(PLATFORM_LABELS.get(p, p)
                          for p in (rule.get("platforms_list") or [])) or "belum ada platform"
        row.addWidget(self._lbl(f"{sources}  ->  {plats}", card, muted=True))
        row.addStretch()
        enabled = bool(rule.get("enabled"))
        blocked = status.get("blocked") or []
        state = QLabel("Active" if enabled else ("Blocked" if blocked else "Disabled"), card)
        state.setObjectName("autStatus")
        color = self.C["GREEN"] if enabled else (self.C["GOLD"] if blocked else self.C["MUTED"])
        state.setStyleSheet(f"color: {color};")
        row.addWidget(state)
        row.addWidget(self._btn("Disable" if enabled else "Enable",
                                lambda _=False, rid=rule["id"], en=enabled: self._toggle_rule(rid, not en),
                                primary=not enabled))
        row.addWidget(self._btn("Preview",
                                lambda _=False, rid=rule["id"]: self._preview_rule(rid)))
        row.addWidget(self._btn("Run now", lambda _=False, rid=rule["id"]: self._run_rule(rid)))
        row.addWidget(self._btn("Edit", lambda _=False, rid=rule["id"]: self._edit_rule(rid)))
        row.addWidget(self._btn("Duplicate", lambda _=False, rid=rule["id"]: self._duplicate_rule(rid)))
        row.addWidget(self._btn("Delete", lambda _=False, rid=rule["id"]: self._delete_rule(rid)))
        card.layout().addLayout(row)
        detail = str(status.get("schedule_label") or "")
        if status.get("next_execution"):
            detail += f"  -  berikutnya {status['next_execution']}"
        if status.get("last_execution"):
            detail += f"  -  terakhir {status['last_execution']}"
        card.layout().addWidget(self._lbl(detail or "Jadwal belum dihitung.", card, muted=True))
        if blocked:
            warn = self._lbl("Belum bisa dijalankan: " + "; ".join(blocked), card, muted=True)
            warn.setStyleSheet(f"color: {self.C['GOLD']};")
            card.layout().addWidget(warn)
        elif status.get("last_error"):
            card.layout().addWidget(
                self._lbl(f"Catatan terakhir: {status['last_error']}", card, muted=True))
        return card

    def _new_rule(self) -> None:
        mgr = self._manager()
        if mgr is None:
            return
        if _RuleDialog(self, None, mgr).exec() == QDialog.Accepted:
            self._refresh_autopilot()

    def _edit_rule(self, rule_id: str) -> None:
        mgr = self._manager()
        if mgr is None:
            return
        rule = next((r for r in mgr.list_rules() if r["id"] == rule_id), None)
        if rule is None:
            return
        if _RuleDialog(self, rule, mgr).exec() == QDialog.Accepted:
            self._refresh_autopilot()

    def _toggle_rule(self, rule_id: str, enabled: bool) -> None:
        mgr = self._manager()
        if mgr is None:
            return
        try:
            result = mgr.set_rule_enabled(rule_id, enabled)
        except Exception as exc:
            QMessageBox.warning(self, "Auto-Pilot", str(getattr(exc, "user_message", None) or exc))
            return
        if not result.get("ok"):
            QMessageBox.information(
                self, "Auto-Pilot",
                "Rule belum bisa diaktifkan:\n\n- " +
                "\n- ".join(result.get("blocked") or ["alasan tidak diketahui"]))
        self._refresh_autopilot()

    def _run_rule(self, rule_id: str) -> None:
        mgr = self._manager()
        if mgr is None:
            return
        try:
            result = mgr.run_rule_now(rule_id)
        except Exception as exc:
            QMessageBox.warning(self, "Auto-Pilot", str(getattr(exc, "user_message", None) or exc))
            return
        QMessageBox.information(self, "Auto-Pilot",
                                str(result.get("message") or "Tidak ada job yang dibuat."))
        if result.get("ok"):
            self.tabs.setCurrentIndex(self.TAB_QUEUE)
            self.refresh_all()
        else:
            self._refresh_autopilot()

    def _preview_rule(self, rule_id: str) -> None:
        """Dry-run preview: show the upcoming slots this rule WOULD use without
        creating anything. Uses the same pure planner as the real engine
        (planner.plan_rule_slot), so what the user sees is exactly what runs."""
        mgr = self._manager()
        if mgr is None:
            return
        try:
            rule = next((r for r in mgr.list_rules() if r["id"] == rule_id), None)
        except Exception:
            rule = None
        if rule is None:
            return
        from chopster.automation.planner import PlanError, describe_slots, plan_rule_slot
        cfg = rule.get("cfg") or {}
        try:
            win = tuple(int(v) for v in (cfg.get("active_hours") or [])[:2]) or None
        except Exception:
            win = None
        days = cfg.get("active_days") or None
        status = {}
        try:
            status = mgr.rule_status(rule_id)
        except Exception:
            pass
        used_today = int(status.get("jobs_today") or 0)
        now = datetime.now()
        slots: list[datetime] = []
        cursor = now
        used = used_today
        try:
            for _ in range(5):
                slot = plan_rule_slot(
                    now=cursor, start_time=str(cfg.get("start_time") or "10:00"),
                    interval_minutes=int(cfg.get("interval_minutes") or 120),
                    used_today=used, per_day=int(cfg.get("per_day") or 1),
                    active_hours=win, active_days=days,
                    missed_policy=str(cfg.get("missed_policy") or "next_slot"))
                slots.append(slot)
                used += 1
                if used >= int(cfg.get("per_day") or 1):
                    used = 0
                cursor = slot + timedelta(seconds=1)
        except PlanError as exc:
            slots_ok = bool(slots)
            slots_txt = describe_slots(slots) if slots_ok else "—"
            QMessageBox.information(
                self, "Preview Rule (dry-run)",
                f"Rule «{rule.get('name') or 'Rule'}»\n\n"
                f"Slot berikutnya (simulasi, TIDAK membuat job): {slots_txt}\n\n"
                f"Tidak bisa dijadwalkan: {exc.user_message}")
            return
        except Exception as exc:
            QMessageBox.information(
                self, "Preview Rule (dry-run)",
                f"Rule «{rule.get('name') or 'Rule'}» tidak bisa di-preview: {str(exc)[:200]}")
            return
        sources = ", ".join(SOURCE_LABELS.get(s, s) for s in (rule.get("sources_list") or []))
        plats = ", ".join(PLATFORM_LABELS.get(p, p) for p in (rule.get("platforms_list") or []))
        blocked = status.get("blocked") or []
        lines = [
            f"Rule «{rule.get('name') or 'Rule'}»",
            f"Sumber: {sources}  →  Platform: {plats or '—'}",
            f"Jadwal: mulai {cfg.get('start_time') or '10:00'}, interval "
            f"{cfg.get('interval_minutes') or 120} menit, maks {cfg.get('per_day') or 1} "
            "job/hari, approval " + ("REVIEW" if str(cfg.get("approval")) == "review"
                                     else "otomatis"),
            "",
            "Slot berikutnya (simulasi — belum ada job yang dibuat):",
            describe_slots(slots, limit=5),
        ]
        if blocked:
            lines += ["", "Belum bisa dijalankan:", *[f"• {b}" for b in blocked]]
        else:
            lines += ["", "✓ Siap — saat diaktifkan, job baru akan memakai slot di atas "
                          "untuk klip baru yang selesai dari sumber di atas."]
        QMessageBox.information(self, "Preview Rule (dry-run)", "\n".join(lines))

    def _duplicate_rule(self, rule_id: str) -> None:
        mgr = self._manager()
        if mgr:
            mgr.duplicate_rule(rule_id)
            self._refresh_autopilot()

    def _delete_rule(self, rule_id: str) -> None:
        if QMessageBox.question(
                self, "Auto-Pilot",
                "Hapus rule ini? Job yang sudah terjadwal tetap diproses."
        ) != QMessageBox.Yes:
            return
        mgr = self._manager()
        if mgr:
            mgr.delete_rule(rule_id)
            self._refresh_autopilot()


def _split_time(value: str) -> tuple[int, int]:
    try:
        hh, mm = str(value).split(":")
        return max(0, min(23, int(hh))), max(0, min(59, int(mm)))
    except Exception:
        return 10, 0


def _source_label(source) -> str:
    key = str(source or "")
    if key == "autopilot":
        return "Auto-Pilot"
    if key == "studio":
        return "Studio"
    if key in SOURCE_LABELS:
        return SOURCE_LABELS[key]
    return key.replace("_", " ").title() or "Manual"


class _RuleDialog(QDialog):
    """Create or edit one Auto-Pilot rule. Every field is persisted on save."""

    def __init__(self, parent, rule: dict | None, mgr) -> None:
        super().__init__(parent)
        self.mgr = mgr
        self.rule = rule or {}
        self.setWindowTitle("Auto-Pilot Rule")
        self.setMinimumWidth(520)
        form = QFormLayout(self)

        self.name_edit = QLineEdit(self)
        self.name_edit.setText(str(self.rule.get("name") or "Daily Shorts Pipeline"))
        form.addRow("Nama rule", self.name_edit)

        srcs = set(self.rule.get("sources_list") or ["acs", "cca"])
        self.src_acs = QCheckBox(SOURCE_LABELS["acs"], self)
        self.src_cca = QCheckBox(SOURCE_LABELS["cca"], self)
        self.src_acs.setChecked("acs" in srcs)
        self.src_cca.setChecked("cca" in srcs)
        src_row = QHBoxLayout()
        src_row.addWidget(self.src_acs)
        src_row.addWidget(self.src_cca)
        self._add_layout_row(form, "Sumber konten", src_row)

        plats = set(self.rule.get("platforms_list") or [])
        self.platform_boxes: dict[str, QCheckBox] = {}
        p_row = QHBoxLayout()
        for platform in PLATFORMS:
            cb = QCheckBox(PLATFORM_LABELS[platform], self)
            cb.setChecked(platform in plats)
            self.platform_boxes[platform] = cb
            p_row.addWidget(cb)
        self._add_layout_row(form, "Platform tujuan", p_row)

        cfg = self.rule.get("cfg") or {}
        self.per_day = QSpinBox(self)
        self.per_day.setRange(1, 50)
        self.per_day.setValue(int(cfg.get("per_day") or 1))
        form.addRow("Posting per hari", self.per_day)

        self.interval = QSpinBox(self)
        self.interval.setRange(1, 1440)
        self.interval.setValue(int(cfg.get("interval_minutes") or 120))
        self.interval.setSuffix(" menit")
        form.addRow("Interval antarvideo", self.interval)

        self.start_time = QTimeEdit(self)
        hh, mm = _split_time(str(cfg.get("start_time") or "10:00"))
        self.start_time.setTime(QTime(hh, mm))
        form.addRow("Jam mulai tiap hari", self.start_time)

        self.approval = QComboBox(self)
        self.approval.addItems(["Auto Publish", "Review Before Publish"])
        self.approval.setCurrentIndex(1 if str(cfg.get("approval")) == "review" else 0)
        form.addRow("Mode approval", self.approval)

        hours = cfg.get("active_hours") or []
        self.win_from = QTimeEdit(self)
        self.win_to = QTimeEdit(self)
        self.win_from.setTime(QTime(int(hours[0]) if len(hours) > 1 else 10, 0))
        self.win_to.setTime(QTime(int(hours[1]) if len(hours) > 1 else 23, 0))
        w_row = QHBoxLayout()
        w_row.addWidget(self.win_from)
        w_row.addWidget(QLabel("sampai", self))
        w_row.addWidget(self.win_to)
        self._add_layout_row(form, "Jam operasional", w_row)

        days = set(cfg.get("active_days") or ALL_DAYS)
        self.day_boxes: dict[int, QCheckBox] = {}
        d_row = QHBoxLayout()
        for index, label in enumerate(WEEKDAY_LABELS):
            cb = QCheckBox(label, self)
            cb.setChecked(index in days)
            self.day_boxes[index] = cb
            d_row.addWidget(cb)
        self._add_layout_row(form, "Hari aktif", d_row)

        self.missed = QComboBox(self)
        self.missed.addItems(["Lewati ke slot berikutnya", "Publish secepatnya"])
        self.missed.setCurrentIndex(1 if str(cfg.get("missed_policy")) == "run_now" else 0)
        form.addRow("Jadwal terlewat", self.missed)

        self.privacy = QComboBox(self)
        self.privacy.addItems(["Default platform", "Public", "Unlisted", "Private"])
        privacy_key = str(cfg.get("privacy") or "")
        self.privacy.setCurrentIndex(
            {"public": 1, "unlisted": 2, "private": 3}.get(privacy_key, 0))
        form.addRow("Privasi", self.privacy)

        self.mfk = QComboBox(self)
        self.mfk.addItems(["YouTube dibuat untuk anak-anak: belum dipilih",
                           "Ya — untuk anak-anak",
                           "Tidak — bukan untuk anak-anak"])
        mfk_val = cfg.get("made_for_kids")
        self.mfk.setCurrentIndex(1 if mfk_val is True
                                 else 2 if mfk_val is False else 0)
        form.addRow("YouTube audience", self.mfk)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    @staticmethod
    def _add_layout_row(form: QFormLayout, label: str, layout) -> None:
        holder = QWidget()
        holder.setLayout(layout)
        form.addRow(label, holder)

    def _config(self) -> dict:
        lo = int(self.win_from.time().hour())
        hi = int(self.win_to.time().hour())
        return {
            "per_day": int(self.per_day.value()),
            "interval_minutes": int(self.interval.value()),
            "start_time": self.start_time.time().toString("HH:mm"),
            "approval": "review" if self.approval.currentIndex() == 1 else "auto",
            "missed_policy": "run_now" if self.missed.currentIndex() == 1 else "next_slot",
            "active_hours": [lo, hi] if lo < hi else [],
            "active_days": sorted(i for i, cb in self.day_boxes.items() if cb.isChecked()),
            "privacy": {"Public": "public", "Unlisted": "unlisted",
                        "Private": "private"}.get(self.privacy.currentText(), ""),
            "made_for_kids": (True if self.mfk.currentIndex() == 1
                              else False if self.mfk.currentIndex() == 2 else None),
        }

    def _save(self) -> None:
        sources = [s for s, cb in (("acs", self.src_acs), ("cca", self.src_cca)) if cb.isChecked()]
        platforms = [p for p, cb in self.platform_boxes.items() if cb.isChecked()]
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.information(self, "Auto-Pilot", "Nama rule wajib diisi.")
            return
        if not sources or not platforms:
            QMessageBox.information(self, "Auto-Pilot",
                                    "Pilih minimal satu sumber konten dan satu platform.")
            return
        if not self._config()["active_days"]:
            QMessageBox.information(self, "Auto-Pilot", "Pilih minimal satu hari aktif.")
            return
        try:
            self.mgr.save_rule(name=name, sources=sources, platforms=platforms,
                               config=self._config(), rule_id=str(self.rule.get("id") or ""))
        except Exception as exc:
            QMessageBox.warning(self, "Auto-Pilot", str(getattr(exc, "user_message", None) or exc))
            return
        self.accept()


def safe_json(value, default=None):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if value else (default if default is not None else {})
    except Exception:
        return default if default is not None else {}
