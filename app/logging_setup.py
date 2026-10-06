"""Logging to file + stderr."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from chopster.app.paths import user_data_dir


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    log_dir = user_data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "chopster.log"

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    root = logging.getLogger("chopster")
    root.setLevel(level)
    root.handlers.clear()

    fh = logging.FileHandler(str(log_file), encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    return root


def get_logger(name: str = "chopster") -> logging.Logger:
    return logging.getLogger(name)
