#!/usr/bin/env bash
#
# Build libgphoto2 from the vendored submodule and rebuild python-gphoto2
# against it. Cross-platform (macOS + Linux). Idempotent: safe to re-run.
#
# Usage:
#     source venv/bin/activate
#     scripts/bootstrap-gphoto2.sh
#
# After this, `python main.py` / `python -m papyri.main` will resolve
# CAMLIBS / IOLIBS to vendor/build/ automatically (see
# byzanz_camera/_gphoto2_paths.py).
#
# Override with `export BYZANZ_GPHOTO2_USE_BUNDLED=1` to disable the
# resolver and trust whatever python-gphoto2 ships — useful for
# bisecting driver issues.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC_DIR="$REPO_ROOT/vendor/libgphoto2"
BUILD_PREFIX="$REPO_ROOT/vendor/build"

PLATFORM="$(uname -s)"

# ---- prereqs --------------------------------------------------------

# Tools required on the PATH. libusb / gettext / libtool come from
# system packages (handled below) and are checked via pkg-config.
PREREQ_CMDS=(git meson ninja pkg-config autoconf automake libtool)

missing_cmds() {
    local missing=()
    for cmd in "${PREREQ_CMDS[@]}"; do
        command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
    done
    printf '%s\n' "${missing[@]:-}"
}

check_prereqs() {
    local missing
    missing="$(missing_cmds)"
    if [ -n "$missing" ]; then
        echo "Missing prerequisites:" >&2
        echo "$missing" | sed 's/^/  - /' >&2
        echo >&2
        case "$PLATFORM" in
            Darwin)
                echo "Install with:" >&2
                echo "  brew install autoconf automake libtool gettext libusb pkg-config meson ninja" >&2
                ;;
            Linux)
                echo "Install with (Debian / Ubuntu):" >&2
                echo "  sudo apt install build-essential autoconf automake libtool libltdl-dev libusb-1.0-0-dev gettext pkg-config meson ninja-build" >&2
                ;;
            *)
                echo "Install the listed tools with your package manager, then re-run." >&2
                ;;
        esac
        exit 1
    fi
}

# ---- submodule ------------------------------------------------------

ensure_submodule() {
    # If `vendor/libgphoto2` is empty (fresh clone without --recursive)
    # or doesn't exist, init the submodule.
    if [ ! -e "$SRC_DIR/meson.build" ]; then
        echo ">> Initializing vendor/libgphoto2 submodule..."
        git -C "$REPO_ROOT" submodule update --init vendor/libgphoto2
    fi
}

# ---- build libgphoto2 ----------------------------------------------

build_libgphoto2() {
    echo ">> Building libgphoto2 into $BUILD_PREFIX..."
    cd "$SRC_DIR"

    if [ "$PLATFORM" = "Darwin" ]; then
        # Homebrew keeps `gettext` (libintl) and `libtool` (libltdl) keg-only
        # so the linker doesn't find them by default. Point LDFLAGS/CPPFLAGS
        # at the kegs explicitly.
        local gettext_prefix libtool_prefix
        gettext_prefix="$(brew --prefix gettext)"
        libtool_prefix="$(brew --prefix libtool)"
        export LDFLAGS="-L${gettext_prefix}/lib -L${libtool_prefix}/lib ${LDFLAGS:-}"
        export CPPFLAGS="-I${gettext_prefix}/include -I${libtool_prefix}/include ${CPPFLAGS:-}"
    fi

    # `usbdiskdirect` / `usbscsi` iolibs are Linux-only; meson errors
    # out on macOS if the default list is used. Pass an explicit list
    # that's safe on both platforms.
    local meson_args=(
        --prefix="$BUILD_PREFIX"
        -Diolibs=disk,ptpip,serial,libusb1,usb
    )
    if [ -d build ]; then
        meson setup build "${meson_args[@]}" --reconfigure
    else
        meson setup build "${meson_args[@]}"
    fi
    meson compile -C build
    meson install -C build
}

# ---- rebuild python-gphoto2 ----------------------------------------

rebuild_python_gphoto2() {
    if [ -z "${VIRTUAL_ENV:-}" ]; then
        echo "ERROR: activate the project venv before running this script." >&2
        echo "  source venv/bin/activate" >&2
        exit 1
    fi

    echo ">> Rebuilding python-gphoto2 against $BUILD_PREFIX..."
    pip uninstall -y gphoto2 >/dev/null 2>&1 || true
    GPHOTO2_ROOT="$BUILD_PREFIX" pip install gphoto2 \
        --no-binary :all: --force-reinstall --no-cache-dir

    # The fresh install re-creates `gphoto2/libgphoto2/{camlibs,iolibs}`
    # inside site-packages — directories that gphoto2/__init__.py uses
    # to rewrite CAMLIBS/IOLIBS on every import. On sdist builds they
    # contain only port libraries (no camera drivers), which silently
    # breaks autodetect. Our resolver (byzanz_camera/_gphoto2_paths.py)
    # restores the correct paths after the rewrite, so the rewrite
    # itself is harmless — we leave these dirs alone here.
}

# ---- verify ---------------------------------------------------------

verify() {
    echo ">> Verifying..."
    cd "$REPO_ROOT"
    python - <<'PY'
import os, sys
_pre = (os.environ.get("CAMLIBS"), os.environ.get("IOLIBS"))
import gphoto2 as gp
from byzanz_camera._gphoto2_paths import apply_paths
apply_paths(*_pre)
ver, *_ = gp.gp_library_version(gp.GP_VERSION_VERBOSE)
print(f"  libgphoto2 version: {ver}")
print(f"  CAMLIBS: {os.environ.get('CAMLIBS')}")
print(f"  IOLIBS:  {os.environ.get('IOLIBS')}")
print(f"  autodetect: {list(gp.Camera.autodetect())}")
PY
}

# ---- main -----------------------------------------------------------

check_prereqs
ensure_submodule
build_libgphoto2
rebuild_python_gphoto2
verify

echo
echo "Done. The vendor build lives at: $BUILD_PREFIX"
echo "The resolver will pick it up automatically on next app start."
