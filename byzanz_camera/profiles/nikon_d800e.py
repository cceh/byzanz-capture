from .base import FocusSignal, Profile
from .nikon_ptp_errors import NikonPTPError


class NikonD800E(Profile):
    def name(self) -> str:
        return "Nikon D800E"

    def gphoto2_model_pattern(self) -> str:
        # gphoto2 typically reports the D800/D800E as "Nikon DSC D800".
        # Adjust if your specific firmware reports differently.
        return "Nikon DSC D800"

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
        return "d030"

    def poll_config(self):
        return None

    def enable_capture_controls_in_live_preview(self) -> bool:
        return False

    def initial_settings(self):
        return {
            "expprogram": "M"
            # "500e": "4",                     # Exposure Program Mode: manual
            # "whitebalance": "Daylight",
            # "d1a7": "2"                      # Enable release w/o card
        }

    def start_autofocus_settings(self):
        return {
            "autofocusdrive": 1,     # AF-S
            "focusmetermode": "Single Area"
        }

    def stop_autofocus_settings(self):
        return {
            "autofocusdrive": 0,
        }

    def focus_signal(self):
        # autofocusdrive blocks until AF completes; failure surfaces as a
        # PTP error, not through a status widget.
        return FocusSignal(failure_ptp_error=NikonPTPError.OutOfFocus)

    def start_live_view_settings(self):
        return {
            "viewfinder": 1,
            "liveviewsize": "VGA",
            "expprogram": "M"
        }

    def stop_live_view_settings(self):
        return {
            "viewfinder": 0,
            "autofocusdrive": 0
        }

    def start_capture_settings(self):
        return {
            "viewfinder": 1,
            "capturetarget": "Internal RAM",
            "recordingmedia": "SDRAM",
            "autofocusdrive": 0,
            "focusmode": "Manual",
            "focusmode2": "MF (fixed)",
            "imagesize": "0",
            # "autoiso": "Aus",
            "expprogram": "M",
            # "focusmode": "Manual",
            # "500e": "4",                  # Exposure Program: Manual
            # "whitebalance": "Daylight",
            # "d1a7": "2"                   # Enable release w/o card
            #"imagequality"
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