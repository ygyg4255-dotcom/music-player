"""Visual theme: colors and stylesheet for the Dynamic Island widget.

Inspired by Apple's Dynamic Island: a near-black rounded pill with soft
edges, a bright accent, and white text.  Kept as plain data so the UI code
stays layout-focused.
"""

from __future__ import annotations

BG = "#1d1d20"          # island background
BG_RAISED = "#2c2c31"    # input fields / inner surfaces
BORDER = "#3a3a40"       # subtle outline
TEXT = "#f5f5f7"         # primary text
TEXT_DIM = "#a1a1a8"     # secondary text
ACCENT = "#0a84ff"       # Apple system blue
ACCENT_HOVER = "#409cff"
SUCCESS = "#30d158"
DANGER = "#ff453a"
#: Soft red used to highlight the corner close button on hover.
CLOSE_HOVER = "#E11D48"

#: Rounded-corner radius for the island and inner widgets (px).
RADIUS = 22

ISLAND_QSS = f"""
QWidget#island {{
    background: {BG};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
}}
QLabel#titleLabel {{
    color: {TEXT};
    font-size: 15px;
    font-weight: 600;
}}
QLabel#timeLabel {{
    color: {ACCENT};
    font-size: 22px;
    font-weight: 700;
    font-family: "Segoe UI", "Arial";
}}
QLabel#statusLabel {{
    color: {TEXT_DIM};
    font-size: 11px;
}}
QLineEdit {{
    background: {BG_RAISED};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 10px;
    padding: 6px 10px;
    font-size: 13px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus {{
    border: 1px solid {ACCENT};
}}
QPushButton {{
    background: {BG_RAISED};
    color: {TEXT};
    border: none;
    border-radius: 10px;
    padding: 6px 14px;
    font-size: 13px;
    font-weight: 600;
}}
QPushButton:hover {{
    background: {ACCENT_HOVER};
}}
QPushButton:pressed {{
    background: {ACCENT};
}}
QPushButton#startButton {{
    background: {ACCENT};
    color: white;
}}
QPushButton#startButton:hover {{
    background: {ACCENT_HOVER};
}}
QPushButton#dangerButton:hover {{
    background: {DANGER};
}}
QDoubleSpinBox {{
    background: {BG_RAISED};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 10px;
    padding: 6px 10px;
    font-size: 13px;
    selection-background-color: {ACCENT};
}}
QDoubleSpinBox:focus {{
    border: 1px solid {ACCENT};
}}
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    width: 16px;
    border: none;
    background: transparent;
}}
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {{
    background: {ACCENT_HOVER};
    border-radius: 4px;
}}
QPushButton#expandButton {{
    background: transparent;
    color: {TEXT_DIM};
    border-radius: 12px;
    padding: 4px 6px;
    font-size: 11px;
}}
QPushButton#expandButton:hover {{
    background: {BG_RAISED};
    color: {TEXT};
}}
QPushButton#closeButton {{
    background: transparent;
    color: {TEXT_DIM};
    border: none;
    border-radius: 11px;
    padding: 0;
    font-size: 12px;
    font-weight: 600;
}}
QPushButton#closeButton:hover {{
    background: {CLOSE_HOVER};
    color: #ffffff;
}}
QPushButton#closeButton:pressed {{
    background: {CLOSE_HOVER};
    color: #ffffff;
}}
QPushButton#ghostButton {{
    background: transparent;
    color: {TEXT_DIM};
    padding: 4px 8px;
    font-weight: 400;
}}
QPushButton#ghostButton:hover {{
    color: {TEXT};
    background: {BG_RAISED};
}}
QCheckBox#startupCheckbox {{
    color: {TEXT_DIM};
    font-size: 11px;
    spacing: 6px;
}}
QCheckBox#startupCheckbox:hover {{
    color: {TEXT};
}}
QCheckBox#startupCheckbox::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid {BORDER};
    border-radius: 4px;
    background: {BG_RAISED};
}}
QCheckBox#startupCheckbox::indicator:checked {{
    background: {ACCENT};
    border: 1px solid {ACCENT};
}}
"""