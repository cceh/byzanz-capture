#!/bin/bash
# Build the distributable macOS bundle (.app + .dmg) of the RTI app
# (main.py). Same phase layout as scripts/vm-win-setup.sh, so CI and a
# local build stay in lockstep:
#
#   ./scripts/mac-setup.sh deps       # brew: everything libgphoto2 needs to build
#   ./scripts/mac-setup.sh venv       # recreate .venv-mac and pip-install requirements-rti.txt
#   ./scripts/mac-setup.sh gphoto2    # build the vendored libgphoto2 fork + python-gphoto2
#   ./scripts/mac-setup.sh build      # PyInstaller windowed bundle into dist/
#   ./scripts/mac-setup.sh smoketest  # launch the frozen app headless; fail on a startup crash
#   ./scripts/mac-setup.sh dmg        # pack the .app into a compressed disk image
#
# Every binary dependency here is a PyPI wheel except libgphoto2: the
# bundle ships the vendored fork (vendor/libgphoto2, built by
# scripts/bootstrap-gphoto2.sh), not Homebrew's. Only the fork carries
# the vusb port driver, so the frozen app offers the virtual camera the
# same way a dev checkout does — and its gphoto2 wheels ship no camera
# drivers at all. The drivers are bundled from vendor/build, which is
# why the runtime hook that points CAMLIBS/IOLIBS at sys._MEIPASS is
# shared with the Windows build.
#
# The result is NOT code-signed or notarized: macOS quarantines it on
# first open. Either right-click → Open once, or run
# `xattr -dr com.apple.quarantine "/Applications/byzanz-capture.app"`.
set -euo pipefail

PHASE="${1:-}"
cd "$(dirname "$0")/.."   # repo root

VENV=".venv-mac"
APP="dist/byzanz-capture.app"

case "$PHASE" in
deps)
    # The build prerequisites bootstrap-gphoto2.sh checks for — it only
    # prints this list, so keep the two in sync.
    brew install autoconf automake libtool gettext libusb pkg-config meson ninja \
                 libexif jpeg-turbo
    ;;
venv)
    rm -rf "$VENV"
    python3 -m venv "$VENV"
    source "$VENV/bin/activate"
    python -m pip install --upgrade pip
    pip install -r requirements-rti.txt
    pip install pyinstaller
    # python-gphoto2 is installed by the gphoto2 phase, built against the
    # fork — a wheel here would be replaced there anyway.
    ;;
gphoto2)
    source "$VENV/bin/activate"
    ./scripts/bootstrap-gphoto2.sh
    ;;
build)
    source "$VENV/bin/activate"
    PREFIX="vendor/build"
    [ -d "$PREFIX/lib/libgphoto2" ] || { echo "build: $PREFIX missing — run: $0 gphoto2"; exit 1; }
    # Versioned driver directories — a libgphoto2 version bump moves them.
    CAMLIB_DIR=$(ls -d "$PREFIX"/lib/libgphoto2/*/ | sort -V | tail -1)
    IOLIB_DIR=$(ls -d "$PREFIX"/lib/libgphoto2_port/*/ | sort -V | tail -1)
    echo "Using camlibs: $CAMLIB_DIR"
    echo "Using iolibs:  $IOLIB_DIR"
    rm -rf build dist
    # vcamera-sources: the vusb driver's image material. Its compiled-in
    # default directory is an absolute path on THIS machine, so a bundle
    # would find nothing there — byzanz_camera._gphoto2_paths points
    # VCAMERADIR* at these bundled folders instead. Only vusb2's material
    # is committed; both virtual cameras serve it.
    pyinstaller --windowed \
        --add-binary "$CAMLIB_DIR:." \
        --add-binary "$IOLIB_DIR:." \
        --add-data vcamera-sources/vusb2:vcamera-sources/vusb \
        --add-data vcamera-sources/vusb2:vcamera-sources/vusb2 \
        --add-data ui:ui \
        --add-data i18n:i18n \
        --add-data cceh-dome-template.lp:. \
        --add-data dome_presets:dome_presets main.py \
        --runtime-hook ./build_runtime_hook.py \
        --icon ui/icon/app_icon.icns \
        --osx-bundle-identifier de.uni-koeln.cceh.byzanz-capture \
        --noconfirm \
        --name byzanz-capture
    # macOS kills an app that touches Bluetooth without declaring why — the
    # dome controller is driven over BLE, so without this the app dies with
    # SIGKILL (termination namespace TCC) the moment it looks for the dome.
    # PyInstaller cannot set arbitrary Info.plist keys from the command line;
    # plutil -replace inserts the key if it is missing, so this is idempotent.
    plutil -replace NSBluetoothAlwaysUsageDescription \
        -string "byzanz-capture drives the RTI dome's light controller over Bluetooth." \
        "$APP/Contents/Info.plist"
    ;;
