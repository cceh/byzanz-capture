#!/bin/bash
# Set up the MSYS2/UCRT64 build environment for byzanz-capture on the
# Windows test VM and build the distributable bundle. Run phases
# individually from any shell (the script establishes the UCRT64
# environment itself, so it also works over `ssh -> cmd -> bash`):
#
#   ./scripts/vm-win-setup.sh sync    # full pacman upgrade (stale DB is unsafe to install against)
#   ./scripts/vm-win-setup.sh deps    # install/upgrade all UCRT64 packages the RTI app needs
#   ./scripts/vm-win-setup.sh venv    # recreate .venv and pip-install the pure-python + sdist deps
#   ./scripts/vm-win-setup.sh gphoto2 # build the vendored libgphoto2 fork + python-gphoto2
#   ./scripts/vm-win-setup.sh build   # run build_win.sh (PyInstaller onedir bundle into dist/)
#   ./scripts/vm-win-setup.sh smoketest  # launch the frozen bundle headless; fail on a startup crash
#   ./scripts/vm-win-setup.sh run     # run the app from source (python main.py) for development
#
# Each phase self-logs to /tmp/vmsetup-<phase>.log via tee.
#
# Notes:
# - requirements.txt pins wheel versions (PyQt6~=6.11, numpy 2.4, ...) that do
#   not exist for UCRT64 python; binary deps come from pacman instead. Do NOT
#   `pip install -r requirements.txt` here.
# - libgphoto2 itself is built from the vendored fork (vendor/libgphoto2) by
#   the gphoto2 phase: only the fork carries the vusb port driver, so the
#   frozen app offers the virtual camera. pacman's libgphoto2 is still
#   installed — it pulls exactly the libraries that build needs.
# - python-gphoto2 is the only pip source build left: that same phase compiles
#   it against the fork (needs gcc + pkg-config).

# --- establish the UCRT64 environment (do NOT rely on cmd `set MSYSTEM=`) ---
export MSYSTEM=UCRT64
source /etc/profile
set -euo pipefail

PHASE="${1:-}"
cd "$(dirname "$0")/.."   # repo root

exec > >(tee "/tmp/vmsetup-${PHASE:-none}.log") 2>&1

PACKAGES=(
    mingw-w64-ucrt-x86_64-python
    mingw-w64-ucrt-x86_64-python-pip
    mingw-w64-ucrt-x86_64-gcc               # python-gphoto2 is built from sdist
    mingw-w64-ucrt-x86_64-pkgconf           # ... and locates libgphoto2 via pkg-config
    # --- what the vendored libgphoto2 fork needs to build (one entry per
    #     dependency its meson build looks for in OUR configuration:
    #     camlibs=ptp2, iolibs=disk,vusb,ptpip,libusb1).
    #     pacman's own libgphoto2 is deliberately NOT installed: the bundle
    #     must ship the fork, and a second libgphoto2-6.dll in /ucrt64/bin
    #     is exactly what PyInstaller would find first. ---
    mingw-w64-ucrt-x86_64-meson             # the fork's build system
    mingw-w64-ucrt-x86_64-ninja
    mingw-w64-ucrt-x86_64-libltdl           # libgphoto2 dlopens its drivers through ltdl
    mingw-w64-ucrt-x86_64-libsystre         # POSIX regex — not in the Windows C library
    mingw-w64-ucrt-x86_64-libusb            # libusb-1.0, for the libusb1 port driver
    mingw-w64-ucrt-x86_64-mman-win32        # Windows has no mmap; libgphoto2 links it from here
    mingw-w64-ucrt-x86_64-libexif           # optional, but ptp2 reads EXIF with it
    mingw-w64-ucrt-x86_64-libjpeg-turbo     # optional, thumbnail decoding
    mingw-w64-ucrt-x86_64-gettext           # libintl, linked by the camlibs
    autotools                               # autoconf/automake/libtool commands the fork's
                                            # build checks for (MSYS, not a mingw package)
    # --- the app itself ---
    mingw-w64-ucrt-x86_64-qt6-base
    mingw-w64-ucrt-x86_64-qt6-svg           # Qt6Svg.dll for PyQt6.QtSvg (SVG icon rendering);
                                            # NOT part of qt6-base, and python-pyqt6 doesn't
                                            # pull it — without it the frozen app crashes at
                                            # startup ("DLL load failed while importing QtSvg").
    mingw-w64-ucrt-x86_64-python-pyqt6
    mingw-w64-ucrt-x86_64-python-pillow
    mingw-w64-ucrt-x86_64-python-numpy
    mingw-w64-ucrt-x86_64-python-scipy      # sharpness erf fit (and its native DLLs)
    mingw-w64-ucrt-x86_64-python-opencv     # the python cv2 bindings (pulls opencv C++ lib as
                                            # dep; the plain `opencv` package ships only
                                            # headers+DLLs, no cv2)
    mingw-w64-ucrt-x86_64-python-psutil
    mingw-w64-ucrt-x86_64-python-rawpy      # prebuilt (pulls libraw) — no sdist compile
    mingw-w64-ucrt-x86_64-python-qasync
    mingw-w64-ucrt-x86_64-python-send2trash
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
    # Everything binary (rawpy, qasync, send2trash, numpy, scipy, opencv,
    # pillow, psutil, pyqt6) comes from pacman; only pyinstaller and piexif
    # are pure-python pip packages with no MSYS2 build. python-gphoto2 is
    # installed by the gphoto2 phase, compiled against the vendored fork.
    pip install setuptools wheel
    pip install pyinstaller piexif
    echo "=== installed ==="
    pip list 2>/dev/null | grep -iE "gphoto2|rawpy|pyinstaller|qasync|send2trash|piexif|numpy|scipy|opencv" || true
    ;;
gphoto2)
    source .venv/bin/activate
    ./scripts/bootstrap-gphoto2.sh
    ;;
