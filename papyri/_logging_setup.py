"""Logging, crash reporting and exception handling for papyri.

Call `install()` first thing in papyri/main.py — before the gphoto2
import, so the resolver's log lines are captured.

What it sets up:
  - stderr logging (as before) PLUS a rotating log file in the
    platform log directory (~/Library/Logs/PapyriCapture on macOS).
  - `faulthandler` into a separate crash.log: on a C-level crash
    (e.g. a segfault inside libgphoto2) the OS kills the process, but
    faulthandler first dumps the Python stack of every thread there.
  - `sys.excepthook`: PyQt6 aborts the whole process on an unhandled
    exception in a slot *only when* sys.excepthook is the default —
    installing our own prevents the abort. Ours logs the traceback
    and shows a dialog, then lets the app keep running (session data
    on disk is safe by design; most slot errors are recoverable).
  - `threading.excepthook`: log-only, for non-Qt helper threads.

Diagnosing in the field: ask the operator for the contents of the
log directory (printed at startup, visible in Console.app on macOS).
Set PAPYRI_DEBUG=1 to capture DEBUG-level detail in the file.
"""
from __future__ import annotations

import faulthandler
import logging
import os
import sys
import threading
import time
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_MAX_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 5

_logger = logging.getLogger("papyri")

# Dialog shown once per unique crash location; repeats only logged,
# and identical tracebacks are rate-limited so a failure inside the
# live-view loop (~20 Hz) can't churn the log rotation.
_seen_dialog_keys: set[tuple] = set()
_last_logged: dict[tuple, float] = {}
_LOG_REPEAT_INTERVAL_S = 10.0


def log_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "PapyriCapture"
    return Path.home() / ".papyri-capture" / "logs"


def install() -> None:
    directory = log_dir()
    directory.mkdir(parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(
        directory / "papyri.log",
        maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8",
    )
    file_handler.setLevel(
        logging.DEBUG if os.environ.get("PAPYRI_DEBUG") == "1"
        else logging.INFO
    )

    stderr_handler = logging.StreamHandler()

    logging.basicConfig(
        level=logging.DEBUG,  # root passes everything; handlers filter
        format=_LOG_FORMAT,
        handlers=[stderr_handler, file_handler],
    )
    # stderr stays at INFO regardless of PAPYRI_DEBUG (DEBUG detail is
    # for the file; the console would just scroll it away).
    stderr_handler.setLevel(logging.INFO)

    _enable_faulthandler(directory / "crash.log")
    sys.excepthook = _excepthook
    threading.excepthook = _threading_excepthook

    _logger.info("=== papyri start · python %s · logs: %s ===",
                 sys.version.split()[0], directory)


def _enable_faulthandler(crash_path: Path) -> None:
    # Append mode + session marker: the file only receives content on
    # an actual crash, so it stays tiny and never needs rotation. The
    # handle must stay open for the lifetime of the process.
    crash_file = open(crash_path, "a", encoding="utf-8")
    crash_file.write(f"--- session start {time.strftime('%Y-%m-%d %H:%M:%S')} "
                     f"(pid {os.getpid()}) ---\n")
    crash_file.flush()
    faulthandler.enable(file=crash_file)
    # Keep a module-level reference so the file object isn't GC-closed.
    global _crash_file
    _crash_file = crash_file


def _excepthook(exc_type, exc, tb) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return

    # Key on the raise site, not the message, so one broken code path
    # counts as one incident regardless of varying exception text.
    frames = traceback.extract_tb(tb)
    key = (exc_type.__name__,
           (frames[-1].filename, frames[-1].lineno) if frames else None)

    now = time.monotonic()
    if now - _last_logged.get(key, -_LOG_REPEAT_INTERVAL_S) >= _LOG_REPEAT_INTERVAL_S:
        _last_logged[key] = now
        _logger.critical(
            "Unhandled exception (app continues):",
            exc_info=(exc_type, exc, tb),
        )

    if key not in _seen_dialog_keys:
        _seen_dialog_keys.add(key)
        _show_error_dialog(exc_type, exc)


def _show_error_dialog(exc_type, exc) -> None:
    # Imported lazily: the hook must work even if it fires before (or
    # during a failure of) QApplication construction.
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox
        if QApplication.instance() is None:
            return
        QMessageBox.critical(
            None,
            "Unexpected error",
            f"An unexpected error occurred:\n\n"
            f"{exc_type.__name__}: {exc}\n\n"
            f"The program will keep running, but if this happens "
            f"repeatedly, please restart it.\n\n"
            f"Details were saved to:\n{log_dir() / 'papyri.log'}",
        )
    except Exception:
        _logger.exception("Failed to show the error dialog")


def _threading_excepthook(args) -> None:
    if args.exc_type is SystemExit:
        return
    _logger.critical(
        "Unhandled exception in thread %r:",
        args.thread.name if args.thread else "?",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )
