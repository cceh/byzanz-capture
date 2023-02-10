import sys
from abc import ABC, ABCMeta
from collections import namedtuple
from enum import Enum, auto
from fractions import Fraction
from pathlib import Path

import os
from typing import NamedTuple, Union

from PyQt6.QtCore import QThread, Qt, QThreadPool, QFileSystemWatcher, QSettings, QStandardPaths
from PyQt6.QtGui import QPixmap, QImageReader
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QMessageBox, QPushButton, QWidget, QListWidget, QListWidgetItem, QFrame, QLineEdit,
    QComboBox, QLabel, QToolBox, QGroupBox
)
from PyQt6.uic import loadUi

from camera_worker import CameraWorker, CaptureImagesRequest
from photo_browser import PhotoBrowser
from load_image_worker import LoadImageWorker, LoadImageWorkerResult
from photo_viewer import PhotoViewer

import gphoto2 as gp
from time import sleep

from settings_dialog import SettingsDialog
from spinner import Spinner


class Session:
    def __init__(self, name, working_dir):
        self.name = name
        self.session_dir = os.path.join(working_dir, self.name)
        self.preview_dir = os.path.join(self.session_dir, "test")
        self.images_dir = os.path.join(self.session_dir, "images")


class CameraStates:
    class WaitingForCamera: pass

    class Disconnected(NamedTuple):
        camera_name: str
        manual: bool = False

    class Disconnecting: pass

    class Connecting: pass

    class Connected(NamedTuple):
        camera_name: str

    class WaitingForConfig: pass

    StateType = Union[WaitingForCamera, Disconnected, Connected, Connecting, Disconnecting]


# Corresponds to itemIndex of the captureView QToolBox
class CaptureMode(Enum):
    Preview = 0
    RTI = 1


