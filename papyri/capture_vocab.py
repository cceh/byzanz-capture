"""Shared capture vocabulary — the axes and file-level primitives that ALL
storage families and the UI modules speak.

Nothing in here knows about any directory tree. Directory schemes (and each
family's completeness rule) live in the per-family layout modules:

    object_layout.py       full papyri objects   <box>/<object>/<side>/<spectrum>/
    calibration_layout.py  calibration runs      _calibration/<run>/<spectrum>/<folder>/
    (simple mode)          no layout module — flat folder, naming inline
                           in simple_target.py

Axes:
    SIDE     — A or B, the physical face of a papyrus. Objects use sides as
               their first bucket axis ("slot"); calibration defines its own
               slot tokens; simple pins SIDE_A as a dead constant.
    SPECTRUM — visible or infrared: which camera / wavelength.
"""
from __future__ import annotations


# ---- axes -------------------------------------------------------------

SIDE_A = "side_a"
SIDE_B = "side_b"
SIDES: tuple[str, ...] = (SIDE_A, SIDE_B)

SPECTRUM_VISIBLE = "visible"
SPECTRUM_INFRARED = "infrared"
SPECTRA: tuple[str, ...] = (SPECTRUM_VISIBLE, SPECTRUM_INFRARED)

# Short filename infix per spectrum — used by all three families' naming
# templates. Strict indexing on purpose: an unknown spectrum is a
# programming error and must raise, not silently become "vis".
SPECTRUM_INFIX: dict[str, str] = {
    SPECTRUM_VISIBLE: "vis",
    SPECTRUM_INFRARED: "ir",
}


# ---- capture files ----------------------------------------------------

JPG_EXTENSIONS = {".jpg", ".jpeg"}
RAW_EXTENSIONS = {".arw", ".nef", ".cr2", ".cr3", ".dng", ".raf", ".orf", ".rw2"}
CAPTURE_EXTENSIONS = JPG_EXTENSIONS | RAW_EXTENSIONS


def is_hidden_file(name: str) -> bool:
    """True for dot-files — hidden entries and macOS AppleDouble sidecars
    (`._foo.ARW`), which macOS writes next to every real file on exFAT/SMB
    volumes that can't store extended attributes natively. They share the
    real file's extension and trailing index, so capture scans must skip
    them or each take shows up twice."""
    return name.startswith(".")


def sanitize_name(text: str) -> str:
    """Normalize a user-typed object name / filename prefix into a single
    path component. Slashes and backslashes become underscores so the name
    can't smuggle in subdirectories; spaces are kept verbatim."""
    return (text or "").strip().replace("/", "_").replace("\\", "_")
