"""Entry point — Chopster (PySide6)."""
from __future__ import annotations

import sys
from pathlib import Path

# Make `import chopster` work BEFORE importing any chopster.* module.
# When launched as `python main.py` from inside the package directory, Python
# normally puts only `chopster/` on sys.path, not its parent.  The previous
# build imported crash_guard too early, which caused `ModuleNotFoundError`.
ROOT = Path(__file__).resolve().parent          # chopster/
PARENT = ROOT.parent                             # project folder
for p in (str(PARENT), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from chopster.app.winconsole import install as _install_hidden_console
from chopster.app.crash_guard import install_crash_guard
from chopster import __version__ as CHOPSTER_VERSION

# Hide child console windows (curl/ffmpeg/yt-dlp/npm/...) before the GUI runs,
# so external CLI tools never flash console windows in the windowed EXE.
_install_hidden_console()
install_crash_guard()

from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from chopster.app.application import Application
from chopster.app.paths import resource_path
from chopster.ui.main_window import MainWindow


def main() -> int:
    app_qt = QApplication(sys.argv)
    app_qt.setApplicationName("Chopster")
    app_qt.setApplicationVersion(CHOPSTER_VERSION)
    app_qt.setOrganizationName("Aris")

    # Window / taskbar / dialog icon (same logo used for the EXE icon).
    icon_path = resource_path("resources/icons/app.ico")
    if icon_path.exists():
        try:
            app_qt.setWindowIcon(QIcon(str(icon_path)))
        except Exception:
            pass

    core = Application()
    win = MainWindow(core)
    core.window = win  # for Browser Bridge -> UI forwarding
    win.show()
    # Start the browser bridge only after the main window is fully constructed.
    # A Qt main-thread pump drains URLs queued by the bridge worker thread.
    QTimer.singleShot(0, core.ensure_bridge)
    bridge_pump = QTimer(win)
    bridge_pump.setInterval(100)
    bridge_pump.timeout.connect(core.drain_external_urls)
    bridge_pump.start()
    code = app_qt.exec()
    try:
        core.shutdown()
    except Exception:
        pass
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
