"""Regression tests for the Automation UI complaints that kept coming back.

Runs offscreen: no window is ever shown, so it is safe in CI.
"""
from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, Signal
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton


def _qt() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv)


class _Cfg:
    def get(self, _key, default=None):
        return default

    def set(self, _key, _value):
        return True


class _StubApp:
    """Minimal Application stand-in: no manager, no task queue."""

    def __init__(self):
        self.config = _Cfg()
        self.automation = None
        self.tasks = None


def _page():
    from chopster.ui.pages.automation_page import AutomationPage

    qt = _qt()
    page = AutomationPage(_StubApp(), theme="dark")
    qt.processEvents()
    return qt, page


def _drain(qt: QApplication) -> None:
    # processEvents() does not flush DeferredDelete on its own.
    for _ in range(3):
        qt.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    qt.processEvents()


def _pump(qt: QApplication, ms: int) -> None:
    """Run the real event loop for a while — entrance animations use real
    timers, so nothing here may be faked."""
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        qt.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        time.sleep(0.005)


def _capture_unhandled(fn):
    """Run ``fn`` while collecting whatever Qt reports through sys.excepthook
    (the same channel app/crash_guard.py uses for its dialog)."""
    captured: list[str] = []
    previous = sys.excepthook
    sys.excepthook = lambda t, v, tb: captured.append(f"{t.__name__}: {v}")
    try:
        fn()
    finally:
        sys.excepthook = previous
    return captured


def test_ready_media_rows_do_not_stack_across_refreshes():
    """The Ready Media card used to leave old labels behind on every refresh,
    so each tick painted a new row on top of the previous ones."""
    qt, page = _page()
    card = page.media_card
    lay = card.layout()

    row = QHBoxLayout()
    row.addWidget(QLabel("CLIP LAMA SATU", card))
    row.addWidget(QLabel("[acs] • barusan", card))
    lay.addLayout(row)

    page._refresh_media_card()
    _drain(qt)

    texts = [lbl.text() for lbl in card.findChildren(
        QLabel, options=Qt.FindDirectChildrenOnly)]
    assert not any("CLIP LAMA SATU" in t for t in texts), texts
    # Nothing may be orphaned outside the layout either.
    for lbl in card.findChildren(QLabel, options=Qt.FindDirectChildrenOnly):
        assert lay.indexOf(lbl) != -1, f"orphan label left in the card: {lbl.text()!r}"

    # Refreshing repeatedly must not accumulate children.
    first = len(card.findChildren(QLabel, options=Qt.FindDirectChildrenOnly))
    for _ in range(3):
        page._refresh_media_card()
        _drain(qt)
    later = len(card.findChildren(QLabel, options=Qt.FindDirectChildrenOnly))
    assert later == first, f"{first} labels before, {later} after repeated refreshes"


def test_global_stylesheet_paints_custom_dialogs():
    """Custom QDialogs (Auto-Pilot rule editor) inherit the light QLabel color
    from the global sheet while their native surface stays light too — the
    popup must paint its own background and text."""
    from chopster.ui.theme import stylesheet

    sheet = stylesheet("dark")
    assert "QDialog" in sheet
    assert "QDialog QLabel" in sheet
    # Inputs used inside dialogs must not fall back to the native white either.
    assert "QTimeEdit" in sheet and "QDateEdit" in sheet


def test_rule_dialog_builds_offscreen():
    qt = _qt()
    from chopster.ui.pages.automation_page import _RuleDialog

    dialog = _RuleDialog(None, {}, None)
    try:
        # Every control the rule editor needs must exist (incl. audience row).
        assert dialog.mfk.count() == 3
        cfg = dialog._config()
        assert cfg["made_for_kids"] is None       # never answered -> no default
        dialog.mfk.setCurrentIndex(1)
        assert dialog._config()["made_for_kids"] is True
        dialog.mfk.setCurrentIndex(2)
        assert dialog._config()["made_for_kids"] is False
    finally:
        dialog.close()
        dialog.deleteLater()
        _drain(qt)


