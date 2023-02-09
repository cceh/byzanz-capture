import sys
from abc import ABC, ABCMeta
from collections import namedtuple
from enum import Enum
from fractions import Fraction
from pathlib import Path

import os
from typing import NamedTuple, Union

from PyQt6.QtCore import QThread, Qt, QThreadPool
from PyQt6.QtGui import QPixmap, QImageReader
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QMessageBox, QPushButton, QWidget, QListWidget, QListWidgetItem, QFrame, QLineEdit,
    QComboBox, QLabel, QToolBox, QGroupBox
)
from PyQt6.uic import loadUi

from camera_thread import CameraWorker
from load_image_worker import LoadImageWorker, LoadImageWorkerResult
from photo_viewer import PhotoViewer

import gphoto2 as gp
from time import sleep

from spinner import Spinner


class Session:
    name: str
    def __init__(self, name):
        self.name = name

class CameraStates:
    class WaitingForCamera(NamedTuple): pass
    class Disconnected(NamedTuple):
        camera_name: str
        manual: bool = False
    class Disconnecting(NamedTuple): pass
    class Connecting(NamedTuple): pass
    class Connected(NamedTuple):
        camera_name: str

    StateType = Union[WaitingForCamera, Disconnected, Connected, Connecting, Disconnecting]




