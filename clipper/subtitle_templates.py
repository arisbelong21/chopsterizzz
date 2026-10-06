"""Subtitle presets and style helpers for the Content Clipper subtitle engine."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any
import json

from chopster.app.paths import user_data_dir


@dataclass
class SubtitleStyle:
    name: str
    font: str = "Arial"
    font_size: int = 30
    bold: bool = True
    italic: bool = False
    text_color: str = "&H00FFFFFF"       # ASS BGR
    outline_color: str = "&H00000000"
    outline: int = 2
    shadow: int = 1
    back_color: str = "&H80000000"
    alignment: int = 2                    # 2 = bottom-center
    margin_v: int = 52
    animation: str = "fade"
    subtitle_mode: str = "line"           # line | word | karaoke
    highlight_color: str = "&H0022BEFF"
    max_words_per_line: int = 8
    max_lines: int = 2


# More varied built-ins: different fonts, weights, colors and animation modes.
PRESETS: dict[str, SubtitleStyle] = {
    "Clean Podcast": SubtitleStyle("Clean Podcast", "Arial", 28, False, False, "&H00FFFFFF", "&H00000000", 2, 1, "&H00000000", 2, 48, "fade", "line", "&H0022BEFF"),
    "Bold Creator": SubtitleStyle("Bold Creator", "Arial Black", 34, True, False, "&H00FFFFFF", "&H00000000", 3, 2, "&H80000000", 2, 70, "pop", "line", "&H0022BEFF"),
    "Minimal White": SubtitleStyle("Minimal White", "Calibri", 26, False, False, "&H00FFFFFF", "&H00000000", 1, 0, "&H00000000", 2, 48, "none", "line", "&H00FFFFFF"),
    "Yellow Punch": SubtitleStyle("Yellow Punch", "Arial Black", 34, True, False, "&H0022FFFF", "&H00000000", 3, 2, "&H55000000", 2, 72, "pop", "line", "&H00FFFFFF"),
    "Cyan Creator": SubtitleStyle("Cyan Creator", "Segoe UI Semibold", 32, True, False, "&H00FFFF00", "&H00101010", 3, 2, "&H66000000", 2, 68, "slide_up", "line", "&H00FFFFFF"),
    "Red Alert": SubtitleStyle("Red Alert", "Impact", 35, True, False, "&H000000FF", "&H00000000", 4, 2, "&H77000000", 2, 74, "bounce", "line", "&H00FFFFFF"),
    "Green Signal": SubtitleStyle("Green Signal", "Trebuchet MS", 32, True, False, "&H0000FF00", "&H00141414", 3, 2, "&H66000000", 2, 70, "fade", "line", "&H00FFFFFF"),
    "Magenta Pop": SubtitleStyle("Magenta Pop", "Verdana", 32, True, False, "&H00FF00FF", "&H00000000", 3, 2, "&H66000000", 2, 70, "pop", "line", "&H00FFFFFF"),
    "Gold Highlight": SubtitleStyle("Gold Highlight", "Bahnschrift SemiBold", 33, True, False, "&H00FFFFFF", "&H00111111", 3, 2, "&H66000000", 2, 70, "keyword", "line", "&H0022BEFF"),
    "Karaoke Gold": SubtitleStyle("Karaoke Gold", "Arial", 31, True, False, "&H00FFFFFF", "&H00000000", 3, 2, "&H66000000", 2, 72, "karaoke", "karaoke", "&H0022BEFF"),
    "Karaoke Cyan": SubtitleStyle("Karaoke Cyan", "Montserrat SemiBold", 31, True, False, "&H00FFFFFF", "&H00111111", 3, 2, "&H77000000", 2, 72, "karaoke", "karaoke", "&H00FFFF00"),
    "Word Pop": SubtitleStyle("Word Pop", "Arial Black", 38, True, False, "&H00FFFFFF", "&H00000000", 4, 2, "&H99000000", 2, 78, "word_pop", "word", "&H0022BEFF"),
    "Word Punch Yellow": SubtitleStyle("Word Punch Yellow", "Impact", 39, True, False, "&H00FFFFFF", "&H00000000", 4, 2, "&H88000000", 2, 82, "word_pop", "word", "&H0022FFFF"),
    "Typewriter": SubtitleStyle("Typewriter", "Consolas", 28, True, False, "&H00FFFFFF", "&H00000000", 2, 1, "&H44000000", 2, 60, "typewriter", "line", "&H00FFFFFF"),
    "Slide Up": SubtitleStyle("Slide Up", "Poppins SemiBold", 32, True, False, "&H00FFFFFF", "&H00101010", 3, 2, "&H66000000", 2, 72, "slide_up", "line", "&H0022BEFF"),
    "Slide Left": SubtitleStyle("Slide Left", "Segoe UI Black", 31, True, False, "&H00FFFFFF", "&H00000000", 3, 2, "&H77000000", 2, 70, "slide_left", "line", "&H00FFFFFF"),
    "Slide Right": SubtitleStyle("Slide Right", "Segoe UI Black", 31, True, False, "&H00FFFFFF", "&H00000000", 3, 2, "&H77000000", 2, 70, "slide_right", "line", "&H00FFFFFF"),
    "Bounce": SubtitleStyle("Bounce", "Arial Black", 35, True, False, "&H00FFFFFF", "&H00000000", 4, 2, "&H77000000", 2, 74, "bounce", "line", "&H0022BEFF"),
    "Neon": SubtitleStyle("Neon", "Segoe UI Black", 35, True, False, "&H00FFFF00", "&H00100010", 4, 3, "&H99001010", 2, 74, "pop", "line", "&H00FF66FF"),
    "News Lower Third": SubtitleStyle("News Lower Third", "Arial", 25, True, False, "&H00FFFFFF", "&H00101010", 2, 1, "&HCC101010", 1, 34, "slide_up", "line", "&H0022BEFF"),
    "Documentary": SubtitleStyle("Documentary", "Georgia", 27, False, True, "&H00FFFFFF", "&H00000000", 2, 2, "&H66000000", 2, 52, "fade", "line", "&H0022BEFF"),
    "Gaming Energy": SubtitleStyle("Gaming Energy", "Arial Black", 36, True, False, "&H0000FFFF", "&H00000000", 5, 3, "&H99000000", 2, 82, "word_pop", "word", "&H00FF00FF"),
}


# Backward-compatible names used by older projects/tests.
LEGACY_ALIASES = {
    "Clean podcast": "Clean Podcast",
    "Bold creator": "Bold Creator",
    "Minimal white": "Minimal White",
    "Yellow keywords": "Gold Highlight",
    "Karaoke": "Karaoke Gold",
    "Social pop": "Magenta Pop",
    "Compact vertical": "News Lower Third",
    "High contrast": "Clean Podcast",
    "Neon Pop": "Neon",
    "Word Punch": "Word Pop",
    "Clean Pop": "Clean Podcast",
    "Punch Yellow": "Yellow Punch",
}


def list_presets() -> list[str]:
    return list(dict.fromkeys(list(PRESETS.keys()) + list(LEGACY_ALIASES.keys()) + list(load_custom_presets().keys())))


def get_preset(name: str) -> SubtitleStyle | None:
    if name in LEGACY_ALIASES:
        name = LEGACY_ALIASES[name]
    if name in PRESETS:
        # Return a copy so UI changes never mutate the global preset.
        return SubtitleStyle(**asdict(PRESETS[name]))
    custom = load_custom_presets()
    if name in custom:
        return custom[name]
    # Case-insensitive lookup for projects saved by older builds.
    low = str(name).strip().lower()
    for k, v in PRESETS.items():
        if k.lower() == low:
            return SubtitleStyle(**asdict(v))
    for k, v in custom.items():
        if k.lower() == low:
            return v
    return None


def custom_presets_path() -> Path:
    return user_data_dir() / "subtitle_presets.json"


def load_custom_presets() -> dict[str, SubtitleStyle]:
    p = custom_presets_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        out: dict[str, SubtitleStyle] = {}
        for k, v in data.items():
            # Backward-compatible defaults for presets saved by older builds.
            out[k] = SubtitleStyle(**v)
        return out
    except Exception:
        return {}


def save_custom_preset(style: SubtitleStyle) -> None:
    p = custom_presets_path()
    existing: dict[str, Any] = {}
    if p.exists():
        try:
            existing = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    existing[style.name] = asdict(style)
    p.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