def test_youtube_audience_row_is_visible_by_default():
    # The made-for-kids question blocks every YouTube upload; hiding it behind
    # an unchecked Advanced box is what made the setting impossible to find.
    qt, page = _page()
    assert page.adv_check.isChecked()
    assert not page.adv_wrap.isHidden()
    assert page._made_for_kids_value() is None       # "belum dipilih"
    page.mfk_combo.setCurrentIndex(1)
    assert page._made_for_kids_value() is True
    page.mfk_combo.setCurrentIndex(2)
    assert page._made_for_kids_value() is False


def test_job_pack_exposes_audience_and_privacy_for_history():
    from chopster.ui.pages.automation_page import _job_pack

    raw = (
        '{"pack": {"title": "Judul", "description_long": "Deskripsi", '
        '"hashtags": ["#a"]}, "privacy": "unlisted", "made_for_kids": false, '
        '"pack_source": "single"}'
    )
    merged = _job_pack({"content_pack": raw})
    assert merged["title"] == "Judul"
    assert merged["description_long"] == "Deskripsi"
    assert merged["made_for_kids"] is False
    assert merged["privacy"] == "unlisted"
    # legacy / broken payloads must not raise
    assert _job_pack({}) == {}
    assert _job_pack({"content_pack": "not json"}) == {}


# --------------------------------------------------------------------- #
# Cancel controls (complaint: "Automation Queue tidak ada tombol batal")
# --------------------------------------------------------------------- #
class _FakeJobDB:
    def __init__(self, jobs):
        self.jobs = list(jobs)

    def automation_list_jobs(self, statuses=None, run_id="", limit=500):
        rows = self.jobs if run_id == "" else [
            j for j in self.jobs if str(j.get("run_id")) == run_id]
        if statuses:
            rows = [j for j in rows if str(j.get("status")) in statuses]
        return rows[:limit]

    def automation_list_accounts(self):
        return []


class _FakeManager:
    def __init__(self, jobs):
        self.db = _FakeJobDB(jobs)
        self.cancelled = []
        self.deleted = []
        self.created = None

    def cancel_job(self, job_id):
        self.cancelled.append(str(job_id))

    def delete_job(self, job_id):
        self.deleted.append(str(job_id))
        self.db.jobs = [j for j in self.db.jobs
                        if str(j.get("id")) != str(job_id)]
        return True

    def account_statuses(self):
        return {"youtube": {"state": "connected"}}

    def list_recipes(self):
        return []

    def rejected_media(self, limit=50):
        return []

    def job_counts(self):
        return {}

    def create_run(self, **kwargs):
        self.created = kwargs
        return {"id": "run_1", "name": kwargs.get("name"), "status": "QUEUED"}


def _row_buttons(page, row: int) -> list[QPushButton]:
    """Buttons inside the per-row Aksi cell (wrapped in a container widget)."""
    cell = page.queue_table.cellWidget(row, 8)
    if isinstance(cell, QPushButton):
        return [cell]            # legacy single-button cell
    if cell is None:
        return []
    return cell.findChildren(QPushButton)


def _row_button(page, row: int, needle: str) -> QPushButton | None:
    for btn in _row_buttons(page, row):
        if needle in btn.text():
            return btn
    return None


def _job(job_id, status):
    return {"id": job_id, "status": status, "media_path": f"/clips/{job_id}.mp4",
            "media_title": job_id, "platform": "youtube", "account_id": "",
            "scheduled_at": "", "progress": 0, "attempt": 0}


