#!/bin/bash
# Build the distributable macOS bundle (.app + .dmg) of the RTI app
# (main.py). Same phase layout as scripts/vm-win-setup.sh, so CI and a
# local build stay in lockstep:
#
#   ./scripts/mac-setup.sh deps       # brew: libgphoto2 + pkg-config (for the gphoto2 sdist build)
#   ./scripts/mac-setup.sh venv       # recreate .venv-mac and pip-install requirements-rti.txt
#   ./scripts/mac-setup.sh build      # PyInstaller windowed bundle into dist/
#   ./scripts/mac-setup.sh smoketest  # launch the frozen app headless; fail on a startup crash
#   ./scripts/mac-setup.sh dmg        # pack the .app into a compressed disk image
#
# Unlike Windows (MSYS2 pacman), every binary dependency here is a PyPI
# wheel — only python-gphoto2 is built from sdist against Homebrew's
# libgphoto2, because its wheels ship no camera drivers. The drivers
# (camlibs/iolibs) are bundled from the Homebrew prefix, which is why
# the runtime hook that points CAMLIBS/IOLIBS at sys._MEIPASS is shared
# with the Windows build.
#
# The result is NOT code-signed or notarized: macOS quarantines it on
# first open. Either right-click → Open once, or run
# `xattr -dr com.apple.quarantine "/Applications/byzanz-capture.app"`.
set -euo pipefail

PHASE="${1:-}"
cd "$(dirname "$0")/.."   # repo root

VENV=".venv-mac"
APP="dist/byzanz-capture.app"

brew_prefix() {
    brew --prefix libgphoto2 2>/dev/null || {
        echo "libgphoto2 not installed — run: $0 deps" >&2
        exit 1
    }
}

case "$PHASE" in
deps)
    brew install libgphoto2 pkg-config
    ;;
venv)
    rm -rf "$VENV"
    python3 -m venv "$VENV"
    source "$VENV/bin/activate"
    python -m pip install --upgrade pip
    pip install -r requirements-rti.txt
    # gphoto2's wheels bundle a libgphoto2 without camera drivers, so
    # autodetect finds nothing. Build it against Homebrew's libgphoto2
    # (whose drivers the build phase bundles) instead.
    PKG_CONFIG_PATH="$(brew_prefix)/lib/pkgconfig" \
        pip install --force-reinstall --no-binary :all: gphoto2
    pip install pyinstaller
    ;;
build)
    source "$VENV/bin/activate"
    PREFIX="$(brew_prefix)"
    # Versioned driver directories — a brew upgrade of libgphoto2 moves them.
    CAMLIB_DIR=$(ls -d "$PREFIX"/lib/libgphoto2/*/ | sort -V | tail -1)
    IOLIB_DIR=$(ls -d "$PREFIX"/lib/libgphoto2_port/*/ | sort -V | tail -1)
    echo "Using camlibs: $CAMLIB_DIR"
    echo "Using iolibs:  $IOLIB_DIR"
    rm -rf build dist
    pyinstaller --windowed \
        --add-binary "$CAMLIB_DIR:." \
        --add-binary "$IOLIB_DIR:." \
        --add-data ui:ui \
        --add-data i18n:i18n \
        --add-data cceh-dome-template.lp:. \
        --add-data dome_presets:dome_presets main.py \
        --runtime-hook ./build_runtime_hook.py \
        --icon ui/icon/app_icon.icns \
        --osx-bundle-identifier de.uni-koeln.cceh.byzanz-capture \
        --noconfirm \
        --name byzanz-capture
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
    hdiutil create -volname "byzanz-capture" -srcfolder "$STAGE" \
        -ov -format UDZO "$DMG"
    rm -rf "$STAGE"
    ls -la "$DMG"
    ;;
*)
    echo "usage: $0 {deps|venv|build|smoketest|dmg}" >&2
    exit 1
    ;;
esac
echo "PHASE_${PHASE}_DONE exit=$?"
