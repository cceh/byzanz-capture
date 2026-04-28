import asyncio.exceptions
import json
import logging
import os
import sys
import threading
from enum import Enum
from pathlib import Path

import gphoto2 as gp
import qasync
from qasync import QEventLoop, QThreadExecutor
from PIL.ImageQt import ImageQt
from PyQt6.QtCore import QThread, QSettings, QStandardPaths, pyqtSignal, Qt
from PyQt6.QtGui import QPixmap, QAction, QPixmapCache, QIcon, QColor, QCloseEvent, QBrush, QPainter, QCursor
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QPushButton, QWidget, QFrame, QLineEdit,
    QComboBox, QLabel, QToolBox, QProgressBar, QMenu, QAbstractButton, QInputDialog, QMessageBox, QStyle, QDialog,
    QLCDNumber, QGraphicsView, QSizePolicy, QVBoxLayout
)
from PyQt6.uic import loadUi
from send2trash import send2trash


try:
    from bt_controller_controller import BtControllerController, BtControllerCommand, BtControllerRequest, BtControllerState
    BT_AVAILABLE = True
except:
    BT_AVAILABLE = False

from camera_worker import CameraWorker, CaptureImagesRequest, CameraStates, PropertyChangeEvent, ConfigRequest
from open_session_dialog import OpenSessionDialog
from photo_browser import PhotoBrowser
from settings_dialog import SettingsDialog
from spinner import Spinner
from camera_config_dialog import CameraConfigDialog


class Session:
    def __init__(self, name, working_dir):
        self.images_dir_loaded = False
        self.preview_dir_loaded = False

        self.name = name
        self.session_dir = os.path.join(working_dir, self.name)
        self.preview_dir = os.path.join(self.session_dir, "test")
        self.images_dir = os.path.join(self.session_dir, "images")
        self.preview_count = 0


# Corresponds to itemIndex of the captureView QToolBox
class CaptureMode(Enum):
    Preview = 0
    RTI = 1


