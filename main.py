"""Entry point — Chopster (PySide6)."""
from __future__ import annotations

import sys
from pathlib import Path

# Resolve source imports from this entry point, not the extraction folder's
# spelling or the caller's working directory. Python package imports remain
# case-sensitive on Windows (e.g. Chopster/ is not the package chopster).
ROOT = Path(__file__).resolve().parent
PARENT = ROOT.parent
for p in (str(PARENT), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Frozen builds keep PyInstaller's import loader; only source launches need
# the explicit package alias. Register before execution for relative imports.
if not getattr(sys, "frozen", False) and "chopster" not in sys.modules:
    from importlib.util import module_from_spec, spec_from_file_location

    _package_spec = spec_from_file_location(
        "chopster", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    if _package_spec is None or _package_spec.loader is None:
        raise ImportError(f"Cannot load Chopster package from {ROOT}")
    _package = module_from_spec(_package_spec)
    sys.modules["chopster"] = _package
    try:
        _package_spec.loader.exec_module(_package)
    except BaseException:
        sys.modules.pop("chopster", None)
        raise

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
    app_qt.aboutToQuit.connect(core.shutdown)
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
