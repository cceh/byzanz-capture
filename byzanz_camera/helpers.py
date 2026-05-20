from os import path
import sys
from typing import Any

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QIcon, QImage, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import QApplication, QWidget

def get_ui_path(file: str):
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        bundle_dir = sys._MEIPASS
    else:
        bundle_dir = path.abspath(path.dirname("__FILE__"))
    return path.join(bundle_dir, file)


# Sizes baked into the in-app QIcon. Includes the small sizes Qt
# downscales from for tray / tooltip contexts (16, 24), the common
# toolbar / dock sizes (32, 48, 64), and the larger sizes Qt picks up
# for HiDPI rendering (128, 256, 512). Larger sizes (1024+) and the
# platform-bundle formats (.icns / .ico) live alongside these PNGs for
# packaging time but aren't loaded into QIcon.
_APP_ICON_SIZES = (16, 24, 32, 48, 64, 128, 256, 512)


def set_state(widget: QWidget, name: str, value: Any) -> None:
    """Set a Qt dynamic property and force a style re-polish so any
    `QSomething[name="value"]` rules in the app stylesheet take effect.

    Qt doesn't re-evaluate selectors on property change the way the
    browser does on `data-*` attribute change — without the unpolish/
    polish dance, the property is set but visuals stay stale. This
    helper centralises the dance so call sites read like web-dev
    attribute toggles: `set_state(viewer, "viewState", "live")`.

    No theme knowledge here — just the Qt plumbing. The QSS rules
    that match the property live in the host app's stylesheet."""
    widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


# Dict keyed by setter (bound methods are hashable + equal by
# (widget, method) — so calling set_themed_*({same setter}, …) twice
# REPLACES the prior registration. Static icons register once at
# construction; state-dependent icons re-register whenever their
# state handler runs — either way the registry stays one entry per
# distinct setter, no unbounded growth.
_themed_apply_fns: dict = {}


def set_themed_icon(setter, svg_path: str) -> None:
    """Apply a themed icon AND register for re-apply on color-scheme
    change. Use instead of `setter(themed_icon(path))` whenever you
    want the icon to live-refresh on light/dark toggle.

    `setter` is a bound method like `button.setIcon` or `action.setIcon`.
    The bound method holds a reference to its widget, which keeps the
    widget alive for the life of the app — fine for window-scoped
    widgets, NOT fine for short-lived dialogs (those should clear
    their registrations on close)."""
    def apply() -> None:
        try:
            setter(themed_icon(svg_path))
        except RuntimeError:
            _themed_apply_fns.pop(setter, None)  # widget C++ side deleted
    apply()
    _themed_apply_fns[setter] = apply


def set_themed_pixmap(setter, svg_path: str, size: int = 64) -> None:
    """Pixmap counterpart of `set_themed_icon` — for QLabel.setPixmap
    and other setters that take a QPixmap rather than a QIcon."""
    def apply() -> None:
        try:
            setter(themed_pixmap(svg_path, size))
        except RuntimeError:
            _themed_apply_fns.pop(setter, None)
    apply()
    _themed_apply_fns[setter] = apply


def refresh_themed_icons() -> None:
    """Re-apply every registered themed icon / pixmap. Called from
    `papyri.styles.install_app_stylesheet` when the system color
    scheme changes; safe to call manually after a theme palette tweak
    during development too."""
    # Snapshot the values — apply() may mutate the dict on RuntimeError.
    for apply in list(_themed_apply_fns.values()):
        apply()


def themed_pixmap(svg_path: str, size: int = 64) -> QPixmap:
    """Render an SVG to a `QPixmap` with `currentColor` substituted
    for a theme-appropriate hex — so monochrome glyph bodies adapt to
    light/dark while any explicit fill colors in the SVG (status
    overlays, semantic accents) pass through unchanged.

    `size` is the rendered pixmap edge in logical pixels. 64 is plenty
    of headroom for downscaling to 16-30px buttons on HiDPI displays.

    Re-call after `colorSchemeChanged` to refresh; pixmaps created
    earlier keep their original color."""
    app = QApplication.instance()
    is_dark = (app is not None
               and app.styleHints().colorScheme() == Qt.ColorScheme.Dark)
    color_hex = "#e5e5e5" if is_dark else "#0f172a"

    with open(svg_path) as f:
        svg = f.read().replace("currentColor", color_hex)

    # Render via QImage with an alpha channel — QPixmap defaults to
    # opaque on most platforms, so its `.fill(transparent)` silently
    # produces solid black around the glyph. ARGB32_Premultiplied gives
    # us the alpha we need; convert to QPixmap at the end for callers.
    renderer = QSvgRenderer(svg.encode())
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    return QPixmap.fromImage(image)


def themed_icon(svg_path: str, render_size: int = 64) -> QIcon:
    """`QIcon` wrapper around `themed_pixmap` — for `setIcon` callers."""
    return QIcon(themed_pixmap(svg_path, render_size))


def get_app_icon():
    """Multi-resolution QIcon for the application window.

    Cross-platform best practice: hand Qt every standard size and let
    it pick the best one for each rendering surface (dock, taskbar,
    alt-tab, window decoration, tooltips), including upscaled choices
    for HiDPI displays. macOS uses up to 512; Windows / Linux
    taskbars settle for 32–48 with HiDPI doubles.

    Call this once after constructing QApplication:

        app.setWindowIcon(get_app_icon())

    For the standalone executable's icon (the one Finder / Explorer
    show when the app isn't running), use the platform-specific
    bundles next to the PNGs:
      - macOS:   ui/icon/app_icon.icns  →  Info.plist CFBundleIconFile
      - Windows: ui/icon/app_icon.ico   →  PyInstaller --icon
    """
    icon = QIcon()
    for size in _APP_ICON_SIZES:
        icon.addFile(
            get_ui_path(f"ui/icon/app_icon_{size}.png"),
            QSize(size, size),
        )
    return icon