class RTICaptureMainWindow(QMainWindow):
    find_camera = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)

        self.logger = logging.getLogger(self.__class__.__name__)

        self.camera_worker = CameraWorker()
        self.__session: Session = None
        self.camera_state: CameraStates.StateType = None
        self.cam_config_dialog: CameraConfigDialog = None

        # Set up UI and find controls
        loadUi('ui/main_window.ui', self)
        self.disconnect_camera_button: QPushButton = self.findChild(QPushButton, "disconnectCameraButton")
        self.connect_camera_button: QPushButton = self.findChild(QPushButton, "connectCameraButton")
        self.camera_busy_spinner: Spinner = self.findChild(QWidget, "cameraBusySpinner")
        self.camera_state_label: QLabel = self.findChild(QLabel, "cameraStateLabel")
        self.camera_state_icon: QLabel = self.findChild(QLabel, "cameraStateIcon")

        self.bluetooth_frame: QFrame = self.findChild(QFrame, "bluetoothFrame")
        self.bluetooth_state_icon: QLabel = self.findChild(QLabel, "bluetoothStateLabel")
        self.bluetooth_connecting_spinner: Spinner = self.findChild(QWidget, "bluetoothConnectingSpinner")

        self.session_controls: QWidget = self.findChild(QWidget, "sessionControls")
        self.session_name_edit: QLineEdit = self.findChild(QLineEdit, "sessionNameEdit")
        self.start_session_button: QPushButton = self.findChild(QPushButton, "startSessionButton")
        self.close_session_button: QPushButton = self.findChild(QPushButton, "closeSessionButton")
        self.session_loading_spinner: Spinner = self.findChild(QWidget, "sessionLoadingSpinner")
        self.session_menu_button: QAbstractButton = self.findChild(QWidget, "sessionMenuButton")

        self.live_view_controls: QWidget = self.findChild(QWidget, "liveViewControls")
        self.toggle_live_view_button: QPushButton = self.findChild(QPushButton, "toggleLiveViewButton")
        self.autofocus_button: QPushButton = self.findChild(QPushButton, "autofocusButton")
        self.light_lcd_number: QLCDNumber = self.findChild(QLCDNumber, "lightLCDNumber")
        self.light_lcd_frame: QFrame = self.findChild(QFrame, "lightLCDFrame")
        self.live_view_error_label: QLabel = self.findChild(QLabel, "liveviewErrorLabel")

        self.preview_led_select: QComboBox = self.findChild(QComboBox, "previewLedSelect")
        self.preview_led_frame: QFrame = self.findChild(QFrame, "previewLedFrame")
        
        self.capture_view: QToolBox = self.findChild(QToolBox, "captureView")
        self.rtiPage: QWidget = self.findChild(QWidget, "rtiPage")
        self.previewPage: QWidget = self.findChild(QWidget, "previewPage")
        self.previewImageBrowser: PhotoBrowser = self.findChild(QWidget, "previewImageBrowser")
        self.rtiImageBrowser: PhotoBrowser = self.findChild(QWidget, "rtiImageBrowser")
        self.capture_button: QPushButton = self.findChild(QPushButton, "captureButton")
        self.cancel_capture_button: QPushButton = self.findChild(QPushButton, "cancelCaptureButton")

        self.rti_progress_view: QWidget = self.findChild(QWidget, "rtiProgressView")
        self.capture_progress_bar: QProgressBar = self.findChild(QProgressBar, "captureProgressBar")
        self.capture_status_label: QLabel = self.findChild(QLabel, "captureStatusLabel")

        self.camera_controls: QFrame = self.findChild(QFrame, "cameraControls")
        self.camera_config_controls: QWidget = self.findChild(QWidget, "cameraConfigControls")
        self.f_number_select: QComboBox = self.findChild(QComboBox, "fNumberSelect")
        self.shutter_speed_select: QComboBox = self.findChild(QComboBox, "shutterSpeedSelect")
        self.crop_select: QComboBox = self.findChild(QComboBox, "cropSelect")
        self.iso_select: QComboBox = self.findChild(QComboBox, "isoSelect")

        self.settings_button: QPushButton = self.findChild(QPushButton, "settingsButton")

        self.session_menu = QMenu(self)
        self.open_session_action = QAction('Vorherige Sitzung öffnen...', self)
        self.open_session_action.triggered.connect(self.open_existing_session_directory)
        self.open_session_action.setIcon(QIcon("ui/open.svg"))
        self.rename_session_action = QAction('Sitzung umbenennen...', self)
        self.rename_session_action.triggered.connect(self.rename_current_session)
        self.rename_session_action.setIcon(QIcon("ui/rename.svg"))
        self.session_menu.addActions([self.open_session_action, self.rename_session_action])

        self.settings_menu = QMenu(self)
        self.open_program_settings_action = QAction('Allgemeine Einstellungen')
        self.open_program_settings_action.triggered.connect(self.open_settings)
        self.open_program_settings_action.setIcon(QIcon("ui/general_settings.svg"))
        self.open_advanced_cam_config_action = QAction('Erweiterte Kamerakonfiguration')
        self.open_advanced_cam_config_action.triggered.connect(self.open_advanced_capture_settings)
        self.open_advanced_cam_config_action.setIcon(QIcon("ui/cam_settings.svg"))
        self.settings_menu.addActions([self.open_program_settings_action, self.open_advanced_cam_config_action])

        self.mirror_graphics_view: QGraphicsView | None = None
        self.second_screen_window: QDialog | None = None

        self.session_name_edit.textChanged.connect(
            lambda text: self.start_session_button.setEnabled(
                True if len(text) > 0 else False
            ))

        self.cancel_capture_button.setVisible(False)

        for i in range(60):
            self.preview_led_select.addItem(str(i + 1), i)

        self.set_camera_connection_busy(True)
        self.capture_mode = CaptureMode.Preview
        self.set_session(None)

        self.camera_thread = QThread()
        self.camera_worker.moveToThread(self.camera_thread)
        self.camera_worker.state_changed.connect(self.set_camera_state)
        self.camera_worker.events.config_updated.connect(self.on_config_update)
        self.camera_worker.property_changed.connect(self.on_property_change)
        self.camera_worker.preview_image.connect(lambda image: self.previewImageBrowser.show_preview(ImageQt(image.image)))
        self.camera_worker.initialized.connect(lambda: self.camera_worker.commands.find_camera.emit())
        self.camera_thread.started.connect(self.camera_worker.initialize)
        self.camera_thread.start()

        self.bt_controller: BtControllerController | None = None

        self.update_ui_bluetooth()

        self.init_mirror_view()
        QApplication.instance().screenAdded.connect(self.reset_mirror_view)
        QApplication.instance().screenRemoved.connect(self.reset_mirror_view)
        QApplication.instance().primaryScreenChanged.connect(self.reset_mirror_view)


    def init_mirror_view(self):
        screens = QApplication.screens()
        mirror_view_enabled = QSettings().value("enableSecondScreenMirror", type=bool)
        if mirror_view_enabled and len(screens) > 1:
            second_screen = screens[1]
            self.second_screen_window = QDialog()
            self.second_screen_window.setWindowFlags(Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.FramelessWindowHint)
            self.second_screen_window.setWindowTitle("Secondary View")
            self.second_screen_window.setGeometry(second_screen.availableGeometry())
            self.second_screen_window.showFullScreen()

            self.mirror_graphics_view = QGraphicsView(self.second_screen_window)
            self.mirror_graphics_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self.mirror_graphics_view.setBackgroundBrush(QBrush(QColor(30, 30, 30)))
            self.mirror_graphics_view.setRenderHint(QPainter.RenderHint.Antialiasing)
            layout = QVBoxLayout(self.second_screen_window)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(self.mirror_graphics_view)
            self.second_screen_window.setLayout(layout)

        self.update_mirror_view()

    def disable_mirror_view(self):
        if self.second_screen_window:
            self.second_screen_window.close()
            self.second_screen_window = None
        self.mirror_graphics_view = None

    def reset_mirror_view(self):
        self.disable_mirror_view()
        self.init_mirror_view()

    async def init_bluetooth(self):
        if not self.bt_controller:
            self.bt_controller = BtControllerController()
        self.bt_controller.state_changed.connect(self.update_ui_bluetooth)
        await self.bt_controller.connect()

    @property
    def capture_mode(self) -> CaptureMode:
        return CaptureMode(self.capture_view.currentIndex())

    @capture_mode.setter
    def capture_mode(self, mode: CaptureMode):
        self.capture_view.setCurrentIndex(mode.value)
        self.update_ui()

    def get_camera_state(self):
        return self.camera_state

    def set_camera_state(self, state: CameraStates.StateType):
        self.logger.debug("Handle camera state:" + state.__class__.__name__)
        self.camera_state = state
        self.update_ui()

        match state:
            case CameraStates.Waiting():
                pass

            case CameraStates.Found():
                self.camera_worker.commands.connect_camera.emit()

            case CameraStates.Disconnected():
                if state.auto_reconnect:
                    self.camera_worker.commands.find_camera.emit()

            case CameraStates.Connecting():
                pass

            case CameraStates.Disconnecting():
                if self.cam_config_dialog:
                    self.cam_config_dialog.reject()

            case CameraStates.ConnectionError():
                self.logger.error(state.error)

            case CameraStates.Ready():
                pass

            case CameraStates.LiveViewStarted():
                if self.bt_controller and self.bt_controller.state == BtControllerState.CONNECTED:
                    request = BtControllerRequest(BtControllerCommand.PILOT_LIGHT_ON)
                    request.signals.success.connect(lambda: print("BT Success!"))
                    request.signals.error.connect(lambda e: logging.exception(e))
                    self.bt_controller.send_command(request)

            case CameraStates.LiveViewStopped():
                if self.bt_controller and self.bt_controller.state == BtControllerState.CONNECTED:
                    request = BtControllerRequest(BtControllerCommand.LED_OFF)
                    request.signals.success.connect(lambda: print("BT Success!"))
                    request.signals.error.connect(lambda e: logging.exception(e))
                    self.bt_controller.send_command(request)

            case CameraStates.Disconnecting():
                pass

            case CameraStates.CaptureInProgress():
                pass

            case CameraStates.CaptureFinished():
                if self.capture_mode == CaptureMode.Preview:
                    self.session.preview_count += 1
                else:
                    self.write_lp()
                    self.dump_camera_config()

            case CameraStates.CaptureCanceled():
                pass

    def update_ui(self):
        # variables on which the UI state depends
        camera_state = self.camera_state

        has_session = self.session is not None
        session_loaded = has_session \
                         and self.session.preview_dir_loaded \
                         and self.session.images_dir_loaded
        capture_mode = self.capture_mode

        # configure UI according to the capture mode
        for item_index in range(self.capture_view.count()):
            if item_index == self.capture_mode.value:
                self.capture_view.setItemIcon(item_index, QIcon("ui/chevron_down.svg"))
            else:
                self.capture_view.setItemIcon(item_index, QIcon("ui/chevron_right.svg"))




        # configure UI according to the state of the current session
        self.session_name_edit.setEnabled(not has_session)
        self.start_session_button.setVisible(not has_session)
        self.open_session_action.setEnabled(not has_session)
        self.rename_session_action.setEnabled(session_loaded)

        self.close_session_button.setVisible(has_session)
        self.close_session_button.setText("Sitzung beenden" if session_loaded else "Laden abbrechen...")

        self.session_loading_spinner.isAnimated = has_session and not session_loaded
        self.capture_view.setEnabled(has_session)

        self.capture_progress_bar.setMaximum(60)
        self.capture_progress_bar.setValue(self.rtiImageBrowser.num_files() if session_loaded else 0)

        if has_session:
            self.session_name_edit.setText(self.session.name)

        # configure UI according to the camera state
        match camera_state:
            case CameraStates.Waiting():
                self.camera_state_label.setText("Suche Kamera...")
                self.camera_state_icon.setPixmap(QPixmap("ui/camera_waiting.png"))
                self.open_advanced_cam_config_action.setEnabled(False)

                self.connect_camera_button.setEnabled(False)
                self.disconnect_camera_button.setVisible(False)
                self.camera_busy_spinner.isAnimated = True
                self.capture_status_label.setText(None)

                self.live_view_controls.setEnabled(False)
                self.light_lcd_frame.setEnabled(False)
                self.light_lcd_number.display(None)
                self.live_view_error_label.setText(None)

                self.camera_controls.setEnabled(False)
                self.camera_config_controls.setEnabled(False)
                self.capture_button.setText("Nicht verbunden")
                self.capture_view.setItemEnabled(CaptureMode.Preview.value, True)
                self.capture_view.setItemEnabled(CaptureMode.RTI.value, True)

            case CameraStates.Found():
                pass

            case CameraStates.Disconnected():
                self.camera_state_label.setText("Kamera getrennt<br><b>%s</b>" % camera_state.camera_name)
                self.camera_state_icon.setPixmap(QPixmap("ui/camera_not_ok.png"))

                self.connect_camera_button.setEnabled(True)
                self.connect_camera_button.setVisible(True)
                self.disconnect_camera_button.setVisible(False)
                self.camera_busy_spinner.isAnimated = False

                self.toggle_live_view_button.setChecked(False)
                self.autofocus_button.setEnabled(False)


                self.capture_view.setItemEnabled(CaptureMode.Preview.value, True)
                self.capture_view.setItemEnabled(CaptureMode.RTI.value, True)

            case CameraStates.Connecting():
                self.camera_state_label.setText("Verbinde... <br><b>%s</b>" % camera_state.camera_name)
                self.connect_camera_button.setEnabled(False)
                self.camera_busy_spinner.isAnimated = True

            case CameraStates.ConnectionError():
                pass

            case CameraStates.Ready():
                self.camera_state_label.setText("Kamera verbunden<br><b>%s</b>" % camera_state.camera_name)
                self.camera_state_icon.setPixmap(QPixmap("ui/camera_ok.png"))

                self.open_advanced_cam_config_action.setEnabled(True)

                self.disconnect_camera_button.setEnabled(True)
                self.disconnect_camera_button.setVisible(True)
                self.connect_camera_button.setVisible(False)
                self.camera_busy_spinner.isAnimated = False

                self.live_view_controls.setEnabled(True)
                self.toggle_live_view_button.setChecked(False)
                self.autofocus_button.setEnabled(False)

                self.camera_controls.setEnabled(True if session_loaded else False)
                self.camera_config_controls.setEnabled(True)
                if self.capture_mode == CaptureMode.Preview:
                    self.capture_button.setText("Vorschaubild aufnehmen")
                else:
                    self.capture_button.setText("RTI-Aufnahme starten")
                self.capture_view.setItemEnabled(CaptureMode.Preview.value, True)
                self.capture_view.setItemEnabled(CaptureMode.RTI.value, True)

            case CameraStates.Disconnecting():
                self.camera_state_label.setText("Trenne Kamera...")
                self.disconnect_camera_button.setEnabled(False)
                self.disconnect_camera_button.setVisible(True)
                self.open_advanced_cam_config_action.setEnabled(False)

                self.live_view_controls.setEnabled(False)

                self.camera_controls.setEnabled(False)
                self.camera_config_controls.setEnabled(False)
                self.capture_button.setText("Nicht verbunden")

            case CameraStates.LiveViewStarted():
                self.camera_config_controls.setEnabled(False)
                self.autofocus_button.setEnabled(True)
                self.light_lcd_frame.setEnabled(True)
                self.update_lightmeter(camera_state.current_lightmeter_value)

            case CameraStates.LiveViewActive():
                pass

            case CameraStates.FocusStarted():
                self.autofocus_button.setEnabled(False)

            case CameraStates.FocusFinished():
                self.autofocus_button.setEnabled(True)
                if not camera_state.success:
                    self.live_view_error_label.setText("Konnte nicht fokussieren. Zu dunkel?")
                else:
                    self.live_view_error_label.setText(None)

            case CameraStates.LiveViewStopped():
                self.previewImageBrowser.show_preview(None)
                self.light_lcd_number.display(None)
                self.light_lcd_frame.setEnabled(False)
                self.live_view_error_label.setText(None)


            case CameraStates.CaptureInProgress():
                # if prev
                # disable combo boxes
                self.session_controls.setEnabled(False)
                self.disconnect_camera_button.setEnabled(False)

                self.live_view_controls.setEnabled(False)
                self.toggle_live_view_button.setChecked(False)


                self.capture_button.setVisible(False)
                self.cancel_capture_button.setVisible(True)
                self.cancel_capture_button.setEnabled(True)
                self.capture_status_label.setStyleSheet(None)
                self.capture_status_label.setText(None)

                self.camera_config_controls.setEnabled(False)

                if self.capture_mode == CaptureMode.Preview:
                    self.capture_view.setItemEnabled(CaptureMode.RTI.value, False)
                else:
                    self.capture_view.setItemEnabled(CaptureMode.Preview.value, False)

                self.capture_progress_bar.setMaximum(camera_state.capture_request.num_images)
                self.capture_progress_bar.setValue(camera_state.num_captured)

            case CameraStates.CaptureCancelling():
                self.cancel_capture_button.setEnabled(False)

            case CameraStates.CaptureCanceled():
                self.capture_status_label.setText("Aufnahme abgebrochen!")
                self.capture_status_label.setStyleSheet("color: red;")

                self.session_controls.setEnabled(True)
                self.cancel_capture_button.setVisible(False)
                self.capture_button.setVisible(True)
                self.capture_view.setItemEnabled(CaptureMode.Preview.value, True)
                self.capture_view.setItemEnabled(CaptureMode.RTI.value, True)

            case CameraStates.CaptureError():
                self.capture_status_label.setText("Fehler: %s" % str(camera_state.error))
                self.capture_status_label.setStyleSheet("color: red;")

                self.session_controls.setEnabled(True)
                self.cancel_capture_button.setVisible(False)
                self.capture_button.setVisible(True)
                self.capture_view.setItemEnabled(CaptureMode.Preview.value, True)
                self.capture_view.setItemEnabled(CaptureMode.RTI.value, True)

            case CameraStates.CaptureFinished():
                self.capture_status_label.setText("Fertig in %ss!" % str(camera_state.elapsed_time / 1000))
                self.capture_progress_bar.setValue(camera_state.num_captured)
                self.session_controls.setEnabled(True)
                self.cancel_capture_button.setVisible(False)
                self.capture_button.setVisible(True)
                self.capture_view.setItemEnabled(CaptureMode.Preview.value, True)
                self.capture_view.setItemEnabled(CaptureMode.RTI.value, True)

    def update_ui_bluetooth(self):
        if self.bt_controller is not None:
            self.bluetooth_frame.setVisible(True)
            self.preview_led_frame.setVisible(True)

            match self.bt_controller.state:
                case BtControllerState.DISCONNECTED:
                    self.bluetooth_state_icon.setPixmap(QPixmap("ui/bluetooth_disconnected.svg"))
                    self.preview_led_select.setEnabled(False)
                    self.bluetooth_connecting_spinner.isAnimated = False
                    self.bluetooth_frame.setToolTip("Bluetooth-Verbindung zum Controller getrennt")
                case BtControllerState.CONNECTING:
                    self.bluetooth_state_icon.setPixmap(QPixmap("ui/bluetooth_connecting.svg"))
                    self.bluetooth_connecting_spinner.isAnimated = True
                    self.bluetooth_frame.setToolTip("Bluetooth-Verbindung zum Controller wird aufgebaut...")
                case BtControllerState.CONNECTED:
                    self.bluetooth_state_icon.setPixmap(QPixmap("ui/bluetooth_connected.svg"))
                    self.preview_led_select.setEnabled(True)
                    self.bluetooth_connecting_spinner.isAnimated = False
                    self.bluetooth_frame.setToolTip("Bluetooth-Verbindung zum Controller aktiv")
                case BtControllerState.DISCONNECTING:
                    self.bluetooth_state_icon.setPixmap(QPixmap("ui/bluetooth_connecting.svg"))
                    self.bluetooth_connecting_spinner.isAnimated = True
                    self.bluetooth_frame.setToolTip("Bluetooth-Verbindung zum Controller wird getrennt...")

        else:
            self.bluetooth_frame.setVisible(False)
            self.preview_led_frame.setVisible(False)

    @property
    def session(self) -> Session:
        return self.__session

    def set_session(self, _session):
        self.__session = _session
        self.update_ui()

        if _session is None:
            self.session_name_edit.clear()
            self.session_name_edit.setFocus()
            return

        os.makedirs(_session.session_dir, exist_ok=True)
        os.makedirs(_session.preview_dir, exist_ok=True)
        os.makedirs(_session.images_dir, exist_ok=True)

        # both browser will emit the directory_loaded signal connected to the
        # session_directory_loaded slot below (in Qt Designer/Creator, main_window.ui)
        self.previewImageBrowser.open_directory(self.session.preview_dir)
        self.rtiImageBrowser.open_directory(self.session.images_dir)

    def on_capture_mode_changed(self):
        self.update_mirror_view()
        if self.capture_mode == CaptureMode.RTI and isinstance(self.camera_state, CameraStates.LiveViewActive):
            self.camera_worker.commands.live_view.emit(False)
        self.update_ui()

    def update_mirror_view(self):
        if self.capture_mode == CaptureMode.Preview:
            self.previewImageBrowser.set_mirror_graphics_view(self.mirror_graphics_view)
        else:
            self.rtiImageBrowser.set_mirror_graphics_view(self.mirror_graphics_view)

    def open_settings(self):
        q_settings = QSettings()
        dialog = SettingsDialog(q_settings, self)
        dialog.setModal(True)
        if dialog.exec():
            for name, value in dialog.settings.items():
                q_settings.setValue(name, value)
                if name == "maxPixmapCache":
                    QPixmapCache.setCacheLimit(value * 1024)
                elif name == "enableBluetooth":
                    event_loop = asyncio.get_running_loop()
                    if value is True:
                        event_loop.create_task(self.init_bluetooth())
                    elif self.bt_controller:
                        self.bt_controller.bt_disconnect()
                    self.update_ui_bluetooth()
                elif name == "enableSecondScreenMirror":
                    self.reset_mirror_view()

    def open_advanced_capture_settings(self):
        def open_dialog(cfg: gp.CameraWidget):
            self.cam_config_dialog = CameraConfigDialog(cfg, self)
            if self.cam_config_dialog.exec():
                self.camera_worker.commands.set_config.emit(cfg)
                print(cfg.__dict__)
            self.cam_config_dialog = None

        req = ConfigRequest()
        req.signal.got_config.connect(open_dialog)
        self.camera_worker.commands.get_config.emit(req)

    def set_camera_connection_busy(self, busy: bool = True):
        self.connect_camera_button.setEnabled(not busy)
        self.disconnect_camera_button.setEnabled(not busy)
        self.camera_busy_spinner.isAnimated = busy

    def connect_camera(self):
        self.camera_worker.commands.connect_camera.emit()

    def disconnect_camera(self):
        self.camera_worker.commands.disconnect_camera.emit()

    def create_session(self):
        name = self.session_name_edit.text()

        print("Create" + name)
        session = Session(name, QSettings().value("workingDirectory"))
        if Path(session.session_dir).exists():
            result = QMessageBox.warning(self, "Fehler",
                                         "Sitzung %s existiert bereits. Soll sie erneut geöffnet werden?" % name,
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if result == QMessageBox.StandardButton.No:
                return

        self.set_session(session)

    def session_directory_loaded(self, path):
        if not self.session:
            return

        if os.path.normpath(path) == os.path.normpath(self.session.preview_dir):
            self.session.preview_dir_loaded = True
            self.session.preview_count = self.previewImageBrowser.last_index()

        elif os.path.normpath(path) == os.path.normpath(self.session.images_dir):
            self.session.images_dir_loaded = True

        self.update_ui()

    def close_session(self):
        self.previewImageBrowser.close_directory()
        self.rtiImageBrowser.close_directory()
        self.set_session(None)

    def show_session_menu(self):
        self.session_menu.exec(self.session_menu_button.mapToGlobal(self.session_menu_button.rect().bottomLeft()))

    def show_settings_menu(self):
        self.settings_menu.exec(self.settings_button.mapToGlobal(self.session_menu_button.rect().bottomLeft()))

    def open_existing_session_directory(self):
        working_dir = QSettings().value("workingDirectory")
        dialog = OpenSessionDialog(working_dir, self)
        path = dialog.get_session_path()
        if path:
            session_name = Path(path).name
            self.set_session(Session(session_name, working_dir))

    def rename_current_session(self):
        new_name, ok = QInputDialog.getText(self, "Aktuelle Sitzung umbenennen", "Neuer Name", text=self.session.name)
        if ok:
            session_dir = self.session.session_dir
            session_dir_parent = Path(session_dir).parent
            new_session_dir = os.path.join(session_dir_parent, os.path.join(session_dir_parent, new_name))

            if Path(new_session_dir).exists():
                QMessageBox.critical(self, "Fehler", "Sitzung %s existiert bereits." % new_name)
                return

            image_files = [os.path.join(self.session.images_dir, f) for f in os.listdir(self.session.images_dir)]
            preview_files = [os.path.join(self.session.preview_dir, f) for f in os.listdir(self.session.preview_dir)]

            for file in image_files + preview_files:
                path = Path(file)
                parent_path = path.parent
                file_name = path.name
                basename, ext = os.path.splitext(file_name)
                if basename.startswith(self.session.name):
                    new_filename = basename.replace(self.session.name, new_name, 1) + ext
                    new_path = os.path.join(parent_path, new_filename)
                    os.rename(path, new_path)

            os.rename(session_dir, new_session_dir)
            self.close_session()
            self.set_session(Session(new_name, session_dir_parent))

    def write_lp(self):
        file_names = [os.path.basename(file_path) for file_path in self.rtiImageBrowser.files()]
        num_files = len(file_names)
        if num_files != 60:
            logging.warning("Wrong number of files, not writing LP file.")
            return

        lp_template_path = "cceh-dome-template.lp"
        lp_output_path = os.path.join(self.session.images_dir, self.session.name + ".lp")
        with open(lp_template_path, 'r') as lp_template_file, open(lp_output_path, 'w') as lp_output_file:
            logging.info("Writing LP file: " + lp_output_path)
            lp_output_file.write(str(num_files) + "\n")
            for i, input_line in enumerate(lp_template_file):
                output_line = file_names[i] + " " + input_line
                lp_output_file.write(output_line)

    def dump_camera_config(self):
        output_path = os.path.join(self.session.images_dir, "camera_config.json")
        self.logger.info(f"Writing camera configuration dump: {output_path}")

        if not self.session:
            return
        def on_got_config(cfg: gp.CameraWidget):
            cfg_dict = {}

            def traverse_widget(widget, widget_dict):
                widget_type = widget.get_type()

                if widget_type == gp.GP_WIDGET_SECTION or widget_type == gp.GP_WIDGET_WINDOW:
                    # If the widget is a section, traverse its children
                    child_count = widget.count_children()
                    for i in range(child_count):
                        child = widget.get_child(i)
                        child_dict = {}
                        traverse_widget(child, child_dict)
                        widget_dict[child.get_name()] = child_dict
                else:
                    try:
                        widget_dict['value'] = widget.get_value()
                    except gp.GPhoto2Error as err:
                        if err.code == -2:
                            self.logger.warning(f"Could not get config value for {cfg.get_label()} ({cfg.get_name()}).")

                widget_dict['label'] = widget.get_label()

                return widget_dict

            traverse_widget(cfg, cfg_dict)

            try:
                with open(output_path, "w") as output_file:
                    json.dump(cfg_dict, output_file, indent=4)
            except Exception as e:
                self.logger.error(f"Could not write camera config dump to {output_path}:")
                self.logger.exception(e)


        req = ConfigRequest()
        req.signal.got_config.connect(on_got_config)
        self.camera_worker.commands.get_config.emit(req)


    def config_hookup_select(self, config: gp.CameraWidget, config_name, combo_box: QComboBox, value_map: dict = None):
        try:
            combo_box.currentIndexChanged.disconnect()
        except:
            pass
        cfg = config.get_child_by_name(config_name)
        combo_box.clear()
        for idx, choice in enumerate(cfg.get_choices()):
            choice_label = value_map[choice] if value_map and choice in value_map else choice
            combo_box.addItem(choice_label, choice)
            if choice == cfg.get_value():
                combo_box.setCurrentIndex(idx)

        combo_box.currentIndexChanged.connect(lambda: self.camera_worker.commands.set_single_config.emit(
            config_name, combo_box.currentData()
        ))

    def on_config_update(self, config: gp.CameraWidget):
        self.config_hookup_select(config, "iso", self.iso_select)
        self.config_hookup_select(config, "f-number", self.f_number_select)
        self.config_hookup_select(config, "shutterspeed2", self.shutter_speed_select)
        self.config_hookup_select(config, "d030", self.crop_select, {
            "0": "Voll",
            "1": "Klein",
            "2": "Mittel",
            "3": "Mittel 2"
        })

    def on_property_change(self, event: PropertyChangeEvent):
        match event.property_name:
            case "lightmeter":
                if isinstance(self.camera_state, CameraStates.LiveViewActive):
                    self.update_lightmeter(event.value)

    def update_lightmeter(self, value):
        if isinstance(value, float):
            self.light_lcd_number.display(int(value))
        else:
            self.light_lcd_number.display(None)

    def enable_live_view(self, enable: bool):
        self.camera_worker.commands.live_view.emit(enable)

    def trigger_autofocus(self):
        self.camera_worker.commands.trigger_autofocus.emit()

    def capture_image(self):
        capture_req: CaptureImagesRequest

        # Capture Previews
        if self.capture_mode == CaptureMode.Preview:
            filename_template = self.session.name.replace(" ", "_") + "_test_" + str(
                self.session.preview_count + 1) + "${extension}"
            file_path_template = os.path.join(self.session.preview_dir, filename_template)
            capture_req = CaptureImagesRequest(file_path_template, num_images=1, image_quality="JPEG Fine")

        # Capture RTI Series
        else:
            if self.rtiImageBrowser.num_files() > 0:
                message_box = QMessageBox(QMessageBox.Icon.Warning, "RTI-Serie aufnehmen",
                                          "Vorhandene Aufnahmen werden gelöscht.")
                message_box.addButton(
                    QPushButton(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogCancelButton), "Abbrechen"),
                    QMessageBox.ButtonRole.NoRole)
                message_box.addButton(
                    QPushButton(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogOkButton), "Fortfahren"),
                    QMessageBox.ButtonRole.YesRole)
                if not message_box.exec():
                    return

            existing_files = [os.path.join(self.session.images_dir, f) for f in os.listdir(self.session.images_dir)]
            send2trash(existing_files)

            filename_template = self.session.name.replace(" ", "_") + "_${num}${extension}"
            file_path_template = os.path.join(self.session.images_dir, filename_template)
            capture_req = CaptureImagesRequest(file_path_template, num_images=60, expect_files=2,
                                               max_burst=int(QSettings().value("maxBurstNumber")), skip=0, image_quality="NEF+Fine")
            self.capture_progress_bar.setMaximum(119)
            self.capture_progress_bar.setValue(0)

        def on_file_received(path: str):
            print("Rec: " + path)
        capture_req.signal.file_received.connect(on_file_received)

        def start_capture(show_button_message: bool):
            if self.capture_mode == CaptureMode.RTI and show_button_message:
                press_buttons_dialog = QDialog()
                loadUi("ui/press-buttons-dialog.ui", press_buttons_dialog)
                if not press_buttons_dialog.exec():
                    return

            self.camera_worker.commands.capture_images.emit(capture_req)

        if self.bt_controller and self.bt_controller.state == BtControllerState.CONNECTED:
            initial_led = 0 if self.capture_mode == CaptureMode.RTI else self.preview_led_select.currentData()
            request = BtControllerRequest(BtControllerCommand.SET_LED, initial_led)
            request.signals.success.connect(lambda: start_capture(False))
            request.signals.error.connect(lambda: start_capture(True))
            self.bt_controller.send_command(request)
        else:
            start_capture(True)


    def on_capture_cancelled(self):
        logging.info("Capture cancelled")

    def cancel_capture(self):
        self.cancel_capture_button.setEnabled(False)
        self.camera_worker.commands.cancel.emit()

    def closeEvent(self, event: QCloseEvent):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self.camera_thread.requestInterruption()
        self.camera_thread.exit()
        if self.bt_controller and self.bt_controller.state != BtControllerState.DISCONNECTED:
            self.bt_controller.bt_disconnect()
        self.camera_thread.wait()
        if self.second_screen_window:
            self.second_screen_window.close()

        super().closeEvent(event)

