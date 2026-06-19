"""Capture-mode configuration — the small set of things that differ
between the full papyri workflow and the stripped-down "simple" capture
mode.

A `CaptureMode` is the global spielregel for one app run (chosen at
startup from the persisted `captureMode` setting). It is pure data +
the workflow-group definitions; `MainWindow` reads it instead of
hard-coding the bucket layout, so the two modes don't litter the window
with `if simple:` branches. The per-folder storage model (`Object` vs
`SimpleTarget`) is built by `MainWindow._make_target`, which branches on
`mode.key` — keeping this module free of an import cycle through main.

Full mode  : 4 buckets (side A/B × VIS/IR), metadata, sidebar, chosen
             thumbs on the bucket cards.
Simple mode: spectrum only (VIS/IR) as a plain camera selector, flat
             folder, no metadata / sidebar / chosen / move; the
             filmstrip shows the whole output folder.
"""
from __future__ import annotations

from dataclasses import dataclass

from papyri._layout import (
    SIDE_A, SIDE_B, SPECTRUM_INFRARED, SPECTRUM_VISIBLE,
)
from papyri.workflow_stepper import WorkflowGroup, WorkflowStep


# Stable step ids per mode — kept identical to the historical papyri ids
# so nothing downstream that special-cased them breaks.
_PAPYRI_STEP_ID_BY_BUCKET = {
    (SIDE_A, SPECTRUM_VISIBLE):  "vis_a",
    (SIDE_B, SPECTRUM_VISIBLE):  "vis_b",
    (SIDE_A, SPECTRUM_INFRARED): "ir_a",
    (SIDE_B, SPECTRUM_INFRARED): "ir_b",
}

# Simple mode drops the side axis entirely: one card per spectrum, both
# pinned to SIDE_A so all the existing (side, spectrum)-parametric
# plumbing keeps working with side as a dead constant.
_SIMPLE_STEP_ID_BY_BUCKET = {
    (SIDE_A, SPECTRUM_VISIBLE):  "vis",
    (SIDE_A, SPECTRUM_INFRARED): "ir",
}


@dataclass(frozen=True)
class CaptureMode:
    """Everything that differs between modes, as data.

    `groups` feeds `BucketSelector.set_groups`. `step_id_by_bucket` maps
    a (side, spectrum) bucket to its stepper id; `bucket_by_step_id` is
    the inverse (built lazily). The booleans gate UI chrome and
    storage-model behaviour in MainWindow.
    """
    key: str                       # "papyri" | "simple"
    groups: tuple[WorkflowGroup, ...]
    step_id_by_bucket: dict[tuple[str, str], str]
    show_sidebar: bool             # objects sidebar visible
    show_metadata: bool            # metadata pane visible
    show_thumbs: bool              # bucket cards carry chosen-take thumbs
    show_sides: bool               # side A/B axis exists
    whole_folder_filmstrip: bool   # filmstrip shows the whole folder,
                                   # no chosen / move actions

    @property
    def bucket_by_step_id(self) -> dict[str, tuple[str, str]]:
        return {v: k for k, v in self.step_id_by_bucket.items()}


def _papyri_groups() -> tuple[WorkflowGroup, ...]:
    """Two groups (Visible, Infrared) × two sides (A, B). Tints overridden
    explicitly so they match the agreed papyri palette exactly (palette
    derivation from a base color doesn't quite hit the right values for
    warm hues)."""
    s = _PAPYRI_STEP_ID_BY_BUCKET
    return (
        WorkflowGroup(
            label="Visible",
            short_label="VIS",
            base_color="#3b82f6",
            bg_active="#3b82f6",      # blue-500
            bg_done="#dbeafe",        # blue-200
            bg_pending="white",
            text_dark="#1e3a8a",      # blue-800
            steps=[
                WorkflowStep(s[(SIDE_A, SPECTRUM_VISIBLE)], "Side A"),
                WorkflowStep(s[(SIDE_B, SPECTRUM_VISIBLE)], "Side B"),
            ],
        ),
        WorkflowGroup(
            label="Infrared",
            short_label="IR",
            base_color="#ea580c",
            bg_active="#ea580c",      # orange-600
            bg_done="#ffedd5",        # orange-100 (pastel)
            bg_pending="white",
            text_dark="#9a3412",      # orange-800
            steps=[
                WorkflowStep(s[(SIDE_A, SPECTRUM_INFRARED)], "Side A"),
                WorkflowStep(s[(SIDE_B, SPECTRUM_INFRARED)], "Side B"),
            ],
        ),
    )


def _simple_groups() -> tuple[WorkflowGroup, ...]:
    """One card per spectrum — the bucket selector becomes a plain
    VIS/IR camera switch. Same colours as full mode so the chrome reads
    consistently; a single step each, no Side A/B."""
    s = _SIMPLE_STEP_ID_BY_BUCKET
    return (
        WorkflowGroup(
            label="Visible",
            short_label="VIS",
            base_color="#3b82f6",
            bg_active="#3b82f6",
            bg_done="#dbeafe",
            bg_pending="white",
            text_dark="#1e3a8a",
            steps=[WorkflowStep(s[(SIDE_A, SPECTRUM_VISIBLE)], "Visible")],
        ),
        WorkflowGroup(
            label="Infrared",
            short_label="IR",
            base_color="#ea580c",
            bg_active="#ea580c",
            bg_done="#ffedd5",
            bg_pending="white",
            text_dark="#9a3412",
            steps=[WorkflowStep(s[(SIDE_A, SPECTRUM_INFRARED)], "Infrared")],
        ),
    )


PAPYRI_MODE = CaptureMode(
    key="papyri",
    groups=_papyri_groups(),
    step_id_by_bucket=_PAPYRI_STEP_ID_BY_BUCKET,
    show_sidebar=True,
    show_metadata=True,
    show_thumbs=True,
    show_sides=True,
    whole_folder_filmstrip=False,
)

SIMPLE_MODE = CaptureMode(
    key="simple",
    groups=_simple_groups(),
    step_id_by_bucket=_SIMPLE_STEP_ID_BY_BUCKET,
    show_sidebar=False,
    show_metadata=False,
    show_thumbs=False,
    show_sides=False,
    whole_folder_filmstrip=True,
)

MODES = {m.key: m for m in (PAPYRI_MODE, SIMPLE_MODE)}


def get_mode(key: str | None) -> CaptureMode:
    """Resolve a persisted `captureMode` value to a CaptureMode,
    defaulting to the full papyri mode for unknown / missing values."""
    return MODES.get(key or "papyri", PAPYRI_MODE)
