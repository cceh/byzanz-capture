"""Metadata schema + completeness helpers shared between the metadata pane
(form generator + writer) and the objects sidebar (status badge derivation).

Lives in its own module to avoid widget→widget imports just for the schema.
The schema is hardcoded for now; a future change can make it loadable from
`<working_dir>/metadata_schema.yaml` per project.
"""
from __future__ import annotations
import json
import os
from dataclasses import dataclass

from papyri._layout import meta_path_for


# Camera-height presets. The capture-row "Height" control, the per-height
# Flatfield calibration, and the per-object `capture_height_vis` stamp all
# read ONE list — configurable in Settings (`captureHeightChoices`, a
# comma-separated string). These are the fallback defaults.
DEFAULT_CAPTURE_HEIGHTS: tuple[str, ...] = ("30", "45", "60", "75", "90")


def parse_height_choices(raw: str | None) -> tuple[str, ...]:
    """Parse the comma-separated `captureHeightChoices` setting into a
    tuple of height strings, falling back to the defaults if it's empty
    or all-blank."""
    items = [s.strip() for s in (raw or "").split(",") if s.strip()]
    return tuple(items) if items else DEFAULT_CAPTURE_HEIGHTS


@dataclass(frozen=True)
class FieldSchema:
    name: str               # JSON key in _meta.json
    label: str              # human label shown in the form
    type: str               # "string" | "choice" | "longtext" | "number"
    required: bool = False
    choices: tuple[str, ...] = ()   # only for type="choice"
    editable: bool = False  # only for type="choice" — when True, the
                            # combo accepts free text alongside the
                            # predefined entries (useful for fields
                            # with common presets but no hard list).
    numeric: bool = False   # restrict typed input to digits (uses
                            # QIntValidator on the underlying line
                            # edit). Pair with `editable=True` for a
                            # numeric free-text combo.
    default: str | int | None = None
                            # pre-fill if missing from JSON; also
                            # persisted on first save. Ints are
                            # accepted for number/choice fields and
                            # stringified before writing to widgets.


# Inventory number is intentionally NOT a metadata field: the object's
# directory name IS its inv no (per the layout convention in `_layout.py`).
# Adding it to the form would risk drift between filename and metadata.
#
# Box no. is likewise NOT a form field: a box is a whole working directory
# (the box folder), so its number is the working dir's basename. MainWindow
# stamps `box_nr` into `_meta.json` from that basename on capture (see
# `_stamp_capture_metadata`) — no per-object typing, no drift.
#
# TODO: load from `<working_dir>/metadata_schema.yaml` when projects need
# project-specific fields. Until then this default applies to all objects.
DEFAULT_SCHEMA: tuple[FieldSchema, ...] = (
    FieldSchema(
        name="mummy_nr", label="Mummy no.", type="string",
    ),
    # Dimensions stored as two queryable integer fields rather than one
    # parsed "wXh" string — downstream tools can sort / filter easily.
    FieldSchema(
        name="width_mm", label="Width (mm)", type="number", required=True,
    ),
    FieldSchema(
        name="height_mm", label="Height (mm)", type="number", required=True,
    ),
    FieldSchema(
        name="language", label="Language", type="choice", required=True,
        choices=("Greek", "Demotic", "unknown"), default="unknown"
    ),
    FieldSchema(
        name="ink", label="Ink", type="string", default="black",
    ),
    # `capture_height_vis` / `capture_height_ir` are NOT form fields — the
    # height is a sticky rig setting (the capture-row "Height" control), and
    # MainWindow stamps it into `_meta.json` on capture. The metadata pane
    # preserves those keys via its merge-write. See docs/calibration-routine.md.
    FieldSchema(
        name="notes", label="Notes", type="longtext",
    )
)


def _read_meta(meta_path: str) -> dict:
    """Read `_meta.json` defensively. Returns empty dict on missing/malformed."""
    try:
        with open(meta_path) as f:
            return json.load(f) or {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def is_metadata_complete(
    meta_path: str,
    schema: tuple[FieldSchema, ...] = DEFAULT_SCHEMA,
) -> bool:
    """True iff every required field in `schema` has a non-empty value in `_meta.json`."""
    data = _read_meta(meta_path)
    for field in schema:
        if field.required and not data.get(field.name):
            return False
    return True


def is_metadata_complete_for(
    working_dir: str,
    name: str,
    schema: tuple[FieldSchema, ...] = DEFAULT_SCHEMA,
) -> bool:
    """Convenience: completeness check by `(working_dir, name)`."""
    obj_dir = os.path.join(working_dir, name)
    return is_metadata_complete(meta_path_for(obj_dir), schema)