class RTICaptureMainWindow(QMainWindow):
    camera_worker = CameraWorker()
    preview_counter = 1

    __session: Session

    @property
    def capture_mode(self) -> CaptureMode:
        return CaptureMode(self.capture_view.currentIndex())

    @capture_mode.setter
    def capture_mode(self, mode: CaptureMode):
        self.capture_view.setCurrentIndex(mode.value)

    @property
    def session(self):
        return self.__session

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
    def camera_state(self):
        return self.__camera_state

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

        self.disconnect_camera_button: QPushButton = self.findChild(QPushButton, "disconnectCameraButton")
        self.connect_camera_button: QPushButton = self.findChild(QPushButton, "connectCameraButton")
        self.camera_busy_spinner: Spinner = self.findChild(QWidget, "cameraBusySpinner")
        self.camera_state_label: QLabel = self.findChild(QLabel, "cameraStateLabel")
        self.camera_state_icon: QLabel = self.findChild(QLabel, "cameraStateIcon")

        self.session_controls: QWidget = self.findChild(QWidget, "sessionControls")
        self.session_name_edit: QLineEdit = self.findChild(QLineEdit, "sessionNameEdit")
        self.start_session_button: QPushButton = self.findChild(QPushButton, "startSessionButton")
        self.close_session_button: QPushButton = self.findChild(QPushButton, "closeSessionButton")

        self.capture_view: QToolBox = self.findChild(QToolBox, "captureView")
        self.previewImageBrowser: PhotoBrowser = self.findChild(QWidget, "previewImageBrowser")
        self.rtiImageBrowser: PhotoBrowser = self.findChild(QWidget, "rtiImageBrowser")
        self.capture_button: QPushButton = self.findChild(QPushButton, "captureButton")
        self.cancel_capture_button: QPushButton = self.findChild(QPushButton, "cancelCaptureButton")

        self.camera_control_frame: QFrame = self.findChild(QFrame, "cameraControlFrame")
        self.f_number_select: QComboBox = self.findChild(QComboBox, "fNumberSelect")
        self.shutter_speed_select: QComboBox = self.findChild(QComboBox, "shutterSpeedSelect")

        self.set_camera_connection_busy(True)
        self.set_camera_state(CameraStates.WaitingForCamera())
        self.session = None
        self.capture_mode = CaptureMode.Preview

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

        self.camera_thread = QThread()
        self.camera_worker.moveToThread(self.camera_thread)
        self.camera_thread.start()

        # self.camera_worker.events.image_captured.connect(self.show_image)

        self.camera_worker.events.image_captured.connect(self.on_image_captured)

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

    def on_capture_mode_changed(self):
        print(self.capture_mode)

    def open_settings(self):
        dialog = SettingsDialog(self)
        dialog.setModal(True)
        if dialog.exec():
            qsettings = QSettings()
            for name, value in dialog.settings.items():
                qsettings.setValue(name, value)
        else:
            print("not ok")

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
        self.session = Session(name, QSettings().value("workingDirectory"))

        os.makedirs(self.session.session_dir, exist_ok=True)
        os.makedirs(self.session.preview_dir, exist_ok=True)
        os.makedirs(self.session.images_dir, exist_ok=True)
        
        self.previewImageBrowser.open_directory(self.session.preview_dir)
        self.rtiImageBrowser.open_directory(self.session.images_dir)

    def close_session(self):
        self.session = None

    def config_hookup_select(self, config: gp.CameraWidget, config_name, combo_box: QComboBox):
        try:
            combo_box.currentIndexChanged.disconnect()
        except:
            pass

        cfg = config.get_child_by_name(config_name)
        for idx, choice in enumerate(cfg.get_choices()):
            combo_box.addItem(choice)
            if choice == cfg.get_value():
                combo_box.setCurrentIndex(idx)

        combo_box.currentIndexChanged.connect(lambda: self.camera_worker.commands.set_config.emit(
            config_name, combo_box.currentText()
        ))

    def on_config_update(self, config: gp.CameraWidget):
        self.config_hookup_select(config, "f-number", self.f_number_select)
        self.config_hookup_select(config, "shutterspeed2", self.shutter_speed_select)

    def capture_image(self):
        capture_req: CaptureImagesRequest

        if self.capture_mode == CaptureMode.Preview:
            filename_template = self.session.name.replace(" ", "_") + "_test_" + str(self.preview_counter) + "${extension}"
            file_path_template = os.path.join(self.session.preview_dir, filename_template)
            capture_req = CaptureImagesRequest(file_path_template, num_images=1)

        else:
            filename_template = self.session.name.replace(" ", "_") + "_${num}${extension}"
            file_path_template = os.path.join(self.session.images_dir, filename_template)
            capture_req = CaptureImagesRequest(file_path_template, num_images=60)

        capture_req.signal.moveToThread(self.camera_thread)
        self.camera_worker.commands.capture_images.emit(capture_req)
        self.capture_button.setVisible(False)
        self.cancel_capture_button.setVisible(True)

        capture_req.signal.canceled.connect(self.on_capture_cancelled)

    def on_image_captured(self):
        if self.capture_mode == CaptureMode.Preview:
            self.preview_counter += 1

        self.cancel_capture_button.setVisible(False)
        self.capture_button.setVisible(True)

    def on_capture_cancelled(self):
        print("CANCELLED")
        self.capture_button.setVisible(True)
        self.cancel_capture_button.setVisible(False)

    def cancel_capture(self):
        self.cancel_capture_button.setEnabled(False)
        self.camera_worker.commands.cancel.emit()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setOrganizationName("CCeH")
    app.setOrganizationDomain("cceh.uni-koeln.de")
    app.setApplicationName("Byzanz RTI")
    print(app.thread())

    settings = QSettings()
    if "workingDirectory" not in settings.allKeys():
        settings.setValue("workingDirectory",
                          QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation))

    win = RTICaptureMainWindow()
    win.show()
    sys.exit(app.exec())
