"""Dark / Light theme definition + global stylesheet."""
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
    ),
    "light": dict(
        BG="#F3F1EB",
        PANEL="#FFFDF8",
        PANEL2="#F0EDE5",
        INPUT="#FFFFFF",
        BORDER="#D6D0C4",
        TEXT="#25251F",
        MUTED="#5C5A53",
        GOLD="#80580C",
        GOLD2="#6B490A",
        ON_GOLD="#FFFFFF",
        GREEN="#147653",
        RED="#B8323B",
        BLUE="#245DAA",
        FOOT="#6B675E",
        PILL_BG="#E9F4EE",
        PILL_BD="#9CCAB1",
        HOVER="#EAE6DC",
    ),
}

FONT = "Segoe UI"


def stylesheet(theme: str = "dark") -> str:
    C = THEMES.get(theme, THEMES["dark"])
    return f"""
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
    background: #3A1D26;
    color: {C['RED']};
    border: 1px solid #5A2530;
    border-radius: 10px;
    padding: 9px 16px;
    font-weight: 700;
}}
QPushButton#danger:hover {{ background: #4C2431; }}

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