def test_queue_toolbar_exposes_cancel_and_arms_on_selection():
    """The Queue must advertise an obvious Batalkan control, and the
    selection-driven buttons must not sit there dead-looking when no row is
    selected (that is what made users conclude there was no button at all)."""
    qt, page = _page()
    mgr = _FakeManager([_job("j1", "QUEUED"), _job("j2", "PUBLISHED")])
    page.app.automation = mgr
    page._refresh_queue()

    assert "Batalkan" in page.q_cancel_btn.text()
    assert "Batalkan Antrean" in page.q_cancel_all_btn.text()

    # Nothing selected -> the row actions are disarmed.
    assert not page.q_cancel_btn.isEnabled()
    assert not page.q_pause_btn.isEnabled()
    page.queue_table.selectRow(0)
    qt.processEvents()
    assert page.q_cancel_btn.isEnabled()
    assert page.q_pause_btn.isEnabled()


def test_queue_rows_carry_a_working_cancel_button(monkeypatch):
    """Per-row ✕ Batal cancels without needing the toolbar, and terminal jobs
    get a disabled button so the state is self-explanatory."""
    from chopster.ui.pages import automation_page as ap

    monkeypatch.setattr(ap.QMessageBox, "question",
                        staticmethod(lambda *a, **k: ap.QMessageBox.Yes))
    qt, page = _page()
    mgr = _FakeManager([_job("j1", "QUEUED"), _job("j2", "SCHEDULED"),
                        _job("j3", "PUBLISHED")])
    page.app.automation = mgr
    page._refresh_queue()
    qt.processEvents()

    btn_queued = _row_button(page, 0, "Batal")
    btn_scheduled = _row_button(page, 1, "Batal")
    btn_published = _row_button(page, 2, "Batal")
    assert btn_queued is not None and btn_queued.isEnabled()
    assert btn_scheduled is not None and btn_scheduled.isEnabled()
    assert btn_published is not None and not btn_published.isEnabled()

    btn_queued.click()
    qt.processEvents()
    assert mgr.cancelled == ["j1"]

    # Bulk cancel only touches still-cancellable jobs.
    page._cancel_all_queued()
    qt.processEvents()
    assert "j2" in mgr.cancelled
    assert "j3" not in mgr.cancelled


def test_queue_rows_offer_delete_for_finished_jobs(monkeypatch):
    """Complaint: a failed/cancelled row could only be cancelled, never
    removed. Finished rows now expose 🗑 Hapus; running ones must not."""
    from chopster.ui.pages import automation_page as ap

    monkeypatch.setattr(ap.QMessageBox, "question",
                        staticmethod(lambda *a, **k: ap.QMessageBox.Yes))
    # The bulk cleanup reports its result with a modal box — never let a test
    # block on it.
    monkeypatch.setattr(ap.QMessageBox, "information",
                        staticmethod(lambda *a, **k: None))
    qt, page = _page()
    mgr = _FakeManager([_job("j1", "UPLOADING"), _job("j2", "FAILED"),
                        _job("j3", "CANCELLED"), _job("j4", "PUBLISHED")])
    page.app.automation = mgr
    page._refresh_queue()
    qt.processEvents()

    assert not _row_button(page, 0, "Hapus").isEnabled()      # still uploading
    assert _row_button(page, 1, "Hapus").isEnabled()          # failed
    assert _row_button(page, 2, "Hapus").isEnabled()          # cancelled
    assert _row_button(page, 3, "Hapus").isEnabled()          # published

    _row_button(page, 1, "Hapus").click()
    qt.processEvents()
    assert mgr.deleted == ["j2"]
    assert page.queue_table.rowCount() == 3

    # Bulk cleanup removes Failed + Cancelled only.
    assert "Hapus" in page.q_delete_finished_btn.text()
    page._delete_finished_bulk()
    qt.processEvents()
    assert set(mgr.deleted) == {"j2", "j3"}
    assert page.queue_table.rowCount() == 2


class _FakeMediaDB:
    def __init__(self, rows):
        self.rows = list(rows)
        self.deleted = []

    def automation_list_media(self, status="ready", limit=200, source=""):
        rows = [r for r in self.rows if not source or str(r.get("source")) == source]
        return rows[:limit]

    def automation_delete_media(self, media_id):
        self.deleted.append(str(media_id))
        self.rows = [r for r in self.rows if str(r.get("id")) != str(media_id)]


