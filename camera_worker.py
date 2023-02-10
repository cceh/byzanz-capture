from enum import Enum, auto
import os
import threading
from time import sleep
import time

from string import Template

import gphoto2 as gp
from PyQt6.QtCore import QThread, pyqtSignal, QObject, QElapsedTimer
from gphoto2 import CameraWidget


class CameraCommands(QObject):
    capture_images = pyqtSignal(str, int)
    capture_test_image = pyqtSignal(str)
    find_camera = pyqtSignal()
    connect_camera = pyqtSignal()
    disconnect_camera = pyqtSignal(bool)
    set_config = pyqtSignal(str, str)

class CameraEvents(QObject):
    image_captured = pyqtSignal(str)
    test_image_captured = pyqtSignal(str)
    config_updated = pyqtSignal(gp.CameraWidget)
    camera_found = pyqtSignal(str)
    camera_connected = pyqtSignal(str)
    camera_disconnected = pyqtSignal(str, bool)

class ConfigChangeResult(QObject):
    config_changed = pyqtSignal

class CameraWorker(QObject):
    commands = CameraCommands()
    events = CameraEvents()

    camera: gp.Camera = None
    camera_name: str = None

    filesCounter = 0
    captureComplete = False

    def __init__(self, parent = None):
        super(CameraWorker, self).__init__(parent)
        # self.input_queue = input_queue
        # self.daemon = True

    def initialize(self):
        # code to run in the new thread
        print("Thread running")
        self.commands.capture_images.connect(self.captureImages)
        self.commands.capture_test_image.connect(self.captureTestImage)
        self.commands.find_camera.connect(self.__find_camera)
        self.commands.connect_camera.connect(self.__connect_camera)
        self.commands.disconnect_camera.connect(self.__disconnect_camera)
        self.commands.set_config.connect(self.__set_config)

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
        current_cfg: CameraWidget = self.camera.get_config()
        self.events.config_updated.emit(current_cfg)
        self.camera_name = "%s %s" % (
            current_cfg.get_child_by_name("cameramodel").get_value(),
            current_cfg.get_child_by_name("manufacturer").get_value()
        )
        self.events.camera_connected.emit(self.camera_name)

    def __disconnect_camera(self, manual: bool):
        self.camera.exit()
        del self.camera
        self.events.camera_disconnected.emit(self.camera_name, manual)
        self.camera_name = None

    def __set_config(self, name, value):
        cfg: CameraWidget = self.camera.get_config()
        cfg_widget = cfg.get_child_by_name(name)
        cfg_widget.set_value(value)
        self.camera.set_config(cfg)
        print("Did set config %s to %s" % (name, value))
        self.empty_event_queue()
        self.__emit_current_config()

    def __emit_current_config(self):
        current_cfg: CameraWidget = self.camera.get_config()
        self.events.config_updated.emit(current_cfg)

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

    def empty_event_queue(self, timeout=500, target_file_path_template=None):
        print("Empty event queue!")
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

                self.camera.file_delete(data.folder, data.name)
                self.filesCounter += 1

            elif typ == gp.GP_EVENT_CAPTURE_COMPLETE:
                self.captureComplete = True


            # self.download_file(cam_file_path)

            # try to grab another event
            typ, data = self.camera.wait_for_event(1)

    def captureTestImage(self, filename):
        try:
            print("Capturing test image")
            self.empty_event_queue()

            cfg = self.camera.get_config()
            capturetarget_cfg = cfg.get_child_by_name('capturetarget')
            capturetarget_cfg.set_value('Internal RAM')

            try:
                recmedia_cfg = cfg.get_child_by_name('recordingmedia')
                recmedia_cfg.set_value('SDRAM')
            except gp.GPhoto2Error:
                pass

            # TODO: set format to jpeg-fine only

            self.camera.set_config(cfg)
            file_path = self.camera.capture(gp.GP_CAPTURE_IMAGE)
            target = os.path.join('/tmp/test', filename)
            print(target)
            camera_file = self.camera.file_get(
                file_path.folder, file_path.name, gp.GP_FILE_TYPE_NORMAL)
            camera_file.save(target)
            self.events.test_image_captured.emit(target)
        except:
            pass

    def captureImages(self, target_path, number = 1):
        timer = QElapsedTimer()
        try:
            timer.start()
            self.empty_event_queue()
            self.filesCounter = 0
            self.captureComplete = False

            cfg = self.camera.get_config()
            self.__try_set_config(cfg, "capturetarget", "Internal RAM")
            self.__try_set_config(cfg, "recordingmedia", "SDRAM")
            # TODO: set format to nef+fine
            self.__try_set_config(cfg, "viewfinder", 1)
            self.__try_set_config(cfg, "burstnumber", number)
            self.camera.set_config(cfg)

            while not self.filesCounter == number:
                self.captureComplete = False
                self.camera.trigger_capture()

                while not self.captureComplete:
                    self.empty_event_queue(target_file_path_template=target_path)

                print("Curr. files: %i" % self.filesCounter)
                remaining = number - self.filesCounter
                print("Remaining: %s" % str(remaining))

            print("Viewfinder 0")
            cfg = self.camera.get_config()
            self.__try_set_config(cfg, "viewfinder", 0)
            self.camera.set_config(cfg)

            # empty the event queue

            print("No. Files: %i" % self.filesCounter)

            print("Took %d ms" % timer.elapsed())

        except:
            pass # TODO: ERROR HANDULNG

    def __try_set_config(self, config: CameraWidget, name: str, value):
        try:
            config_widget = config.get_child_by_name(name)
            config_widget.set_value(value)
        except gp.GPhoto2Error:
            print("Config '%s' not supported by camera.", name)

    def __del__(self):
        print("Disconnecting camera")
        self.camera.exit()


