"""py2app build config for the desktop launcher.

Build the dev launcher (alias mode — uses the live source tree + venv,
freezes nothing):

    .venv/bin/python setup.py py2app -A

Produces dist/CCeH Crocodile Capture.app. See scripts/make-macos-launcher.sh
for the wrapper that builds it and drops it on the Desktop.
"""
from setuptools import setup

setup(
    app=["run_papyri.py"],
    options={
        "py2app": {
            "iconfile": "ui/icon/app_icon.icns",
            "plist": {
                "CFBundleName": "CCeH Crocodile Capture",
                "CFBundleDisplayName": "CCeH Crocodile Capture",
                "CFBundleIdentifier": "info.cceh.crocodile-capture",
                "CFBundleVersion": "1.0",
                "CFBundleShortVersionString": "1.0",
                "NSHighResolutionCapable": True,
            },
        }
    },
)