class _FakeMediaManager:
    def __init__(self, rows):
        self.db = _FakeMediaDB(rows)

    def list_media(self, status="ready", source=""):
        return self.db.automation_list_media(status=status, source=source)

    def rejected_media(self, limit=50):
        return []


def test_media_list_rows_can_be_removed_and_cleared(monkeypatch, tmp_path):
    """Complaint: the Studio list only had Select/Deselect, so its history
    could never be deleted."""
    from chopster.ui.pages import automation_page as ap

    monkeypatch.setattr(ap.QMessageBox, "question",
                        staticmethod(lambda *a, **k: ap.QMessageBox.Yes))
    monkeypatch.setattr(ap.QMessageBox, "information",
                        staticmethod(lambda *a, **k: None))
    qt, page = _page()
    first = tmp_path / "satu.mp4"
    second = tmp_path / "dua.mp4"
    for f in (first, second):
        f.write_bytes(b"\x00" * 32)
    mgr = _FakeMediaManager([
        {"id": "m1", "media_path": str(first), "title": "Satu", "source": "acs"},
        {"id": "m2", "media_path": str(second), "title": "Dua", "source": "acs"},
    ])
    page.app.automation = mgr
    page._reload_media_list()
    assert page.media_list.count() == 2

    page.media_list.item(1).setCheckState(Qt.Unchecked)
    page._remove_selected_media()
    qt.processEvents()
    assert page.media_list.count() == 1
    assert mgr.db.deleted == ["m1"]           # registry entry released too

    page._clear_media_list()
    qt.processEvents()
    assert page.media_list.count() == 0
    # A reload must not resurrect deleted rows.
    page._reload_media_list()
    assert page.media_list.count() == 0


def test_source_hint_explains_where_videos_come_from():
    """Complaint: nothing told the user how Auto Clip Studio clips reach the
    Studio tab (scan/render first, no download needed)."""
    qt, page = _page()
    page.media_source_filter.setCurrentText("Auto Clip Studio")
    qt.processEvents()
    hint = page.media_source_hint.text().lower()
    assert "auto clip studio" in hint
    assert "scan" in hint or "render" in hint
    assert "download" in hint
    page.media_source_filter.setCurrentText("Manual Import")
    qt.processEvents()
    assert "komputer" in page.media_source_hint.text().lower()


def _ctx_dict(asset_id: str, title: str, media_path) -> dict:
    return {
        "version": 1, "source": "acs", "source_asset_id": asset_id,
        "project_id": "proj1", "media_path": str(media_path), "title": title,
        "language": "id", "clip_start": 0.0, "clip_end": 10.0,
        "evidence": "transcript analisis",
        "segments": [
            {"start": 0.0, "end": 5.0,
             "text": f"{title} menjelaskan cara membuat sesuatu yang menarik"},
            {"start": 5.0, "end": 10.0,
             "text": "bagian kedua berisi tips dan trik tambahan untuk pemula"},
        ],
    }


def test_pack_scope_reads_only_checked_videos(tmp_path):
    """Complaint: Generate looked like it ignored the checklist. The scope
    line and targets now follow the checked rows exactly."""
    qt, page = _page()
    page._add_media_row({"path": str(tmp_path / "satu.mp4"), "title": "Satu",
                         "source": "acs",
                         "clip_context": _ctx_dict("asset-0", "Satu",
                                                   tmp_path / "satu.mp4")})
    page._add_media_row({"path": str(tmp_path / "dua.mp4"), "title": "Dua",
                         "source": "acs",
                         "clip_context": _ctx_dict("asset-1", "Dua",
                                                   tmp_path / "dua.mp4")})
    qt.processEvents()
    assert [t["title"] for t in page._pack_targets()] == ["Satu", "Dua"]
    assert "2 video terpilih" in page.pack_scope_lbl.text()

    page.media_list.item(0).setCheckState(Qt.Unchecked)
    qt.processEvents()
    assert [t["title"] for t in page._pack_targets()] == ["Dua"]
    assert "Dua" in page.pack_scope_lbl.text()

    # A checked row without its own transcript is announced, never silently
    # described by another video's context.
    page.media_list.item(0).setCheckState(Qt.Checked)
    page.media_list.item(1).setData(Qt.UserRole, {"path": str(tmp_path / "dua.mp4"),
                                                 "title": "Dua", "source": "acs"})
    page._update_media_count()
    qt.processEvents()
    assert "1/2" in page.pack_scope_lbl.text()


