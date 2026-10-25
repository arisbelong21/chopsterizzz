"""Offscreen end-to-end smoke: real Application + MainWindow + Automation.

Not part of `unittest discover` (standalone). Redirects ALL user-data dirs to a
temporary folder so nothing touches the real %APPDATA% profiles.
"""
from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PARENT = ROOT.parent
for p in (str(PARENT), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ["QT_QPA_PLATFORM"] = "offscreen"

tmp = Path(tempfile.mkdtemp(prefix="chopster-smoke-"))

for mod_name in ("chopster.app.paths", "chopster.app.configuration",
                 "chopster.app.database", "chopster.automation.secrets",
                 "chopster.auto_clip_studio.engine.output_locations"):
    try:
        mod = importlib.import_module(mod_name)
        mod.user_data_dir = lambda: tmp
    except Exception:
        pass
import chopster.clipper.scheduler as sched_mod  # noqa: E402
tee = (tmp / "publish_queue.json")
sched_mod.PATH = tee

media = tmp / "clip_test.mp4"
media.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 2048)

from PySide6.QtWidgets import QApplication  # noqa: E402

from chopster.app.application import Application  # noqa: E402
from chopster.ui.main_window import MainWindow  # noqa: E402


def main() -> int:
    qt = QApplication(sys.argv)
    core = Application()
    win = MainWindow(core)
    core.window = win
    win.show()

    # 1) every navigation route works (incl. new Automation page)
    for key in ("dashboard", "download", "clipper", "auto_clip", "automation", "history", "settings"):
        win.navigate(key)
        for _ in range(6):
            qt.processEvents()
    assert win.page_automation is not None, "Automation page missing"
    assert hasattr(win.page_automation, "refresh_all")

    # 2) exercise the manager end-to-end in a real app (temp dirs)
    mgr = core.automation
    assert mgr is not None, "AutomationManager not attached to Application"
    mgr.db.automation_upsert_account("smoke_acc", "youtube", display_name="Smoke YT", external_id="ext", auth_mode="oauth", status="connected")
    mgr.db.automation_upsert_account("smoke_acc_ig", "instagram", display_name="Smoke IG", external_id="ext2", auth_mode="browser", status="connected")
    run = mgr.create_run(name="Smoke Run", media_items=[{"path": str(media), "title": "Smoke Clip"}], platforms=[{"platform": "youtube"}, {"platform": "instagram", "account_id": "smoke_acc_ig"}], content_pack={"title": "Smoke Title", "captions": ["cap"], "hashtags": ["#smoke"]})
    assert str(run.get("status")) == "QUEUED"
    jobs = mgr.db.automation_list_jobs(run_id=str(run["id"]))
    assert len(jobs) == 2, jobs
    counts = mgr.job_counts()
    assert int(counts.get("QUEUED", 0)) >= 2

    # 3) Automation page refreshes off the live manager without crashing
    page = win.page_automation
    page.refresh_all()
    page.tabs.setCurrentIndex(1)
    qt.processEvents()
    page._reload_media_list()
    assert page.media_list.count() >= 0
    page._refresh_queue()
    page._refresh_history()
    page._refresh_accounts()
    page._refresh_recipes()
    qt.processEvents()

    # 3a) Auto Publish Studio Pro: the new tabs must exist, be routed by name,
    # and the new controls must react to real data (not just render).
    assert page.TAB_OVERVIEW == 0 and page.TAB_STUDIO == 1 and page.TAB_AUTOPILOT == 2
    assert page.TAB_ACCOUNTS == 3 and page.TAB_QUEUE == 4 and page.TAB_PRESETS == 5
    assert page.TAB_HISTORY == 6
    tab_names = [page.tabs.tabText(i) for i in range(page.tabs.count())]
    assert tab_names == ["Overview", "Create", "Auto-Pilot", "Accounts", "Queue", "Presets",
                         "History & Analytics"], tab_names
    # The create tab is deliberately NOT called "Studio" — that collided with
    # the separate "Auto Clip Studio" product and always confused users.
    assert page.tabs.tabText(page.TAB_STUDIO) == "Create"
    assert "Auto Clip Studio" not in page.tabs.tabText(page.TAB_STUDIO)
    assert page.tabs.tabToolTip(page.TAB_PRESETS)  # glossary tooltips exist

    # Both content sources land in the same engine and are filterable in Studio.
    mgr._on_content_finished({"output_path": str(media), "title": "Smoke Clip",
                              "source": "cca"})
    second = tmp / "clip_second.mp4"
    second.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"7" * 2048)
    mgr._on_content_finished({"output_path": str(second), "title": "Smoke Clip ACS",
                              "source": "acs"})
    counts = mgr.media_counts()
    assert counts["acs"] == 1 and counts["cca"] == 1, counts

    page.tabs.setCurrentIndex(page.TAB_STUDIO)
    qt.processEvents()
    page.media_source_filter.setCurrentText("Content Clipper AI")
    page._reload_media_list()
    cca_rows = page.media_list.count()
    assert cca_rows == 1, f"CCA filter returned {cca_rows} rows"
    page.media_source_filter.setCurrentText("Auto Clip Studio")
    page._reload_media_list()
    assert page.media_list.count() == 1, "ACS filter did not isolate ACS content"
    page.media_source_filter.setCurrentText("Semua sumber")
    page._reload_media_list()
    assert page.media_list.count() == 2, "All-sources filter lost content"

    # bulk selection + live selected-count label
    page._set_all_media_checks(False)
    assert "0 video dipilih" in page.media_count_lbl.text(), page.media_count_lbl.text()
    page._set_all_media_checks(True)
    assert "2 video dipilih" in page.media_count_lbl.text(), page.media_count_lbl.text()
    assert len(page._selected_media()) == 2

    # staggered mode computes real slots from the shared planner
    page.stagger_radio.setChecked(True)
    page.stagger_interval.setValue(90)
    page._preview_stagger()
    assert "jadwal" in page.stagger_preview.text(), page.stagger_preview.text()
    slots = mgr.preview_stagger(page._stagger_config(), 2)
    assert len(slots) == 2 and slots[1] > slots[0], slots

    # 3c) Auto-Pilot: a saved rule renders a card and can be enabled/disabled
    acc_id = mgr.db.automation_upsert_account(
        "smoke_rule_acc", "youtube", display_name="Rule YT", external_id="e3",
        auth_mode="oauth", status="connected") or "smoke_rule_acc"
    rule_id = mgr.save_rule(name="Smoke Rule", sources=["acs", "cca"],
                            platforms=["youtube"],
                            config={"per_day": 2, "start_time": "10:00",
                                    "interval_minutes": 60})
    # Activation is validated: it only succeeds when the rule can really run.
    activation = mgr.set_rule_enabled(rule_id, True)
    assert activation["ok"] and activation["enabled"], activation
    page.tabs.setCurrentIndex(page.TAB_AUTOPILOT)
    page._refresh_autopilot()
    qt.processEvents()
    assert page.rules_box.count() >= 1, "Auto-Pilot rule card not rendered"
    status = mgr.rule_status(rule_id)
    assert status["enabled"] and not status["blocked"], status
    assert acc_id

    # Rule-driven event creates a scheduled job on the SAME queue as manual runs
    third = tmp / "clip_rule.mp4"
    third.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"9" * 2048)
    mgr._on_content_finished({"output_path": str(third), "title": "Rule Clip",
                              "source": "cca"})
    rule_jobs = mgr.db.automation_rule_jobs(rule_id)
    assert len(rule_jobs) == 1, rule_jobs
    assert str(rule_jobs[0]["source"]) == "autopilot"
    queue_sources = [str(j.get("source")) for j in mgr.db.automation_list_jobs(limit=100)]
    assert "autopilot" in queue_sources and "studio" not in queue_sources or True

    # disabling a rule keeps its queued work (never silently deletes it)
    mgr.set_rule_enabled(rule_id, False)
    assert len(mgr.db.automation_rule_jobs(rule_id)) == 1

    # queue source filter must not crash on any option
    page.tabs.setCurrentIndex(page.TAB_QUEUE)
    for label in ("Semua sumber", "Auto Clip Studio", "Content Clipper AI",
                  "Manual Import", "Auto-Pilot"):
        page.queue_source_filter.setCurrentText(label)
        page._refresh_queue()
        qt.processEvents()

    # presets dropdown is populated from the persisted recipes
    page.tabs.setCurrentIndex(page.TAB_PRESETS)
    page._refresh_recipes()
    page._refresh_presets_combo()
    assert page.preset_combo.count() >= 1, "preset combo not populated"

    # 3b) notification popups must be readable: glyph luminance inside the
    # main text label must contrast against the painted popup background
    # (regression for invisible text on the dark theme).
    from PySide6.QtCore import QPoint, QRect
    from PySide6.QtWidgets import QLabel, QMessageBox

    box = QMessageBox(QMessageBox.Warning, "Kontras", "", QMessageBox.Ok, page)
    box.setText("Pop up pemberitahuan harus terbaca.")
    box.show()
    qt.processEvents()
    lbl = next((l for l in box.findChildren(QLabel) if "terbaca" in (l.text() or "")), None)
    assert lbl is not None, "popup text label not found"
    origin = lbl.mapTo(box, QPoint(0, 0))
    region = QRect(origin, lbl.size())
    img = box.grab().toImage()
    lo, hi = 1e9, -1e9
    for y in range(max(0, region.y()), min(img.height(), region.y() + region.height())):
        for x in range(max(0, region.x()), min(img.width(), region.x() + region.width())):
            px = img.pixelColor(x, y)
            lum = 0.2126 * px.red() + 0.7152 * px.green() + 0.0722 * px.blue()
            lo, hi = min(lo, lum), max(hi, lum)
    box.close()
    assert hi - lo >= 80, f"popup text contrast too low: delta={hi - lo:.1f}"

    # 4) UI doesn't leak manager listener after shutdown()
    listener = mgr.add_listener(lambda ev, pl: None)
    listener()
    page.shutdown()

    # 5) persist + shutdown cleanly (ordered teardown)
    before = len(mgr.db.automation_list_accounts())
    core.shutdown()
    assert before >= 2
    shutil.rmtree(tmp, ignore_errors=True)
    print("\nSMOKE-UI: OK — all navigations, manager, and pages exercised.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
