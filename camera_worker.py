from contextlib import contextmanager
from enum import Enum, auto
import os
import threading
from time import sleep
import time
import logging

from string import Template
from typing import NamedTuple, Literal, Generator

import gphoto2 as gp
from PyQt6.QtCore import QThread, pyqtSignal, QObject, QElapsedTimer, QThreadPool, QRunnable, pyqtSlot, QEventLoop
from PyQt6.QtWidgets import QApplication
from gphoto2 import CameraWidget


class CaptureImagesRequest(NamedTuple):
    class Signal(QObject):
        finished = pyqtSignal()
        canceled = pyqtSignal()

    file_path_template: str
    num_images: int
    expect_files: int = 1
    signal = Signal()
    # def __init__(self, file_path_template: str, num_images: str, parent=None):
    #    super(CaptureImagesRequest, self).__init__(parent)
    #    self.file_path_template = file_path_template
    #    self.num_images = num_images

class CameraCommands(QObject):
    capture_images = pyqtSignal(CaptureImagesRequest)
    find_camera = pyqtSignal()
    connect_camera = pyqtSignal()
    disconnect_camera = pyqtSignal()
    set_config = pyqtSignal(str, str)
    cancel = pyqtSignal()


class CameraEvents(QObject):
    capture_finished = pyqtSignal()
    received_image = pyqtSignal()
    config_updated = pyqtSignal(gp.CameraWidget)
    camera_found = pyqtSignal(str)
    camera_connected = pyqtSignal(str)
    camera_disconnected = pyqtSignal(str, bool)





