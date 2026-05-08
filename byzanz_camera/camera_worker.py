import io
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from enum import Enum
from string import Template
from time import sleep
from typing import NamedTuple, Literal, Generator, Union, Protocol

import gphoto2 as gp
from PIL import Image
from PyQt6.QtCore import pyqtSignal, QObject, QElapsedTimer, QTimer
from PyQt6.QtWidgets import QApplication

from byzanz_camera._autodetect import (
    autodetect as _gphoto2_autodetect,
    set_libgphoto2_setting as _gphoto2_set_setting,
)

# Shorten the PTP camlib's start-of-init timeout from libgphoto2's default
# of 8000 ms (USB_START_TIMEOUT in camlibs/ptp2/library.c) to 3000 ms.
# When a camera is in a bad PTP state and gp_camera_init() hangs waiting
# for it to respond, this caps the freeze at ~3 s instead of 8 s before
# our outer reconnect loop gets to retry. Should make the user wait less
# after a recovery power cycle.
_gphoto2_set_setting("ptp2", "start_timeout", "3000")
from gphoto2 import CameraWidget

from .profiles.base import Profile

# libgphoto2 is per-camera-thread-safe but its global operations
# (autodetect, port enumeration, abilities listing) are NOT. Concurrent
# calls from multiple worker threads can deadlock or corrupt internal
# state. Serialize them across all CameraWorker instances.
#
# This lock also serves as the "GIL release point" for the SWIG-deadlock
# class of bug — see docs/gphoto2-deadlock-analysis.md. Acquiring this
# Python lock releases the GIL during the wait, which lets another
# worker that's holding a libgphoto2 internal mutex AND wants the GIL
# (for the gp_log_call_python callback) make progress.
_GPHOTO2_GLOBAL_LOCK = threading.Lock()


EVENT_DESCRIPTIONS = {
    gp.GP_EVENT_UNKNOWN: "Unknown",
    gp.GP_EVENT_CAPTURE_COMPLETE: "Capture Complete",
    gp.GP_EVENT_FILE_ADDED: "File Added",
    gp.GP_EVENT_FOLDER_ADDED: "Folder Added",
    gp.GP_EVENT_TIMEOUT: "Timeout"
}

class ConfigProtocol(Protocol):
    def get_child_by_name(self, name: str): ...
    def get_value(self): ...

class PseudoConfig:
    def __init__(self, dict_or_widget: dict[str, gp.CameraWidget] | gp.CameraWidget):
        self.widget = None
        self.dict = None
        if isinstance(dict_or_widget, gp.CameraWidget):
            self.widget: gp.CameraWidget = dict_or_widget
        elif isinstance(dict_or_widget, dict):
            self.dict: dict[str, gp.CameraWidget] = dict_or_widget

    def get_child_by_name(self, name: str):
        if self.widget is not None:
           return self.widget.get_child_by_name(name)
        else:
            return self.dict[name]



class SonyPTPId:
    AF_WITH_SHUTTER = "d1ad"

class NikonPTPError(Enum):
    OutOfFocus = "0xa002"

class ConfigRequest():
    class Signal(QObject):
        got_config = pyqtSignal(gp.CameraWidget)

    def __init__(self):
        self.signal = ConfigRequest.Signal()


class CaptureImagesRequest():
    class CaptureFormat(Enum):
        JPEG = "jpeg"
        JPEG_AND_RAW = "jpeg_and_raw"

    class Signal(QObject):
        file_received = pyqtSignal(str)

    file_path_template: str
    num_images: int
    expect_files: int
    max_burst: int = 1
    manual_trigger: bool = False
    image_quality: CaptureFormat

    def __init__(self, file_path_template, num_images, image_quality, max_burst = 1, manual_trigger = False):
        self.file_path_template = file_path_template
        self.num_images = num_images
        self.expect_files = 2 if image_quality == CaptureImagesRequest.CaptureFormat.JPEG_AND_RAW else 1
        self.max_burst = max_burst
        self.manual_trigger = manual_trigger
        self.image_quality = image_quality

        self.signal = CaptureImagesRequest.Signal()