def test_start_automation_attaches_each_videos_own_pack(monkeypatch, tmp_path):
    """A pack must never be stamped onto a different video's job: every
    checked clip carries its own content into create_run."""
    from chopster.ui.pages import automation_page as ap

    monkeypatch.setattr(ap.QMessageBox, "information",
                        staticmethod(lambda *a, **k: None))
    qt, page = _page()
    mgr = _FakeManager([])
    page.app.automation = mgr
    page.refresh_all = lambda: None
    satu = tmp_path / "satu.mp4"
    dua = tmp_path / "dua.mp4"
    for i, (path, title) in enumerate(((satu, "Satu"), (dua, "Dua"))):
        page._add_media_row({"path": str(path), "title": title, "source": "acs",
                             "clip_context": _ctx_dict(f"asset-{i}", title, path)})
    page.platform_checks["youtube"].setChecked(True)
    page._packs = {
        str(satu): {"title": "Judul Satu", "titles": ["Judul Satu"]},
        str(dua): {"title": "Judul Dua", "titles": ["Judul Dua"]},
    }
    page._pack = {"title": "Judul Satu", "titles": ["Judul Satu"],
                  "_media_path": str(satu), "_clip_binding": "asset-0"}
    page._start_automation()
    qt.processEvents()

    assert mgr.created is not None
    items = mgr.created["media_items"]
    assert [i["content"]["title"] for i in items] == ["Judul Satu", "Judul Dua"]
    # Internal bookkeeping markers never leak into the persisted pack.
    assert not any(str(k).startswith("_") for k in items[1]["content"])


def test_presets_tab_builds_self_explaining_cards():
    """Complaint: the Presets tab was four bare rows above a huge empty area,
    with no hint about what each preset does."""
    qt, page = _page()
    mgr = _FakeManager([])
    mgr.list_recipes = lambda: [
        {"id": "rcp1", "name": "Multi-Platform Daily", "enabled": 1,
         "config_obj": {"platforms": ["youtube", "tiktok"], "mode": "schedule",
                        "time": "19:00", "repeat": "daily", "max_per_run": 1}},
        {"id": "rcp2", "name": "Manual Review Before Publish", "enabled": 0,
         "config_obj": {"platforms": [], "mode": "now", "review": True}},
    ]
    page.app.automation = mgr
    page._refresh_recipes()
    qt.processEvents()

    buttons = [b.text() for b in page.tab_recipes.findChildren(QPushButton)]
    assert any("Jalankan sekarang" in t for t in buttons)
    assert any("Nonaktifkan" in t for t in buttons)     # rcp1 is active
    assert any("Aktifkan" in t for t in buttons)        # rcp2 is paused
    labels = [lbl.text() for lbl in page.tab_recipes.findChildren(QLabel)]
    assert any("Fungsi: publish otomatis ke YouTube, TikTok" in t for t in labels)
    assert any("CARA PAKAI" in t for t in labels)


def _seven_recipes() -> list[dict]:
    return [
        {"id": f"rcp{i}", "name": f"Preset {i}", "enabled": 1,
         "config_obj": {"platforms": ["youtube"], "mode": "schedule",
                        "time": "19:00", "repeat": "daily", "max_per_run": 1}}
        for i in range(7)
    ]


