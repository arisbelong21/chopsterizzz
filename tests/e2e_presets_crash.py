"""End-to-end check for the Automation/Presets crash reported in crash.log.

Real Application + MainWindow + AutomationManager, offscreen, with every
user-data dir redirected to a temp folder. The real crash guard is installed,
so the run FAILS if a single exception reaches it — i.e. if the modal
"Chopster mengalami error…" dialog would have appeared.

Run:  python tests\\_e2e_presets_crash.py   (exit 0 = clean)
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PARENT = ROOT.parent
for p in (str(PARENT), str(ROOT)):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

os.environ["QT_QPA_PLATFORM"] = "offscreen"

TMP = Path(tempfile.mkdtemp(prefix="chopster-e2e-"))
for mod_name in ("chopster.app.paths", "chopster.app.configuration",
                 "chopster.app.database", "chopster.automation.secrets",
                 "chopster.auto_clip_studio.engine.output_locations"):
    try:
        importlib.import_module(mod_name).user_data_dir = lambda: TMP
    except Exception:
        pass
try:
    import chopster.clipper.scheduler as sched_mod
    sched_mod.PATH = TMP / "publish_queue.json"
except Exception:
    pass

from PySide6.QtWidgets import (QApplication, QGraphicsOpacityEffect,  # noqa: E402
                               QMessageBox, QWidget)

from chopster.app import crash_guard  # noqa: E402

# An unattended run must never block on a modal dialog — the crash guard's
# modal is exactly what the user experiences as "Chopster berhenti".
QMessageBox.exec = lambda self: int(QMessageBox.Ok)  # type: ignore[method-assign]

LOG = crash_guard.install_crash_guard()

from chopster.app.application import Application  # noqa: E402
from chopster.ui.main_window import MainWindow  # noqa: E402
from chopster.ui.pages import automation_page as ap  # noqa: E402


def pump(qt: QApplication, ms: int) -> None:
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        qt.processEvents()
        time.sleep(0.005)


def fading_cards(page) -> int:
    """Cards whose entrance animation is still pending/running."""
    return sum(1 for w in page.tab_recipes.findChildren(QWidget)
               if isinstance(w.graphicsEffect(), QGraphicsOpacityEffect))


def main() -> int:
    qt = QApplication(sys.argv)
    core = Application()
    win = MainWindow(core)
    core.window = win
    win.show()
    win.navigate("automation")
    pump(qt, 150)

    page = win.page_automation
    mgr = core.automation
    assert mgr is not None

    # Never block on a confirmation dialog while testing.
    ap.QMessageBox.question = staticmethod(lambda *a, **k: ap.QMessageBox.Yes)
    ap.QMessageBox.information = staticmethod(lambda *a, **k: None)

    for i in range(6):
        mgr.save_recipe(f"E2E Preset {i}",
                        {"platforms": ["youtube"], "mode": "schedule",
                         "time": "19:00", "repeat": "daily", "max_per_run": 1})
    recipes = mgr.list_recipes()
    assert len(recipes) >= 6, f"only {len(recipes)} presets seeded"

    before = LOG.read_text(encoding="utf-8", errors="replace")
    if not os.environ.get("E2E_ALLOW_DIRTY"):
        # Opening the Automation page must be crash-free on its own; the flag
        # exists only to *measure* a broken build.
        assert "[UNHANDLED]" not in before, \
            "crash guard already fired before the presets flow started"

    page.tabs.setCurrentIndex(page.TAB_PRESETS)      # currentChanged -> recipes
    pump(qt, 40)
    pending = fading_cards(page)
    assert pending >= 5, f"entrance animations not pending ({pending} cards)"
    print(f"presets={len(recipes)}  cards fading in right now={pending}")

    # 1) the exact turn that produced six crashes in crash.log: the grid is
    #    rebuilt by currentChanged (refresh_current) and by showEvent
    #    (refresh_all) while the entrance animations are still pending.
    for _ in range(3):
        page.refresh_current()
        page.refresh_all()
        qt.processEvents()

    # 2) hide/show stops and restarts the tick, each show rebuilds the grid
    for _ in range(3):
        page.hide()
        page.show()
        qt.processEvents()

    # 3) deleting a preset rebuilds the grid mid-animation
    for recipe in recipes[:3]:
        page._delete_recipe(recipe["id"])
        qt.processEvents()

    pump(qt, 1500)                                    # every delayed start fires
    left = fading_cards(page)
    assert left == 0, f"{left} cards stuck with an unfinished entrance effect"

    after = LOG.read_text(encoding="utf-8", errors="replace")
    new_text = after[len(before):]
    print(f"new crash.log bytes: {len(new_text.encode('utf-8'))}"
          f"  unhandled blocks: {new_text.count('[UNHANDLED]')}")
    if "[UNHANDLED]" in new_text:
        print(new_text[-4000:])
        print("E2E-PRESETS: FAIL — crash guard was triggered")
        return 1
    print("E2E-PRESETS: OK — no exception reached the crash guard")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())