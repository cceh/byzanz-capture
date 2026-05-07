"""GIL-releasing libgphoto2 autodetect wrapper.

The default `gp.Camera.autodetect()` is a SWIG-wrapped C call that does NOT
release the GIL during its USB-bus scan (~100-500 ms). Because the GIL is
held, the Qt UI thread can't execute any Python bytecode for the duration
of the scan — every scan freezes the UI for that window. With a 1-second
poll loop, the user sees micro-hangs every second.

This module wraps `gp_camera_autodetect` directly via ctypes. ctypes
releases the GIL automatically around C calls, so the UI thread keeps
running while the worker thread scans USB.

If symbol resolution fails at import time (unusual platform, statically-
linked python-gphoto2 with hidden symbols, etc.), `autodetect()` silently
falls back to `gp.Camera.autodetect()`. Papyri keeps working — just with
the original GIL-hold behaviour. Check the module-level log line to see
which path is in use.

See `docs/ui-hangs-during-camera-detection.md` for the full analysis.
"""
from __future__ import annotations
import ctypes
import logging
import os
import sys

import gphoto2 as gp  # ensure libgphoto2 is loaded into the process

_logger = logging.getLogger(__name__)

# Escape hatch for debugging — set to "1" to force the SWIG fallback path
# even when ctypes is available.
_DISABLE_CTYPES = os.environ.get("BYZANZ_DISABLE_CTYPES_AUTODETECT") == "1"


def _resolve_libgphoto2() -> ctypes.CDLL | None:
    """Return a ctypes handle that exposes the gp_* symbols, or None if
    we couldn't reach them by any method."""

    # Path 1: process symbol table (RTLD_DEFAULT). Works on macOS / Linux
    # when python-gphoto2 was loaded with default symbol visibility.
    if sys.platform != "win32":
        try:
            h = ctypes.CDLL(None)
            _ = h.gp_camera_autodetect  # AttributeError if unresolved
            _logger.info(
                "libgphoto2 symbols resolved via process symbol table "
                "(ctypes.CDLL(None))"
            )
            return h
        except (AttributeError, OSError):
            pass

    # Path 2: dlopen by SONAME. Since python-gphoto2 has already loaded the
    # library, dlopen with the same name just bumps the refcount on the
    # existing handle — no second copy in process memory.
    if sys.platform == "darwin":
        candidates = ["libgphoto2.6.dylib", "libgphoto2.dylib"]
    elif sys.platform.startswith("linux"):
        candidates = ["libgphoto2.so.6", "libgphoto2.so"]
    elif sys.platform == "win32":
        candidates = ["libgphoto2-6.dll", "libgphoto2.dll"]
    else:
        candidates = []

    for name in candidates:
        try:
            h = ctypes.CDLL(name)
            _ = h.gp_camera_autodetect
            _logger.info("libgphoto2 symbols resolved via dlopen(%r)", name)
            return h
        except (AttributeError, OSError):
            continue

    return None


def _setup_signatures(lib: ctypes.CDLL) -> None:
    """Declare argtypes/restype for the handful of functions we call.
    Without this, ctypes assumes int defaults and pointer args break on
    64-bit platforms."""
    lib.gp_list_new.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    lib.gp_list_new.restype = ctypes.c_int

    lib.gp_list_unref.argtypes = [ctypes.c_void_p]
    lib.gp_list_unref.restype = ctypes.c_int

    lib.gp_camera_autodetect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.gp_camera_autodetect.restype = ctypes.c_int

    lib.gp_list_count.argtypes = [ctypes.c_void_p]
    lib.gp_list_count.restype = ctypes.c_int

    lib.gp_list_get_name.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_char_p),
    ]
    lib.gp_list_get_name.restype = ctypes.c_int

    lib.gp_list_get_value.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_char_p),
    ]
    lib.gp_list_get_value.restype = ctypes.c_int


_LIB: ctypes.CDLL | None = None
if not _DISABLE_CTYPES:
    _LIB = _resolve_libgphoto2()
    if _LIB is not None:
        _setup_signatures(_LIB)
        _logger.info("autodetect: using ctypes path (GIL-free)")