class LiveViewImage(NamedTuple):
    image: Image.Image

class CameraStates:
    class Waiting:
        pass

    class Found:
        def __init__(self, camera_name: str):
            super().__init__()
            self.camera_name = camera_name

    class Disconnected:
        def __init__(self, camera_name: str, auto_reconnect: bool = True):
            super().__init__()
            self.auto_reconnect = auto_reconnect
            self.camera_name = camera_name

    class Connecting:
        def __init__(self, camera_name: str):
            self.camera_name = camera_name

    class Disconnecting:
        pass

    class Ready:
        def __init__(self, camera_name: str):
            super().__init__()
            self.camera_name = camera_name

    class LiveViewStarted(NamedTuple):
        current_lightmeter_value: int

    class LiveViewActive:
        pass

    class FocusStarted:
        pass

    class FocusFinished(NamedTuple):
        success: bool

    class LiveViewStopped:
        pass

    class CaptureInProgress:
        def __init__(self, capture_request: CaptureImagesRequest, num_captured: int):
            super().__init__()
            self.num_captured = num_captured
            self.capture_request = capture_request

    class CaptureFinished:
        def __init__(self, capture_request, elapsed_time: int, num_captured: int,
                     file_paths: list[str] | None = None):
            super().__init__()
            self.num_captured = num_captured
            self.elapsed_time = elapsed_time
            self.capture_request = capture_request
            # Populated by capture_one (list of saved file paths). RTI's
            # captureImages emits per-file via the request signal and leaves
            # this as None.
            self.file_paths = file_paths or []

    class CaptureCancelling:
        pass

    class CaptureCanceled:
        def __init__(self, capture_request: CaptureImagesRequest, elapsed_time: int):
            super().__init__()
            self.elapsed_time = elapsed_time
            self.capture_request = capture_request

    class CaptureError:
        def __init__(self, capture_request: CaptureImagesRequest, error: str):
            super().__init__()
            self.capture_request = capture_request
            self.error = error

    class ConnectionError:
        def __init__(self, error: gp.GPhoto2Error):
            super().__init__()
            self.error = error

    StateType = Union[
        Waiting, Found, Disconnected, Connecting, Disconnecting, Ready, CaptureInProgress,
        CaptureFinished, CaptureCanceled, CaptureCancelling, CaptureError, ConnectionError,
        LiveViewStarted, LiveViewStopped, LiveViewActive, FocusStarted, FocusFinished
    ]


class CameraCommands(QObject):
    capture_images = pyqtSignal(CaptureImagesRequest)
    find_camera = pyqtSignal()
    connect_camera = pyqtSignal(Profile)
    disconnect_camera = pyqtSignal()
    reconnect_camera = pyqtSignal()
    set_config = pyqtSignal(gp.CameraWidget)
    set_single_config = pyqtSignal(str, str)
    cancel = pyqtSignal()
    live_view = pyqtSignal(bool)
    trigger_autofocus = pyqtSignal()
    get_config = pyqtSignal(ConfigRequest)


class PropertyChangeEvent(NamedTuple):
    property: str
    property_name: str
    value: str | float


class CameraEvents(QObject):
    config_updated = pyqtSignal(PseudoConfig)


