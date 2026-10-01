#!/bin/bash
set -euo pipefail

# Camera drivers come from the vendored libgphoto2 fork (built by
# scripts/vm-win-setup.sh gphoto2), not from pacman: only the fork carries the
# vusb port driver that backs the virtual camera. Resolve the versioned
# camlib/iolib directories instead of hardcoding them — a version bump moves them.
PREFIX=vendor/build
[ -d "$PREFIX/lib/libgphoto2" ] || { echo "$PREFIX missing — run: ./scripts/vm-win-setup.sh gphoto2" >&2; exit 1; }
CAMLIB_DIR=$(ls -d "$PREFIX"/lib/libgphoto2/*/ | sort -V | tail -1)
IOLIB_DIR=$(ls -d "$PREFIX"/lib/libgphoto2_port/*/ | sort -V | tail -1)
# PyInstaller traces DLL dependencies through PATH. The fork's own
# libgphoto2-6.dll / libgphoto2_port-12.dll live in the prefix, nowhere a
# default PATH points — without this they would simply be missing from the
# bundle (on Windows a DLL carries no path of its own, unlike a macOS dylib).
export PATH="$PWD/$PREFIX/bin:$PATH"
echo "Using camlibs: $CAMLIB_DIR"
echo "Using iolibs:  $IOLIB_DIR"

# The MSYS2 `python-opencv` package ships cv2 as a single ABI-tagged .pyd
# (not the PyPI package dir), which PyInstaller's bundled opencv hook doesn't
# collect. Add the .pyd explicitly — PyInstaller then traces and bundles its
# opencv DLL dependencies from /ucrt64/bin.
CV2_PYD=$(python -c "import cv2; print(cv2.__file__)")
echo "Using cv2:     $CV2_PYD"

# vcamera-sources: the vusb driver's image material. Its compiled-in default
# directory is an absolute path on the BUILD machine, so a bundle would find
# nothing there — byzanz_camera._gphoto2_paths points VCAMERADIR* at these
# bundled folders instead. Only vusb2's material is committed; both virtual
# cameras serve it.
#
# libusb: $IOLIB_DIR ships its own STALE libusb-1.0.dll, so bundle the current
# pacman one LAST to override it — it's the build usb1.dll actually links
# against. mingw-w64-ucrt-x86_64-libusb comes in as a libgphoto2 dependency, so no
# separate download/extract of an upstream libusb release is needed.
pyinstaller --onedir \
    --add-binary "$IOLIB_DIR":. \
    --add-binary "$CAMLIB_DIR":. \
    --add-binary "$CV2_PYD":. \
    --add-binary /ucrt64/bin/libusb-1.0.dll:. \
    --add-data vcamera-sources/vusb2:vcamera-sources/vusb \
    --add-data vcamera-sources/vusb2:vcamera-sources/vusb2 \
    --add-data ui:ui \
    --add-data i18n:i18n \
    --add-data cceh-dome-template.lp:. \
    --add-data dome_presets:dome_presets main.py \
    --runtime-hook ./build_runtime_hook.py \
    --icon ui/icon/app_icon.ico \
    --noconfirm \
    --name byzanz-capture
