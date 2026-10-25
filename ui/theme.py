"""Chopster premium theme palettes + global stylesheet.

Four themes ship with the app, each with its own visual language (not just a
palette swap — gradients, bevels, rounding and glow differ per theme):

- "dark"    Klasik Dark — GitHub Dark+ / VS Code Dark+ charcoal + amber gold
- "modern"  Modern — Linear / Arc / Raycast style: crisp light glass, indigo
- "3d"     3D Neon — cyberpunk depth: deep navy, cyan→violet glow, bevels
- "casual"  Casual — Notion-warm: soft cream, rounded, friendly orange

Every palette defines the full key set; "dark" is the fallback for any missing
key so a theme can never raise KeyError while switching.
"""
from __future__ import annotations

THEMES: dict[str, dict[str, str]] = {
    "dark": dict(
        BG="#101115",
        PANEL="#191B20",
        PANEL2="#22252B",
        INPUT="#15171C",
        BORDER="#393D45",
        TEXT="#F6F4EE",
        MUTED="#C3C5C9",
        GOLD="#E3B653",
        GOLD2="#F0C96E",
        ON_GOLD="#1A1710",
        GREEN="#65CDA8",
        RED="#F27C83",
        BLUE="#8ABEFF",
        FOOT="#9DA2AA",
        PILL_BG="#192720",
        PILL_BD="#426452",
        HOVER="#2B2E35",
        DANGER_BG="#3A1D26",
        DANGER_BD="#5A2530",
        DANGER_HOVER="#4C2431",
    ),
    "modern": dict(
        BG="#EEF2F9",
        PANEL="#FFFFFF",
        PANEL2="#E8EDF6",
        INPUT="#FFFFFF",
        BORDER="#D6DEEB",
        TEXT="#0F172A",
        MUTED="#55607A",
        GOLD="#4F46E5",
        GOLD2="#4338CA",
        ON_GOLD="#FFFFFF",
        GREEN="#0E9F6E",
        RED="#E5484D",
        BLUE="#2563EB",
        FOOT="#78839B",
        PILL_BG="#E6E9FC",
        PILL_BD="#B9C1F7",
        HOVER="#EEF1F9",
        DANGER_BG="#FEECEC",
        DANGER_BD="#F5B5B5",
        DANGER_HOVER="#FCD9D9",
    ),
    "3d": dict(
        BG="#070B16",
        PANEL="#0F1628",
        PANEL2="#18223C",
        INPUT="#0B1120",
        BORDER="#29345A",
        TEXT="#EAF0FF",
        MUTED="#9AA6C9",
        GOLD="#00D4FF",
        GOLD2="#7C5CFF",
        ON_GOLD="#04121A",
        GREEN="#2DD4BF",
        RED="#FF5C8A",
        BLUE="#60A5FA",
        FOOT="#7C87A6",
        PILL_BG="#0F2A3A",
        PILL_BD="#1E566B",
        HOVER="#1B2544",
        DANGER_BG="#3A1830",
        DANGER_BD="#6B2A52",
        DANGER_HOVER="#4E2142",
    ),
    "casual": dict(
        BG="#FAF5EC",
        PANEL="#FFFDF8",
        PANEL2="#F4EBDD",
        INPUT="#FFFFFF",
        BORDER="#E6D8C2",
        TEXT="#33261B",
        MUTED="#806E5B",
        GOLD="#E8590C",
        GOLD2="#C2410C",
        ON_GOLD="#FFFFFF",
        GREEN="#12A150",
        RED="#E5484D",
        BLUE="#1B7DD6",
        FOOT="#9A8A75",
        PILL_BG="#FDEBD8",
        PILL_BD="#F6CBA4",
        HOVER="#F7EFE2",
        DANGER_BG="#FDECEC",
        DANGER_BD="#F6B4B4",
        DANGER_HOVER="#FBDCDC",
    ),
}

# Display names used by the top-right theme switcher and the Settings row.
THEME_NAMES: dict[str, str] = {
    "dark": "Klasik Dark",
    "modern": "Modern",
    "3d": "3D Neon",
    "casual": "Casual",
}

FONT = "Segoe UI"

