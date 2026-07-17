from .base import Profile


class VirtualCameraVusb(Profile):
    """Profile for libgphoto2's built-in virtual camera (the `vusb` port
    driver + `ptp2` camlib), for running the app without real hardware.

    Requires the patched vendor build — see scripts/bootstrap-gphoto2.sh and
    vendor/patches/. With those applied the emulator coexists with a real USB
    camera in the same process AND exposes two independent virtual cameras,
    reported by autodetect as "Nikon DSC D750" on the `vusb:` and `vusb:2`
    ports. So a single instance can back one slot (e.g. IR) alongside a real
    body (VIS), or — with `VirtualCameraVusb2` pinned to `vusb:2` — both the
    visible and IR slots can run on the emulator with no hardware at all.
    The `port` ctor arg is what assigns an instance to a specific vusb port
    (they are otherwise indistinguishable — same model name).

    The emulator is deliberately minimal. What it does and does NOT do
    shapes every method below:
      * Capture works via trigger_capture(): it duplicates a JPEG seeded
        into its data dir (bootstrap's seed_vcamera) and emits FILE_ADDED +
        CAPTURE_COMPLETE — so the normal non-burst capture path applies.
      * It cannot do live view: capture_preview() returns
        "[-6] Unsupported operation". Hence supports_live_view() is False,
        which stops the worker from auto-starting the preview loop (which
        would otherwise error out and tear down the connection).
      * It has no autofocus, no settable image format/quality, and its
        exposure properties don't affect the duplicated JPEG. The settings
        dicts are therefore empty: writes to absent config keys are
        harmless (the worker's __try_set_config swallows them), but there's
        nothing real to set.
    """

    def __init__(self, port: str = "vusb:", name: str = "Virtual Camera (vusb)"):
        # `port` pins this profile to a specific vusb port. The patched
        # vendor build exposes two identical "Nikon DSC D750" cameras on
        # "vusb:" and "vusb:2" (see libgphoto2_port/vusb/vusb.c), so papyri
        # can run BOTH the visible and IR slots on the emulator by giving
        # each worker a different port — the model pattern alone can't tell
        # two identical virtual cameras apart.
        self._port = port
        self._name = name

    def name(self) -> str:
        return self._name

    def gphoto2_model_pattern(self) -> str:
        # The vcamera defaults to emulating a Nikon D750; autodetect reports
        # it as "Nikon DSC D750" (see libgphoto2_port/vusb/vusb.c).
        return "Nikon DSC D750"

    def gphoto2_port(self) -> str:
        return self._port

    def supports_live_view(self) -> bool:
        # capture_preview() is unsupported by the emulator. Returning False
        # keeps the worker from entering the live-view loop entirely.
        return False

    def supports_autofocus(self) -> bool:
        return False

    def has_settable_aperture(self) -> bool:
        # The emulated f-number is cosmetic — it doesn't change the produced
        # image. Leave the aperture combo disabled rather than offer a
        # control that silently does nothing.
        return False

    def burstnumber_property_name(self):
        # Unused: the emulator emits a clean per-trigger CAPTURE_COMPLETE and
        # has no `burstnumber` config, so no dome ever bursts it.
        return "burstnumber"

    def iso_property_name(self):
        return "iso"

    def shutterspeed_property_name(self):
        return "shutterspeed"

    def f_number_property_name(self):
        return "f-number"

    def image_format_property_name(self):
        return "imageformat"

    def poll_config(self):
        # The emulator's config is static — nothing useful to poll.
        return None

    def enable_capture_controls_in_live_preview(self) -> bool:
        return False

    def initial_settings(self):
        return {}

    def start_autofocus_settings(self):
        return {}

    def stop_autofocus_settings(self):
        return {}

    def start_live_view_settings(self):
        return {}

    def stop_live_view_settings(self):
        return {}

    def start_capture_settings(self):
        return {}

    def stop_capture_settings(self):
        return {}

    def capture_format_jpeg_settings(self):
        return {}

    def capture_format_jpeg_and_raw_settings(self):
        return {}

    def capture_format_raw_settings(self):
        return {}
