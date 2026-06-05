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

# Tools required on the PATH.
PREREQ_CMDS=(git meson ninja pkg-config autoconf automake libtool)

# Libraries required by libgphoto2 — checked via pkg-config rather than
# by command, since they're shared libs without a CLI. Discovered the
# hard way: missing `gdlib` (Homebrew `gd`) only surfaced at `meson
# setup` with "dependency gdlib not found", not from a missing command.
PREREQ_PKGS=(libxml-2.0 libcurl gdlib libexif libjpeg libtiff-4 libusb-1.0)

check_prereqs() {
    # Newline-separated strings rather than arrays — macOS ships Bash
    # 3.2 forever (Apple won't ship GPLv3 Bash 4+), and Bash 3.2 trips
    # over both combined `local arr1=() arr2=()` declarations AND
    # iterating empty `"${arr[@]}"` under `set -u`.
    local missing_cmds=""
    local missing_pkgs=""
    for cmd in "${PREREQ_CMDS[@]}"; do
        command -v "$cmd" >/dev/null 2>&1 || missing_cmds="${missing_cmds}${cmd}
"
    done
    if command -v pkg-config >/dev/null 2>&1; then
        for pkg in "${PREREQ_PKGS[@]}"; do
            pkg-config --exists "$pkg" 2>/dev/null || missing_pkgs="${missing_pkgs}${pkg}
"
        done
    fi
    if [ -z "$missing_cmds" ] && [ -z "$missing_pkgs" ]; then
        return
    fi
    echo "Missing prerequisites:" >&2
    [ -n "$missing_cmds" ] && printf '%s' "$missing_cmds" | sed 's/^/  - /; s/$/ (command)/' >&2
    [ -n "$missing_pkgs" ] && printf '%s' "$missing_pkgs" | sed 's/^/  - /; s/$/ (pkg-config)/' >&2
    echo >&2
    case "$PLATFORM" in
        Darwin)
            echo "Install with:" >&2
            echo "  brew install autoconf automake libtool gettext libusb pkg-config meson ninja \\" >&2
            echo "              libxml2 curl gd libexif jpeg-turbo libtiff" >&2
            ;;
        Linux)
            echo "Install with (Debian / Ubuntu):" >&2
            echo "  sudo apt install build-essential autoconf automake libtool libltdl-dev \\" >&2
            echo "                   libusb-1.0-0-dev gettext pkg-config meson ninja-build \\" >&2
            echo "                   libxml2-dev libcurl4-openssl-dev libgd-dev libexif-dev \\" >&2
            echo "                   libjpeg-dev libtiff-dev" >&2
            ;;
        *)
            echo "Install the listed tools with your package manager, then re-run." >&2
            ;;
    esac
    exit 1
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
        # Two Homebrew-on-macOS problems to compensate for here:
        #
        # 1. `gettext` (libintl) and `libtool` (libltdl) are keg-only, so the
        #    compiler/linker don't see them by default.
        #
        # 2. libgphoto2's meson build has a dependency-propagation gap: the
        #    public `libgphoto2_dep` that the camlibs consume (see
        #    libgphoto2/meson.build) re-exports libgphoto2_port_dep and
        #    config_dep but NOT libexif_dep. Several camlibs (canon,
        #    directory, ptp2, ...) `#include <libexif/exif-data.h>` yet never
        #    receive libexif's include path. On Linux this is masked because
        #    libexif sits in /usr/include; on Homebrew it lives under the
        #    Cellar, which clang doesn't search by default -> "file not
        #    found". The same can affect the other pkg-config deps, so inject
        #    every prereq's include/lib path globally rather than chase them
        #    one camlib at a time.
        local gettext_prefix libtool_prefix pkg_cflags pkg_libs
        gettext_prefix="$(brew --prefix gettext)"
        libtool_prefix="$(brew --prefix libtool)"
        pkg_cflags=""
        pkg_libs=""
        for pkg in "${PREREQ_PKGS[@]}"; do
            pkg_cflags="${pkg_cflags} $(pkg-config --cflags "$pkg" 2>/dev/null)"
            pkg_libs="${pkg_libs} $(pkg-config --libs-only-L "$pkg" 2>/dev/null)"
        done
        export CPPFLAGS="-I${gettext_prefix}/include -I${libtool_prefix}/include${pkg_cflags} ${CPPFLAGS:-}"
        export LDFLAGS="-L${gettext_prefix}/lib -L${libtool_prefix}/lib${pkg_libs} ${LDFLAGS:-}"
    fi

    # `usbdiskdirect` / `usbscsi` iolibs are Linux-only; meson errors
    # out on macOS if the default list is used. Pass an explicit list
    # that's safe on both platforms.
    local meson_args=(
        --prefix="$BUILD_PREFIX"
        -Diolibs=disk,ptpip,serial,libusb1,usb
    )
    if [ -d build ]; then
        # Re-run with --wipe rather than --reconfigure. meson only reads
        # CFLAGS/CPPFLAGS/LDFLAGS from the environment on a *fresh* configure;
        # --reconfigure keeps the args baked in at the first setup, so a
        # changed CPPFLAGS (e.g. a newly added include path above, or a stale
        # build/ left by an earlier failed run) would silently not apply.
        # --wipe re-reads the environment while preserving meson's saved
        # command-line options.
        meson setup build "${meson_args[@]}" --wipe
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
