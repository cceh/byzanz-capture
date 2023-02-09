from enum import Enum, auto
import os
import threading
from time import sleep
import time

import gphoto2 as gp
from PyQt6.QtCore import QThread, pyqtSignal, QObject
from gphoto2 import CameraWidget


class CameraCommands(QObject):
    capture_image = pyqtSignal()
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
        self.commands.capture_image.connect(self.captureImage)
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

    def empty_event_queue(self):
        print("Empty event queue!")
        typ, data = self.camera.wait_for_event(500)
        while typ != gp.GP_EVENT_TIMEOUT:

            print("Event: %s, data: %s" % (self.event_text(typ), data))


            if typ == gp.GP_EVENT_FILE_ADDED:
                fn = os.path.join(data.folder, data.name)
                print("New file: %s" % fn)
                cam_file = self.camera.file_get(
                    data.folder, data.name, gp.GP_FILE_TYPE_NORMAL)
                target_path = os.path.join(os.getcwd(), data.name)
                print("Image is being saved to {}".format(target_path))
                cam_file.save(target_path)
                self.filesCounter += 1
            elif typ == gp.GP_EVENT_CAPTURE_COMPLETE:
                self.captureComplete = True


            # self.download_file(fn)

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

    def captureImage(self):
        try:
            start_time = time.process_time()
            self.camera.init()
            self.empty_event_queue()
            self.filesCounter = 0
            self.captureComplete = False

            cfg = self.camera.get_config()
            capturetarget_cfg = cfg.get_child_by_name('capturetarget')
            capturetarget_cfg.set_value('Internal RAM')

            recmedia_cfg = cfg.get_child_by_name('recordingmedia')
            recmedia_cfg.set_value('SDRAM')

            # TODO: set format to nef+fine

            print("viewfinder 1")
            x = cfg.get_child_by_name('viewfinder')
            x.set_value(1)

            y = cfg.get_child_by_name('burstnumber')
            y.set_value(60)

            self.camera.set_config(cfg)

            sleep(3)

            print("hallo")
            while not self.filesCounter == 120:
                self.camera.trigger_capture()

                while not self.captureComplete:
                    self.empty_event_queue()

                print("Curr. files: %i" % self.filesCounter)
                remaining = 120 - self.filesCounter
                print("Remaining: %s" % str(self.empty_event_queue()))
                print("Restarting")

            print("viewfinder0")
            x.set_value(0)
            self.camera.set_config(cfg)

            # empty the event queue

            print("No. Files: %i" % self.filesCounter)

            print("Took %i" % time.process_time() - start_time)

            # file_path = self.camera.capture(gp.GP_CAPTURE_IMAGE)
            # print('Camera file path: {0}/{1}'.format(file_path.folder, file_path.name))
            # target = os.path.join('/tmp', file_path.name)
            # camera_file = self.camera.file_get(
            #     file_path.folder, file_path.name, gp.GP_FILE_TYPE_NORMAL
            # )
            # camera_file.save(target)
            # self.camera.file_delete(file_path.folder, file_path.name)

            # self.image_captured.emit(target)
        finally:
            if self.camera:
                self.camera.exit()


    def __del__(self):
        print("Disconnecting camera")
        self.camera.exit()