# Optional per-theme QSS appended after the base sheet. Cosmetic only — these
# never change widget behaviour, so every feature keeps working in any theme.
_THEME_EXTRAS: dict[str, str] = {
    "modern": """
/* modern — Linear/Arc style: crisp light surfaces, soft depth, indigo accent */
QMainWindow, QWidget#root {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #F6F8FC, stop:1 #E9EEF7);
}
QFrame#card {
    background: #FFFFFF;
    border: 1px solid #DDE3EF;
    border-bottom: 2px solid #CBD5E8;
    border-radius: 18px;
}
QGroupBox {
    background: #FFFFFF;
    border: 1px solid #DDE3EF;
    border-radius: 16px;
}
QPushButton#primary {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #6366F1, stop:1 #4F46E5);
    border: none;
    border-radius: 12px;
}
QPushButton#primary:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #7477F5, stop:1 #5B54EA);
}
QPushButton#ghost {
    background: #FFFFFF;
    border: 1px solid #D6DEEB;
    border-radius: 12px;
}
QPushButton#ghost:hover { background: #EEF1F9; }
QPushButton#nav, QPushButton#tab { border-radius: 12px; }
QPushButton#navActive, QPushButton#tabActive {
    background: #FFFFFF;
    border: 1px solid #D6DEEB;
    border-radius: 12px;
}
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QTimeEdit, QDateEdit { border-radius: 12px; }
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #6366F1, stop:1 #A855F7);
}
QFrame#themeSwitcher {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #FFFFFF, stop:1 #F1F4FA);
    border: 1px solid #D6DEEB;
    border-radius: 16px;
}
""",
    "3d": """
/* 3d — cyberpunk neon: deep-space depth, gradient glow, beveled edges */
QMainWindow, QWidget#root {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #0B1122, stop:1 #05070F);
}
QFrame#card {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #182241, stop:1 #0C1326);
    border-top: 1px solid #3B4C8A;
    border-left: 1px solid #2C3A70;
    border-right: 1px solid #0A1124;
    border-bottom: 1px solid #060A16;
    border-radius: 16px;
}
QGroupBox {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #182241, stop:1 #0E1529);
    border-top: 1px solid #33427A;
    border-radius: 16px;
}
QPushButton#primary {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #00D4FF, stop:0.55 #7C5CFF, stop:1 #B44CFF);
    border: none;
    border-top: 1px solid #9EEBFF;
    border-radius: 12px;
}
QPushButton#primary:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #35E0FF, stop:0.55 #8D6FFF, stop:1 #C25CFF);
}
QPushButton#ghost {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #1B2544, stop:1 #121A33);
    border-top: 1px solid #33427A;
    border-left: 1px solid #29355C;
    border-right: 1px solid #0D1428;
    border-bottom: 1px solid #090F1F;
    border-radius: 12px;
}
QPushButton#ghost:hover { background: #202B4E; }
QPushButton#navActive, QPushButton#tabActive {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #1A2342, stop:1 #101731);
    border: 1px solid #2C3A66;
    border-radius: 12px;
}
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QTimeEdit, QDateEdit {
    background: #0B1120;
    border: 1px solid #2A3558;
    border-radius: 12px;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {
    border: 1px solid #00D4FF;
    background: #0D1526;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #00D4FF, stop:1 #7C5CFF);
}
QHeaderView::section {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #182238, stop:1 #131B30);
}
QFrame#themeSwitcher {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #141D38, stop:1 #0B1120);
    border: 1px solid #2C3A66;
    border-radius: 16px;
}
""",
    "casual": """
/* casual — Notion-warm: soft cream, generous rounding, warm orange glow */
QMainWindow, QWidget#root {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #FDF9F1, stop:1 #F6EDDD);
}
QFrame#card {
    background: #FFFFFF;
    border: 1px solid #EBDCC5;
    border-radius: 22px;
}
QGroupBox {
    background: #FFFDF6;
    border: 1px solid #EBDCC5;
    border-radius: 18px;
}
QPushButton#primary {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #FF7A18, stop:1 #EA5E0C);
    border: none;
    border-radius: 16px;
}
QPushButton#primary:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #FF8A35, stop:1 #F2680F);
}
QPushButton#ghost {
    background: #FFFDF8;
    border: 1px solid #E6D8C2;
    border-radius: 16px;
}
QPushButton#ghost:hover { background: #F9F1E2; }
QPushButton#nav, QPushButton#tab { border-radius: 14px; }
QPushButton#navActive, QPushButton#tabActive {
    background: #FFFDF8;
    border: 1px solid #E6D8C2;
    border-radius: 14px;
}
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QTimeEdit, QDateEdit { border-radius: 14px; }
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #FF8A35, stop:1 #E8590C);
}
QFrame#themeSwitcher {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #FFFFFF, stop:1 #FBF3E6);
    border: 1px solid #E6D8C2;
    border-radius: 18px;
}
""",
}