class CameraWorker(QObject):
    commands = CameraCommands()
    events = CameraEvents()

    camera: gp.Camera = None
    camera_name: str = None

    filesCounter = 0
    captureComplete = False

    shouldCancel = False

    class Worker(QRunnable):
        class Signals:
            finished = pyqtSignal()

        def __init__(self, camera: gp.Camera, timeout=200, target_file_path_template=None):
            super(CameraWorker.Worker, self).__init__()
            self.camera = camera
            self.timeout = timeout
            self.target_file_path_template = target_file_path_template
            self.filesCounter = 0

        @pyqtSlot()
        def run(self):
            self.camera.trigger_capture()

    def __init__(self, parent=None):
        super(CameraWorker, self).__init__(parent)
        # self.input_queue = input_queue
        # self.daemon = True

    def initialize(self):
        # code to run in the new thread
        print("Thread running")
        self.commands.capture_images.connect(self.captureImages)
        self.commands.find_camera.connect(self.__find_camera)
        self.commands.connect_camera.connect(self.__connect_camera)
        self.commands.disconnect_camera.connect(self.__disconnect_camera)
        self.commands.set_config.connect(self.__set_config)
        self.commands.cancel.connect(self.__cancel)

        self.threadpool = QThreadPool()

        logging.basicConfig(
            format='%(levelname)s: %(name)s: %(message)s', level=logging.INFO)
        self.callback_obj = gp.check_result(gp.use_python_logging(mapping={
            gp.GP_LOG_ERROR: logging.INFO,
            gp.GP_LOG_DEBUG: logging.DEBUG,
            gp.GP_LOG_VERBOSE: logging.DEBUG - 3,
            gp.GP_LOG_DATA: logging.DEBUG - 6}))
        print(self.callback_obj)

    def __find_camera(self):
        camera_list = None
        while not camera_list:
            print("Waiting fort camera...")
            camera_list = list(gp.Camera.autodetect())
            sleep(1)

        name, _ = camera_list[0]
        self.events.camera_found.emit(name)

    def __connect_camera(self):
        self.camera = gp.Camera()
        self.camera.init()
        with self.__open_config("read") as cfg:
            self.events.config_updated.emit(cfg)
            self.camera_name = "%s %s" % (
                cfg.get_child_by_name("cameramodel").get_value(),
                cfg.get_child_by_name("manufacturer").get_value()
            )
        self.events.camera_connected.emit(self.camera_name)
        while self.camera:
            try:
                self.empty_event_queue(100)
            except gp.GPhoto2Error as err:
                print(err)
                self.__disconnect_camera()
            finally:
                QApplication.processEvents()

    def __disconnect_camera(self):
        try:
            self.camera.exit()
        except gp.GPhoto2Error:
            pass

        del self.camera
        self.events.camera_disconnected.emit(self.camera_name, True)
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

    def empty_event_queue(self, timeout=100, target_file_path_template=None):
        typ, data = self.camera.wait_for_event(timeout)
        while typ != gp.GP_EVENT_TIMEOUT:

            print("Event: %s, data: %s" % (self.event_text(typ), data))

            if typ == gp.GP_EVENT_FILE_ADDED:
                cam_file_path = os.path.join(data.folder, data.name)
                print("New file: %s" % cam_file_path)
                basename, extension = os.path.splitext(data.name)

                if target_file_path_template is not None:
                    tpl = Template(target_file_path_template)
                    file_target_path = tpl.substitute(
                        basename=basename,
                        extension=extension,
                        num=str(self.filesCounter).zfill(3)
                    )
                    cam_file = self.camera.file_get(
                        data.folder, data.name, gp.GP_FILE_TYPE_NORMAL)
                    print("Image is being saved to {}".format(file_target_path))
                    cam_file.save(file_target_path)
                    self.events.received_image.emit()

                self.camera.file_delete(data.folder, data.name)
                self.filesCounter += 1

            elif typ == gp.GP_EVENT_CAPTURE_COMPLETE:
                self.captureComplete = True

            # self.download_file(cam_file_path)

            # try to grab another event
            typ, data = self.camera.wait_for_event(1)


    def captureImages(self, capture_req: CaptureImagesRequest):
        timer = QElapsedTimer()
        try:
            timer.start()
            self.empty_event_queue()
            self.filesCounter = 0
            self.captureComplete = False

            with self.__open_config("write") as cfg:
                self.__try_set_config(cfg, "capturetarget", "Internal RAM")
                self.__try_set_config(cfg, "recordingmedia", "SDRAM")
                # TODO: set format to nef+fine
                self.__try_set_config(cfg, "viewfinder", 1)
                self.__try_set_config(cfg, "burstnumber", capture_req.num_images)


            remaining = capture_req.num_images * capture_req.expect_files
            while remaining > 0:
                print(1)
                QApplication.processEvents()
                print(2)
                if self.shouldCancel:
                    print("Capture cancelled")
                    break

                self.captureComplete = False
                self.camera.trigger_capture()
                while not self.captureComplete:
                    try:
                        QApplication.processEvents()
                        target_path = capture_req.file_path_template if not self.shouldCancel else None
                        self.empty_event_queue(target_file_path_template=target_path, timeout=100)
                    except gp.GPhoto2Error as err:
                        print(err)

                print("Curr. files: %i" % self.filesCounter)
                remaining = capture_req.num_images - self.filesCounter / capture_req.expect_files

                burst = remaining / capture_req.expect_files
                with self.__open_config("write") as cfg:
                    self.__try_set_config(cfg, "burstnumber", burst)
                    print("Burst number set to %i" % burst)

                print("Remaining: %s" % str(remaining))

            print("No. Files: %i" % self.filesCounter)

            print("Took %d ms" % timer.elapsed())

            if not self.shouldCancel:
                self.events.capture_finished.emit()
                capture_req.signal.finished.emit()
            else:
                capture_req.signal.canceled.emit()

        except Exception as err:
            raise err
            pass  # TODO: ERROR HANDULNG
        finally:
            self.shouldCancel = False
            # If camera is still there, try to reset Camera to a default state
            if self.camera:
                try:
                    with self.__open_config("write") as cfg:
                        print("Viewfinder 0")
                        self.__try_set_config(cfg, "viewfinder", 0)
                        self.__try_set_config(cfg, "burstnumber", 1)
                except:
                    raise err

    @contextmanager
    def __open_config(self, mode: Literal["read", "write"]) -> Generator[CameraWidget, None, None]:
        try:
            cfg: CameraWidget = self.camera.get_config()
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
        except gp.GPhoto2Error:
            print("Config '%s' not supported by camera." % name)

    def __del__(self):
        print("Disconnecting camera")
        self.camera.exit()
