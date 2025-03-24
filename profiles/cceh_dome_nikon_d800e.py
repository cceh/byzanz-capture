from profiles.base import Profile


class CCeHDomeNikonD800E(Profile):
    def name(self) -> str:
        return "CCeH Dome with Nikon D800E"

    def supports_chs(self):
        return False

    def manual_trigger(self):
        return False

    def num_captures(self):
        return 60

    def burstnumber_property_name(self):
        return "burstnumber"

    def use_burst(self):
        return True

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