build)
    source .venv/bin/activate
    ./build_win.sh
    ;;
smoketest)
    # Launch the frozen bundle headless and verify it starts up cleanly. A
    # missing PyInstaller inclusion (module, Qt plugin, or bundled ui/ /
    # dome_presets/ data) crashes at startup with a non-zero exit. The app
    # self-quits ~5s after init (BYZANZ_SMOKE_TEST); the timeout is only a
    # backstop against a hang (which would also fail the test).
    EXE="dist/byzanz-capture/byzanz-capture.exe"
    [ -x "$EXE" ] || { echo "smoke test: $EXE not found — run the build phase first"; exit 1; }
    export QT_QPA_PLATFORM=offscreen BYZANZ_SMOKE_TEST=1
    set +e
    out="$(timeout 120 "$EXE" 2>&1)"; rc=$?
    set -e
    echo "$out"
    if [ "$rc" -ne 0 ]; then
        echo "SMOKE TEST FAILED (exit=$rc)"; exit 1
    fi
    if echo "$out" | grep -qiE "Fatal Python error|could not (find|load) the Qt platform|DLL load failed|ModuleNotFoundError"; then
        echo "SMOKE TEST FAILED (crash marker in output)"; exit 1
    fi
    echo "SMOKE TEST OK"
    ;;
installer)
    # Compile a per-user Windows installer (setup.exe) from the onedir
    # bundle via Inno Setup — preinstalled on GitHub windows runners;
    # locally: winget install JRSoftware.InnoSetup
    EXE="dist/byzanz-capture/byzanz-capture.exe"
    [ -f "$EXE" ] || { echo "installer: $EXE not found — run the build phase first"; exit 1; }
    ISCC=""
    for candidate in "/c/Program Files (x86)/Inno Setup 6/ISCC.exe" \
                     "/c/Program Files/Inno Setup 6/ISCC.exe"; do
        [ -f "$candidate" ] && ISCC="$candidate" && break
    done
    [ -n "$ISCC" ] || { echo "installer: ISCC.exe not found — install Inno Setup 6 (winget install JRSoftware.InnoSetup)"; exit 1; }
    # Version: date + short commit (CI provides GITHUB_SHA; local git fallback).
    # BYZANZ_VERSION names the version for a published build (the release
    # workflow passes the tag); otherwise date + short commit.
    SHA="${GITHUB_SHA:-$(git rev-parse HEAD 2>/dev/null || echo unknown)}"
    VERSION="${BYZANZ_VERSION:-$(date +%Y.%m.%d)-${SHA:0:7}}"
    echo "installer: version $VERSION"
    "$ISCC" "-DAppVersion=$VERSION" scripts/win-installer.iss
    ls -la dist/installer/
    ;;
run)
    # Run the app from source (python main.py) — for developing on the Windows
    # machine without producing a frozen bundle. Needs `deps` + `venv` done once.
    #
    # python-gphoto2 is pip-built from sdist here, and its import rewrites
    # CAMLIBS/IOLIBS to package-internal dirs that ship NO camera drivers (only
    # port libs) — so autodetect would find nothing. Point both at the MINGW
    # system libgphoto2 as *Windows* paths (via cygpath -w, like the frozen
    # bundle uses sys._MEIPASS); byzanz_camera/_gphoto2_paths.py picks these up
    # as the pre-import env override so the drivers are found.
    source .venv/bin/activate
    CAMLIB_DIR=$(ls -d /ucrt64/lib/libgphoto2/*/ | sort -V | tail -1)
    IOLIB_DIR=$(ls -d /ucrt64/lib/libgphoto2_port/*/ | sort -V | tail -1)
    export CAMLIBS="$(cygpath -w "${CAMLIB_DIR%/}")"
    export IOLIBS="$(cygpath -w "${IOLIB_DIR%/}")"
    echo "CAMLIBS=$CAMLIBS"
    echo "IOLIBS=$IOLIBS"
    python main.py
    ;;
*)
    echo "usage: $0 {sync|deps|venv|gphoto2|build|smoketest|installer|run}" >&2
    exit 1
    ;;
esac
echo "PHASE_${PHASE}_DONE exit=$?"
