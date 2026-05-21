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
# TODO: load from `<working_dir>/metadata_schema.yaml` when projects need
# project-specific fields. Until then this default applies to all objects.
DEFAULT_SCHEMA: tuple[FieldSchema, ...] = (
    FieldSchema(
        name="box_nr", label="Box no.", type="string", required=True,
    ),
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
    FieldSchema(
        name="capture_height_vis", label="Camera height VIS (cm)", type="choice",
        choices=("30", "45", "60", "75", "90"),
        editable=True, numeric=True, default=45,
    ),
FieldSchema(
        name="capture_height_ir", label="Camera height IR (cm)", type="choice",
        choices=("30", "45", "60", "75", "90"),
        editable=True, numeric=True, default=45,
    ),
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
