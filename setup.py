"""py2app build config for the desktop launcher.

Build the dev launcher (alias mode — uses the live source tree + venv,
freezes nothing):

    .venv/bin/python setup.py py2app -A

Produces dist/CCeH Crocodile Capture.app. See scripts/make-macos-launcher.sh
for the wrapper that builds it and drops it on the Desktop.

CROC_APP_SUFFIX (env, optional): short token like "ALT". Appended to the
bundle name and identifier ("CCeH Crocodile Capture (ALT)",
info.cceh.crocodile-capture.alt) so a second launcher pointing at the
pinned fallback worktree can coexist with the regular one. Set by
make-macos-launcher.sh's second argument — keep both in sync.

CROC_APP_ICON (env, optional): icon filename prefix, default "app_icon".
"""
import os

from setuptools import setup

_suffix = os.environ.get("CROC_APP_SUFFIX", "")
_icon_name = os.environ.get("CROC_APP_ICON", "app_icon")
_app_name = "CCeH Crocodile Capture" + (f" ({_suffix})" if _suffix else "")
_bundle_id = "info.cceh.crocodile-capture" + (f".{_suffix.lower()}" if _suffix else "")

setup(
    app=["run_papyri.py"],
    options={
        "py2app": {
            "iconfile": f"ui/icon/{_icon_name}.icns",
            "plist": {
                "CFBundleName": _app_name,
                "CFBundleDisplayName": _app_name,
                "CFBundleIdentifier": _bundle_id,
                "CFBundleVersion": "1.0",
                "CFBundleShortVersionString": "1.0",
                "CrocAppIcon": _icon_name,
                "NSHighResolutionCapable": True,
            },
        }
    },
)
