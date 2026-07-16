#!/bin/bash
# Set up the MSYS2/MINGW64 build environment for byzanz-capture on the
# Windows test VM and build the distributable bundle. Run phases
# individually from any shell (the script establishes the MINGW64
# environment itself, so it also works over `ssh -> cmd -> bash`):
#
#   ./scripts/vm-win-setup.sh sync    # full pacman upgrade (stale DB is unsafe to install against)
#   ./scripts/vm-win-setup.sh deps    # install/upgrade all MINGW64 packages the RTI app needs
#   ./scripts/vm-win-setup.sh venv    # recreate .venv and pip-install the pure-python + sdist deps
#   ./scripts/vm-win-setup.sh build   # run build_win.sh (PyInstaller onedir bundle into dist/)
#
# Each phase self-logs to /tmp/vmsetup-<phase>.log via tee.
#
# Notes:
# - requirements.txt pins wheel versions (PyQt6~=6.11, numpy 2.4, ...) that do
#   not exist for MINGW64 python; binary deps come from pacman instead. Do NOT
#   `pip install -r requirements.txt` here.
# - rawpy has no MINGW64 wheel/package: pip builds it from sdist against
#   pacman's libraw (needs gcc + pkg-config).

# --- establish the MINGW64 environment (do NOT rely on cmd `set MSYSTEM=`) ---
export MSYSTEM=MINGW64
source /etc/profile
set -euo pipefail

PHASE="${1:-}"
cd "$(dirname "$0")/.."   # repo root

exec > >(tee "/tmp/vmsetup-${PHASE:-none}.log") 2>&1

PACKAGES=(
    mingw-w64-x86_64-python
    mingw-w64-x86_64-python-pip
    mingw-w64-x86_64-gcc             # for the gphoto2 sdist build (the only pip source build left)
    mingw-w64-x86_64-pkgconf         # gphoto2 sdist build locates libgphoto2 via pkg-config
    mingw-w64-x86_64-libgphoto2
    mingw-w64-x86_64-qt6-base
    mingw-w64-x86_64-python-pyqt6
    mingw-w64-x86_64-python-pillow
    mingw-w64-x86_64-python-numpy
    mingw-w64-x86_64-python-opencv   # the python cv2 bindings (pulls opencv C++ lib as dep;
                                     # the plain `opencv` package ships only headers+DLLs, no cv2)
    mingw-w64-x86_64-python-psutil
    mingw-w64-x86_64-python-rawpy    # prebuilt (pulls libraw) — no sdist compile
    mingw-w64-x86_64-python-qasync
    mingw-w64-x86_64-python-send2trash
)

case "$PHASE" in
sync)
    pacman -Syuu --noconfirm
    ;;
deps)
    pacman -S --needed --noconfirm "${PACKAGES[@]}"
    ;;
venv)
    rm -rf .venv
    python -m venv --system-site-packages .venv
    source .venv/bin/activate
    python -m pip install --upgrade pip
    # gphoto2 is the only dependency with no MSYS2 package: build it from sdist
    # against pacman's libgphoto2. --no-build-isolation reuses the pacman
    # setuptools (visible via --system-site-packages) instead of pip building an
    # isolated env. Everything else binary (rawpy, qasync, send2trash, numpy,
    # opencv, pillow, psutil, pyqt6) comes from pacman; only pyinstaller and
    # piexif are pure-python pip packages with no MSYS2 build.
    pip install setuptools wheel
    pip install gphoto2 --no-binary :all: --no-build-isolation
    pip install pyinstaller piexif
    echo "=== installed ==="
    pip list 2>/dev/null | grep -iE "gphoto2|rawpy|pyinstaller|qasync|send2trash|piexif|numpy|opencv" || true
    ;;
build)
    source .venv/bin/activate
    ./build_win.sh
    ;;
*)
    echo "usage: $0 {sync|deps|venv|build}" >&2
    exit 1
    ;;
esac
echo "PHASE_${PHASE}_DONE exit=$?"
