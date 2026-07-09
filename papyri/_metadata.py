"""Metadata schema + completeness helpers shared between the metadata pane
(form generator + writer) and the objects sidebar (status badge derivation).

Lives in its own module to avoid widget→widget imports just for the schema.
The schema is hardcoded for now; a future change can make it loadable from
`<working_dir>/metadata_schema.yaml` per project.
"""
from __future__ import annotations
import os
from dataclasses import dataclass

from papyri.capture_vocab import SPECTRUM_VISIBLE
from papyri.object_layout import MetaKey, meta_path_for, read_meta


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


def _height_key(spectrum: str) -> str:
    """QSettings key holding a camera's current rig height: VIS = the
    sticky `currentHeight` the user picks, IR = the fixed `irCaptureHeight`."""
    return "currentHeight" if spectrum == SPECTRUM_VISIBLE else "irCaptureHeight"


def current_height_for(q_settings, spectrum: str) -> str:
    """Current rig height (str) for a camera. Single source for everyone
    who resolves a height per spectrum (capture stamping, Flatfield
    calibration paths, due-tracking)."""
    return str(q_settings.value(_height_key(spectrum), "") or "")


def set_current_height(q_settings, spectrum: str, value: str) -> None:
    """Persist a camera's current rig height (mirror of `current_height_for`)."""
    q_settings.setValue(_height_key(spectrum), value)


def height_choices_for(q_settings, spectrum: str) -> tuple[str, ...]:
    """Selectable rig heights for a camera: VIS = the configurable
    `captureHeightChoices` preset list; IR = the single fixed
    `irCaptureHeight`. A one-element result means the height is not
    adjustable — the control is simply fixed at that value, so nothing
    needs a per-camera 'is this editable' branch (see the height control
    in MainWindow: `setEnabled(len(choices) > 1)`)."""
    if spectrum == SPECTRUM_VISIBLE:
        return parse_height_choices(q_settings.value("captureHeightChoices", ""))
    ir = str(q_settings.value("irCaptureHeight", "") or "").strip()
    return (ir,) if ir else ()


@dataclass(frozen=True)
class FieldSchema:
    name: str               # JSON key in _meta.json
    label: str              # human label shown in the form
    type: str               # "string" | "choice" | "longtext" | "number" | "boolean"
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
    default: str | int | bool | None = None
                            # pre-fill if missing from JSON; also
                            # persisted on first save. Ints are
                            # accepted for number/choice fields and
                            # stringified before writing to widgets.
                            # For type="boolean", pass a bool (default
                            # unchecked = False).


# Inventory number is intentionally NOT a metadata field: the object's
# directory name IS its inv no (per the layout convention in `object_layout.py`).
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
    # FieldSchema(
    #     name="mummy_nr", label="Mummy no.", type="string",
    # ),
    # Dimensions stored as two queryable integer fields rather than one
    # parsed "wXh" string — downstream tools can sort / filter easily.
    # FieldSchema(
    #     name="width_mm", label="Width (mm)", type="number", required=True,
    # ),
    # FieldSchema(
    #     name="height_mm", label="Height (mm)", type="number", required=True,
    # ),
    FieldSchema(
        name="casing", label="Casing", type="choice", required=True,
        choices=("loose", "glass", "vinylite"), default="loose"
    ),
    FieldSchema(
        name="language", label="Language", type="choice", required=False,
        choices=("Greek", "Demotic", "unknown"), default=None
    ),
    FieldSchema(
        name="ink", label="Ink", type="string", default="black",
    ),
    FieldSchema(
        name="has_markings", label="Has direct/modern markings", type="boolean", default=False,
    ),
    FieldSchema(
        name="has_tape", label="Has restoration tape", type="boolean", default=False,
    ),
    # `capture_height_vis` / `capture_height_ir` are NOT form fields — the
    # height is a sticky rig setting (the capture-row "Height" control), and
    # MainWindow stamps it into `_meta.json` on capture. The metadata pane
    # preserves those keys via its merge-write. See docs/calibration-routine.md.
    FieldSchema(
        name="notes", label="Notes", type="longtext",
    )
)


# A metadata field sharing a name with an app-owned `_meta.json` key would
# let the form's merge-write (which drops cleared fields) clobber app state.
# Fail loudly at import rather than silently at runtime.
_reserved_collisions = {f.name for f in DEFAULT_SCHEMA} & set(MetaKey)
if _reserved_collisions:
    raise ValueError(
        "metadata schema fields collide with reserved _meta.json keys: "
        f"{sorted(_reserved_collisions)}")


def is_metadata_complete(
    meta_path: str,
    schema: tuple[FieldSchema, ...] = DEFAULT_SCHEMA,
) -> bool:
    """True iff every required field in `schema` has a non-empty value in `_meta.json`."""
    data = read_meta(meta_path)
    for field in schema:
        if not field.required:
            continue
        # Booleans are complete once present — False is a valid answer, not
        # "unset" (so the `not value` test that text/number fields use would
        # wrongly flag an unchecked-but-answered box as incomplete).
        if field.type == "boolean":
            if field.name not in data:
                return False
        elif not data.get(field.name):
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