def test_fade_in_delayed_start_is_bound_to_its_widget():
    """crash.log: ``RuntimeError: ... QPropertyAnimation already deleted`` at
    the delayed start in ``_fade_in``. The card must fade while it lives, and a
    card destroyed before its delay elapses must never start an animation that
    died with it."""
    qt, page = _page()

    # 1. A live card really animates and cleans up after itself.
    card = page._card()
    page._fade_in(card, delay_ms=40)
    _pump(qt, 500)
    assert card.graphicsEffect() is None, "entrance effect was never cleared"

    # 2. A card deleted while its delayed start is still pending stays silent.
    doomed = page._card()
    errors = _capture_unhandled(lambda: (page._fade_in(doomed, delay_ms=40),
                                         doomed.setParent(None),
                                         doomed.deleteLater(),
                                         _pump(qt, 500)))
    assert errors == [], errors


def test_preset_grid_rebuild_never_touches_dead_animations():
    """The preset grid is rebuilt by ``currentChanged`` (refresh_current) and by
    ``showEvent`` (refresh_all) in the same event-loop turn; six identical
    crashes in one burst came from the pending entrance animations of the
    discarded cards."""
    qt, page = _page()
    mgr = _FakeManager([])
    mgr.list_recipes = _seven_recipes
    page.app.automation = mgr
    page.isVisible = lambda: True          # the offscreen page is never shown

    def run() -> None:
        page.tabs.setCurrentIndex(page.TAB_PRESETS)
        page.refresh_current()             # currentChanged handler
        page.refresh_all()                 # second rebuild, same turn
        page.deleteLater()                 # …and the cards go away mid-fade
        _pump(qt, 700)                     # every delayed start has fired by now

    assert _capture_unhandled(run) == []


class _Signals(QObject):
    result = Signal(object, object)
    failed = Signal(object, object)


class _Worker:
    def __init__(self):
        self.signals = _Signals()


class _Tasks:
    """Stand-in for app.tasks: records submit/cancel, never runs the worker."""

    def __init__(self):
        self.submitted = []
        self.cancelled = []
        self.worker = _Worker()

    def submit(self, task_id, title, worker):
        self.submitted.append(task_id)
        return self.worker

    def cancel(self, task_id):
        self.cancelled.append(task_id)


def test_generate_pack_submits_and_can_be_cancelled():
    """Regression: the submit block used to live inside _mark_pack_stale, so
    Generate did nothing and the stale-check raised NameError."""
    import types

    qt, page = _page()
    tasks = _Tasks()
    page.app.tasks = tasks
    page.app.window = types.SimpleNamespace(
        page_clipper=types.SimpleNamespace(transcript=object(),
                                           _ai_config=lambda: {}))

    page._generate_pack()
    qt.processEvents()
    assert tasks.submitted == ["automation:pack"]
    assert not page.pack_gen_btn.isEnabled()      # Generate locked while running
    assert page.pack_cancel_btn.isEnabled()       # Batalkan armed

    page._cancel_pack()
    assert tasks.cancelled == ["automation:pack"]

    # The task manager reports a cancel as failed("Dibatalkan").
    tasks.worker.signals.failed.emit("automation:pack", "Dibatalkan")
    for _ in range(5):
        qt.processEvents()
    assert "dibatalkan" in page.pack_state.text().lower()
    assert page.pack_gen_btn.isEnabled()
    assert not page.pack_cancel_btn.isEnabled()


def test_mark_pack_stale_clears_without_touching_the_task_queue():
    qt, page = _page()
    tasks = _Tasks()
    page.app.tasks = tasks
    page._pack = {"_clip_binding": "asset-1", "title": "T"}
    page._selected_context = lambda: None        # binding no longer matches

    page._mark_pack_stale()                       # must not raise
    qt.processEvents()
    assert page._pack == {}
    assert "Pilihan berubah" in page.pack_state.text()
    assert tasks.submitted == [] and tasks.cancelled == []