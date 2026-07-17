"""Single source of truth for camera profiles — every camera in every app.

Both the RTI app and papyri do `from byzanz_camera.profiles import PROFILES`, so
adding a camera is: create its `Profile` subclass module, then add one line to
the dict below. It then appears in every app variant's camera dropdown.

The dict keys are the ids persisted in QSettings (cameraProfile / profile /
irProfile); keep them stable, and add a rename step in `settings_migration` if
one ever has to change.

Explicit, not auto-discovered: the apps ship as PyInstaller bundles, where
module auto-discovery (pkgutil) is unreliable. An explicit list is
PyInstaller-safe and keeps the dropdown order deterministic.
"""
from .base import Profile
from .nikon_d800e import NikonD800E
from .nikon_d90 import NikonD90
from .sony_a7rm5 import SonyA7RM5
from .sony_a7iii import SonyA7III
from .virtual_camera_vusb import VirtualCameraVusb

PROFILES: dict[str, Profile] = {
    "NikonD800E": NikonD800E(),
    "NikonD90": NikonD90(),
    "SonyA7RM5": SonyA7RM5(),
    "SonyA7III": SonyA7III(),
    "VirtualCameraVusb": VirtualCameraVusb(),
    # Second emulator on the "vusb:2" port (patched vendor build), so the
    # visible AND infrared slots can both run without hardware: assign this to
    # the IR profile in Settings while the visible slot uses the first.
    "VirtualCameraVusb2": VirtualCameraVusb(
        port="vusb:2", name="Virtual Camera 2 (vusb)"
    ),
}