def colors(theme: str = "dark") -> dict[str, str]:
    """Full palette for *theme*; any missing key falls back to the dark theme."""
    merged = dict(THEMES["dark"])
    merged.update(THEMES.get(theme, {}))
    return merged


def stylesheet(theme: str = "dark") -> str:
    C = colors(theme)
    sheet = f"""
* {{ font-family: "{FONT}"; }}

QMainWindow, QWidget#root {{ background: {C['BG']}; color: {C['TEXT']}; }}

/* Cards */
QFrame#card {{
    background: {C['PANEL']};
    border: 1px solid {C['BORDER']};
    border-radius: 14px;
}}

/* Inputs */
QLineEdit, QTextEdit, QPlainTextEdit {{
    background: {C['INPUT']};
    color: {C['TEXT']};
    border: 1px solid {C['BORDER']};
    border-radius: 10px;
    padding: 10px 12px;
    selection-background-color: {C['GOLD']};
    selection-color: {C['ON_GOLD']};
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {{
    border: 1px solid {C['GOLD']};
}}
QComboBox {{
    background: {C['INPUT']};
    color: {C['TEXT']};
    border: 1px solid {C['BORDER']};
    border-radius: 10px;
    padding: 8px 12px;
}}
QComboBox::drop-down {{ border: none; width: 30px; }}
QComboBox QAbstractItemView {{
    background: {C['INPUT']};
    color: {C['TEXT']};
    selection-background-color: {C['GOLD']};
    selection-color: {C['ON_GOLD']};
    border: 1px solid {C['BORDER']};
}}
QSpinBox {{
    background: {C['INPUT']};
    color: {C['TEXT']};
    border: 1px solid {C['BORDER']};
    border-radius: 10px;
    padding: 6px 10px;
}}
QTimeEdit, QDateEdit {{
    background: {C['INPUT']};
    color: {C['TEXT']};
    border: 1px solid {C['BORDER']};
    border-radius: 10px;
    padding: 6px 10px;
}}

/* Buttons */
QPushButton#primary {{
    background: {C['GOLD']};
    color: {C['ON_GOLD']};
    border: none;
    border-radius: 10px;
    padding: 10px 18px;
    font-weight: 700;
}}
QPushButton#primary:hover {{ background: {C['GOLD2']}; }}
QPushButton#primary:disabled {{ background: {C['PANEL2']}; color: {C['FOOT']}; }}

QPushButton#ghost {{
    background: {C['PANEL2']};
    color: {C['TEXT']};
    border: 1px solid {C['BORDER']};
    border-radius: 10px;
    padding: 9px 16px;
}}
QPushButton#ghost:hover {{ background: {C['HOVER']}; }}

QPushButton#danger {{
    background: {C['DANGER_BG']};
    color: {C['RED']};
    border: 1px solid {C['DANGER_BD']};
    border-radius: 10px;
    padding: 9px 16px;
    font-weight: 700;
}}
QPushButton#danger:hover {{ background: {C['DANGER_HOVER']}; }}

/* Theme switcher pill (top-right) */
QFrame#themeSwitcher {{
    background: {C['PANEL']};
    border: 1px solid {C['BORDER']};
    border-radius: 14px;
}}
QPushButton#themeMenu {{
    background: transparent;
    color: {C['TEXT']};
    border: none;
    border-radius: 10px;
    padding: 8px 12px;
    font-weight: 700;
    font-size: 11px;
}}
QPushButton#themeMenu:hover {{ background: {C['HOVER']}; }}

/* Theme dropdown panel (custom column — swatch + name + tagline + check) */
QFrame#themePanel {{
    background: {C['PANEL']};
    color: {C['TEXT']};
    border: 1px solid {C['BORDER']};
    border-radius: 12px;
    padding: 6px;
}}
QLabel#themePanelCap {{
    color: {C['MUTED']};
    font-size: 9px;
    font-weight: 800;
    letter-spacing: 1px;
}}
QFrame#themeRow {{
    border-radius: 10px;
}}
QFrame#themeRow:hover {{ background: {C['HOVER']}; }}
QLabel#themeRowName {{ color: {C['TEXT']}; font-size: 13px; font-weight: 800; }}
QLabel#themeRowTag {{ color: {C['MUTED']}; font-size: 10px; }}
QLabel#themeRowMark {{ color: {C['GOLD']}; font-size: 13px; font-weight: 900; }}

/* Nav */
QPushButton#nav {{
    background: transparent;
    color: {C['MUTED']};
    border: none;
    border-radius: 10px;
    padding: 10px 14px;
    text-align: left;
    font-weight: 600;
    font-size: 13px;
}}
QPushButton#nav:hover {{ background: {C['HOVER']}; color: {C['TEXT']}; }}
QPushButton#navActive {{
    background: {C['PANEL']};
    color: {C['GOLD']};
    border: 1px solid {C['BORDER']};
    border-radius: 10px;
    padding: 10px 14px;
    text-align: left;
    font-weight: 700;
    font-size: 13px;
}}

/* Progress */
QProgressBar {{
    background: {C['PANEL2']};
    border: none;
    border-radius: 6px;
    height: 10px;
}}
QProgressBar::chunk {{
    background: {C['GOLD']};
    border-radius: 6px;
}}

/* Tables */
QHeaderView::section {{
    background: {C['PANEL2']};
    color: {C['MUTED']};
    padding: 9px 10px;
    border: none;
    border-bottom: 1px solid {C['BORDER']};
    font-weight: 700;
    font-size: 11px;
}}
QTableWidget {{
    background: {C['INPUT']};
    color: {C['TEXT']};
    border: 1px solid {C['BORDER']};
    border-radius: 10px;
    gridline-color: {C['BORDER']};
    selection-background-color: {C['HOVER']};
    selection-color: {C['TEXT']};
}}
QTableWidget::item {{ padding: 8px; }}

/* Scrollbars */
QScrollBar:vertical {{
    background: {C['INPUT']};
    width: 10px;
    border-radius: 5px;
}}
QScrollBar::handle:vertical {{
    background: {C['BORDER']};
    border-radius: 5px;
    min-height: 30px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ height: 10px; background: {C['INPUT']}; }}
QScrollBar::handle:horizontal {{ background: {C['BORDER']}; border-radius: 5px; }}

/* Premium workspace surfaces */
QFrame#card {{ padding: 0px; }}
QToolTip {{ background: {C['PANEL']}; color: {C['TEXT']}; border: 1px solid {C['BORDER']}; padding: 6px 8px; }}
QTabBar::tab {{ min-height: 30px; }}
QListWidget {{ background: {C['INPUT']}; color: {C['TEXT']}; border: 1px solid {C['BORDER']}; border-radius: 10px; padding: 4px; }}
QListWidget::item {{ padding: 8px 10px; border-radius: 7px; }}
QListWidget::item:selected {{ background: {C['PANEL2']}; color: {C['TEXT']}; }}

/* Tabs inline */
QPushButton#tabActive {{
    background: {C['PANEL']};
    color: {C['GOLD']};
    border: 1px solid {C['BORDER']};
    border-radius: 10px;
    padding: 8px 14px;
    font-weight: 700;
}}
QPushButton#tab {{
    background: transparent;
    color: {C['MUTED']};
    border: none;
    border-radius: 10px;
    padding: 8px 14px;
    font-weight: 600;
}}
QPushButton#tab:hover {{ background: {C['HOVER']}; color: {C['TEXT']}; }}

/* Checkboxes */
QCheckBox {{ color: {C['TEXT']}; spacing: 8px; }}
QCheckBox::indicator {{ width: 18px; height: 18px; border: 1px solid {C['BORDER']}; border-radius: 4px; background: {C['INPUT']}; }}
QCheckBox::indicator:checked {{ background: {C['GOLD']}; border-color: {C['GOLD']}; }}

QLabel#muted {{ color: {C['MUTED']}; }}
QLabel#gold {{ color: {C['GOLD']}; font-weight: 700; }}
QLabel#h1 {{ color: {C['TEXT']}; font-size: 22px; font-weight: 800; }}
QLabel#h2 {{ color: {C['TEXT']}; font-size: 15px; font-weight: 700; }}
QLabel#foot {{ color: {C['FOOT']}; }}
QLabel {{ color: {C['TEXT']}; }}
QWidget:disabled {{ color: {C['FOOT']}; }}

/* Popups: notification message boxes + input dialogs. */
QMessageBox, QInputDialog {{
    background: {C['PANEL']};
    color: {C['TEXT']};
}}
QMessageBox QLabel, QInputDialog QLabel {{
    background: transparent;
    color: {C['TEXT']};
}}
QMessageBox QDialogButtonBox, QInputDialog QDialogButtonBox {{
    background: transparent;
}}
QMessageBox QPushButton, QInputDialog QPushButton {{
    background: {C['PANEL2']};
    color: {C['TEXT']};
    border: 1px solid {C['BORDER']};
    border-radius: 8px;
    padding: 7px 16px;
    min-width: 72px;
}}
QMessageBox QPushButton:hover, QInputDialog QPushButton:hover {{
    background: {C['HOVER']};
    border-color: {C['GOLD']};
}}
QMessageBox QPushButton:default, QInputDialog QPushButton:default {{
    background: {C['GOLD']};
    color: {C['ON_GOLD']};
    border: none;
    font-weight: 700;
}}
QMessageBox QPushButton:default:hover, QInputDialog QPushButton:default:hover {{
    background: {C['GOLD2']};
}}

/* Custom dialogs (Auto-Pilot rule editor, pickers). */
QDialog {{
    background: {C['PANEL']};
    color: {C['TEXT']};
}}
QDialog QLabel {{
    background: transparent;
    color: {C['TEXT']};
}}
QDialog QDialogButtonBox {{
    background: transparent;
}}
QDialog QPushButton {{
    background: {C['PANEL2']};
    color: {C['TEXT']};
    border: 1px solid {C['BORDER']};
    border-radius: 8px;
    padding: 7px 16px;
    min-width: 72px;
}}
QDialog QPushButton:hover {{
    background: {C['HOVER']};
    border-color: {C['GOLD']};
}}
QDialog QPushButton:default {{
    background: {C['GOLD']};
    color: {C['ON_GOLD']};
    border: none;
    font-weight: 700;
}}
QDialog QPushButton:default:hover {{
    background: {C['GOLD2']};
}}

QTabWidget::pane {{ border: 1px solid {C['BORDER']}; border-radius: 12px; background: {C['PANEL']}; top: -1px; }}
QTabBar::tab {{ background: {C['PANEL2']}; color: {C['MUTED']}; border: 1px solid {C['BORDER']}; border-bottom: none; padding: 8px 12px; margin-right: 4px; border-top-left-radius: 10px; border-top-right-radius: 10px; min-height: 28px; }}
QTabBar::tab:selected {{ background: {C['PANEL']}; color: {C['TEXT']}; }}
QTabBar::tab:hover {{ color: {C['TEXT']}; }}
QGroupBox {{ background: {C['PANEL2']}; color: {C['TEXT']}; border: 1px solid {C['BORDER']}; border-radius: 12px; margin-top: 14px; font-weight: 700; padding: 14px 10px 10px 10px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px; color: {C['TEXT']}; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QSplitter::handle {{ background: {C['BORDER']}; }}
QSplitter::handle:horizontal {{ width: 6px; }}
QSplitter::handle:vertical {{ height: 6px; }}
"""
    return sheet + _THEME_EXTRAS.get(theme, "")
