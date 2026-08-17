"""Versioned, idempotent QSettings migration for the RTI app.

Mirrors the pattern of papyri's on-disk layout migration (object_layout.py):
a monotonic version key gates idempotent steps. Call `migrate_settings(qs)`
once at startup, before reading any other key.

v1 unbundles the old combined `"profile"` (which mixed camera + dome) into a
`cameraProfile` id plus the `dome/*` config. This is a one-time unbundling that
preserves each install's capture behaviour; afterwards camera and dome are fully
independent (nothing couples them). The camera-centric id rename happens in a
later step with its own version bump.

Papyri keeps its own QSettings store (a different application name) and has a
two-camera model with no dome, so it has its own version steps in
`migrate_papyri_settings`: v1 renames profile ids; v2 moves the legacy
capture-sharpness switch into the per-audit settings group.
"""
from __future__ import annotations

from PyQt6.QtCore import QSettings

from byzanz_camera import dome_config
from byzanz_camera.dome_config import DomeConfig, CaptureStrategy

SETTINGS_VERSION = "settingsVersion"
CURRENT_SETTINGS_VERSION = 2

# Papyri audit settings. Kept beside their migration so old and new keys
# cannot drift; papyri.audits.sharpness is the sole runtime reader.
PAPYRI_SHARPNESS_ENABLED_KEY = "audits/sharpness/enabled"
PAPYRI_SHARPNESS_VIS_THRESHOLD_KEY = "audits/sharpness/visWarnFrom"
PAPYRI_SHARPNESS_IR_THRESHOLD_KEY = "audits/sharpness/irWarnFrom"
_PAPYRI_LEGACY_SHARPNESS_ENABLED_KEY = "sharpnessCheckEnabled"
PAPYRI_CURRENT_SETTINGS_VERSION = 2

# Camera-centric id rename (v2). The old ids conflated camera + dome
# ("CCeHDome…", "ParisDome…"); the new ones name only the camera body. Applied
# to every stored camera-profile id (RTI's cameraProfile, papyri's profile /
# irProfile).
_ID_RENAME = {
    "CCeHDomeNikonD800E":    "NikonD800E",
    "ParisDomeSonyIlce7RM5": "SonyA7RM5",
    "MoritzA7III":           "SonyA7III",
}

# Old bundled "profile" id → the dome it implied:
#   (preset display name, capture_strategy value, base light_controller,
#   show_capture_instructions). Values mirror the presets in dome_presets/.
# num_positions was always 60 (the old num_captures); max_burst carries over
# from the old `maxBurstNumber` setting. Virtual cameras are not domes and are
# absent here — they migrate to a camera id with no dome seed.
_V1_DOME_SEED = {
    "CCeHDomeNikonD800E":    ("Cologne (CCeH)", "camera_burst",      dome_config.LIGHT_CCEH_BLE, True),
    "ParisDomeSonyIlce7RM5": ("Paris",          "external_per_shot", dome_config.LIGHT_NONE,     False),
    "MoritzA7III":           ("Manual",         "external_per_shot", dome_config.LIGHT_NONE,     False),
}


def migrate_settings(qs: QSettings) -> None:
    """Bring the RTI app's `qs` up to CURRENT_SETTINGS_VERSION. Idempotent."""
    version = int(qs.value(SETTINGS_VERSION, 0))
    if version < 1:
        _migrate_v1_unbundle_profile(qs)
    if version < 2:
        _rename_camera_id(qs, "cameraProfile")
    qs.setValue(SETTINGS_VERSION, CURRENT_SETTINGS_VERSION)


def migrate_papyri_settings(qs: QSettings) -> None:
    """Bring papyri's independent QSettings schema to its current version."""
    version = int(qs.value(SETTINGS_VERSION, 0))
    if version < 1:
        _rename_camera_id(qs, "profile")
        _rename_camera_id(qs, "irProfile")
    if version < 2:
        _migrate_papyri_v2_audit_settings(qs)
    qs.setValue(SETTINGS_VERSION, PAPYRI_CURRENT_SETTINGS_VERSION)


def _migrate_papyri_v2_audit_settings(qs: QSettings) -> None:
    """v1 -> v2: move the capture-sharpness gate into its audit group."""
    if qs.value(PAPYRI_SHARPNESS_ENABLED_KEY) is None:
        legacy = qs.value(_PAPYRI_LEGACY_SHARPNESS_ENABLED_KEY)
        if legacy is not None:
            qs.setValue(PAPYRI_SHARPNESS_ENABLED_KEY, qs.value(
                _PAPYRI_LEGACY_SHARPNESS_ENABLED_KEY, type=bool))
    qs.remove(_PAPYRI_LEGACY_SHARPNESS_ENABLED_KEY)


def _rename_camera_id(qs: QSettings, key: str) -> None:
    old = qs.value(key)
    if old in _ID_RENAME:
        qs.setValue(key, _ID_RENAME[old])


def _migrate_v1_unbundle_profile(qs: QSettings) -> None:
    old_profile = qs.value("profile")
    if old_profile is None:
        return  # fresh install — nothing to unbundle; defaults are seeded elsewhere

    qs.setValue("cameraProfile", old_profile)  # id unchanged here (rename is a later step)

    seed = _V1_DOME_SEED.get(old_profile)
    if seed is not None:
        name, strategy, base_light, show_instructions = seed
        # Preserve the old BLE state: only keep cceh_ble if BT was enabled.
        enable_bt = qs.value("enableBluetooth", False, type=bool)
        light = base_light if (base_light != dome_config.LIGHT_CCEH_BLE or enable_bt) else dome_config.LIGHT_NONE
        dome = DomeConfig(
            name=name,
            num_positions=60,
            capture_strategy=CaptureStrategy(strategy),
            max_burst=int(qs.value("maxBurstNumber", 60)),
            light_controller=light,
            show_capture_instructions=show_instructions,
        )
        dome_config.apply_preset(qs, dome)

    for retired in ("profile", "maxBurstNumber", "enableBluetooth"):
        qs.remove(retired)
