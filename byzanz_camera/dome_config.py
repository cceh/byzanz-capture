"""RTI dome configuration as data.

A dome is *not* a camera property: whether the capture runs as one camera
burst (Cologne) or step-by-step (Paris), how many LED positions it has, and
which light controller drives it — none of that belongs on a CameraProfile.
So it lives here as a small `DomeConfig` record.

`DomeConfig`s ship as read-only JSON presets in `dome_presets/` (bundled with
the app, updated by shipping a new version). Picking a preset is a one-shot
loader: `apply_preset` writes its values into the `dome/*` QSettings, and from
then on those settings *are* the config — the user may edit them, and there is
no persistent "which dome is active". Read them back with `current_dome`.

Camera and dome are chosen independently; nothing maps a camera to a dome.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from PyQt6.QtCore import QSettings

from byzanz_camera.camera_worker import CaptureImagesRequest
from byzanz_camera.helpers import get_ui_path

CaptureStrategy = CaptureImagesRequest.CaptureStrategy

_PRESETS_DIR = "dome_presets"

# QSettings keys. Flat and greppable so they can be hand-edited/inspected.
NAME = "dome/name"
NUM_POSITIONS = "dome/num_positions"
CAPTURE_STRATEGY = "dome/capture_strategy"
MAX_BURST = "dome/max_burst"
LIGHT_CONTROLLER = "dome/light_controller"

# light_controller values. Only cceh_ble is a real (Cologne-specific) mechanism
# today; "none" is an autonomous / no-controller dome (Paris, manual).
LIGHT_CCEH_BLE = "cceh_ble"
LIGHT_NONE = "none"


@dataclass(frozen=True)
class DomeConfig:
    name: str
    num_positions: int
    capture_strategy: CaptureStrategy
    max_burst: int
    light_controller: str  # LIGHT_CCEH_BLE | LIGHT_NONE

    @property
    def uses_bluetooth(self) -> bool:
        return self.light_controller == LIGHT_CCEH_BLE

    @classmethod
    def from_dict(cls, d: dict) -> "DomeConfig":
        return cls(
            name=str(d["name"]),
            num_positions=int(d["num_positions"]),
            capture_strategy=CaptureStrategy(d["capture_strategy"]),
            max_burst=int(d.get("max_burst", 1)),
            light_controller=str(d.get("light_controller", LIGHT_NONE)),
        )


def load_presets() -> dict[str, DomeConfig]:
    """Read the shipped dome presets, keyed by filename stem (e.g. 'cologne').
    Read-only — presets are updated by shipping a new app version."""
    directory = get_ui_path(_PRESETS_DIR)
    presets: dict[str, DomeConfig] = {}
    for filename in sorted(os.listdir(directory)):
        if not filename.endswith(".json"):
            continue
        with open(os.path.join(directory, filename), encoding="utf-8") as f:
            presets[os.path.splitext(filename)[0]] = DomeConfig.from_dict(json.load(f))
    return presets


def apply_preset(qs: QSettings, dome: DomeConfig) -> None:
    """Load a preset's values into the dome/* settings. One-shot: afterwards the
    settings are the source of truth and the user may edit them."""
    qs.setValue(NAME, dome.name)
    qs.setValue(NUM_POSITIONS, dome.num_positions)
    qs.setValue(CAPTURE_STRATEGY, dome.capture_strategy.value)  # store the string, not the enum
    qs.setValue(MAX_BURST, dome.max_burst)
    qs.setValue(LIGHT_CONTROLLER, dome.light_controller)


def current_dome(qs: QSettings) -> DomeConfig:
    """The dome config currently held in QSettings."""
    return DomeConfig(
        name=str(qs.value(NAME, "")),
        num_positions=int(qs.value(NUM_POSITIONS, 60)),
        capture_strategy=CaptureStrategy(
            qs.value(CAPTURE_STRATEGY, CaptureStrategy.APP_PER_SHOT.value)),
        max_burst=int(qs.value(MAX_BURST, 1)),
        light_controller=str(qs.value(LIGHT_CONTROLLER, LIGHT_NONE)),
    )
