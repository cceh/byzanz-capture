#!/bin/bash
set -euo pipefail

# Resolve the versioned camlib/iolib directories instead of hardcoding them —
# pacman upgrades of libgphoto2 change these paths.
CAMLIB_DIR=$(ls -d /mingw64/lib/libgphoto2/*/ | sort -V | tail -1)
IOLIB_DIR=$(ls -d /mingw64/lib/libgphoto2_port/*/ | sort -V | tail -1)
echo "Using camlibs: $CAMLIB_DIR"
echo "Using iolibs:  $IOLIB_DIR"

# The MSYS2 `python-opencv` package ships cv2 as a single ABI-tagged .pyd
# (not the PyPI package dir), which PyInstaller's bundled opencv hook doesn't
# collect. Add the .pyd explicitly — PyInstaller then traces and bundles its
# opencv DLL dependencies from /mingw64/bin.
CV2_PYD=$(python -c "import cv2; print(cv2.__file__)")
echo "Using cv2:     $CV2_PYD"

# libusb: $IOLIB_DIR ships its own STALE libusb-1.0.dll, so bundle the current
# mingw one LAST to override it — it's the build usb1.dll actually links
# against. mingw-w64-x86_64-libusb comes in as a libgphoto2 dependency, so no
# separate download/extract of an upstream libusb release is needed.
pyinstaller --onedir \
    --add-binary "$IOLIB_DIR":. \
    --add-binary "$CAMLIB_DIR":. \
    --add-binary "$CV2_PYD":. \
    --add-binary /mingw64/bin/libusb-1.0.dll:. \
    --add-data ui:ui \
    --add-data i18n:i18n \
    --add-data cceh-dome-template.lp:. \
    --add-data dome_presets:dome_presets main.py \
    --runtime-hook ./build_win_hook.py \
    --noconfirm \
    --name byzanz-capture
