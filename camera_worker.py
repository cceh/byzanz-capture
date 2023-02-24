import io
import logging
import os
from contextlib import contextmanager
from string import Template
from time import sleep
from typing import NamedTuple, Literal, Generator, Union

import gphoto2 as gp
from PIL import Image
from PyQt6.QtCore import pyqtSignal, QObject, QElapsedTimer, QTimer
from PyQt6.QtWidgets import QApplication
from gphoto2 import CameraWidget


class CaptureImagesRequest(NamedTuple):
    file_path_template: str
    num_images: int
    expect_files: int = 1
    max_burst: int = 1
    skip: int = 0
    manual_trigger: bool = False
    image_quality: str | None = None


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

    class LiveViewStarted:
        pass

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
        CaptureFinished, CaptureCanceled, CaptureError, IOError, ConnectionError, LiveViewStarted, LiveViewStopped
    ]



class CameraCommands(QObject):
    capture_images = pyqtSignal(CaptureImagesRequest)
    find_camera = pyqtSignal()
    connect_camera = pyqtSignal()
    disconnect_camera = pyqtSignal()
    reconnect_camera = pyqtSignal()
    set_config = pyqtSignal(str, str)
    cancel = pyqtSignal()
    live_view = pyqtSignal(bool)
    trigger_autofocus = pyqtSignal()


class CameraEvents(QObject):
    config_updated = pyqtSignal(gp.CameraWidget)

