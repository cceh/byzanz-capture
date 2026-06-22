"""NULL-safe reads of gphoto2 widget values.

python-gphoto2's `CameraWidget.get_value()` segfaults when a
char*-valued widget (TEXT / RADIO / MENU) holds a NULL value: the SWIG
typemap unconditionally calls `PyUnicode_FromString(NULL)`. Some Sony
vendor PTP properties (e.g. `d2c1` under /main/other) expose exactly
this in certain camera states, hard-crashing the advanced camera-config
dialog when it walks the tree — uncatchable from Python.

We read those values via ctypes instead, checking the value pointer for
NULL before decoding. TOGGLE/RANGE/DATE widgets store their value inline
(int/float, never a NULL pointer) so they don't need this and keep using
the binding directly.
"""
from __future__ import annotations

import ctypes
import logging

from byzanz_camera._autodetect import _resolve_libgphoto2

_logger = logging.getLogger(__name__)

# Reuse the same libgphoto2 handle resolution the autodetect path uses
# (process symbol table, else dlopen by name) so this works in frozen
# bundles too. Declared once; None if it couldn't be resolved.
_lib = _resolve_libgphoto2()
if _lib is not None:
    try:
        _lib.gp_widget_get_value.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        _lib.gp_widget_get_value.restype = ctypes.c_int
    except Exception:  # noqa: BLE001 - symbol missing → disable, never raise
        _logger.warning("gp_widget_get_value unavailable via ctypes; "
                        "NULL-safe widget reads disabled")
        _lib = None


def widget_text_value(widget) -> str | None:
    """NULL-safe value of a char*-valued widget (TEXT/RADIO/MENU).

    Returns the decoded string, or None when the widget's value pointer
    is NULL — the case that segfaults `widget.get_value()`. On any
    failure (ctypes unavailable, pointer not extractable) returns None
    rather than risking the crash; the caller renders an empty field.
    """
    if _lib is None:
        return None
    try:
        ptr = int(widget.this)              # CameraWidget* address (SWIG)
    except Exception:  # noqa: BLE001
        return None
    out = ctypes.c_void_p()
    rc = _lib.gp_widget_get_value(ptr, ctypes.byref(out))
    if rc < 0 or not out.value:             # error or NULL char*
        return None
    try:
        return ctypes.cast(out.value, ctypes.c_char_p).value.decode(
            "utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
