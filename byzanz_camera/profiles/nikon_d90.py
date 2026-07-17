from .base import Profile


class NikonD90(Profile):
    """Profile for the Nikon D90 (PTP mode).

    Built against a live D90 detected as "Nikon DSC D90 (PTP mode)"
    (cameramodel = 'D90'). Every property name and value below was taken
    from that body's actual gphoto2 config tree, so the choices are known
    to exist on this model. Modelled on the D800E dome profile, with the
    D90 differences applied:
      * imagesize choices are resolution strings (no plain "0" index)
      * the D90 does not expose `liveviewsize`, so live-view start omits it
    """

    def name(self) -> str:
        return "Nikon D90"

    def gphoto2_model_pattern(self) -> str:
        # gphoto2 reports the body as "Nikon DSC D90 (PTP mode)".
        return "Nikon DSC D90"

    # NOTE on the IR lens (Coastal Optical Systems 60mm f/4 UV-VIS-IR Macro
    # Apo): it is a chipped AI-P lens — manual focus, but with a CPU and an
    # aperture ring. The ring MUST be parked and locked at its minimum
    # aperture (f/45, the red marking); otherwise the body throws "fEE" and
    # f-number reads the f/655.35 placeholder. With the ring at f/45 the body
    # drives the aperture electronically, so f-number IS settable (verified:
    # set f/8 -> reads f/8, set f/16 -> reads f/16) — hence we keep the
    # inherited has_settable_aperture()=True. Only autofocus is unavailable.

    def supports_autofocus(self) -> bool:
        # The CoastalOpt 60/4 is manual-focus only (no AF motor); it is
        # apochromatic across UV-VIS-IR, so focus set in visible light holds
        # in IR. The autofocus button stays disabled for this body.
        return False

    def focus_magnify_property_name(self) -> str:
        # The D90 magnifies the live view via "liveviewimagezoomratio";
        # "Entire Display" is unzoomed. Essential here since focus is manual.
        return "liveviewimagezoomratio"

    def focus_magnify_value(self, on: bool) -> str:
        return "50%" if on else "Entire Display"

    def burstnumber_property_name(self):
        return "burstnumber"

    def iso_property_name(self):
        return "iso"

    def shutterspeed_property_name(self):
        return "shutterspeed2"

    def f_number_property_name(self):
        return "f-number"

    def image_format_property_name(self):
        return "imagequality"

    def poll_config(self):
        return None

    def enable_capture_controls_in_live_preview(self) -> bool:
        return False

    def initial_settings(self):
        return {
            "expprogram": "M"
        }

    def start_autofocus_settings(self):
        return {
            "autofocusdrive": 1,
            "focusmetermode": "Single Area"
        }

    def stop_autofocus_settings(self):
        return {
            "autofocusdrive": 0,
        }

    def start_live_view_settings(self):
        # The D90 cannot start live view by writing viewfinder=1 — the driver
        # rejects that trahnsition with a generic "[-1] Unspecified error", and
        # because settings are pushed as one set_config() that single bad
        # widget fails the whole push (and tears down the connection).
        #
        # On the D90, live view is started implicitly by capture_preview():
        # the worker's preview loop calls camera.capture_preview(), which makes
        # the Nikon driver enter live view on its own (verified: after the
        # first preview frame, viewfinder reads back as 1). So there is nothing
        # to set here — return an empty dict and let capture_preview drive it.
        return {}

    def stop_live_view_settings(self):
        # viewfinder=0 IS accepted (it ends live view), so use it to leave LV
        # cleanly once capture_preview has turned it on.
        return {
            "viewfinder": 0
        }

    def start_capture_settings(self):
        # IMPORTANT: viewfinder MUST be 0 here. With viewfinder=1 the mirror
        # stays up (live view) and the D90's still capture is taken from the
        # live-view sensor readout — the result is a black frame with only
        # sensor noise. Setting viewfinder=0 drops the mirror so the shot is a
        # normal mechanical-shutter exposure. (The capture path still pulls the
        # file over USB via recordingmedia=SDRAM / capturetarget=Internal RAM;
        # that works fine with the mirror down.)
        return {
            "viewfinder": 0,
            "capturetarget": "Internal RAM",
            "recordingmedia": "SDRAM",
            "autofocusdrive": 0,
            "focusmode": "Manual",
            "focusmode2": "MF (fixed)",
            "imagesize": "4288x2848",
            "expprogram": "M",
        }

    def stop_capture_settings(self):
        return {
            "viewfinder": 0
        }

    def capture_format_jpeg_and_raw_settings(self):
        return {
            "imagequality": "NEF+Fine"
        }

    def capture_format_jpeg_settings(self):
        return {
            "imagequality": "JPEG Fine"
        }

    def capture_format_raw_settings(self):
        return {
            "imagequality": "NEF (Raw)"
        }
