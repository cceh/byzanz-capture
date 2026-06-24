"""On-disk layout constants and helpers shared between Object (main.py) and
the objects sidebar. Lives in its own module to avoid a main↔sidebar import
cycle.

Two orthogonal axes:
    SIDE     — A or B, the physical face of the papyrus
    SPECTRUM — visible or infrared, which camera/wavelength was used

Layout:
    <working_dir>/
        <object_name>/
            _meta.json                          -- presence marks "this is a managed object dir"
            side_a/
                _chosen_visible.txt             -- optional; chosen visible-take stem; absent = first
                _chosen_infrared.txt            -- optional; chosen IR-take stem; absent = first
                visible/
                    <name>_a_vis_NNN.{jpg,arw,...}
                infrared/
                    <name>_a_ir_NNN.{jpg,arw,...}
            side_b/
                visible/  ...
                infrared/ ...

Naming rule: identifiers that mean "side" (A/B) use SIDE_*; identifiers
that mean "spectrum" (visible/infrared) use SPECTRUM_*. Earlier code used
"side" to mean spectrum — that terminology has been retired.
"""
from __future__ import annotations
import os

META_FILENAME = "_meta.json"

# Sides — physical faces of the papyrus.
SIDE_A = "side_a"
SIDE_B = "side_b"
SIDES: tuple[str, ...] = (SIDE_A, SIDE_B)

# Spectra — which camera / wavelength.
SPECTRUM_VISIBLE = "visible"
SPECTRUM_INFRARED = "infrared"
SPECTRA: tuple[str, ...] = (SPECTRUM_VISIBLE, SPECTRUM_INFRARED)

# Subdir name = side or spectrum identifier directly.
_SIDE_SUBDIRS = {SIDE_A: "side_a", SIDE_B: "side_b"}
_SPECTRUM_SUBDIRS = {SPECTRUM_VISIBLE: "visible", SPECTRUM_INFRARED: "infrared"}

# All four (side, spectrum) buckets in a stable iteration order.
BUCKETS: tuple[tuple[str, str], ...] = tuple(
    (s, sp) for s in SIDES for sp in SPECTRA
)

JPG_EXTENSIONS = {".jpg", ".jpeg"}
RAW_EXTENSIONS = {".arw", ".nef", ".cr2", ".cr3", ".dng", ".raf", ".orf", ".rw2"}
CAPTURE_EXTENSIONS = JPG_EXTENSIONS | RAW_EXTENSIONS


def sanitize_name(text: str) -> str:
    """Normalize a user-typed object name / filename prefix into a single
    path component. Slashes and backslashes become underscores so the name
    can't smuggle in subdirectories; spaces are kept verbatim."""
    return (text or "").strip().replace("/", "_").replace("\\", "_")


def is_hidden_file(name: str) -> bool:
    """True for dot-files — hidden entries and macOS AppleDouble sidecars
    (`._foo.ARW`), which macOS writes next to every real file on exFAT/SMB
    volumes that can't store extended attributes natively. They share the
    real file's extension and trailing index, so capture scans must skip
    them or each take shows up twice."""
    return name.startswith(".")


# ---- core helpers ---------------------------------------------------------

def meta_path_for(object_dir: str) -> str:
    return os.path.join(object_dir, META_FILENAME)


def side_dir_for(object_dir: str, side: str) -> str:
    """Return `<object_dir>/<side>/`. Raises ValueError on unknown side."""
    if side not in _SIDE_SUBDIRS:
        raise ValueError(f"unknown side: {side!r}")
    return os.path.join(object_dir, _SIDE_SUBDIRS[side])


def dir_for_bucket(object_dir: str, side: str, spectrum: str) -> str:
    """Return `<object_dir>/<side>/<spectrum>/`. Raises on unknown side/spectrum."""
    if spectrum not in _SPECTRUM_SUBDIRS:
        raise ValueError(f"unknown spectrum: {spectrum!r}")
    return os.path.join(side_dir_for(object_dir, side), _SPECTRUM_SUBDIRS[spectrum])


def chosen_path_for(object_dir: str, side: str, spectrum: str) -> str:
    """Return `<object_dir>/<side>/_chosen_<spectrum>.txt`."""
    if spectrum not in _SPECTRUM_SUBDIRS:
        raise ValueError(f"unknown spectrum: {spectrum!r}")
    return os.path.join(side_dir_for(object_dir, side), f"_chosen_{spectrum}.txt")


def is_managed_object_dir(object_dir: str) -> bool:
    """True if `object_dir` contains the `_meta.json` marker."""
    return os.path.isfile(meta_path_for(object_dir))


def has_captures_for_bucket(object_dir: str, side: str, spectrum: str) -> bool:
    """True if `<object_dir>/<side>/<spectrum>/` contains at least one supported file."""
    bucket_dir = dir_for_bucket(object_dir, side, spectrum)
    if not os.path.isdir(bucket_dir):
        return False
    for f in os.listdir(bucket_dir):
        if is_hidden_file(f):
            continue
        if os.path.splitext(f)[1].lower() in CAPTURE_EXTENSIONS:
            return True
    return False


def has_any_captures(object_dir: str) -> bool:
    """True if any of the four buckets contains at least one capture."""
    return any(
        has_captures_for_bucket(object_dir, side, spectrum)
        for side, spectrum in BUCKETS
    )


def filled_bucket_count(object_dir: str) -> int:
    """How many of the 4 buckets have ≥ 1 capture (used for sidebar badge)."""
    return sum(
        1 for side, spectrum in BUCKETS
        if has_captures_for_bucket(object_dir, side, spectrum)
    )


# ---- working-dir-level helpers --------------------------------------------

def list_managed_objects(working_dir: str | None) -> list[str]:
    """Sorted names of managed object directories directly under `working_dir`."""
    if not working_dir or not os.path.isdir(working_dir):
        return []
    return sorted(
        name for name in os.listdir(working_dir)
        if os.path.isdir(os.path.join(working_dir, name))
        and is_managed_object_dir(os.path.join(working_dir, name))
    )


def has_any_captures_for(working_dir: str, name: str) -> bool:
    """Convenience: any captures (across all 4 buckets) for `(working_dir, name)`."""
    return has_any_captures(os.path.join(working_dir, name))


def filled_bucket_count_for(working_dir: str, name: str) -> int:
    return filled_bucket_count(os.path.join(working_dir, name))
