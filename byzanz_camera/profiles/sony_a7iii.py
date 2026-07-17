from .base import Profile


class SonyA7III(Profile):
    def name(self) -> str:
        return "Sony A7 III"

    def gphoto2_model_pattern(self) -> str:
        # Verified via `gphoto2 --auto-detect` against this camera:
        # "Sony Alpha-A7 III (PC Control)".
        return "Alpha-A7 III"

    def burstnumber_property_name(self):
        return None

    def iso_property_name(self):
        return "iso"

    def shutterspeed_property_name(self):
        # Sony's gphoto2 driver names the exposure-time property
        # "shutterspeed" and the aperture property "f-number" (these were
        # previously swapped — see SonyA7RM5).
        return "shutterspeed"

    def f_number_property_name(self):
        return "f-number"

    def image_format_property_name(self):
        return "aspectratio"

    def poll_config(self):
        return ["iso", "f-number", "shutterspeed", "aspectratio"]

    def enable_capture_controls_in_live_preview(self) -> bool:
        return True

    def initial_settings(self):
        return {
 #          "500e": "4",                     # Exposure Program Mode: manual
 #          "whitebalance": "Daylight",
 #          "d1a7": "2"                      # Enable release w/o card
            # Decouple AF from the shutter: capture (trigger_capture) must
            # NEVER refocus. Focusing happens only on demand via the app's
            # autofocus button (the separate `autofocus` action below — S1
            # half-press emulation, independent of the shutter). AF-S holds
            # focus after that command, so it stays "locked" until the next
            # AF trigger. Workflow: AF button -> focus & hold -> capture only
            # releases the shutter.
            "afwithshutter": "Off",
            # Lens distortion compensation: 1=Off, 2=Auto (Sony vendor PTP
            # property, same code as on the A7R V — unknown keys are logged
            # and skipped by __try_set_config, so this is safe if this body
            # names it differently). Keeps the embedded JPEG previews that
            # the stitching check/preview read geometrically corrected.
            "d1a4": "2",
        }

    def start_autofocus_settings(self):
        return {
            "focusmode": "Automatic",      # AF-S
            # Intentionally NOT re-enabling afwithshutter here — that would
            # re-couple AF to the shutter and bring back refocus-on-capture.
            # The `autofocus` action triggers AF on its own.
            "autofocus": 1
        }

    def stop_autofocus_settings(self):
        return {
            # Lock focus by dropping to Manual once the AF button's focus
            # completes: the lens holds its current position and the camera
            # can't refocus. start_autofocus switches back to AF-S for the
            # next AF button press.
            "focusmode": "Manual",
            "autofocus": 0
        }

    def start_live_view_settings(self):
        return {
            "focusmode": "Automatic",  # AF-S
            "afwithshutter": "Off",    # re-assert: shutter never autofocuses
        }

    def stop_live_view_settings(self):
        return {
            "autofocus": 0
        }

    def start_capture_settings(self):
        return {
            # Force Manual focus right before the shutter fires — applied by
            # the worker immediately before trigger_capture(). This is the
            # hard guarantee that capture never autofocuses, regardless of
            # what live view left focusmode at (afwithshutter=Off alone
            # wasn't enough on this body). Switching AF-S -> MF holds the
            # lens at its last-focused position, so a prior AF-button focus
            # is preserved.
            "focusmode": "Manual",
            # "capturetarget": "sdram",
            # "autofocus": 0,
            # "500e": "4",                  # Exposure Program: Manual
            # "whitebalance": "Daylight",
            # "d1a7": "2",                   # Enable release w/o card
            # "jpegquality": "X.Fine"
        }

    def stop_capture_settings(self):
        return {

        }

    def capture_format_jpeg_settings(self):
        return {
            "imagequality": "JPEG"
        }

    def capture_format_jpeg_and_raw_settings(self):
        return {
            "imagequality": "RAW+JPEG"
        }

    def capture_format_raw_settings(self):
        return {
            "imagequality": "RAW"
        }