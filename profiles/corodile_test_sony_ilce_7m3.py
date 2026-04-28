from profiles.base import Profile


class ParisDomeSonyIlce7RM5(Profile):
    def name(self) -> str:
        return "Paris Dome with Sony A7alpha 5"

    def supports_chs(self):
        return False

    def manual_trigger(self):
        return True

    def num_captures(self):
        return 60

    def burstnumber_property_name(self):
        return None

    def use_burst(self):
        return False


    def iso_property_name(self):
        return "iso"

    def shutterspeed_property_name(self):
        return "f-number"

    def f_number_property_name(self):
        return "shutterspeed"

    def image_format_property_name(self):
        return "aspectratio"

    def poll_config(self):
        return ["iso", "f-number", "shutterspeed", "aspectratio"]

    def enable_capture_controls_in_live_preview(self) -> bool:
        return True

    def initial_settings(self):
        return {
            "500e": "4",                     # Exposure Program Mode: manual
            "whitebalance": "Daylight",
            "d1a7": "2"                      # Enable release w/o card
        }

    def start_autofocus_settings(self):
        return {
            "focusmode": "Automatic",      # AF-S
            "afwithshutter": "On",
            "autofocus": 1
        }

    def stop_autofocus_settings(self):
        return {
            "focusmode": "Manual",
            "autofocus": 0
        }

    def start_live_view_settings(self):
        return {
            "focusmode": "Automatic",  # AF-S
        }

    def stop_live_view_settings(self):
        return {
            "autofocus": 0
        }

    def start_capture_settings(self):
        return {
            # "capturetarget": "sdram",
            # "autofocus": 0,
            # "focusmode": "Manual",
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