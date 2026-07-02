"""Object layout — the on-disk contract of the FULL-mode papyri object
family: directory scheme, marker file, per-bucket scans, completeness
rules. Pure path functions, no Qt — shared between `Object` (main.py) and
the objects sidebar without a main↔sidebar import cycle.

One layout module per storage family, all built alike (tree diagram,
naming, completeness): this one for objects, `calibration_layout.py` for
calibration runs; simple mode has no layout module (flat folder, naming
inline in `simple_target.py`). The axes and file-level primitives that
all families share live in `capture_vocab.py`.

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

Completeness rules (drive the sidebar chips):
    - a SPECTRUM is complete when every side has ≥1 capture
      (`is_spectrum_complete`); the calibration counterpart is the
      `required` flag in `calibration_layout.py`
    - metadata completeness is a separate, schema-driven rule — see
      `papyri._metadata.is_metadata_complete`

Naming rule: identifiers that mean "side" (A/B) use SIDE_*; identifiers
that mean "spectrum" (visible/infrared) use SPECTRUM_*. Earlier code used
"side" to mean spectrum — that terminology has been retired.
"""
from __future__ import annotations
import os

from papyri.capture_vocab import (
    CAPTURE_EXTENSIONS, SIDE_A, SIDE_B, SIDES, SPECTRA, SPECTRUM_INFRARED,
    SPECTRUM_VISIBLE, is_hidden_file,
)

META_FILENAME = "_meta.json"

# Subdir name = side or spectrum identifier directly.
_SIDE_SUBDIRS = {SIDE_A: "side_a", SIDE_B: "side_b"}
_SPECTRUM_SUBDIRS = {SPECTRUM_VISIBLE: "visible", SPECTRUM_INFRARED: "infrared"}

# All four (side, spectrum) buckets in a stable iteration order — the
# object family's bucket universe (counterpart: CALIBRATION_BUCKETS).
BUCKETS: tuple[tuple[str, str], ...] = tuple(
    (s, sp) for s in SIDES for sp in SPECTRA
)


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


def captured_sides_for_spectrum(object_dir: str, spectrum: str) -> int:
    """How many of the two sides have ≥1 capture for `spectrum` (0–2)."""
    return sum(
        1 for side in SIDES
        if has_captures_for_bucket(object_dir, side, spectrum)
    )


def is_spectrum_complete(object_dir: str, spectrum: str) -> bool:
    """THE completeness rule of the object family: a spectrum is complete
    when every physical side has ≥1 capture for it."""
    return captured_sides_for_spectrum(object_dir, spectrum) == len(SIDES)


def newest_capture_mtime(object_dir: str) -> float | None:
    """mtime of the newest capture file across all four buckets, or None if
    the object has no captures. Drives the sidebar's per-object date line."""
    newest: float | None = None
    for side, spectrum in BUCKETS:
        bucket_dir = dir_for_bucket(object_dir, side, spectrum)
        if not os.path.isdir(bucket_dir):
            continue
        for f in os.listdir(bucket_dir):
            if is_hidden_file(f):
                continue
            if os.path.splitext(f)[1].lower() not in CAPTURE_EXTENSIONS:
                continue
            try:
                mtime = os.path.getmtime(os.path.join(bucket_dir, f))
            except OSError:
                continue    # file vanished between listdir and stat
            if newest is None or mtime > newest:
                newest = mtime
    return newest


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


