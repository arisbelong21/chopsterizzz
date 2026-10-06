"""Chopster package version."""
from pathlib import Path

try:
    __version__ = Path(__file__).with_name("VERSION.txt").read_text(encoding="utf-8").strip()
except OSError:
    __version__ = "0.0.0"
