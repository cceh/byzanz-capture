import io
import logging
import os
import re
from contextlib import contextmanager
from enum import Enum
from string import Template
from time import sleep
from typing import NamedTuple, Literal, Generator, Union

import gphoto2 as gp
from PIL import Image
from PyQt6.QtCore import pyqtSignal, QObject, QElapsedTimer, QTimer, Qt, pyqtSlot
from PyQt6.QtWidgets import QApplication
from gphoto2 import CameraWidget

EVENT_DESCRIPTIONS = {
    gp.GP_EVENT_UNKNOWN: "Unknown",
    gp.GP_EVENT_CAPTURE_COMPLETE: "Capture Complete",
    gp.GP_EVENT_FILE_ADDED: "File Added",
    gp.GP_EVENT_FOLDER_ADDED: "Folder Added",
    gp.GP_EVENT_TIMEOUT: "Timeout"
}

class NikonPTPError(Enum):
    OutOfFocus = "0xa002"

class ConfigRequest():
    class Signal(QObject):
        got_config = pyqtSignal(gp.CameraWidget)

    def __init__(self):
        self.signal = ConfigRequest.Signal()


class CaptureImagesRequest(NamedTuple):
    file_path_template: str
    num_images: int
    expect_files: int = 1
    max_burst: int = 1
    skip: int = 0
    manual_trigger: bool = False
    image_quality: str | None = None

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
        def __init__(self, capture_request: CaptureImagesRequest, elapsed_time: int, num_captured: int):
            super().__init__()
            self.num_captured = num_captured
            self.elapsed_time = elapsed_time
            self.capture_request = capture_request

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

    class IOError:
        def __init__(self, error: str):
            super().__init__()
            self.error = error

    class ConnectionError:
        def __init__(self, error: gp.GPhoto2Error):
            super().__init__()
            self.error = error

    StateType = Union[
        Waiting, Found, Disconnected, Connecting, Disconnecting, Ready, CaptureInProgress,
        CaptureFinished, CaptureCanceled, CaptureError, IOError, ConnectionError, LiveViewStarted, LiveViewStopped,
        LiveViewActive, FocusStarted, FocusFinished
    ]