class GUI(QMainWindow):
    camera_worker = CameraWorker()
    preview_counter = 1

    __session: Session
    @property
    def session(self): return self.__session
    @session.setter
    def session(self, _session):
        self.__session = _session
        is_session = self.session is not None
        self.capture_view.setEnabled(is_session)
        self.camera_control_frame.setEnabled(is_session)
        self.session_name_edit.setEnabled(not is_session)
        self.start_session_button.setVisible(not is_session)
        self.close_session_button.setVisible(is_session)

        if _session:
            self.session_name_edit.setText(self.session.name)
        else:
            self.session_name_edit.clear()
            self.session_name_edit.setFocus()



    __camera_state: CameraStates.StateType
    @property
    def camera_state(self): return self.__camera_state
    def set_camera_state(self, state: CameraStates.StateType):
        print(state)

        match state:
            case CameraStates.WaitingForCamera():
                self.set_camera_connection_busy(True)
                self.camera_state_label.setText("Suche Kamera...")
                self.camera_state_icon.setPixmap(QPixmap("ui/camera_waiting.png"))
                self.disconnect_camera_button.setVisible(True)
                self.connect_camera_button.setVisible(True)

            case CameraStates.Disconnected():
                self.set_camera_connection_busy(False)
                self.camera_state_label.setText("Kamera getrennt<br><b>%s</b>" % state.camera_name)
                self.camera_state_icon.setPixmap(QPixmap("ui/camera_not_ok.png"))
                self.disconnect_camera_button.setVisible(False)
                self.connect_camera_button.setVisible(True)
            case CameraStates.Disconnected(manual=False):
                self.camera_worker.commands.connect_camera.emit()

            case CameraStates.Connecting():
                self.set_camera_connection_busy(True)
                self.camera_state_label.setText("Verbinde Kamera...")

            case CameraStates.Connected():
                self.set_camera_connection_busy(False)
                self.camera_state_label.setText("Kamera verbunden<br><b>%s</b>" % state.camera_name)
                self.camera_state_icon.setPixmap(QPixmap("ui/camera_ok.png"))
                self.disconnect_camera_button.setVisible(True)
                self.disconnect_camera_button.setEnabled(True)
                self.connect_camera_button.setVisible(False)

            case CameraStates.Disconnecting():
                self.set_camera_connection_busy(True)
                self.camera_state_label.setText("Trenne Kamera...")

    def __init__(self, parent=None):
        super().__init__(parent)

        loadUi('ui/main_window.ui', self)

        self.threadpool = QThreadPool()

        capture_image_test_button: QPushButton = self.findChild(QPushButton, "captureTestImageButton")
        self.start_session_button: QPushButton = self.findChild(QPushButton, "startSessionButton")
        self.close_session_button: QPushButton = self.findChild(QPushButton, "closeSessionButton")

        self.disconnect_camera_button: QPushButton = self.findChild(QPushButton, "disconnectCameraButton")
        self.connect_camera_button: QPushButton = self.findChild(QPushButton, "connectCameraButton")
        self.camera_busy_spinner: Spinner = self.findChild(QWidget, "cameraBusySpinner")

        self.photo_viewer: PhotoViewer = self.findChild(QWidget, "photoViewer")
        self.preview_list: QListWidget = self.findChild(QListWidget, "previewList")
        self.session_controls: QWidget = self.findChild(QWidget, "sessionControls")
        self.capture_view: QFrame = self.findChild(QFrame, "captureView")
        self.camera_control_frame: QFrame = self.findChild(QFrame, "cameraControlFrame")
        self.session_name_edit: QLineEdit = self.findChild(QLineEdit, "sessionNameEdit")

        self.f_number_select: QComboBox = self.findChild(QComboBox, "fNumberSelect")
        self.shutter_speed_select: QComboBox = self.findChild(QComboBox, "shutterSpeedSelect")

        self.camera_state_label: QLabel = self.findChild(QLabel, "cameraStateLabel")
        self.camera_state_icon: QLabel = self.findChild(QLabel, "cameraStateIcon")

        self.set_camera_connection_busy(True)
        self.set_camera_state(CameraStates.WaitingForCamera())
        self.session = None

        capture_image_test_button.clicked.connect(self.test)
        self.start_session_button.clicked.connect(
            lambda: self.create_session(self.session_name_edit.text())
        )
        self.close_session_button.clicked.connect(self.close_session)
        self.disconnect_camera_button.clicked.connect(self.disconnect_camera)

        self.connect_camera_button.clicked.connect(self.connect_camera)

        self.session_name_edit.textChanged.connect(
            lambda text: self.start_session_button.setEnabled(
                True if len(text) > 0 else False
            ))

        self.preview_list.currentItemChanged.connect(self.on_select_preview_image_item)

        self.thread = QThread()
        self.camera_worker.moveToThread(self.thread)
        self.thread.start()

        # self.camera_worker.events.image_captured.connect(self.show_image)
        self.camera_worker.events.test_image_captured.connect(self.on_preview_image_captured)
        self.camera_worker.events.config_updated.connect(self.on_config_update)
        self.camera_worker.events.camera_found.connect(
            lambda camera_name:
                self.set_camera_state(CameraStates.Disconnected(camera_name=camera_name))
        )
        self.camera_worker.events.camera_connected.connect(
            lambda camera_name:
                self.set_camera_state(CameraStates.Connected(camera_name=camera_name))
        )
        self.camera_worker.events.camera_disconnected.connect(self.on_camera_disconnected)

        self.camera_worker.initialize()

        self.camera_worker.commands.find_camera.emit()
        self.show()

    def set_camera_connection_busy(self, busy: bool = True):
        self.connect_camera_button.setEnabled(not busy)
        self.disconnect_camera_button.setEnabled(not busy)
        self.camera_busy_spinner.isAnimated = busy

    def connect_camera(self):
        self.set_camera_state(CameraStates.Connecting())
        self.camera_worker.commands.connect_camera.emit()

    def disconnect_camera(self):
        self.set_camera_state(CameraStates.Disconnecting())
        self.camera_worker.commands.disconnect_camera.emit(True)


    def on_camera_disconnected(self, camera_name, manual):
        self.set_camera_state(CameraStates.Disconnected(camera_name=camera_name, manual=manual))


    def create_session(self, name):
        print("Create" + name)
        self.session = Session(name)

    def close_session(self):
        self.session = None

    def config_hookup_select(self, config: gp.CameraWidget, config_name, combo_box: QComboBox):
        cfg = config.get_child_by_name(config_name)
        for idx, choice in enumerate(cfg.get_choices()):
            combo_box.addItem(choice)
            if choice == cfg.get_value():
                combo_box.setCurrentIndex(idx)

    def on_config_update(self, config: gp.CameraWidget):
        self.config_hookup_select(config, "f-number", self.f_number_select)
        self.config_hookup_select(config, "shutterspeed", self.shutter_speed_select)

    def test(self):
        self.camera_worker.commands.capture_test_image.emit("%s_test_%s.jpg" % (self.session.name, str(self.preview_counter).zfill(2)))

    def on_preview_image_captured(self, path):

        if not os.path.isfile(path):
            mbox = QMessageBox()
            mbox.setText("Datei %s wurde nicht gespeichert" % path)
            return

        worker = LoadImageWorker(path)
        worker.signals.finished.connect(self.add_preview_image_item)
        worker.signals.finished.connect(self.show_image)
        self.threadpool.start(worker)

        self.preview_counter += 1

    def add_preview_image_item(self, load_image_result: LoadImageWorkerResult):
        list_item = QListWidgetItem()
        file_name = Path(load_image_result.path).name
        list_item.setData(Qt.ItemDataRole.UserRole, load_image_result.path)
        list_item.setData(Qt.ItemDataRole.DecorationRole, load_image_result.pixmap.scaled(200, 200, Qt.AspectRatioMode.KeepAspectRatio))

        exposure_time = Fraction(float(load_image_result.exif["ExposureTime"]))
        f_number = load_image_result.exif["FNumber"]
        list_item.setText("%s\nf/%s | %s" %(file_name, f_number, exposure_time))


        self.preview_list.addItem(list_item)
        self.preview_list.blockSignals(True)
        self.preview_list.setCurrentItem(list_item)
        self.preview_list.blockSignals(False)

    def on_select_preview_image_item(self, item: QListWidgetItem):
        print("SELECTED")
        path = item.data(Qt.ItemDataRole.UserRole)
        worker = LoadImageWorker(path)
        worker.signals.finished.connect(self.show_image)

        self.threadpool.start(worker)

        print(path)

    def show_image(self, load_image_result: LoadImageWorkerResult):
        self.photo_viewer.setPhoto(load_image_result.pixmap)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = GUI()
    win.show()
    sys.exit(app.exec())
