"""py2app entry point for CCeH Crocodile Capture (the papyri app).

Built in ALIAS mode (`python setup.py py2app -A`): nothing is frozen — the
.app runs this live source tree against the project venv, so code changes
are picked up on the next launch. Because py2app's bundle process *is* this
Python interpreter, the Dock shows a single "CCeH Crocodile Capture" tile.

`byzanz_camera.helpers.get_ui_path` resolves UI assets relative to the
current working directory, so chdir into the repo root before launching.
"""
import os
import plistlib
import sys
from pathlib import Path

os.chdir(os.path.dirname(os.path.abspath(__file__)))

def _app_icon_name() -> str:
    """Return the icon name baked into this app bundle, if any."""
    if icon_name := os.environ.get("CROC_APP_ICON"):
        return icon_name
    if getattr(sys, "frozen", False):
        info_plist = Path(sys.executable).parents[1] / "Info.plist"
        try:
            with info_plist.open("rb") as fh:
                return plistlib.load(fh).get("CrocAppIcon", "app_icon")
        except (OSError, plistlib.InvalidFileException):
            pass
    return "app_icon"


if __name__ == "__main__":
    from papyri.main import main

    main(app_icon=_app_icon_name())
