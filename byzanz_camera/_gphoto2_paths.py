"""Resolve `CAMLIBS` / `IOLIBS` after `import gphoto2`.

`gphoto2/__init__.py` rewrites `CAMLIBS` and `IOLIBS` to package-internal
directories on every import. When `python-gphoto2` was built from sdist
(`pip install ... --no-binary :all:`) those directories ship only port
libs — no camera drivers — so the rewrite silently breaks autodetect.

This module decides which paths *should* win and applies them right
after `import gphoto2`. Precedence (highest first):

  0. `BYZANZ_GPHOTO2_USE_BUNDLED=1`   — escape hatch, trust gphoto2's
     rewrite (i.e. accept whatever the installed wheel ships).
  1. `sys.frozen` (PyInstaller)       — trust the runtime hook
     (`build_win_hook.py`), which pointed both vars at `sys._MEIPASS`
     before any Python code ran.
  2. Pre-import env (`CAMLIBS` /
     `IOLIBS` set in the shell or by   — restore the user's choice; the
     PyCharm / build_win_hook)          rewrite at import time clobbered it.
  3. Repo-local vendor build at
     `vendor/build/lib/libgphoto2/*`   — for collaborators who ran
     and `..._port/*`                    `scripts/bootstrap-gphoto2.sh`.
  4. Fall through                      — leave the rewrite in place
                                         (system default behavior).

The caller MUST capture pre-import env vars BEFORE `import gphoto2`,
since the import wipes them — then pass the captured values to
`apply_paths`. See the dance at the top of `papyri/main.py` and
`main.py`.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

_logger = logging.getLogger(__name__)

_KILL_SWITCH_VAR = "BYZANZ_GPHOTO2_USE_BUNDLED"


def apply_paths(pre_camlibs: str | None, pre_iolibs: str | None) -> None:
    """Apply the resolved CAMLIBS/IOLIBS per the precedence in this
    module's docstring. Call AFTER `import gphoto2`. `pre_camlibs` /
    `pre_iolibs` are the values captured from the env BEFORE that
    import (None if unset).

    Logs a single INFO line naming which source won, so a failed
    autodetect later in the run can be triaged against the resolved
    path."""

    # 0. Kill switch — skip everything.
    if os.environ.get(_KILL_SWITCH_VAR) == "1":
        _logger.info("gphoto2 paths: bundled (%s=1)", _KILL_SWITCH_VAR)
        return

    # 1. Frozen bundle — the runtime hook (build_win_hook.py) set env
    #    to sys._MEIPASS before any imports. gphoto2's import-time
    #    rewrite still happened, but inside a PyInstaller onedir bundle
    #    the gphoto2 package sits inside _MEIPASS too, so we restore
    #    explicitly to keep the contract identical to the dev path.
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            os.environ["CAMLIBS"] = meipass
            os.environ["IOLIBS"] = meipass
        _logger.info("gphoto2 paths: frozen bundle (%s)", meipass)
        return

    # 2. Pre-import env — user explicit override.
    if pre_camlibs and pre_iolibs:
        os.environ["CAMLIBS"] = pre_camlibs
        os.environ["IOLIBS"] = pre_iolibs
        _logger.info("gphoto2 paths: env (CAMLIBS=%s, IOLIBS=%s)",
                     pre_camlibs, pre_iolibs)
        return

    # 3. Vendor build alongside this repo.
    vendor = _resolve_vendor_paths()
    if vendor is not None:
        cam, io = vendor
        os.environ["CAMLIBS"] = cam
        os.environ["IOLIBS"] = io
        _logger.info("gphoto2 paths: vendor build (CAMLIBS=%s, IOLIBS=%s)",
                     cam, io)
        return

    # 4. Fall through — gphoto2's rewrite stays. Camera detection may
    #    still work if the installed wheel happens to ship drivers
    #    (Linux distro builds, prebuilt wheels). The INFO line makes
    #    it easy to spot when this path was taken in a bug report.
    cam = os.environ.get("CAMLIBS")
    io = os.environ.get("IOLIBS")
    _logger.info("gphoto2 paths: bundled fallback (CAMLIBS=%s, IOLIBS=%s)",
                 cam, io)


def _resolve_vendor_paths() -> tuple[str, str] | None:
    """Look for a repo-local libgphoto2 build at
    `vendor/build/lib/libgphoto2/<ver>` and
    `vendor/build/lib/libgphoto2_port/<ver>`. Returns (camlibs,
    iolibs) for the highest version found in each, or None if either
    base directory is missing/empty."""
    repo_root = Path(__file__).resolve().parents[1]
    cam_base = repo_root / "vendor" / "build" / "lib" / "libgphoto2"
    io_base = repo_root / "vendor" / "build" / "lib" / "libgphoto2_port"
    if not (cam_base.is_dir() and io_base.is_dir()):
        return None
    cam_versions = sorted(p for p in cam_base.iterdir() if p.is_dir())
    io_versions = sorted(p for p in io_base.iterdir() if p.is_dir())
    if not (cam_versions and io_versions):
        return None
    # Lexicographic max works for semver-shaped names like "2.5.33.1".
    return str(cam_versions[-1]), str(io_versions[-1])