class CameraWorker(QObject):
    state_changed = pyqtSignal(object)
    preview_image = pyqtSignal(Image.Image)

    def __init__(self, parent=None):
        super(CameraWorker, self).__init__(parent)

        self.__skip = 0
        self.__state = None
        self.commands = CameraCommands()
        self.events = CameraEvents()

        self.camera: gp.Camera = None
        self.camera_name: str = None

        self.filesCounter = 0
        self.captureComplete = False

        self.shouldCancel = False
        self.liveView = False
        self.liveViewTimer: QTimer = None

        # self.thread().started.connect(self.initialize)


    def initialize(self):
        self.liveViewTimer = QTimer()

        self.commands.capture_images.connect(self.captureImages)
        self.commands.find_camera.connect(self.__find_camera)
        self.commands.connect_camera.connect(self.__connect_camera)
        self.commands.disconnect_camera.connect(lambda: self.__disconnect_camera(False))
        self.commands.reconnect_camera.connect(lambda: self.__disconnect_camera(True))
        self.commands.set_config.connect(self.__set_config)
        self.commands.cancel.connect(self.__cancel)
        self.commands.live_view.connect(lambda active: self.__start_live_view() if active else self.__stop_live_view())
        self.commands.trigger_autofocus.connect(self.__trigger_autofocus)

        self.__set_state(CameraStates.Waiting())

        logging.basicConfig(
            format='%(levelname)s: %(name)s: %(message)s', level=logging.INFO)
        # self.callback_obj = gp.check_result(gp.use_python_logging(mapping={
        #     gp.GP_LOG_ERROR: logging.INFO,
        #     gp.GP_LOG_DEBUG: logging.DEBUG,
        #     gp.GP_LOG_VERBOSE: logging.DEBUG - 3,
        #     gp.GP_LOG_DATA: logging.DEBUG - 6}))




    def __set_state(self, state: CameraStates.StateType):
        self.__state = state
        print("Set camera state: " + state.__class__.__name__)
        self.state_changed.emit(state)

    def __find_camera(self):
        self.__set_state(CameraStates.Waiting())
        camera_list = None
        while not camera_list:
            print("Waiting fort camera...")
            camera_list = list(gp.Camera.autodetect())
            sleep(1)

        name, _ = camera_list[0]
        self.__set_state(CameraStates.Found(camera_name=name))
        # self.events.camera_found.emit(name)

    def __connect_camera(self):
        try:
            self.__set_state(CameraStates.Connecting(self.camera_name))
            self.camera = gp.Camera()
            self.camera.init()
            self.empty_event_queue(1000)
            with self.__open_config("read") as cfg:
                self.events.config_updated.emit(cfg)
                self.camera_name = "%s %s" % (
                    cfg.get_child_by_name("cameramodel").get_value(),
                    cfg.get_child_by_name("manufacturer").get_value()
                )
            self.__set_state(CameraStates.Ready(self.camera_name))

            # self.liveViewTimer = QTimer()
            # self.liveViewTimer.timeout.connect(lambda: print("timeout"))
            # self.liveViewTimer.start(1000)

            live_view_on = False
            while self.camera:
                try:
                    live_view_changed = self.liveView != live_view_on
                    if live_view_changed:
                        live_view_on = self.liveView
                        self.__init_live_view() if live_view_on else self.__deinit_live_view()
                        if live_view_on:
                            self.__set_state(CameraStates.LiveViewStarted())
                        else:
                            self.__set_state(CameraStates.Ready(self.camera_name))

                    if live_view_on:
                        self.__live_view_capture_preview()
                        self.thread().msleep(100)
                    else:
                        self.empty_event_queue(1)
                finally:
                    QApplication.processEvents()

            # while self.camera:
            #     try:
            #         if self.liveView:
            #             self.__live_view_capture_preview()
            #             self.thread().msleep(100)
            #         else:
            #             self.empty_event_queue(1)
            #     finally:
            #         pass

        except gp.GPhoto2Error as err:
            self.__set_state(CameraStates.ConnectionError(error=err))
            self.__disconnect_camera()
        except AttributeError:
            # camera gone
            self.__disconnect_camera()

    def __disconnect_camera(self, auto_reconnect = True):
        self.__set_state(CameraStates.Disconnecting())

        try:
            self.camera.exit()
        except gp.GPhoto2Error:
            pass
        except AttributeError: # Camera gone
            pass

        self.camera = None
        # self.events.camera_disconnected.emit(self.camera_name, True)
        self.__set_state(CameraStates.Disconnected(camera_name=self.camera_name, auto_reconnect=auto_reconnect))
        self.camera_name = None

    def __set_config(self, name, value):
        print("Set config %s to %s" % (name, value))
        with self.__open_config("write") as cfg:
            cfg_widget = cfg.get_child_by_name(name)
            cfg_widget.set_value(value)

        self.empty_event_queue()
        self.__emit_current_config()

    def __emit_current_config(self):
        with self.__open_config("read") as cfg:
            self.events.config_updated.emit(cfg)

    def __cancel(self):
        # TODO: cancel interrupt?
        self.shouldCancel = True

    def event_text(self, event_type):
        if event_type == gp.GP_EVENT_CAPTURE_COMPLETE:
            return "Capture Complete"
        elif event_type == gp.GP_EVENT_FILE_ADDED:
            return "File Added"
        elif event_type == gp.GP_EVENT_FOLDER_ADDED:
            return "Folder Added"
        elif event_type == gp.GP_EVENT_TIMEOUT:
            return "Timeout"
        else:
            return "Unknown Event"

    def empty_event_queue(self, timeout=100):
        typ, data = self.camera.wait_for_event(timeout)

        while typ != gp.GP_EVENT_TIMEOUT:

            print("Event: %s, data: %s" % (self.event_text(typ), data))

            if typ == gp.GP_EVENT_FILE_ADDED:
                cam_file_path = os.path.join(data.folder, data.name)
                print("New file: %s" % cam_file_path)
                basename, extension = os.path.splitext(data.name)

                if isinstance(self.__state, CameraStates.CaptureInProgress) and self.shouldCancel is False and self.__skip == 0:
                    tpl = Template(self.__state.capture_request.file_path_template)
                    file_target_path = tpl.substitute(
                        basename=basename,
                        extension=extension,
                        num=str(self.filesCounter + 1).zfill(3)
                    )
                    cam_file = self.camera.file_get(
                        data.folder, data.name, gp.GP_FILE_TYPE_NORMAL)
                    print("Image is being saved to {}".format(file_target_path))
                    cam_file.save(file_target_path)
                    if isinstance(self.__state, CameraStates.CaptureInProgress):
                        current_capture_req = self.__state.capture_request
                        if self.filesCounter % current_capture_req.expect_files == 0:
                            num_captured = int(self.filesCounter / current_capture_req.expect_files)
                            self.__set_state(CameraStates.CaptureInProgress(self.__state.capture_request, num_captured))
                else:
                    self.__skip -= 1

                self.camera.file_delete(data.folder, data.name)
                self.filesCounter += 1

            elif typ == gp.GP_EVENT_CAPTURE_COMPLETE:
                self.captureComplete = True

            # self.download_file(cam_file_path)

            # try to grab another event
            typ, data = self.camera.wait_for_event(1)

    def __start_live_view(self):
        self.liveView = True

    def __init_live_view(self):
        with self.__open_config("write") as cfg:
            self.__try_set_config(cfg, "capturetarget", "Internal RAM")
            self.__try_set_config(cfg, "recordingmedia", "SDRAM")
            self.__try_set_config(cfg, "viewfinder", 1)
            self.__try_set_config(cfg, "liveviewsize", "VGA")

        # self.liveView = True
        #
        # camera_busy = True
        # while camera_busy:
        #     try:
        #
        #         camera_busy = False
        #
        #     except gp.GPhoto2Error as err:
        #         if err.code == -110:
        #             pass
        #
        # while self.liveView:
        #     self.__live_view_capture_preview()
        #     QApplication.processEvents()
        #     sleep(0.1)


    def __live_view_capture_preview(self):
        try:
            camera_file = self.camera.capture_preview()
            file_data = camera_file.get_data_and_size()
            image = Image.open(io.BytesIO(file_data))
            self.preview_image.emit(image)
        except Exception as err:
            print(err)
            self.__stop_live_view()

    def __stop_live_view(self):
        self.liveView = False

    def __deinit_live_view(self):
        with self.__open_config("write") as cfg:
            self.__try_set_config(cfg, "viewfinder", 0)
            self.__try_set_config(cfg, "autofocusdrive", 0)

    def __trigger_autofocus(self):
        with self.__open_config("write") as cfg:
            lightmeter = cfg.get_child_by_name("lightmeter").get_value()
            if lightmeter <= -5:
                print("Not enough light for autofocus: " + str(lightmeter))
                return
            self.__try_set_config(cfg, "autofocusdrive", 1)

    def captureImages(self, capture_req: CaptureImagesRequest):
        self.__stop_live_view()

        timer = QElapsedTimer()
        try:
            timer.start()
            self.empty_event_queue()
            self.filesCounter = 0
            self.captureComplete = False
            self.__skip = capture_req.skip * capture_req.expect_files

            self.__set_state(CameraStates.CaptureInProgress(capture_request=capture_req, num_captured=0))


            with self.__open_config("write") as cfg:
                self.__try_set_config(cfg, "capturetarget", "Internal RAM")
                self.__try_set_config(cfg, "recordingmedia", "SDRAM")
                self.__try_set_config(cfg, "viewfinder", 1)
                if capture_req.image_quality:
                    self.__try_set_config(cfg, "imagequality", capture_req.image_quality)

            # TODO: enable again when trigger works
            sleep(1)
            remaining = capture_req.num_images * capture_req.expect_files
            while remaining > 0 and not self.shouldCancel:
                burst = min(capture_req.max_burst, int(remaining / capture_req.expect_files))
                if not capture_req.manual_trigger:
                    with self.__open_config("write") as cfg:
                       self.__try_set_config(cfg, "burstnumber", burst)

                QApplication.processEvents()

                self.captureComplete = False
                if not capture_req.manual_trigger:
                    self.camera.trigger_capture()
                while not self.captureComplete and not self.shouldCancel:
                    self.empty_event_queue(timeout=100)
                    QApplication.processEvents()

                print("Curr. files: %i" % self.filesCounter)
                remaining = capture_req.num_images * capture_req.expect_files - self.filesCounter

                burst = remaining / capture_req.expect_files
                if not capture_req.manual_trigger:
                    with self.__open_config("write") as cfg:
                        self.__try_set_config(cfg, "burstnumber", burst)
                        print("Burst number set to %i" % burst)

                print("Remaining: %s" % str(remaining))

            print("No. Files: %i" % self.filesCounter)

            print("Took %d ms" % timer.elapsed())

            if not self.shouldCancel:
                num_captured = int(self.filesCounter / capture_req.expect_files)
                self.__set_state(CameraStates.CaptureFinished(capture_req, elapsed_time=timer.elapsed(), num_captured=num_captured))
            else:
                print("Capture cancelled")
                self.__set_state(CameraStates.CaptureCanceled(capture_req, elapsed_time=timer.elapsed()))

        except gp.GPhoto2Error as err:
            self.__set_state(CameraStates.CaptureError(capture_req, err.string))
        finally:
            self.shouldCancel = False
            # If camera is still there, try to reset Camera to a default state
            if self.camera:
                try:
                    with self.__open_config("write") as cfg:
                        print("Viewfinder 0")
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
            print(cfg)
            yield cfg
        finally:
            print(cfg)
            if mode == "write":
                self.camera.set_config(cfg)
            elif not mode == "read":
                raise Exception("Invalid cfg open mode: %s" % mode)

    def __try_set_config(self, config: CameraWidget, name: str, value) -> None:
        try:
            config_widget = config.get_child_by_name(name)
            config_widget.set_value(value)
            print("Set config '%s' to %s." % (name, str(value)))
        except gp.GPhoto2Error:
            print("Config '%s' not supported by camera." % name)

    def __del__(self):
        print("Disconnecting camera")
        self.camera.exit()
