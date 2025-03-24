import io
import logging
import os
import re
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
from gphoto2 import CameraWidget

from profiles.base import Profile

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
        def __init__(self, capture_request: CaptureImagesRequest, elapsed_time: int, num_captured: int):
            super().__init__()
            self.num_captured = num_captured
            self.elapsed_time = elapsed_time
            self.capture_request = capture_request

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
        CaptureFinished, CaptureCanceled, CaptureCancelling, CaptureError, IOError, ConnectionError, LiveViewStarted, LiveViewStopped,
        LiveViewActive, FocusStarted, FocusFinished
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

        self.filesCounter = 0
        self.captureComplete = False

        self.shouldCancel = False
        self.liveView = False
        self.timer: QTimer = None

        self.__saved_config = None
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

        self.__logging_callback_extract_gp2_error = gp.check_result(
            gp.gp_log_add_func(gp.GP_LOG_DEBUG, self.__extract_gp2_error_from_log))

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
        camera_list = None
        while not camera_list and not self.thread().isInterruptionRequested():
            self.__logger.info("Waiting for camera...")
            camera_list = list(gp.Camera.autodetect())
            sleep(1)

        name, _ = camera_list[0]
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
        self.camera = gp.Camera()
        self.__last_ptp_error = None

        self.camera.init()
        self.empty_event_queue(1000)
        self.__apply_settings(profile.initial_settings())

        with self.__open_config("read") as cfg:
            self.__saved_config = cfg
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
                    cam_file.save(file_target_path)

                    current_capture_req = self.__state.capture_request
                    current_capture_req.signal.file_received.emit(file_target_path)
                    if self.filesCounter % current_capture_req.expect_files == 0:
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
                    # No match found, check for other config changes
                    # with self.__open_config("read") as current_cfg:
                    #     if self.__saved_config:
                    #         changes = self.__get_config_diff(self.__saved_config, current_cfg)
                    #         if changes:
                    #             for name, old_val, new_val in changes:
                    #                 if name not in ["d20c"]:
                    #                     self.__logger.info(
                    #                         f"Config change detected: {name} changed from {old_val} to {new_val}")
                    #                 self.property_changed.emit(PropertyChangeEvent(
                    #                     property=name,
                    #                     property_name=name,
                    #                     value=new_val
                    #                 ))
                    #
                    #     # Update saved config
                    #     self.__saved_config = current_cfg

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

            self.__apply_settings(self.profile.start_capture_settings())

            if capture_req.image_quality == CaptureImagesRequest.CaptureFormat.JPEG_AND_RAW:
                self.__apply_settings(self.profile.capture_format_jpeg_and_raw_settings())
            elif capture_req.image_quality == CaptureImagesRequest.CaptureFormat.JPEG:
                self.__apply_settings(self.profile.capture_format_jpeg_settings())

            # self.thread().sleep(1)

            remaining = capture_req.num_images * capture_req.expect_files
            while remaining > 0 and not self.shouldCancel and not self.thread().isInterruptionRequested():
                print(f"remaining#{remaining}")
                if self.profile.use_burst():
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
                self.__logger.info("Curr. files: {0} (remaining: {1}).".format(self.filesCounter, remaining))


                if self.profile.use_burst() and not capture_req.manual_trigger:
                    with self.__open_config("write") as cfg:
                        self.__try_set_config(cfg, self.profile.burstnumber_property_name(), burst)

            # with self.__open_config("write") as cfg:
            #     self.__try_set_config(cfg, "capture", 0)

            self.__logger.info("No. Files captured: {0} (took {1}).".format(self.filesCounter, timer.elapsed()))

            if not self.shouldCancel:
                num_captured = int(self.filesCounter / capture_req.expect_files)
                self.__set_state(
                    CameraStates.CaptureFinished(capture_req, elapsed_time=timer.elapsed(), num_captured=num_captured))
            else:
                self.__logger.info("Capture cancelled")
                # with self.__open_config("write") as cfg:
                #     self.__try_set_config(cfg, "capture", 0)
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