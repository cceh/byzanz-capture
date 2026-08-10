from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class FocusSignal:
    """How a camera reports that autofocus has finished.

    Three camera classes, all declarative:
    - AF call blocks until done (Nikon autofocusdrive): all defaults.
      AF failure surfaces as a PTP error -> set `failure_ptp_error`.
    - Camera exposes a status widget (Sony d213): set `widget` and the
      value sets; the worker polls until success/failure/timeout.
    - Fire-and-forget without any signal: set `fixed_wait_s` as a blind
      grace period.
    """
    widget: str | None = None
    success_values: frozenset[str] = frozenset()
    failure_values: frozenset[str] = frozenset()
    failure_ptp_error: str | None = None
    fixed_wait_s: float = 0.0
    timeout_s: float = 4.0


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
        capture_preview(). Default True — every body here does, including
        the vusb virtual camera (whose liveview is emulated in the vendor
        build, fork patch 0005). Return False for cameras without
        live view; the worker then never enters the preview loop, which
        would otherwise error out on the first frame and tear down the
        connection."""
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
        """Settings that release the AF trigger (e.g. Sony S1 half-press,
        Nikon autofocusdrive). Must NOT contain the focus lock — that goes
        in lock_focus_settings, which the worker applies afterwards."""
        pass

    @abstractmethod
    def focus_signal(self) -> FocusSignal:
        """How this camera reports AF completion — see FocusSignal."""
        pass

    def lock_focus_settings(self) -> dict:
        """Settings that freeze focus after the AF trigger is released
        (e.g. Sony `focusmode: Manual`). Applied as a separate write after
        stop_autofocus_settings and confirmed by read-back; empty = no
        lock step."""
        return {}

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
