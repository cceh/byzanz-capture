"""Centralized styling for the papyri app — palette + stylesheet loader.

- `COLORS` — light-mode palette. Identity colors (spectrum, CTA, state
  indicators) and the photo-viewer dark backdrop live here unchanged.
- `_DARK_OVERRIDES` — entries that flip in dark mode (bg / text / line /
  soft tints). Anything not overridden inherits from `COLORS`.
- `current_palette()` — returns the merged palette for whichever color
  scheme Qt currently reports. Reads via `app.styleHints().colorScheme()`.
- `load_app_stylesheet()` — reads `ui/app.qss` and substitutes the
  active palette via `string.Template` ($name) syntax — keeps the QSS
  file valid for editor highlighters.
- `install_app_stylesheet(app)` — applies the stylesheet AND watches
  both the .qss file (hot-reload during dev) and the system color
  scheme (auto re-apply when the user toggles dark/light mode).

The property-driven styling helper (`set_state`) lives in
`byzanz_camera.helpers` — pure Qt utility, no theme knowledge.
"""
from __future__ import annotations
from pathlib import Path
from string import Template

from PyQt6.QtCore import QFileSystemWatcher, Qt
from PyQt6.QtWidgets import QApplication

from byzanz_camera.helpers import refresh_themed_icons


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
    # Accent tints — soft fill + hairline + readable text for low-emphasis
    # accent surfaces (rig-height status cluster). Teal reads distinct from
    # the VIS-blue / IR-orange spectrum hues.
    "accent_soft":     "#cfe6e3",
    "accent_line":     "#8fbdb8",
    "accent_text":     "#1c4a48",
    # View-state pill borders.
    "live_dot":        "#06b6d4",
    "paused_icon":     "#fbbf24",
    "preview_pill":    "#94a3b8",
    # Status colors.
    "status_error":    "#dc2626",
    # Calibration status chip — theme-independent (work on both light and
    # dark), so no _DARK_OVERRIDES entries.
    "cal_ok":          "#16a34a",   # green-600 — calibration set is fresh
    "cal_due":         "#f59e0b",   # amber-500 — a calibration is due
}


# Dark-mode overrides. Only entries that need to flip live here —
# everything else (spectrum hues, accent, state indicators, photo
# backdrop) is theme-independent and inherited from `COLORS`.
_DARK_OVERRIDES: dict[str, str] = {
    "bg_pane":         "#1e1e1e",
    "bg_pane_alt":     "#262626",
    "bg_card":         "#2a2a2a",
    "line_soft":       "#333333",
    "line":            "#404040",
    "ink":             "#e5e5e5",
    "ink_2":           "#a3a3a3",
    "ink_3":           "#737373",
    "ink_card_title":  "#f5f5f5",
    "ink_card_sub":    "#d4d4d4",
    # Soft tints flip: light pastels become deep tones; the dark-text
    # paired color flips to a light tint so contrast works on the new bg.
    "vis_soft":        "#1e3a5c",
    "vis_text_dark":   "#bfdbfe",
    "ir_soft":         "#5c2f15",
    "ir_text_dark":    "#fdba74",
    # Accent tints flip to deep teal fill / lighter hairline + text so the
    # rig-height cluster keeps contrast on the dark top bar.
    "accent_soft":     "#22403d",
    "accent_line":     "#3d6b67",
    "accent_text":     "#8fc4bf",
}


def current_palette() -> dict[str, str]:
    """Active palette for the current Qt color scheme. Custom-painter
    code (e.g. bucket cards) should read colors via this function at
    paint time so they track theme switches automatically."""
    app = QApplication.instance()
    if app is not None and app.styleHints().colorScheme() == Qt.ColorScheme.Dark:
        return {**COLORS, **_DARK_OVERRIDES}
    return COLORS


# ---- API ---------------------------------------------------------------

_QSS_PATH = Path(__file__).parent / "ui" / "app.qss"


def load_app_stylesheet() -> str:
    """Read `ui/app.qss` and substitute `$name` placeholders from the
    active palette (`current_palette()` — light or dark depending on
    system scheme). Raises `KeyError` if the QSS references a name not
    in the palette — preferred over silent fallback so missing keys
    fail loud. Prefer `install_app_stylesheet(app)` over calling this
    directly so file changes AND scheme changes hot-reload."""
    return Template(_QSS_PATH.read_text()).substitute(current_palette())


def install_app_stylesheet(app: QApplication) -> QFileSystemWatcher:
    """Apply the app stylesheet. Re-applies automatically when:
    - the .qss file changes (dev hot-reload), OR
    - the system color scheme flips (dark/light toggle).

    Returns the file watcher so the caller can keep a reference
    (Qt-parented to the app, so lifetime matches).

    Editors that replace-on-save (vim, JetBrains, etc.) drop the
    watch; we re-add after every fileChanged to survive those."""
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

    def _on_scheme_change(_scheme) -> None:
        # Icons first — re-render with the new theme color so the next
        # paint includes them; then re-substitute the QSS palette.
        refresh_themed_icons()
        _apply()
    app.styleHints().colorSchemeChanged.connect(_on_scheme_change)
    return watcher


