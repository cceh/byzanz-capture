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
    type: str               # "string" | "choice" | "longtext"
    required: bool = False
    choices: tuple[str, ...] = ()   # only for type="choice"


# Inventory number is intentionally NOT a metadata field: the object's
# directory name IS its inv no (per the layout convention in `_layout.py`).
# Adding it to the form would risk drift between filename and metadata.
#
# TODO: load from `<working_dir>/metadata_schema.yaml` when projects need
# project-specific fields. Until then this default applies to all objects.
DEFAULT_SCHEMA: tuple[FieldSchema, ...] = (
    FieldSchema(
        name="condition",
        label="Condition",
        type="choice",
        required=True,
        choices=("stable", "fragile", "damaged"),
    ),
    FieldSchema(
        name="notes",
        label="Notes",
        type="longtext",
        required=False,
    ),
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