if _LIB is None:
    _logger.warning(
        "autodetect: ctypes path unavailable, falling back to "
        "gp.Camera.autodetect() (holds GIL during USB scan)"
    )


# ---- pre-warm the Python logging path -----------------------------------
#
# python-gphoto2 routes libgphoto2's log callbacks through Python's logging
# module, by logger name like "gphoto2.context" / "gphoto2.port_info_list".
# Each Logger instance is created lazily on first lookup — and lookup walks
# the parent hierarchy and resolves the handler chain.
#
# If that lazy initialization were to happen the first time the callback
# fires from inside a GIL-released ctypes autodetect call (worker thread,
# C stack frame inside libgphoto2), it would race with our PyGILState_Ensure
# pattern at exactly the kind of edge case that bit us during the camera
# init deadlock. Pre-warming the loggers here, at module-import time with
# the main thread holding the GIL, eliminates that race surface.
#
# Cheap insurance — instantiates the Logger objects and walks the parent
# chain, but emits no visible output (debug level, default-suppressed).

for _name in (
    "gphoto2",
    "gphoto2.camera",
    "gphoto2.context",
    "gphoto2.port_info_list",
    "gphoto2.abilities_list",
):
    logging.getLogger(_name).debug("pre-warm")


def _autodetect_ctypes() -> list[tuple[str, str]]:
    """Call gp_camera_autodetect via ctypes. The C call releases the GIL,
    so the UI thread keeps running during the USB scan."""
    list_ptr = ctypes.c_void_p()
    err = _LIB.gp_list_new(ctypes.byref(list_ptr))
    if err < 0:
        raise RuntimeError(f"gp_list_new failed: {err}")
    try:
        err = _LIB.gp_camera_autodetect(list_ptr, None)  # NULL context
        if err < 0:
            raise RuntimeError(f"gp_camera_autodetect failed: {err}")
        n = _LIB.gp_list_count(list_ptr)
        results: list[tuple[str, str]] = []
        name = ctypes.c_char_p()
        value = ctypes.c_char_p()
        for i in range(n):
            err = _LIB.gp_list_get_name(list_ptr, i, ctypes.byref(name))
            if err < 0:
                raise RuntimeError(f"gp_list_get_name failed: {err}")
            err = _LIB.gp_list_get_value(list_ptr, i, ctypes.byref(value))
            if err < 0:
                raise RuntimeError(f"gp_list_get_value failed: {err}")
            results.append((name.value.decode(), value.value.decode()))
        return results
    finally:
        _LIB.gp_list_unref(list_ptr)


def _autodetect_swig() -> list[tuple[str, str]]:
    """Fallback path — calls python-gphoto2's SWIG wrapper. Holds the GIL
    for the duration of the USB scan."""
    return [(model, port) for model, port in gp.Camera.autodetect()]


# Sticky flag: flipped to True the first time the ctypes path raises at
# runtime, so we don't keep retrying a broken path on every poll.
_runtime_ctypes_failed = False


def autodetect() -> list[tuple[str, str]]:
    """Detect connected cameras. Returns a list of (model, port) tuples.

    Uses the GIL-releasing ctypes path when available; otherwise (or after
    a runtime failure of the ctypes path) falls back to
    gp.Camera.autodetect() and remains on the fallback for the rest of the
    process lifetime. Each transition is logged so the operator / future
    debugger can see which path was active when."""
    global _runtime_ctypes_failed

    if _LIB is None or _runtime_ctypes_failed:
        return _autodetect_swig()

    try:
        return _autodetect_ctypes()
    except Exception as e:
        # If the ctypes call ever throws (corrupted
        # library, ABI mismatch, signature drift, etc.) we don't want every
        # subsequent poll to re-hit the same failure. Flip the sticky flag
        # and continue on the SWIG path. Loud log so this never goes
        # unnoticed in production.
        _runtime_ctypes_failed = True
        _logger.exception(
            "autodetect: ctypes path raised at runtime — permanently "
            "falling back to gp.Camera.autodetect() for this process. "
            "Error was: %s", e,
        )
        return _autodetect_swig()