class CameraWorker(QObject):
    initialized = pyqtSignal()
    state_changed = pyqtSignal(object)
    property_changed = pyqtSignal(PropertyChangeEvent)
    preview_image = pyqtSignal(LiveViewImage)

    def __init__(self, parent=None):
        super(CameraWorker, self).__init__(parent)

        self.__logger = logging.getLogger(self.__class__.__name__)

        self.__logging_callback_extract_gp2_error = None
        self.__state = None
        self.commands = CameraCommands()
        self.events = CameraEvents()

        self.camera: gp.Camera = None
        self.camera_name: str = None

        # Multi-camera identification (set by the orchestrator before
        # emitting find_camera). When None, behavior matches single-camera
        # mode: pick the first detected camera, no port pinning.
        self.target_model_pattern: str | None = None
        self.target_port: str | None = None

        self.filesCounter = 0
        self.captureComplete = False

        self.shouldCancel = False
        self.timer: QTimer = None

        # Per-capture transient — list of saved file paths populated as
        # FILE_ADDED events arrive in empty_event_queue, snapshotted into
        # CaptureFinished's file_paths at end of captureImages.
        self.captured_file_paths: list[str] = []

        self.__last_ptp_error: NikonPTPError = None
        self.__last_config_poll = 0.0

        self.profile = None

    def initialize(self):
        self.__logger.info("Init Camera Worker")
        self.timer = QTimer()

        self.commands.capture_images.connect(self.captureImages)
        self.commands.find_camera.connect(self.__find_camera)
        self.commands.connect_camera.connect(self.__connect_camera)
        self.commands.disconnect_camera.connect(lambda: self.__disconnect_camera(False))
        self.commands.reconnect_camera.connect(lambda: self.__disconnect_camera(True))
        self.commands.set_config.connect(self.__set_config)
        self.commands.set_single_config.connect(self.__set_single_config)
        self.commands.cancel.connect(self.__cancel)
        self.commands.live_view.connect(lambda active: self.__start_live_view() if active else self.__stop_live_view())
        self.commands.trigger_autofocus.connect(self.__trigger_autofocus)
        self.commands.get_config.connect(self.__get_config)

        self.__set_state(CameraStates.Waiting())

        self.initialized.emit()

        # self.__logging_callback_python_logging = gp.check_result(gp.use_python_logging(mapping={
        #     gp.GP_LOG_ERROR: logging.INFO,
        #     gp.GP_LOG_DEBUG: logging.DEBUG,
        #     gp.GP_LOG_VERBOSE: logging.DEBUG - 3,
        #     gp.GP_LOG_DATA: logging.DEBUG - 6}))

        # Filter at ERROR level (not DEBUG) so the callback fires
        # ~1000x less often during normal operation. This shrinks the
        # deadlock window for the SWIG-GIL-vs-libgphoto2-mutex AB-BA
        # pattern (see docs/gphoto2-deadlock-analysis.md). PTP error
        # detection is preserved: ptp2/usb.c logs all PTP transaction
        # failures at GP_LOG_E with the response code as `(0x%04x)`,
        # which is what __extract_gp2_error_from_log scans for.
        self.__logging_callback_extract_gp2_error = gp.check_result(
            gp.gp_log_add_func(gp.GP_LOG_ERROR, self.__extract_gp2_error_from_log))

    def __extract_gp2_error_from_log(self, _level: int, domain: bytes, string: bytes, _data=None):
        error_str = string
        for ptp_error in NikonPTPError:
            error_suffix = "(%s)" % ptp_error.value
            if error_str.endswith(error_suffix):
                self.__last_ptp_error = ptp_error
                self.__logger.debug("PTP Error: {} {}".format(ptp_error, error_suffix))

    def __set_state(self, state: CameraStates.StateType):
        self.__state = state
        self.__logger.debug("Set camera state: " + state.__class__.__name__)
        self.state_changed.emit(state)

    @staticmethod
    def __handle_camera_error(func):
        def wrapper(self, *args, **kwargs):
            try:
                return func(self, *args, **kwargs)
            except gp.GPhoto2Error as err:
                self.__logger.exception("Camera Error {0}: {1}".format(err.code, err.string))
                self.__set_state(CameraStates.ConnectionError(error=err))
                self.__disconnect_camera()

        return wrapper

    def __find_camera(self):
        self.__set_state(CameraStates.Waiting())
        pattern = self.target_model_pattern  # snapshot — orchestrator may rebind
        found = None
        log_target = f" matching {pattern!r}" if pattern else ""

        while not found and not self.thread().isInterruptionRequested():
            self.__logger.info(f"Waiting for camera{log_target}...")
            # `_gphoto2_autodetect()` uses a ctypes wrapper around
            # `gp_camera_autodetect` that releases the GIL during the USB
            # scan, so the Qt UI thread isn't frozen for the duration.
            # Falls back to gp.Camera.autodetect() if ctypes can't resolve
            # the symbols (see byzanz_camera._autodetect for details).
            with _GPHOTO2_GLOBAL_LOCK:
                detected = _gphoto2_autodetect()
            for model, port in detected:
                if pattern is None or pattern in model:
                    found = (model, port)
                    break
            if not found:
                sleep(1)

        if not found:
            # Loop exited because of requestInterruption() (e.g. app shutdown),
            # not because a camera was found. Just return — state stays Waiting.
            return

        name, port = found
        self.target_port = port  # remember for __connect_camera to pin via port_info
        self.__set_state(CameraStates.Found(camera_name=name))

    def __apply_settings(self, settings: dict):
        with self.__open_config("write") as cfg:
            print(settings)
            for key, value in settings.items():
                self.__try_set_config(cfg, key, value)


    @__handle_camera_error
    def __connect_camera(self, profile: Profile):
        self.profile = profile
        self.__set_state(CameraStates.Connecting(self.camera_name))
        self.__last_ptp_error = None

        # libgphoto2's PTP init is per-camera in theory but does USB
        # enumeration internally that races with concurrent autodetect /
        # other inits. Serialize the whole gp.Camera() + set_port_info() +
        # init() block under the global lock to be safe.
        self.__logger.info("Acquiring global lock to init %s on %s",
                            self.target_model_pattern or "(any)", self.target_port or "(auto)")
        with _GPHOTO2_GLOBAL_LOCK:
            self.camera = gp.Camera()
            if self.target_port is not None:
                port_info_list = gp.PortInfoList()
                port_info_list.load()
                idx = port_info_list.lookup_path(self.target_port)
                self.camera.set_port_info(port_info_list[idx])
            self.__logger.info("Calling camera.init() …")
            self.camera.init()
            self.__logger.info("camera.init() returned")
        self.empty_event_queue(1000)
        self.__apply_settings(profile.initial_settings())

        with self.__open_config("read") as cfg:
            self.events.config_updated.emit(PseudoConfig(cfg))
            self.camera_name = "%s %s" % (
                cfg.get_child_by_name("manufacturer").get_value(),
                cfg.get_child_by_name("cameramodel").get_value()
            )

        self.__set_state(CameraStates.Ready(self.camera_name))

        while self.camera:
            if self.thread().isInterruptionRequested():
                self.__disconnect_camera()
                self.thread().exit()
                return

            try:
                if self.profile.poll_config() is not None:
                    if not isinstance(self.__state, CameraStates.CaptureInProgress):
                        current_time = time.time()
                        if current_time - self.__last_config_poll >= 0.5:
                            self.__emit_current_config(self.profile.poll_config())
                            self.__last_config_poll = current_time

                if isinstance(self.__state, CameraStates.LiveViewActive):
                    self.__live_view_capture_preview()
                    self.thread().msleep(50)
                else:
                    self.empty_event_queue(1)
            finally:
                QApplication.processEvents()

    def __disconnect_camera(self, auto_reconnect=True):
        self.__set_state(CameraStates.Disconnecting())

        # Hold the global lock around BOTH camera.exit() and the
        # `self.camera = None` that triggers SWIG's _wrap_delete_Camera →
        # gp_camera_free → gp_port_free. Without this, the SWIG destructor
        # runs while we still hold the GIL and tries to acquire libgphoto2's
        # internal port mutex; if the OTHER worker is currently inside
        # gp_camera_autodetect (holding that mutex AND waiting for the GIL
        # to invoke gp_log_call_python), we deadlock AB-BA. Acquiring the
        # global lock here releases the GIL during the wait, so the other
        # worker can finish first and free the port mutex.
        with _GPHOTO2_GLOBAL_LOCK:
            try:
                self.camera.exit()
            except gp.GPhoto2Error:
                pass
            except AttributeError:  # Camera already gone
                pass
            # Drop the only Python ref so SWIG destructor runs synchronously
            # inside this lock — when it tries the port mutex, no other
            # worker can be inside libgphoto2 (they'd be waiting for us).
            self.camera = None

        self.__set_state(CameraStates.Disconnected(camera_name=self.camera_name, auto_reconnect=auto_reconnect))
        self.camera_name = None

        # Brief pause before the UI's auto-reconnect handler kicks off another
        # find_camera. Stops us from tight-looping the USB stack after a failed
        # init() (e.g. when another process holds the camera or it's locked up
        # mid-handshake). Also gives the user time to power-cycle the camera.
        if auto_reconnect:
            sleep(2)

    @__handle_camera_error
    def __set_single_config(self, name, value):
        self.__logger.info("1 Set config %s to %s" % (name, value))
        with self.__open_config("write") as cfg:
            # program_widget = cfg.get_child_by_name("expprogram")
            # program_widget.set_value("M")
            cfg_widget = cfg.get_child_by_name(name)
            cfg_widget.set_value(value)

        self.empty_event_queue()
        if self.profile.poll_config() is None:
            self.__emit_current_config()

    @__handle_camera_error
    def __set_config(self, cfg: gp.CameraWidget):
        self.camera.set_config(cfg)

    @__handle_camera_error
    def __get_config(self, req: ConfigRequest):
        with self.__open_config("read") as cfg:
            req.signal.got_config.emit(cfg)

    @__handle_camera_error
    def __emit_current_config(self, config_names: list[str] = None):
       if config_names is not None:
           config_dict = {}
           for name in config_names:
               try:
                   config_dict[name] = self.camera.get_single_config(name)
               except gp.GPhoto2Error:
                   self.__logger.error(f"Could not get config {name}")
                   continue
           self.events.config_updated.emit(PseudoConfig(config_dict))
       else:
           with self.__open_config("read") as cfg:
               self.events.config_updated.emit(PseudoConfig(cfg))

    def __cancel(self):
        self.shouldCancel = True
        self.__set_state(CameraStates.CaptureCancelling())

    def empty_event_queue(self, timeout=100):
        event_type, data = self.camera.wait_for_event(timeout)

        while event_type != gp.GP_EVENT_TIMEOUT:
            self.__logger.info("Event: %s, data: %s" % (EVENT_DESCRIPTIONS.get(event_type, "Unknown"), data))
            QApplication.processEvents()

            if event_type == gp.GP_EVENT_FILE_ADDED:
                cam_file_path = os.path.join(data.folder, data.name)
                self.__logger.info("New file: %s" % cam_file_path)
                basename, extension = os.path.splitext(data.name)

                if isinstance(self.__state, CameraStates.CaptureInProgress) and not self.shouldCancel and not self.thread().isInterruptionRequested():
                    self.filesCounter += 1
                    tpl = Template(self.__state.capture_request.file_path_template)
                    file_target_path = tpl.substitute(
                        basename=basename,
                        extension=extension,
                        num=str(self.filesCounter + 1).zfill(3)
                    )
                    cam_file = self.camera.file_get(
                        data.folder, data.name, gp.GP_FILE_TYPE_NORMAL)
                    self.__logger.info("Saving to %s" % file_target_path)
                    # Write to a `.part` temp file then atomic-rename. Stops
                    # consumers (PhotoBrowser FS watcher, papyri Object refresh)
                    # from seeing a half-written file. PhotoBrowser's filter
                    # ignores `.part` extensions; rawpy only ever opens the
                    # final, fully-written file.
                    # `os.replace` is atomic on both POSIX and NTFS (Python 3.3+
                    # uses MoveFileEx with REPLACE_EXISTING) — `os.rename`
                    # would fail on Windows if the destination already exists.
                    temp_path = file_target_path + ".part"
                    cam_file.save(temp_path)
                    os.replace(temp_path, file_target_path)
                    self.captured_file_paths.append(file_target_path)

                    current_capture_req = self.__state.capture_request
                    current_capture_req.signal.file_received.emit(file_target_path)
                    # Per-file num_captured tracking is only meaningful for the
                    # burst path (where we don't have per-shot CAPTURE_COMPLETE).
                    # The non-burst path updates num_captured per-shot in the
                    # outer loop, so skip the inner update there.
                    if self.profile.use_burst() and self.filesCounter % current_capture_req.expect_files == 0:
                        num_captured = int(self.filesCounter / current_capture_req.expect_files)
                        self.__set_state(CameraStates.CaptureInProgress(self.__state.capture_request, num_captured))


                    remaining = current_capture_req.num_images * current_capture_req.expect_files - self.filesCounter
                    self.__logger.info("Curr. files: {0} (remaining: {1}).".format(self.filesCounter, remaining))



                else:
                    self.__logger.warning(
                        "Received file but capture not in progress, ignoring. State: " + self.__state.__class__.__name__)
                    break

                self.camera.file_delete(data.folder, data.name)

            elif event_type == gp.GP_EVENT_CAPTURE_COMPLETE:
                print("GP_EVENT_CAPTURE_COMPLETE")
                self.captureComplete = True

            elif event_type == gp.GP_EVENT_UNKNOWN:
                #match = re.search(r'PTP Property (\w+) changed, "(\w+)" to "(-?\d+[,\.]\d+)"', data)
                match = re.search(r'PTP Property (\w+) changed, "(.*?)" to "(.*?)"', data)
                if match:
                    property = match.group(1)
                    property_name = match.group(2)
                    value_str = match.group(3)
                    try:
                        value = float(value_str.replace(',', '.'))
                    except ValueError:
                        value = value_str
                    self.property_changed.emit(PropertyChangeEvent(property=property, property_name=property_name, value=value))
                    # print(f"Property '{property_name}' changed to {value}")
                # else:
                    # print(data)
                    # match = re.search(r'PTP Event (\w+)', data)
                    # if match:
                    #     print(f"PTP Event '{match.group(1)}' received")
                else:
                    match = re.search(r'PTP Event (\w+)', data)
                    if match:
                        print(f"PTP Event '{match.group(1)}' received")

            # try to grab another event
            event_type, data = self.camera.wait_for_event(1)

    @__handle_camera_error
    def __start_live_view(self):
        lightmeter: int = 0
        self.__apply_settings(self.profile.start_live_view_settings())
        self.__set_state(CameraStates.LiveViewStarted(current_lightmeter_value=lightmeter))
        self.__set_state(CameraStates.LiveViewActive())

    @__handle_camera_error
    def __live_view_capture_preview(self):
        lightmeter = None
        try:
            camera_file = self.camera.capture_preview()
            file_data = camera_file.get_data_and_size()
            image = Image.open(io.BytesIO(file_data))

            self.empty_event_queue(1)
            self.preview_image.emit(LiveViewImage(image=image))

            #
            # self.preview_image.emit(LiveViewImage(image=image, lightmeter_value=lightmeter))
        except gp.GPhoto2Error:
            self.__stop_live_view()

    @__handle_camera_error
    def __stop_live_view(self):
        if isinstance(self.__state, CameraStates.LiveViewActive):
            self.__apply_settings(self.profile.stop_live_view_settings())
            self.__set_state(CameraStates.LiveViewStopped())
            self.__set_state(CameraStates.Ready(self.camera_name))

    @__handle_camera_error
    def __trigger_autofocus(self):
        lightmeter = None
        self.__set_state(CameraStates.FocusStarted())
        try:
            self.__apply_settings(self.profile.start_autofocus_settings())
            # with self.__open_config("write") as cfg:
            #     # lightmeter = cfg.get_child_by_name("lightmeter").get_value()
            self.__set_state(CameraStates.FocusFinished(success=True))
        except gp.GPhoto2Error:
            if self.__last_ptp_error == NikonPTPError.OutOfFocus:
                # self.__logger.warning("Could not get focus (light: %s)." % lightmeter)
                self.__set_state(CameraStates.FocusFinished(success=False))
            else:
                raise
        finally:
            self.__apply_settings(self.profile.stop_autofocus_settings())
            self.__set_state(CameraStates.LiveViewActive())
            # TODO handle general camera error

    def captureImages(self, capture_req: CaptureImagesRequest):
        # Stop live view inline (apply settings, no state transition). Going
        # through __stop_live_view here would emit LiveViewStopped → Ready,
        # and any client that auto-resumes live view on Ready would queue a
        # live_view(True) command that fires during processEvents() below —
        # racing with file handling and trapping the loop in endless retries.
        if isinstance(self.__state, CameraStates.LiveViewActive):
            try:
                self.__apply_settings(self.profile.stop_live_view_settings())
            except gp.GPhoto2Error:
                pass  # not fatal; we're about to capture

        self.__logger.info("Start capture (%s)", str(capture_req))

        timer = QElapsedTimer()
        try:
            timer.start()
            self.empty_event_queue()
            self.filesCounter = 0
            self.captured_file_paths.clear()
            self.captureComplete = False

            self.__set_state(CameraStates.CaptureInProgress(capture_request=capture_req, num_captured=0))

            self.__apply_settings(self.profile.start_capture_settings())

            if capture_req.image_quality == CaptureImagesRequest.CaptureFormat.JPEG_AND_RAW:
                self.__apply_settings(self.profile.capture_format_jpeg_and_raw_settings())
            elif capture_req.image_quality == CaptureImagesRequest.CaptureFormat.JPEG:
                self.__apply_settings(self.profile.capture_format_jpeg_settings())

            # Two outer-loop strategies depending on the profile:
            # 1. Burst (Nikon dome): one trigger fires N shots and we don't
            #    get a reliable per-shot CAPTURE_COMPLETE, so we keep the
            #    historical "count files until num_images × expect_files"
            #    pattern.
            # 2. Non-burst (Sony, etc.): per-shot CAPTURE_COMPLETE bounds each
            #    shot. expect_files isn't consulted — the camera tells us how
            #    many files it produced (handles RAW-only, JPEG-only, AEB
            #    brackets, multi-exposure, etc. uniformly).

            if self.profile.use_burst():
                # ---- BURST PATH (existing logic, unchanged) ----
                remaining = capture_req.num_images * capture_req.expect_files
                while remaining > 0 and not self.shouldCancel and not self.thread().isInterruptionRequested():
                    burst = min(capture_req.max_burst, int(remaining / capture_req.expect_files))
                    if not capture_req.manual_trigger:
                        with self.__open_config("write") as cfg:
                            self.__try_set_config(cfg, self.profile.burstnumber_property_name(), burst)

                    self.captureComplete = False
                    if not capture_req.manual_trigger:
                        self.camera.trigger_capture()

                    while not self.captureComplete and not self.shouldCancel and not self.thread().isInterruptionRequested():
                        self.empty_event_queue(timeout=100)
                        QApplication.processEvents()

                    remaining = capture_req.num_images * capture_req.expect_files - self.filesCounter
                    self.__logger.info("Burst: {0} files (remaining: {1}).".format(self.filesCounter, remaining))

                    if not capture_req.manual_trigger:
                        with self.__open_config("write") as cfg:
                            self.__try_set_config(cfg, self.profile.burstnumber_property_name(), burst)

                num_captured = int(self.filesCounter / capture_req.expect_files)
            else:
                # ---- NON-BURST PATH (per-shot CAPTURE_COMPLETE) ----
                shot_idx = 0
                while shot_idx < capture_req.num_images and not self.shouldCancel and not self.thread().isInterruptionRequested():
                    self.captureComplete = False
                    if not capture_req.manual_trigger:
                        self.camera.trigger_capture()

                    # Wait for CAPTURE_COMPLETE; FILE_ADDED events are saved
                    # by empty_event_queue's handler and tracked in
                    # self.captured_file_paths.
                    while not self.captureComplete and not self.shouldCancel and not self.thread().isInterruptionRequested():
                        self.empty_event_queue(timeout=100)
                        QApplication.processEvents()

                    # Brief grace window for late FILE_ADDED events that arrive
                    # after CAPTURE_COMPLETE on slower cameras.
                    self.empty_event_queue(timeout=300)
                    QApplication.processEvents()

                    shot_idx += 1
                    self.__set_state(CameraStates.CaptureInProgress(
                        capture_request=capture_req, num_captured=shot_idx
                    ))
                    self.__logger.info("Shot {0}/{1}: {2} files so far".format(
                        shot_idx, capture_req.num_images, self.filesCounter))

                num_captured = shot_idx

            self.__logger.info("No. files captured: {0} ({1} ms).".format(
                self.filesCounter, timer.elapsed()))

            if not self.shouldCancel:
                self.__set_state(CameraStates.CaptureFinished(
                    capture_req,
                    elapsed_time=timer.elapsed(),
                    num_captured=num_captured,
                    file_paths=list(self.captured_file_paths),
                ))
            else:
                self.__logger.info("Capture cancelled")
                self.__set_state(CameraStates.CaptureCanceled(capture_req, elapsed_time=timer.elapsed()))

        except gp.GPhoto2Error as err:
            self.__set_state(CameraStates.CaptureError(capture_req, err.string))
        finally:
            self.shouldCancel = False
            # If camera is still there, try to reset Camera to a default state
            if self.camera:
                try:
                    self.__apply_settings(self.profile.stop_capture_settings())
                    # with self.__open_config("write") as cfg:
                    #     # TODO: enable again when trigger works
                    #     self.__try_set_config(cfg, "viewfinder", 0)
                    #     # if not capture_req.manual_trigger:
                    #     #     self.__try_set_config(cfg, "burstnumber", 1)
                    self.empty_event_queue()
                    self.__set_state(CameraStates.Ready(self.camera_name))
                except gp.GPhoto2Error as err:
                    self.__set_state(CameraStates.ConnectionError(err.string))

    @contextmanager
    def __open_config(self, mode: Literal["read", "write"]) -> Generator[CameraWidget, None, None]:
        cfg: CameraWidget = None
        try:
            cfg = self.camera.get_config()
            yield cfg
        finally:
            if mode == "write":
                self.camera.set_config(cfg)
            elif not mode == "read":
                raise Exception("Invalid cfg open mode: %s" % mode)

    def __try_set_config(self, config: CameraWidget, name: str, value) -> None:
        try:
            config_widget = config.get_child_by_name(name)
            self.__logger.info("2 Set config '%s' to %s." % (name, str(value)))
            config_widget.set_value(value)
        except gp.GPhoto2Error:
            self.__logger.error("Config '%s' not supported by camera." % name)

    def __get_config_diff(self, old_config: gp.CameraWidget, new_config: gp.CameraWidget) -> list[tuple[str, str, str]]:
        """
        Compare two camera configurations and return differences.
        Returns list of tuples containing (name, old_value, new_value).
        """
        changes = []

        def traverse_config(config: gp.CameraWidget, path=""):
            count = config.count_children()
            for i in range(count):
                child = config.get_child(i)
                child_path = f"{path}/{child.get_name()}" if path else child.get_name()
                if child.count_children() > 0:
                    traverse_config(child, child_path)
                else:
                    return child_path, child.get_value()

        old_values = {}
        new_values = {}

        # Build dictionaries of all values
        def build_value_dict(config, values_dict):
            count = config.count_children()
            for i in range(count):
                child = config.get_child(i)
                if child.count_children() > 0:
                    build_value_dict(child, values_dict)
                else:
                    values_dict[child.get_name()] = child.get_value()

        build_value_dict(old_config, old_values)
        build_value_dict(new_config, new_values)

        # Compare values
        for name in set(old_values.keys()) | set(new_values.keys()):
            old_val = old_values.get(name)
            new_val = new_values.get(name)
            if old_val != new_val:
                changes.append((name, str(old_val), str(new_val)))

        return changes