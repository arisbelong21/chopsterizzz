"""Crash diagnostics and safe top-level exception handling for Chopster."""
from __future__ import annotations

import faulthandler
import logging
import sys
import threading
import traceback
from pathlib import Path
from typing import Any


_INSTALLED = False
_LOG_PATH: Path | None = None


def install_crash_guard() -> Path:
    global _INSTALLED, _LOG_PATH
    if _INSTALLED and _LOG_PATH is not None:
        return _LOG_PATH
    try:
        from chopster.app.paths import user_data_dir
        log_dir = user_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        log_dir = Path.cwd() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
    _LOG_PATH = log_dir / "crash.log"
    try:
        fh = open(_LOG_PATH, "a", encoding="utf-8", buffering=1)
        fh.write("\n===== Chopster crash guard started =====\n")
        faulthandler.enable(fh, all_threads=True)
    except Exception:
        fh = None

    def report(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            logger = logging.getLogger("chopster.crash")
            logger.error("Unhandled exception:\n%s", text)
        except Exception:
            pass
        if _LOG_PATH:
            try:
                with _LOG_PATH.open("a", encoding="utf-8") as out:
                    out.write("\n[UNHANDLED]\n")
                    out.write(text)
            except Exception:
                pass
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox
            app = QApplication.instance()
            if app is not None:
                msg = QMessageBox()
                msg.setIcon(QMessageBox.Critical)
                msg.setWindowTitle("Chopster — terjadi error")
                msg.setText("Chopster mengalami error dan tidak melanjutkan operasi ini.")
                msg.setInformativeText(f"Detail tersimpan di:\n{_LOG_PATH}")
                msg.setDetailedText(text[-10000:])
                msg.exec()
        except Exception:
            pass

    def thread_report(args: threading.ExceptHookArgs) -> None:
        report(args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = report
    try:
        threading.excepthook = thread_report
    except Exception:
        pass
    _INSTALLED = True
    return _LOG_PATH
