"""Calibration layout — the single source of truth for "which calibration
targets exist, per camera".

One list (`CALIBRATION_TARGETS`) drives three things, so they can never
drift apart:
  - the calibration tabs (capture_mode._calibration_groups)
  - the on-disk folders of the CalibrationTarget (calibration_target.py)
  - the per-camera "due" logic (calibration.CalibrationController)

To add/remove a target it is enough to edit `CALIBRATION_TARGETS`:
  - add a target  → append a CalSpec line (a tab appears for that camera)
  - drop a target → delete its line
Asymmetry between cameras is fine (e.g. IR has no ColorChecker here).

Axes:
  slot     — an opaque token for the first bucket axis. Objects use the
             physical side (SIDE_A/SIDE_B); calibration uses its own
             tokens ("cal_cc", …) so nothing has to pretend a side IS a
             calibration kind. The session stores it in `active_side`.
  spectrum — which camera (SPECTRUM_VISIBLE / SPECTRUM_INFRARED).

Pure data + small functions only — no Qt — so every layer can import it
without cycles.
"""
from __future__ import annotations

from dataclasses import dataclass

from papyri._layout import SPECTRUM_INFRARED, SPECTRUM_VISIBLE

CALIBRATION_DIRNAME = "_calibration"

_INFIX = {SPECTRUM_VISIBLE: "vis", SPECTRUM_INFRARED: "ir"}


@dataclass(frozen=True)
class CalSpec:
    """One calibration target for one camera."""
    slot: str            # first-axis token, e.g. "cal_cc" / "cal_ff"
    spectrum: str        # SPECTRUM_VISIBLE | SPECTRUM_INFRARED
    folder: str          # on-disk folder + filename prefix, e.g. "flatfield"
    label: str           # tab caption, e.g. "Flatfield"
    per_height: bool = False  # foldered/tagged by the current rig height
                              # (the Flatfield depends on framing; ColorChecker
                              # does not). The height value comes from the
                              # shared capture-row setting, NOT from here.
    required: bool = True     # counts toward the camera's "calibration done"
                              # (the timer nudge). ColorChecker is optional.


# The layout. Edit this to change what calibration offers.
#   - Grid is NOT here: lens geometry is calibrated manually/externally.
#   - ColorChecker is VIS-only and not required (the in-frame ColorChecker
#     Nano covers colour per shot; this is a fallback). IR has no colour.
#   - Flatfield is the per-height core for both cameras.
CALIBRATION_TARGETS: tuple[CalSpec, ...] = (
    CalSpec("cal_ff", SPECTRUM_VISIBLE,  "flatfield",    "Flatfield",
            per_height=True, required=True),
    CalSpec("cal_cc", SPECTRUM_VISIBLE,  "colorchecker", "ColorChecker",
            per_height=False, required=False),
    CalSpec("cal_ff", SPECTRUM_INFRARED, "flatfield",    "Flatfield",
            per_height=True, required=True),
)

# Derived once — every consumer reads these instead of re-deriving.
CALIBRATION_BUCKETS: tuple[tuple[str, str], ...] = tuple(
    (s.slot, s.spectrum) for s in CALIBRATION_TARGETS
)
_FOLDER_FOR_SLOT = {s.slot: s.folder for s in CALIBRATION_TARGETS}
_LABEL_FOR_SLOT = {s.slot: s.label for s in CALIBRATION_TARGETS}
_PER_HEIGHT_SLOTS = {s.slot for s in CALIBRATION_TARGETS if s.per_height}


def infix_for(spectrum: str) -> str:
    return _INFIX.get(spectrum, "vis")


def cal_step_id(slot: str, spectrum: str) -> str:
    """Stepper id for a calibration bucket, e.g. ('cal_cc','visible') → 'cal_cc_vis'."""
    return f"{slot}_{infix_for(spectrum)}"


def folder_for_slot(slot: str) -> str:
    return _FOLDER_FOR_SLOT.get(slot, slot)


def is_per_height(slot: str) -> bool:
    """True if this target's storage is foldered by the current rig height
    (Flatfield). The height value itself is the shared `currentHeight`
    setting, supplied by MainWindow — not stored here."""
    return slot in _PER_HEIGHT_SLOTS


def label_for_slot(slot: str) -> str:
    return _LABEL_FOR_SLOT.get(slot, slot)


def specs_for(spectrum: str) -> list[CalSpec]:
    return [s for s in CALIBRATION_TARGETS if s.spectrum == spectrum]


def required_specs_for(spectrum: str) -> list[CalSpec]:
    return [s for s in CALIBRATION_TARGETS if s.spectrum == spectrum and s.required]


def first_slot_for(spectrum: str) -> str | None:
    """The default slot to open when entering calibration for a camera."""
    specs = specs_for(spectrum)
    return specs[0].slot if specs else None
