from abc import ABC, abstractmethod


class Profile(ABC):
    @abstractmethod
    def name(self) -> str:
        """Return user-friendly name of the camera driver"""
        pass

    @abstractmethod
    def supports_chs(self):
        pass

    @abstractmethod
    def manual_trigger(self):
        pass

    @abstractmethod
    def poll_config(self) -> list[str] | None:
        pass

    @abstractmethod
    def num_captures(self):
        pass

    @abstractmethod
    def enable_capture_controls_in_live_preview(self) -> bool:
        pass

    @abstractmethod
    def use_burst(self):
        pass

    @abstractmethod
    def burstnumber_property_name(self):
        pass

    @abstractmethod
    def iso_property_name(self):
        pass

    @abstractmethod
    def shutterspeed_property_name(self):
        pass

    @abstractmethod
    def f_number_property_name(self):
        pass

    @abstractmethod
    def image_format_property_name(self):
        pass

    @abstractmethod
    def initial_settings(self):
        pass

    @abstractmethod
    def start_autofocus_settings(self):
        pass

    @abstractmethod
    def stop_autofocus_settings(self):
        pass

    @abstractmethod
    def start_live_view_settings(self):
        pass

    @abstractmethod
    def stop_live_view_settings(self):
        pass

    @abstractmethod
    def start_capture_settings(self):
        pass

    @abstractmethod
    def stop_capture_settings(self):
        pass

    @abstractmethod
    def capture_format_jpeg_settings(self):
        pass

    @abstractmethod
    def capture_format_jpeg_and_raw_settings(self):
        pass
