"""Versioned, idempotent QSettings migration — shared by the RTI app and papyri.

Mirrors the pattern of papyri's on-disk layout migration (object_layout.py):
a monotonic version key gates idempotent steps. Call `migrate_settings(qs)`
once at startup, before reading any other key.

v1 unbundles the old combined `"profile"` (which mixed camera + dome) into a
`cameraProfile` id plus the `dome/*` config. This is a one-time unbundling that
preserves each install's capture behaviour; afterwards camera and dome are fully
independent (nothing couples them). The camera-centric id rename happens in a
later step with its own version bump.
"""
from __future__ import annotations

from PyQt6.QtCore import QSettings

from byzanz_camera import dome_config
from byzanz_camera.dome_config import DomeConfig, CaptureStrategy

SETTINGS_VERSION = "settingsVersion"
CURRENT_SETTINGS_VERSION = 1

# Old bundled "profile" id → the dome it implied:
#   (preset display name, capture_strategy value, base light_controller).
# num_positions was always 60 (the old num_captures); max_burst carries over
# from the old `maxBurstNumber` setting. Virtual cameras are not domes and are
# absent here — they migrate to a camera id with no dome seed.
_V1_DOME_SEED = {
    "CCeHDomeNikonD800E":    ("Cologne (CCeH)", "camera_burst",      dome_config.LIGHT_CCEH_BLE),
    "ParisDomeSonyIlce7RM5": ("Paris",          "external_per_shot", dome_config.LIGHT_NONE),
    "MoritzA7III":           ("Manual",         "external_per_shot", dome_config.LIGHT_NONE),
}


def migrate_settings(qs: QSettings) -> None:
    """Bring `qs` up to CURRENT_SETTINGS_VERSION. Idempotent."""
    version = int(qs.value(SETTINGS_VERSION, 0))
    if version < 1:
        _migrate_v1_unbundle_profile(qs)
    qs.setValue(SETTINGS_VERSION, CURRENT_SETTINGS_VERSION)


def _migrate_v1_unbundle_profile(qs: QSettings) -> None:
    old_profile = qs.value("profile")
    if old_profile is None:
        return  # fresh install — nothing to unbundle; defaults are seeded elsewhere

    qs.setValue("cameraProfile", old_profile)  # id unchanged here (rename is a later step)

    seed = _V1_DOME_SEED.get(old_profile)
    if seed is not None:
        name, strategy, base_light = seed
        # Preserve the old BLE state: only keep cceh_ble if BT was enabled.
        enable_bt = qs.value("enableBluetooth", False, type=bool)
        light = base_light if (base_light != dome_config.LIGHT_CCEH_BLE or enable_bt) else dome_config.LIGHT_NONE
        dome = DomeConfig(
            name=name,
            num_positions=60,
            capture_strategy=CaptureStrategy(strategy),
            max_burst=int(qs.value("maxBurstNumber", 60)),
            light_controller=light,
        )
        dome_config.apply_preset(qs, dome)

    for retired in ("profile", "maxBurstNumber", "enableBluetooth"):
        qs.remove(retired)
