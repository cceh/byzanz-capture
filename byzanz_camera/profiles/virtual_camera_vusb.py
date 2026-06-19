from .base import Profile


class VirtualCameraVusb(Profile):
    """Profile for libgphoto2's built-in virtual camera (the `vusb` port
    driver + `ptp2` camlib), for running the app without real hardware.

    Requires the patched vendor build — see scripts/bootstrap-gphoto2.sh and
    vendor/patches/. With those applied the virtual camera coexists with a
    real USB camera in the same process: autodetect reports it as
    "Nikon DSC D750" on the `vusb:` port, so this profile can be assigned to
    one camera slot (e.g. IR) while a real body drives the other (VIS).

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

    def name(self) -> str:
        return "Virtual Camera (vusb)"

    def gphoto2_model_pattern(self) -> str:
        # The vcamera defaults to emulating a Nikon D750; autodetect reports
        # it as "Nikon DSC D750" (see libgphoto2_port/vusb/vusb.c).
        return "Nikon DSC D750"

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

    def supports_chs(self):
        return False

    def manual_trigger(self):
        return False

    def num_captures(self):
        return 1

    def use_burst(self):
        # The emulator emits a clean per-trigger CAPTURE_COMPLETE, so the
        # non-burst path (one trigger → one shot) fits. It also has no
        # `burstnumber` config for the burst path to drive.
        return False

    def burstnumber_property_name(self):
        return "burstnumber"  # unused (use_burst() is False)

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
