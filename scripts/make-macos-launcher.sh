#!/bin/bash
# Build a double-clickable macOS .app that launches CCeH Crocodile Capture
# (the papyri app) from THIS repo's live source + .venv — single Dock tile,
# app icon, proper name, no Rosetta prompt.
#
# Uses py2app in ALIAS mode (-A): nothing is frozen, the bundle runs the live
# source tree, so code changes show up on the next launch. py2app's native
# stub IS the app process, so the Dock shows one "CCeH Crocodile Capture"
# tile (not a separate "Python" process like a shell/applet wrapper would).
#
# Usage:
#   scripts/make-macos-launcher.sh [destination-dir] [name-suffix]
#   (defaults: ~/Desktop, no suffix)
#
# The optional name-suffix (a short token like "ALT") produces
# "CCeH Crocodile Capture (ALT).app" with its own bundle identifier —
# used for the desktop icon of the pinned fallback worktree, so the
# regular and the fallback launcher can coexist. 
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO/.venv"
APP_NAME="CCeH Crocodile Capture"
DEST="${1:-$HOME/Desktop}"
SUFFIX="${2:-}"

if [ -n "$SUFFIX" ]; then
    APP_NAME="$APP_NAME ($SUFFIX)"
    # setup.py reads this and adjusts CFBundleName/DisplayName/Identifier
    # to match — py2app names the bundle after CFBundleName, so the
    # dist/"$APP_NAME".app path below stays in sync.
    export CROC_APP_SUFFIX="$SUFFIX"
fi

if [ ! -x "$VENV/bin/python" ]; then
    echo "error: venv python not found at $VENV/bin/python" >&2
    exit 1
fi

cd "$REPO"
"$VENV/bin/python" -c "import py2app" 2>/dev/null || "$VENV/bin/python" -m pip install py2app

# Alias build (absolute paths baked in → the .app can live anywhere and still
# reference this repo + venv).
rm -rf build "dist/$APP_NAME.app"
"$VENV/bin/python" setup.py py2app -A >/dev/null

rm -rf "$DEST/$APP_NAME.app"
cp -R "dist/$APP_NAME.app" "$DEST/"
rm -rf build dist

touch "$DEST/$APP_NAME.app"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
    -f "$DEST/$APP_NAME.app" 2>/dev/null || true
echo "Created: $DEST/$APP_NAME.app"
