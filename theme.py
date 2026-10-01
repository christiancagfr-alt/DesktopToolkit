"""App theme: dark / light."""

from __future__ import annotations

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication


DARK = {
    "bg": "#0b1224",
    "panel": "#111827",
    "card": "#1e293b",
    "border": "rgba(99,102,241,0.45)",
    "text": "#e2e8f0",
    "muted": "#94a3b8",
    "accent": "#6366f1",
    "accent2": "#22d3ee",
    "input_bg": "#020617",
    "danger": "#ef4444",
}

LIGHT = {
    "bg": "#f1f5f9",
    "panel": "#ffffff",
    "card": "#f8fafc",
    "border": "rgba(99,102,241,0.35)",
    "text": "#0f172a",
    "muted": "#64748b",
    "accent": "#4f46e5",
    "accent2": "#0891b2",
    "input_bg": "#ffffff",
    "danger": "#dc2626",
}


def theme_tokens(mode: str) -> dict[str, str]:
    return LIGHT if mode == "light" else DARK


def apply_app_palette(app: QApplication, mode: str) -> None:
    t = theme_tokens(mode)
    p = QPalette()
    if mode == "light":
        p.setColor(QPalette.ColorRole.Window, QColor(t["bg"]))
        p.setColor(QPalette.ColorRole.WindowText, QColor(t["text"]))
        p.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
        p.setColor(QPalette.ColorRole.Text, QColor(t["text"]))
        p.setColor(QPalette.ColorRole.Button, QColor(t["card"]))
        p.setColor(QPalette.ColorRole.ButtonText, QColor(t["text"]))
        p.setColor(QPalette.ColorRole.Highlight, QColor(t["accent"]))
        p.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    else:
        p.setColor(QPalette.ColorRole.Window, QColor(t["bg"]))
        p.setColor(QPalette.ColorRole.WindowText, QColor(t["text"]))
        p.setColor(QPalette.ColorRole.Base, QColor(t["input_bg"]))
        p.setColor(QPalette.ColorRole.Text, QColor(t["text"]))
        p.setColor(QPalette.ColorRole.Button, QColor(t["card"]))
        p.setColor(QPalette.ColorRole.ButtonText, QColor(t["text"]))
        p.setColor(QPalette.ColorRole.Highlight, QColor(t["accent"]))
        p.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    app.setPalette(p)


def main_window_qss(mode: str) -> str:
    t = theme_tokens(mode)
    return f"""
    QMainWindow, QWidget#root {{
        background: {t['bg']};
        color: {t['text']};
    }}
    QFrame#sideNav {{
        background: {t['panel']};
        border-right: 1px solid {t['border']};
    }}
    QLabel#brand {{
        color: {t['accent2'] if mode == 'dark' else t['accent']};
        font-size: 15px; font-weight: 900;
    }}
    QLabel#section {{
        color: {t['muted']}; font-size: 11px; font-weight: 700;
        padding: 10px 4px 4px 4px;
    }}
    QLabel#pageTitle {{
        color: {t['text']}; font-size: 18px; font-weight: 800;
    }}
    QLabel#muted {{ color: {t['muted']}; font-size: 12px; }}
    QPushButton#nav {{
        text-align: left; padding: 10px 12px; border: none; border-radius: 10px;
        background: transparent; color: {t['text']}; font-weight: 700;
    }}
    QPushButton#nav:hover {{ background: {t['card']}; }}
    QPushButton#nav:checked {{
        background: {t['accent']}; color: white;
    }}
    QPushButton#navChild {{
        text-align: left; padding: 8px 10px; border: none; border-radius: 8px;
        background: transparent; color: {t['muted']}; font-weight: 600; font-size: 12px;
    }}
    QPushButton#navChild:hover {{ background: {t['card']}; color: {t['text']}; }}
    QPushButton#navChild:checked {{
        background: {t['accent']}; color: white;
    }}
    QPushButton#homeCard {{
        background: {t['card']}; color: {t['text']};
        border: 1px solid {t['border']}; border-radius: 14px;
        padding: 16px 10px; font-weight: 700; min-height: 72px;
    }}
    QPushButton#homeCard:hover {{
        border-color: {t['accent2']};
        background: {t['accent']}; color: white;
    }}
    QPushButton#primary {{
        background: {t['accent']}; color: white; border: none;
        border-radius: 10px; padding: 9px 14px; font-weight: 800;
    }}
    QPushButton#soft {{
        background: {t['card']}; color: {t['text']};
        border: 1px solid {t['border']}; border-radius: 10px;
        padding: 8px 12px; font-weight: 700;
    }}
    QLineEdit, QTextEdit, QListWidget, QComboBox, QSpinBox {{
        background: {t['input_bg']}; color: {t['text']};
        border: 1px solid {t['border']}; border-radius: 10px; padding: 8px;
    }}
    QFrame#panel {{
        background: {t['panel']}; border: 1px solid {t['border']}; border-radius: 14px;
    }}
    QCheckBox {{ color: {t['text']}; spacing: 8px; }}
    QScrollArea {{ border: none; background: transparent; }}
    """
