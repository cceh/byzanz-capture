from os import path
import sys

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
    from PyQt6.QtCore import QSize
    from PyQt6.QtGui import QIcon
    icon = QIcon()
    for size in _APP_ICON_SIZES:
        icon.addFile(
            get_ui_path(f"ui/icon/app_icon_{size}.png"),
            QSize(size, size),
        )
    return icon