smoketest)
    # Launch the frozen app headless and verify it starts up cleanly. A
    # missing PyInstaller inclusion (module, Qt plugin, or bundled ui/ /
    # dome_presets/ data) crashes at startup with a non-zero exit. The app
    # self-quits ~5s after init (BYZANZ_SMOKE_TEST); a hang is caught by
    # the CI job timeout (macOS ships no coreutils `timeout`).
    EXE="$APP/Contents/MacOS/byzanz-capture"
    [ -x "$EXE" ] || { echo "smoke test: $EXE not found — run the build phase first"; exit 1; }
    export QT_QPA_PLATFORM=offscreen BYZANZ_SMOKE_TEST=1
    set +e
    out="$("$EXE" 2>&1)"; rc=$?
    set -e
    echo "$out"
    if [ "$rc" -ne 0 ]; then
        echo "SMOKE TEST FAILED (exit=$rc)"; exit 1
    fi
    if echo "$out" | grep -qiE "Fatal Python error|could not (find|load) the Qt platform|ModuleNotFoundError|Library not loaded"; then
        echo "SMOKE TEST FAILED (crash marker in output)"; exit 1
    fi
    # The bundle must declare its Bluetooth use — see the build phase. A
    # launch alone does not prove it: macOS only kills the app once BLE is
    # actually touched, which depends on the configured dome.
    if ! plutil -p "$APP/Contents/Info.plist" | grep -q NSBluetoothAlwaysUsageDescription; then
        echo "SMOKE TEST FAILED (Info.plist has no NSBluetoothAlwaysUsageDescription)"; exit 1
    fi
    echo "SMOKE TEST OK"
    ;;
dmg)
    [ -d "$APP" ] || { echo "dmg: $APP not found — run the build phase first"; exit 1; }
    # Version: date + short commit (CI provides GITHUB_SHA; local git fallback).
    SHA="${GITHUB_SHA:-$(git rev-parse HEAD 2>/dev/null || echo unknown)}"
    VERSION="$(date +%Y.%m.%d)-${SHA:0:7}"
    DMG="dist/byzanz-capture-$VERSION.dmg"
    echo "dmg: version $VERSION"
    # Stage the .app next to an /Applications symlink, so the mounted
    # image offers the usual drag-to-install gesture.
    STAGE="$(mktemp -d)"
    cp -R "$APP" "$STAGE/"
    ln -s /Applications "$STAGE/Applications"
    rm -f "$DMG"
    # "hdiutil: create failed - Resource busy" happens on CI runners when
    # something (Spotlight, antivirus-alikes) still holds the staged tree.
    # It passes on the next attempt.
    for attempt in 1 2 3; do
        hdiutil create -volname "byzanz-capture" -srcfolder "$STAGE" \
            -ov -format UDZO "$DMG" && break
        echo "dmg: attempt $attempt failed, retrying in 10s"
        sleep 10
    done
    [ -f "$DMG" ] || { echo "dmg: could not create the image"; exit 1; }
    rm -rf "$STAGE"
    ls -la "$DMG"
    ;;
*)
    echo "usage: $0 {deps|venv|build|smoketest|dmg}" >&2
    exit 1
    ;;
esac
echo "PHASE_${PHASE}_DONE exit=$?"
