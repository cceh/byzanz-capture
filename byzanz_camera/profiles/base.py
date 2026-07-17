from abc import ABC, abstractmethod


class Profile(ABC):
    @abstractmethod
    def name(self) -> str:
        """Return user-friendly name of the camera driver"""
        pass

    def gphoto2_model_pattern(self) -> str | None:
        """Substring of the gphoto2-detected model name that should match a
        camera using this profile. Default `None` = match the first detected
        camera (preserves existing single-camera behavior). Subclasses
        override to enable model-based identification when multiple cameras
        are connected (e.g. visible + IR).
        """
        return None

    def gphoto2_port(self) -> str | None:
        """Exact gphoto2 port path this profile must bind to, or `None` to
        pick the first camera matching gphoto2_model_pattern (the normal
        case — a real camera's USB port is discovered at detection time and
        must not be hard-pinned, or it wouldn't survive re-enumeration).

        Only the virtual-camera profiles override this: the vusb emulator
        exposes two identical "Nikon DSC D750" cameras that differ only by
        port ("vusb:" vs "vusb:2"), so pinning the port is the only way to
        assign one to the visible slot and the other to IR."""
        return None

    def has_settable_aperture(self) -> bool:
        """Whether the body can drive the aperture electronically. Return
        False for a manual aperture-ring lens (e.g. D90 + CoastalOpt 60/4
        UV-VIS-IR), where the f-number combo would be inert — the UI then
        leaves that combo disabled instead of offering dead choices."""
        return True

    def supports_autofocus(self) -> bool:
        """Whether autofocus can be triggered. Return False for a
        manual-focus-only lens, so the UI keeps the autofocus button
        disabled even in live view."""
        return True

    def supports_live_view(self) -> bool:
        """Whether the camera can stream a live preview via
        capture_preview(). Default True — every real body here does. Return
        False for cameras without live view (e.g. the vusb virtual camera,
        whose capture_preview() returns "[-6] Unsupported operation"); the
        worker then never enters the preview loop, which would otherwise
        error out on the first frame and tear down the connection."""
        return True

    def focus_magnify_property_name(self) -> str | None:
        """gphoto2 config key that toggles the live-view focus zoom (a
        focusing aid that magnifies the live preview), or `None` if this
        body can't do it — then the UI hides the magnify button. The UI
        only knows "magnify on/off"; the profile maps that to the
        camera-specific PTP property and values (see focus_magnify_value).
        Mirrors the `None`-means-unsupported convention of
        gphoto2_model_pattern."""
        return None

    def focus_magnify_value(self, on: bool) -> str:
        """Value to write to focus_magnify_property_name() to turn the
        live-view focus zoom on / off. The magnification step lives here
        in the profile (each body's choices differ)."""
        return ""

    @abstractmethod
    def poll_config(self) -> list[str] | None:
        pass

    @abstractmethod
    def enable_capture_controls_in_live_preview(self) -> bool:
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

    @abstractmethod
    def capture_format_raw_settings(self):
        pass