class CameraCommands(QObject):
    capture_images = pyqtSignal(CaptureImagesRequest)
    find_camera = pyqtSignal()
    connect_camera = pyqtSignal()
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
    config_updated = pyqtSignal(gp.CameraWidget)


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

        self.filesCounter = 0
        self.captureComplete = False

        self.shouldCancel = False
        self.liveView = False
        self.timer: QTimer = None

        self.__last_ptp_error: NikonPTPError = None

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

        self.__logging_callback_extract_gp2_error = gp.check_result(
            gp.gp_log_add_func(gp.GP_LOG_ERROR, self.__extract_gp2_error_from_log))

    def __extract_gp2_error_from_log(self, _level: int, domain: bytes, string: bytes, _data=None):
        error_str = string.decode()
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
        camera_list = None
        while not camera_list and not self.thread().isInterruptionRequested():
            self.__logger.info("Waiting for camera...")
            camera_list = list(gp.Camera.autodetect())
            sleep(1)

        name, _ = camera_list[0]
        self.__set_state(CameraStates.Found(camera_name=name))

    @__handle_camera_error
    def __connect_camera(self):
        self.__set_state(CameraStates.Connecting(self.camera_name))
        self.camera = gp.Camera()
        self.__last_ptp_error = None

        self.camera.init()
        self.empty_event_queue(1000)
        with self.__open_config("read") as cfg:
            self.events.config_updated.emit(cfg)
            self.camera_name = "%s %s" % (
                cfg.get_child_by_name("cameramodel").get_value(),
                cfg.get_child_by_name("manufacturer").get_value()
            )
        self.__set_state(CameraStates.Ready(self.camera_name))

        while self.camera:
            if self.thread().isInterruptionRequested():
                self.__disconnect_camera()
                self.thread().exit()
                return

            try:
                if isinstance(self.__state, CameraStates.LiveViewActive):
                    self.__live_view_capture_preview()
                    # self.empty_event_queue(1)
                    self.thread().msleep(50)
                else:
                    self.empty_event_queue(1)
            finally:
                QApplication.processEvents()

    def __disconnect_camera(self, auto_reconnect=True):
        self.__set_state(CameraStates.Disconnecting())

        # Ignore errors while disconnecting
        try:
            self.camera.exit()
        except gp.GPhoto2Error:
            pass
        except AttributeError:  # Camera already gone
            pass

        self.__set_state(CameraStates.Disconnected(camera_name=self.camera_name, auto_reconnect=auto_reconnect))
        self.camera = None
        self.camera_name = None

    @__handle_camera_error
    def __set_single_config(self, name, value):
        self.__logger.info("Set config %s to %s" % (name, value))
        with self.__open_config("write") as cfg:
            cfg_widget = cfg.get_child_by_name(name)
            cfg_widget.set_value(value)

        self.empty_event_queue()
        self.__emit_current_config()

    @__handle_camera_error
    def __set_config(self, cfg: gp.CameraWidget):
        self.camera.set_config(cfg)

    @__handle_camera_error
    def __get_config(self, req: ConfigRequest):
        with self.__open_config("read") as cfg:
            req.signal.got_config.emit(cfg)

    def __emit_current_config(self):
        with self.__open_config("read") as cfg:
            self.events.config_updated.emit(cfg)

    def __cancel(self):
        self.shouldCancel = True

    def empty_event_queue(self, timeout=100):
        event_type, data = self.camera.wait_for_event(timeout)

        while event_type != gp.GP_EVENT_TIMEOUT:
            self.__logger.debug("Event: %s, data: %s" % (EVENT_DESCRIPTIONS.get(event_type, "Unknown"), data))

            if event_type == gp.GP_EVENT_FILE_ADDED:
                cam_file_path = os.path.join(data.folder, data.name)
                self.__logger.info("New file: %s" % cam_file_path)
                basename, extension = os.path.splitext(data.name)

                if isinstance(self.__state, CameraStates.CaptureInProgress) and not self.shouldCancel and not self.thread().isInterruptionRequested():
                    tpl = Template(self.__state.capture_request.file_path_template)
                    file_target_path = tpl.substitute(
                        basename=basename,
                        extension=extension,
                        num=str(self.filesCounter + 1).zfill(3)
                    )
                    cam_file = self.camera.file_get(
                        data.folder, data.name, gp.GP_FILE_TYPE_NORMAL)
                    self.__logger.info("Saving to %s" % file_target_path)
                    cam_file.save(file_target_path)
                    if isinstance(self.__state, CameraStates.CaptureInProgress):
                        current_capture_req = self.__state.capture_request
                        if self.filesCounter % current_capture_req.expect_files == 0:
                            num_captured = int(self.filesCounter / current_capture_req.expect_files)
                            self.__set_state(CameraStates.CaptureInProgress(self.__state.capture_request, num_captured))
                    self.filesCounter += 1
                else:
                    self.__logger.warning(
                        "Received file but capture not in progress, ignoring. State: " + self.__state.__class__.__name__)

                self.camera.file_delete(data.folder, data.name)

            elif event_type == gp.GP_EVENT_CAPTURE_COMPLETE:
                self.captureComplete = True

            elif event_type == gp.GP_EVENT_UNKNOWN:
                match = re.search(r'PTP Property (\w+) changed, "(\w+)" to "(-?\d+[,\.]\d+)"', data)
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

            # try to grab another event
            event_type, data = self.camera.wait_for_event(1)

    @__handle_camera_error
    def __start_live_view(self):
        lightmeter: int
        with self.__open_config("write") as cfg:
            self.__try_set_config(cfg, "capturetarget", "Internal RAM")
            self.__try_set_config(cfg, "recordingmedia", "SDRAM")
            self.__try_set_config(cfg, "viewfinder", 1)
            self.__try_set_config(cfg, "liveviewsize", "VGA")
            lightmeter = cfg.get_child_by_name("lightmeter").get_value()
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
            with self.__open_config("write") as cfg:
                self.__try_set_config(cfg, "viewfinder", 0)
                self.__try_set_config(cfg, "autofocusdrive", 0)
            self.__set_state(CameraStates.LiveViewStopped())
            self.__set_state(CameraStates.Ready(self.camera_name))

    @__handle_camera_error
    def __trigger_autofocus(self):
        lightmeter = None
        self.__set_state(CameraStates.FocusStarted())
        try:
            with self.__open_config("write") as cfg:
                lightmeter = cfg.get_child_by_name("lightmeter").get_value()
                self.__try_set_config(cfg, "autofocusdrive", 1)
            self.__set_state(CameraStates.FocusFinished(success=True))
        except gp.GPhoto2Error:
            if self.__last_ptp_error == NikonPTPError.OutOfFocus:
                self.__logger.warning("Could not get focus (light: %s)." % lightmeter)
                self.__set_state(CameraStates.FocusFinished(success=False))
            else:
                raise
        finally:
            with self.__open_config("write") as cfg:
                self.__try_set_config(cfg, "autofocusdrive", 0)
            self.__set_state(CameraStates.LiveViewActive())
            # TODO handle general camera error

    def captureImages(self, capture_req: CaptureImagesRequest):
        if isinstance(self.__state, CameraStates.LiveViewActive):
            self.__stop_live_view()

        self.__logger.info("Start capture (%s)", str(capture_req))

        timer = QElapsedTimer()
        try:
            timer.start()
            self.empty_event_queue()
            self.filesCounter = 0
            self.captureComplete = False

            self.__set_state(CameraStates.CaptureInProgress(capture_request=capture_req, num_captured=0))

            with self.__open_config("write") as cfg:
                self.__try_set_config(cfg, "capturetarget", "Internal RAM")
                self.__try_set_config(cfg, "recordingmedia", "SDRAM")
                self.__try_set_config(cfg, "viewfinder", 1)
                self.__try_set_config(cfg, "autofocusdrive", 0)
                self.__try_set_config(cfg, "focusmode", "Manual")
                self.__try_set_config(cfg, "focusmode2", "MF (fixed)")
                if capture_req.image_quality:
                    self.__try_set_config(cfg, "imagequality", capture_req.image_quality)

            self.thread().sleep(1)

            remaining = capture_req.num_images * capture_req.expect_files
            while remaining > 0 and not self.shouldCancel and not self.thread().isInterruptionRequested():
                burst = min(capture_req.max_burst, int(remaining / capture_req.expect_files))
                if not capture_req.manual_trigger:
                    with self.__open_config("write") as cfg:
                        self.__try_set_config(cfg, "burstnumber", burst)

                self.captureComplete = False
                if not capture_req.manual_trigger:
                    self.camera.trigger_capture()
                while not self.captureComplete and not self.shouldCancel and not self.thread().isInterruptionRequested():
                    self.empty_event_queue(timeout=100)
                    QApplication.processEvents()

                remaining = capture_req.num_images * capture_req.expect_files - self.filesCounter
                self.__logger.info("Curr. files: {0} (remaining: {1}).".format(self.filesCounter, remaining))

                if not capture_req.manual_trigger:
                    with self.__open_config("write") as cfg:
                        self.__try_set_config(cfg, "burstnumber", burst)

            self.__logger.info("No. Files captured: {0} (took {1}).".format(self.filesCounter, timer.elapsed()))

            if not self.shouldCancel:
                num_captured = int(self.filesCounter / capture_req.expect_files)
                self.__set_state(
                    CameraStates.CaptureFinished(capture_req, elapsed_time=timer.elapsed(), num_captured=num_captured))
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
                    with self.__open_config("write") as cfg:
                        # TODO: enable again when trigger works
                        self.__try_set_config(cfg, "viewfinder", 0)
                        if not capture_req.manual_trigger:
                            self.__try_set_config(cfg, "burstnumber", 1)
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
            config_widget.set_value(value)
            self.__logger.info("Set config '%s' to %s." % (name, str(value)))
        except gp.GPhoto2Error:
            self.__logger.error("Config '%s' not supported by camera." % name)

    def __del__(self):
        self.__disconnect_camera()
