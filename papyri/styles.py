"""Centralized styling for the papyri app — palette + stylesheet loader.

- `COLORS` — single palette dict. Anything referenced from QSS via
  `$name` (`string.Template` syntax — keeps the QSS file valid for
  editor highlighters, unlike f-string braces) lives here, so
  swapping a theme means swapping one dict.

- `load_app_stylesheet()` — reads `ui/app.qss`, substitutes the
  placeholders, returns the final stylesheet string.

- `install_app_stylesheet(app)` — applies the stylesheet AND attaches
  a file watcher so edits to `app.qss` hot-reload during development.

The property-driven styling helper (`set_state`) lives in
`byzanz_camera.helpers` since it's a pure Qt utility with no theme
knowledge — usable from any host app, not just papyri.
"""
from __future__ import annotations
from pathlib import Path
from string import Template

from PyQt6.QtCore import QFileSystemWatcher
from PyQt6.QtWidgets import QApplication


# ---- palette ------------------------------------------------------------

# Single source of truth for app-wide colors. QSS rules reference these
# by name via $placeholder — `load_app_stylesheet` substitutes them in.
# Keep this dict and `ui/app.qss` aligned: adding a placeholder to the
# QSS without adding the key here will raise `KeyError` at load time
# (which is the desired loud failure mode).
COLORS: dict[str, str] = {
    # Spectrum identity — used by camera-state badges, bucket selector
    # accents, fusing-panel border, capture-area chrome.
    "vis":             "#3b82f6",
    "ir":              "#ea580c",
    # Soft variants for low-emphasis treatments (sidebar selection,
    # inactive bucket tab badges).
    "vis_soft":        "#dbeafe",   # vis-100
    "vis_text_dark":   "#1e3a8a",   # vis-800 — text on vis_soft
    "ir_soft":         "#ffedd5",   # ir-100
    "ir_text_dark":    "#9a3412",   # ir-800 — text on ir_soft
    # Generic neutrals.
    "bg_pane":         "#f8fafc",
    "bg_pane_alt":     "#f1f5f9",
    "bg_card":         "#ffffff",
    "bg_dark_viewer":  "rgb(30, 30, 30)",
    "line_soft":       "#e2e8f0",
    "line":            "#cbd5e1",
    "ink":             "#0f172a",
    "ink_2":           "#475569",
    "ink_3":           "#94a3b8",
    "ink_card_title":  "#1c1c1c",
    "ink_card_sub":    "#5a5a5a",
    "slate_hover":     "#334155",
    "slate_select":    "#2563eb",
    "slate_neutral":   "#64748b",
    # Accent / CTA.
    "accent":          "#1c4a48",
    "accent_hover":    "#2c5f5c",
    # View-state pill borders.
    "live_dot":        "#06b6d4",
    "paused_icon":     "#fbbf24",
    "preview_pill":    "#94a3b8",
    # Status colors.
    "status_error":    "#dc2626",
}


# ---- API ---------------------------------------------------------------

_QSS_PATH = Path(__file__).parent / "ui" / "app.qss"


def load_app_stylesheet() -> str:
    """Read `ui/app.qss` and substitute `$name` placeholders from
    `COLORS`. Raises `KeyError` if the QSS references a name not in
    `COLORS` — preferred over silent fallback so missing keys fail
    loud. Prefer `install_app_stylesheet(app)` over calling this
    directly so file changes hot-reload during development."""
    return Template(_QSS_PATH.read_text()).substitute(COLORS)


def install_app_stylesheet(app: QApplication) -> QFileSystemWatcher:
    """Apply the app stylesheet and watch the .qss file for edits;
    re-apply automatically on change. Returns the watcher so the
    caller can keep a reference (Qt parent the app, so its lifetime
    matches the app's).

    Hot-reload is always on — the cost is one file watcher + an I/O
    re-read on save, both trivial. Editors that replace-on-save
    (vim, JetBrains, etc.) drop the watch; we re-add the path after
    every fileChanged so the watch survives those saves."""
    def _apply() -> None:
        try:
            app.setStyleSheet(load_app_stylesheet())
        except (KeyError, ValueError, OSError) as e:
            print(f"app.qss reload failed: {e!r}")

    _apply()
    watcher = QFileSystemWatcher([str(_QSS_PATH)], app)

    def _on_file_changed(_path: str) -> None:
        _apply()
        if str(_QSS_PATH) not in watcher.files():
            watcher.addPath(str(_QSS_PATH))

    watcher.fileChanged.connect(_on_file_changed)
    return watcher