if __name__ == "__main__":
    win: RTICaptureMainWindow

    logging.basicConfig(
        format='%(levelname)s: %(name)s: %(message)s', level=logging.INFO)

    app = QApplication(sys.argv)
    app.setOrganizationName("CCeH")
    app.setOrganizationDomain("cceh.uni-koeln.de")
    app.setApplicationName("Byzanz RTI")

    settings = QSettings()
    if "workingDirectory" not in settings.allKeys():
        settings.setValue("workingDirectory",
                          QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation))

    if "maxPixmapCache" not in settings.allKeys():
        settings.setValue("maxPixmapCache", 1024)

    if "maxBurstNumber" not in settings.allKeys():
        settings.setValue("maxBurstNumber", 60)

    if "enableBluetooth" not in settings.allKeys():
        settings.setValue("enableBluetooth", True)

    if "enableSecondScreenMirror" not in settings.allKeys():
        settings.setValue("enableSecondScreenMirror", True)

    QPixmapCache.setCacheLimit(int(settings.value("maxPixmapCache")) * 1024)

    loop: qasync.QEventLoop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    app_close_event = asyncio.Event()
    app.aboutToQuit.connect(app_close_event.set)

    orig_exceptionhook = sys.__excepthook__

    win = RTICaptureMainWindow()
    win.show()

    def excepthook(exc_type, exc_value, exc_traceback):
        logging.exception(msg="Exception", exc_info=(exc_type, exc_value, exc_traceback))
        if win.bt_controller and win.bt_controller.state != BtControllerState.DISCONNECTED:
            win.bt_controller.bt_disconnect()

        loop.call_soon(lambda _loop: _loop.stop(), loop)


    # sys.excepthook = excepthook
    # threading.excepthook = excepthook


    with loop:
        if BT_AVAILABLE and QSettings().value("enableBluetooth", type=bool):
            loop.create_task(win.init_bluetooth())
        else:
            logging.info("Bluetooth not available. Is bleak installed?")

        loop.run_until_complete(app_close_event.wait())

        # asyncio.get_running_loop().run_forever()


