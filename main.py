import asyncio.exceptions
import json
import logging
import os
import sys
from enum import Enum
from pathlib import Path

# Logging + crash reporting BEFORE the gphoto2-path resolver so its
# INFO line (and the autodetect logs from byzanz_camera) are captured.
# Installs the rotating log file, faulthandler (crash.log with per-thread
# stacks on SIGSEGV) and the excepthook that stops PyQt6 from aborting on
# unhandled slot exceptions. Same mechanism as papyri.
from byzanz_camera.logging_setup import install as _install_logging
_install_logging("byzanz-rti", dir_name="ByzanzCapture", debug_env="BYZANZ_DEBUG")

# `gphoto2/__init__.py` rewrites CAMLIBS/IOLIBS on import. Capture the
# env-provided values before that happens, then let the resolver
# decide which source wins (frozen / env / vendor / bundled). See
# byzanz_camera/_gphoto2_paths.py for the full precedence chain.
_pre_camlibs = os.environ.get('CAMLIBS')
_pre_iolibs = os.environ.get('IOLIBS')
import gphoto2 as gp
from byzanz_camera._gphoto2_paths import apply_paths as _apply_gphoto2_paths
_apply_gphoto2_paths(_pre_camlibs, _pre_iolibs)

import qasync
from PIL.ImageQt import ImageQt
from PyQt6.QtCore import QThread, QSettings, QStandardPaths, pyqtSignal, Qt, QTranslator, QTimer, QLocale
from PyQt6.QtGui import QPixmap, QAction, QPixmapCache, QIcon, QColor, QCloseEvent, QBrush, QPainter
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QPushButton, QWidget, QFrame, QLineEdit,
    QComboBox, QLabel, QToolBox, QProgressBar, QMenu, QAbstractButton, QInputDialog, QMessageBox, QStyle, QDialog,
    QLCDNumber, QGraphicsView, QSizePolicy, QVBoxLayout
)
from PyQt6.uic import loadUi
from send2trash import send2trash

from byzanz_camera.helpers import get_ui_path
from byzanz_camera.config_combo import ConfigComboBox
from byzanz_camera.profiles import PROFILES

try:
    from bt_controller_controller import BtControllerController, BtControllerCommand, BtControllerRequest, BtControllerState
    BT_AVAILABLE = True
except:
    BT_AVAILABLE = False

from byzanz_camera.camera_worker import CameraWorker, CaptureImagesRequest, CameraStates, PropertyChangeEvent, ConfigRequest, \
    ConfigProtocol, widget_to_dict
from byzanz_camera import dome_config
from byzanz_camera.settings_migration import migrate_settings
from open_session_dialog import OpenSessionDialog
from byzanz_camera.filmstrip_widget import FilmstripWidget
from byzanz_camera.viewer_widget import ViewerWidget
from byzanz_camera.zoom_control_bar import ZoomControlBar
from settings_dialog import SettingsDialog
from byzanz_camera.spinner import Spinner
from camera_config_dialog import CameraConfigDialog


# Camera profiles live in the shared registry (byzanz_camera/profiles) so every
# app variant offers the same cameras. Virtual cameras are manually selectable
# in Settings, never auto-selected — a real profile's model pattern excludes
# them from autodetect (see _apply_camera_filter).

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

        self.profile = PROFILES[QSettings().value("cameraProfile", "NikonD800E")]
        # The dome (shot count, capture strategy, light controller) is config
        # data, independent of the camera — see byzanz_camera/dome_config.py.
        self.dome = dome_config.current_dome(QSettings())
        # Filter detection to this profile's camera before the worker's first
        # find_camera (emitted on `initialized`, below).
        self._apply_camera_filter(self.profile)

        # Set up UI and find controls
        loadUi(get_ui_path('ui/main_window.ui'), self)
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
        self.preview_viewer: ViewerWidget = self.findChild(ViewerWidget, "previewViewer")
        self.preview_filmstrip: FilmstripWidget = self.findChild(FilmstripWidget, "previewFilmstrip")
        self.preview_zoom_bar: ZoomControlBar = self.findChild(ZoomControlBar, "previewZoomBar")
        self.preview_viewer.attach_zoom_bar(self.preview_zoom_bar)
        self.rti_viewer: ViewerWidget = self.findChild(ViewerWidget, "rtiViewer")
        self.rti_filmstrip: FilmstripWidget = self.findChild(FilmstripWidget, "rtiFilmstrip")
        self.rti_zoom_bar: ZoomControlBar = self.findChild(ZoomControlBar, "rtiZoomBar")
        self.rti_viewer.attach_zoom_bar(self.rti_zoom_bar)

        # Filmstrip → viewer wiring (per mode). The filmstrip handles all
        # the directory/async-thumb work and emits a decoded pixmap when
        # the user clicks a thumb (or when a fresh capture lands); the
        # viewer just receives and displays. The .ui still wires the
        # directory_loaded signal to session_directory_loaded.
        for filmstrip, viewer in (
            (self.preview_filmstrip, self.preview_viewer),
            (self.rti_filmstrip, self.rti_viewer),
        ):
            filmstrip.image_decoded.connect(
                lambda path, pixmap, v=viewer: v.show_image(pixmap)
            )
            filmstrip.image_decode_started.connect(viewer.show_busy)
            filmstrip.image_cleared.connect(viewer.clear)
            filmstrip.directory_closed.connect(lambda path, v=viewer: v.clear())
            # Zoom controls belong to a static photo being reviewed — keep their
            # visibility in step with what the viewer actually shows.
            filmstrip.image_decoded.connect(lambda *_: self._update_zoom_visibility())
            filmstrip.image_cleared.connect(self._update_zoom_visibility)
            filmstrip.directory_closed.connect(lambda *_: self._update_zoom_visibility())
        # Choosing a preview test shot means "review this" — switch live view off
        # so the shot stays put and the zoom controls appear over it.
        self.preview_filmstrip.image_selected.connect(self._on_preview_capture_selected)
        # Toggling live view flips the zoom controls immediately (the toggle is
        # the source of truth), rather than waiting for the async camera state.
        self.toggle_live_view_button.toggled.connect(lambda *_: self._update_zoom_visibility())
        self.capture_button: QPushButton = self.findChild(QPushButton, "captureButton")
        self.cancel_capture_button: QPushButton = self.findChild(QPushButton, "cancelCaptureButton")

        self.rti_progress_view: QWidget = self.findChild(QWidget, "rtiProgressView")
        self.capture_progress_bar: QProgressBar = self.findChild(QProgressBar, "captureProgressBar")
        self.capture_status_label: QLabel = self.findChild(QLabel, "captureStatusLabel")

        self.camera_controls: QFrame = self.findChild(QFrame, "cameraControls")
        self.camera_config_controls: QWidget = self.findChild(QWidget, "cameraConfigControls")
        self.f_number_select: ConfigComboBox = self.findChild(ConfigComboBox, "fNumberSelect")
        self.shutter_speed_select: ConfigComboBox = self.findChild(ConfigComboBox, "shutterSpeedSelect")
        self.crop_select: ConfigComboBox = self.findChild(ConfigComboBox, "cropSelect")
        self.iso_select: ConfigComboBox = self.findChild(ConfigComboBox, "isoSelect")
        # A user pick on any capture-setting combo routes to the worker.
        # Connected once; the widget only emits on genuine user changes
        # (never on the 0.5s poll), so no disconnect/reconnect churn.
        for combo in (self.iso_select, self.f_number_select,
                      self.shutter_speed_select, self.crop_select):
            combo.value_chosen.connect(
                lambda name, value:
                    self.camera_worker.commands.set_single_config.emit(name, value)
            )

        self.settings_button: QPushButton = self.findChild(QPushButton, "settingsButton")

        self.session_menu = QMenu(self)
        self.open_session_action = QAction('Vorherige Sitzung öffnen...', self)
        self.open_session_action.triggered.connect(self.open_existing_session_directory)
        self.open_session_action.setIcon(QIcon(get_ui_path("ui/open.svg")))
        self.rename_session_action = QAction('Sitzung umbenennen...', self)
        self.rename_session_action.triggered.connect(self.rename_current_session)
        self.rename_session_action.setIcon(QIcon(get_ui_path("ui/rename.svg")))
        self.session_menu.addActions([self.open_session_action, self.rename_session_action])

        self.settings_menu = QMenu(self)
        self.open_program_settings_action = QAction(self.tr('Allgemeine Einstellungen'))
        self.open_program_settings_action.triggered.connect(self.open_settings)
        self.open_program_settings_action.setIcon(QIcon(get_ui_path("ui/general_settings.svg")))
        self.open_advanced_cam_config_action = QAction(self.tr('Erweiterte Kamerakonfiguration'))
        self.open_advanced_cam_config_action.triggered.connect(self.open_advanced_capture_settings)
        self.open_advanced_cam_config_action.setIcon(QIcon(get_ui_path("ui/cam_settings.svg")))
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
        self.camera_worker.preview_image.connect(self._on_live_frame)
        self.camera_worker.initialized.connect(lambda: self.camera_worker.commands.find_camera.emit())
        self.camera_worker.usb_offenders_detected.connect(self._on_usb_offenders_detected)
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
            self.second_screen_window.setWindowTitle(self.tr("Sekundäransicht"))
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

    def _on_usb_offenders_detected(self, offenders: list):
        """macOS USB-claim recovery couldn't free the camera. While the
        dialog is on screen, set_camera_state suppresses the auto-
        reconnect dispatch. Dismissing the dialog re-triggers
        find_camera to resume."""
        if getattr(self, "_usb_offender_dialog_open", False):
            return
        labels = sorted({label for _, label in offenders})
        message = (
            "The camera is being held by another application:\n\n  · "
            + "\n  · ".join(labels)
            + "\n\nQuit the listed application(s) and click OK to retry."
        )
        self.logger.warning("USB-claim recovery failed; offenders: %s",
                            ", ".join(labels))
        self._usb_offender_dialog_open = True
        try:
            QMessageBox.warning(self, "Camera is busy", message)
        finally:
            self._usb_offender_dialog_open = False
        self.camera_worker.commands.find_camera.emit()

    def set_camera_state(self, state: CameraStates.StateType):
        self.logger.debug("Handle camera state:" + state.__class__.__name__)
        self.camera_state = state
        self.update_ui()

        match state:
            case CameraStates.Waiting():
                pass

            case CameraStates.Found():
                self.connect_camera()

            case CameraStates.Disconnected():
                # Suppress auto-reconnect while the "camera is busy"
                # dialog is on screen. Dismissing the dialog triggers
                # a manual find_camera in _on_usb_offenders_detected.
                if state.auto_reconnect and not getattr(
                    self, "_usb_offender_dialog_open", False
                ):
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
                    if state.num_captured == state.capture_request.num_images:
                        self.check_and_write_lp(state.capture_request.num_images, 1000)
                    else:
                        logging.warning("Wrong number of files, not writing LP file.")

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
                self.capture_view.setItemIcon(item_index, QIcon(get_ui_path("ui/chevron_down.svg")))
            else:
                self.capture_view.setItemIcon(item_index, QIcon(get_ui_path("ui/chevron_right.svg")))




        # configure UI according to the state of the current session
        self.session_name_edit.setEnabled(not has_session)
        self.start_session_button.setVisible(not has_session)
        self.open_session_action.setEnabled(not has_session)
        self.rename_session_action.setEnabled(session_loaded)

        self.close_session_button.setVisible(has_session)
        self.close_session_button.setText(self.tr("Sitzung beenden") if session_loaded else self.tr("Laden abbrechen..."))

        self.session_loading_spinner.isAnimated = has_session and not session_loaded
        self.capture_view.setEnabled(has_session)

        self.capture_progress_bar.setMaximum(self.dome.num_positions)
        self.capture_progress_bar.setValue(self.rti_filmstrip.num_files() if session_loaded else 0)

        if has_session:
            self.session_name_edit.setText(self.session.name)

        # configure UI according to the camera state
        match camera_state:
            case CameraStates.Waiting():
                self.camera_state_label.setText(self.tr("Suche Kamera..."))
                self.camera_state_icon.setPixmap(QPixmap(get_ui_path("ui/camera_waiting.png")))
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
                self.capture_button.setText(self.tr("Nicht verbunden"))
                self.capture_view.setItemEnabled(CaptureMode.Preview.value, True)
                self.capture_view.setItemEnabled(CaptureMode.RTI.value, True)

            case CameraStates.Found():
                pass

            case CameraStates.Disconnected():
                self.camera_state_label.setText(self.tr("Kamera getrennt<br><b>%s</b>") % camera_state.camera_name)
                self.camera_state_icon.setPixmap(QPixmap(get_ui_path("ui/camera_not_ok.png")))

                self.connect_camera_button.setEnabled(True)
                self.connect_camera_button.setVisible(True)
                self.disconnect_camera_button.setVisible(False)
                self.camera_busy_spinner.isAnimated = False

                self.toggle_live_view_button.setChecked(False)
                self.autofocus_button.setEnabled(False)


                self.capture_view.setItemEnabled(CaptureMode.Preview.value, True)
                self.capture_view.setItemEnabled(CaptureMode.RTI.value, True)

            case CameraStates.Connecting():
                self.camera_state_label.setText(self.tr("Verbinde... <br><b>%s</b>") % camera_state.camera_name)
                self.connect_camera_button.setEnabled(False)
                self.camera_busy_spinner.isAnimated = True

            case CameraStates.ConnectionError():
                pass

            case CameraStates.Ready():
                self.camera_state_label.setText(self.tr("Kamera verbunden<br><b>%s</b>") % camera_state.camera_name)
                self.camera_state_icon.setPixmap(QPixmap(get_ui_path("ui/camera_ok.png")))

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
                    self.capture_button.setText(self.tr("Vorschaubild aufnehmen"))
                else:
                    self.capture_button.setText(self.tr("RTI-Aufnahme starten"))
                self.capture_view.setItemEnabled(CaptureMode.Preview.value, True)
                self.capture_view.setItemEnabled(CaptureMode.RTI.value, True)

            case CameraStates.Disconnecting():
                self.camera_state_label.setText(self.tr("Trenne Kamera..."))
                self.disconnect_camera_button.setEnabled(False)
                self.disconnect_camera_button.setVisible(True)
                self.open_advanced_cam_config_action.setEnabled(False)

                self.live_view_controls.setEnabled(False)

                self.camera_controls.setEnabled(False)
                self.camera_config_controls.setEnabled(False)
                self.capture_button.setText(self.tr("Nicht verbunden"))

            case CameraStates.LiveViewStarted():
                if not self.profile.enable_capture_controls_in_live_preview():
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
                    self.live_view_error_label.setText(self.tr("Konnte nicht fokussieren. Zu dunkel?"))
                else:
                    self.live_view_error_label.setText(None)

            case CameraStates.LiveViewStopped():
                # Don't blank the viewer here: with live view off, incoming
                # frames are already dropped (see _on_live_frame), so a reviewed
                # shot / the last frame stays put instead of flashing away.
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
                print(camera_state.capture_request.num_images)
                print(camera_state.num_captured)


            case CameraStates.CaptureCancelling():
                self.cancel_capture_button.setEnabled(False)

            case CameraStates.CaptureCanceled():
                self.capture_status_label.setText(self.tr("Aufnahme abgebrochen!"))
                self.capture_status_label.setStyleSheet("color: red;")

                self.session_controls.setEnabled(True)
                self.cancel_capture_button.setVisible(False)
                self.capture_button.setVisible(True)
                self.capture_view.setItemEnabled(CaptureMode.Preview.value, True)
                self.capture_view.setItemEnabled(CaptureMode.RTI.value, True)

            case CameraStates.CaptureError():
                self.capture_status_label.setText(self.tr("Fehler: %s" % str(camera_state.error)))
                self.capture_status_label.setStyleSheet("color: red;")

                self.session_controls.setEnabled(True)
                self.cancel_capture_button.setVisible(False)
                self.capture_button.setVisible(True)
                self.capture_view.setItemEnabled(CaptureMode.Preview.value, True)
                self.capture_view.setItemEnabled(CaptureMode.RTI.value, True)

            case CameraStates.CaptureFinished():
                self.capture_status_label.setText(self.tr("Fertig in %ss!") % str(camera_state.elapsed_time / 1000))
                self.capture_progress_bar.setValue(camera_state.num_captured)
                self.session_controls.setEnabled(True)
                self.cancel_capture_button.setVisible(False)
                self.capture_button.setVisible(True)
                self.capture_view.setItemEnabled(CaptureMode.Preview.value, True)
                self.capture_view.setItemEnabled(CaptureMode.RTI.value, True)

        # Show the zoom controls only over a static photo — never during live
        # view, never when empty (keeps them in step with every state change).
        self._update_zoom_visibility()

    def update_ui_bluetooth(self):
        if self.bt_controller is not None:
            self.bluetooth_frame.setVisible(True)
            self.preview_led_frame.setVisible(True)

            match self.bt_controller.state:
                case BtControllerState.DISCONNECTED:
                    self.bluetooth_state_icon.setPixmap(QPixmap(get_ui_path("ui/bluetooth_disconnected.svg")))
                    self.preview_led_select.setEnabled(False)
                    self.bluetooth_connecting_spinner.isAnimated = False
                    self.bluetooth_frame.setToolTip(self.tr("Bluetooth-Verbindung zum Controller getrennt"))
                case BtControllerState.CONNECTING:
                    self.bluetooth_state_icon.setPixmap(QPixmap(get_ui_path("ui/bluetooth_connecting.svg")))
                    self.bluetooth_connecting_spinner.isAnimated = True
                    self.bluetooth_frame.setToolTip(self.tr("Bluetooth-Verbindung zum Controller wird aufgebaut..."))
                case BtControllerState.CONNECTED:
                    self.bluetooth_state_icon.setPixmap(QPixmap(get_ui_path("ui/bluetooth_connected.svg")))
                    self.preview_led_select.setEnabled(True)
                    self.bluetooth_connecting_spinner.isAnimated = False
                    self.bluetooth_frame.setToolTip(self.tr("Bluetooth-Verbindung zum Controller aktiv"))
                case BtControllerState.DISCONNECTING:
                    self.bluetooth_state_icon.setPixmap(QPixmap(get_ui_path("ui/bluetooth_connecting.svg")))
                    self.bluetooth_connecting_spinner.isAnimated = True
                    self.bluetooth_frame.setToolTip(self.tr("Bluetooth-Verbindung zum Controller wird getrennt..."))

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

        # Both filmstrips emit directory_loaded → session_directory_loaded
        # via the .ui-defined slot connection.
        self.preview_filmstrip.open_directory(self.session.preview_dir)
        self.rti_filmstrip.open_directory(self.session.images_dir)

    def on_capture_mode_changed(self):
        self.update_mirror_view()
        if self.capture_mode == CaptureMode.RTI and self.toggle_live_view_button.isChecked():
            # The RTI-series page has no live view — make sure it's off (so the
            # RTI zoom controls, gated on "not live", can appear).
            self.toggle_live_view_button.setChecked(False)
        # The viewer on a QToolBox page is laid out at zero size while that page
        # is hidden, so an image it decoded then fit against an empty viewport
        # (shows tiny). Re-fit the now-visible page's viewer once layout settles.
        viewer = self.preview_viewer if self.capture_mode == CaptureMode.Preview else self.rti_viewer
        QTimer.singleShot(0, viewer.fit)
        self.update_ui()

    def _update_zoom_visibility(self):
        """Zoom controls belong to a static photo being reviewed: hide them
        during live view (a live stream isn't zoomed) and when the viewer is
        empty. Called on camera-state changes and whenever the viewer's content
        changes (filmstrip decode/clear)."""
        # "Live view on" is exactly the toggle's state (set the instant the user
        # clicks, unlike the async camera_state) — so it also hides the controls
        # the moment a shot is selected for review.
        live = self.toggle_live_view_button.isChecked()
        self.preview_zoom_bar.setVisible(self.preview_viewer.has_photo() and not live)
        self.rti_zoom_bar.setVisible(self.rti_viewer.has_photo())

    def update_mirror_view(self):
        if self.capture_mode == CaptureMode.Preview:
            self.preview_viewer.set_mirror_graphics_view(self.mirror_graphics_view)
        else:
            self.rti_viewer.set_mirror_graphics_view(self.mirror_graphics_view)

    def open_settings(self):
        q_settings = QSettings()
        dialog = SettingsDialog(q_settings, PROFILES, self)
        dialog.setModal(True)
        if dialog.exec():
            dome_changed = False
            for name, value in dialog.settings.items():
                if name == "cameraProfile":
                    new_profile = PROFILES[value]
                    if new_profile is not self.profile:
                        # Never yank the camera out from under an in-flight
                        # capture. The setting is not persisted either, so
                        # QSettings and runtime state stay consistent.
                        if isinstance(self.camera_state,
                                      (CameraStates.CaptureInProgress,
                                       CameraStates.CaptureCancelling)):
                            QMessageBox.information(
                                self, self.tr("Capture in progress"),
                                self.tr("The camera profile was not changed "
                                        "because a capture is running. Change "
                                        "it again once the capture has "
                                        "finished."))
                            continue
                        self.profile = new_profile
                        # Refilter detection to the new camera and grant the
                        # new target a fresh USB-recovery allowance (the old
                        # one may have spent it). The detection filter lives as
                        # plain worker attributes, so it must be rebound here;
                        # the pending connect_camera(self.profile) carries the
                        # new profile object.
                        self._apply_camera_filter(new_profile)
                        self.camera_worker.reset_usb_recovery_budget()
                        # In Waiting there is no connection to tear down and
                        # the worker can't process a queued reconnect while
                        # inside its find loop — the pending Found →
                        # connect_camera(self.profile) picks up the new
                        # profile by itself.
                        if not isinstance(self.camera_state,
                                          CameraStates.Waiting):
                            self.reconnect_cammera()
                    q_settings.setValue(name, value)
                    continue
                q_settings.setValue(name, value)
                if name == "maxPixmapCache":
                    QPixmapCache.setCacheLimit(value * 1024)
                elif name == "enableSecondScreenMirror":
                    self.reset_mirror_view()
                elif name.startswith("dome/"):
                    dome_changed = True

            # The dome/* keys are written above; re-read the whole record once
            # and reconcile anything that depends on it (BLE, progress range).
            if dome_changed:
                was_using_bt = self.dome.uses_bluetooth
                self.dome = dome_config.current_dome(q_settings)
                self._reconcile_bluetooth(was_using_bt)
                self.update_ui()

    def _reconcile_bluetooth(self, was_enabled: bool):
        """Bring the BLE controller in line with the dome's light_controller
        after a settings change. Disabling drops the controller entirely — so
        its indicator disappears (update_ui_bluetooth hides the frame when
        bt_controller is None) and its now-False keep_connected flag can't wedge
        a later reconnect. Enabling builds a fresh controller (keep_connected
        True), which reconnects on the spot instead of only after a restart."""
        now_enabled = self.dome.uses_bluetooth
        if now_enabled and not was_enabled and BT_AVAILABLE:
            asyncio.get_running_loop().create_task(self.init_bluetooth())
        elif not now_enabled and self.bt_controller is not None:
            self.bt_controller.bt_disconnect()
            self.bt_controller = None
        self.update_ui_bluetooth()

    def open_advanced_capture_settings(self):
        def open_dialog(cfg: gp.CameraWidget):
            self.cam_config_dialog = CameraConfigDialog(cfg, self.camera_worker, self)
            self.cam_config_dialog.setModal(False)
            self.cam_config_dialog.show()
            self.cam_config_dialog.finished.connect(lambda: setattr(self, "cam_config_dialog", None))

        req = ConfigRequest()
        req.signal.got_config.connect(open_dialog)
        self.camera_worker.commands.get_config.emit(req)

    def set_camera_connection_busy(self, busy: bool = True):
        self.connect_camera_button.setEnabled(not busy)
        self.disconnect_camera_button.setEnabled(not busy)
        self.camera_busy_spinner.isAnimated = busy

    def _apply_camera_filter(self, profile):
        """Restrict autodetect to the profile's camera, so the app never
        latches onto a different body — notably a virtual vusb camera — when
        the real one is momentarily absent. Without this the worker connects
        to the first camera gphoto2 reports (see __find_camera). Mirrors
        papyri's _spawn_worker / _hot_switch_profile. Plain attribute writes,
        safe from the UI thread; __find_camera re-reads them each pass."""
        self.camera_worker.target_model_pattern = profile.gphoto2_model_pattern()
        self.camera_worker.pinned_port = profile.gphoto2_port()

    def connect_camera(self):
        self.camera_worker.commands.connect_camera.emit(self.profile)

    def disconnect_camera(self):
        self.camera_worker.commands.disconnect_camera.emit()

    def reconnect_cammera(self):
        self.camera_worker.commands.reconnect_camera.emit()

    def create_session(self):
        name = self.session_name_edit.text()

        print("Create" + name)
        session = Session(name, QSettings().value("workingDirectory"))
        if Path(session.session_dir).exists():
            result = QMessageBox.warning(self, self.tr("Fehler"),
                                         self.tr("Sitzung %s existiert bereits. Soll sie erneut geöffnet werden?") % name,
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if result == QMessageBox.StandardButton.No:
                return

        self.set_session(session)

    def session_directory_loaded(self, path):
        if not self.session:
            return

        if os.path.normpath(path) == os.path.normpath(self.session.preview_dir):
            self.session.preview_dir_loaded = True
            self.session.preview_count = self.preview_filmstrip.last_index()

        elif os.path.normpath(path) == os.path.normpath(self.session.images_dir):
            self.session.images_dir_loaded = True

        self.update_ui()

        # papyri-style: a freshly opened session with no test shots yet starts
        # live view once so the user can frame. Seeded HERE (session fully
        # loaded, capture controls already re-enabled by update_ui) — never in
        # the camera-ready handler, so turning live view off stays off and the
        # capture button keeps its ready state.
        if (self.session.preview_dir_loaded and self.session.images_dir_loaded
                and self.capture_mode == CaptureMode.Preview
                and self.preview_filmstrip.num_files() == 0
                and isinstance(self.camera_state, CameraStates.Ready)
                and self.profile.supports_live_view()
                and not self.toggle_live_view_button.isChecked()):
            self.toggle_live_view_button.setChecked(True)

    def close_session(self):
        self.preview_filmstrip.close_directory()
        self.rti_filmstrip.close_directory()
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
        new_name, ok = QInputDialog.getText(self, self.tr("Aktuelle Sitzung umbenennen"), self.tr("Neuer Name"), text=self.session.name)
        if ok:
            session_dir = self.session.session_dir
            session_dir_parent = Path(session_dir).parent
            new_session_dir = os.path.join(session_dir_parent, os.path.join(session_dir_parent, new_name))

            if Path(new_session_dir).exists():
                QMessageBox.critical(self, self.tr("Fehler"), self.tr("Sitzung %s existiert bereits.") % new_name)
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
        file_names = [os.path.basename(file_path) for file_path in self.rti_filmstrip.files()]
        num_files = len(file_names)

        lp_template_path = "cceh-dome-template.lp"
        lp_output_path = os.path.join(self.session.images_dir, self.session.name + ".lp")
        with open(lp_template_path, 'r') as lp_template_file, open(lp_output_path, 'w') as lp_output_file:
            logging.info("Writing LP file: " + lp_output_path)
            lp_output_file.write(str(num_files) + "\n")
            for i, input_line in enumerate(lp_template_file):
                output_line = file_names[i] + " " + input_line
                lp_output_file.write(output_line)

    def check_and_write_lp(self, expected_count: int, attempts_remaining: int = 20):
        """
        Checks if the expected number of files are present and writes the LP file.
        Uses QTimer to check periodically without blocking the UI.

        Args:
            expected_count: Number of files we expect to find
            attempts_remaining: Number of remaining check attempts before giving up
        """
        current_count = len(self.rti_filmstrip.files())

        if current_count == expected_count:
            self.write_lp()
            return

        if attempts_remaining <= 0:
            logging.warning(f"Timeout waiting for files. Expected {expected_count}, found {current_count}")
            return

        # Check again in 500ms
        QTimer.singleShot(500, lambda: self.check_and_write_lp(expected_count, attempts_remaining - 1))


    def dump_camera_config(self):
        if not self.session:
            return
        output_path = os.path.join(self.session.images_dir, "camera_config.json")
        self.logger.info(f"Writing camera configuration dump: {output_path}")

        def on_got_config(cfg: gp.CameraWidget):
            # widget_to_dict reads char*-valued widgets NULL-safely — a raw
            # PTP TEXT property (e.g. Sony '/main/other/d2c7') can hold a NULL
            # value that plain CameraWidget.get_value() segfaults on
            # (uncatchable). See camera_worker.widget_to_dict / gphoto2_safe.
            cfg_dict = widget_to_dict(cfg, self.logger)
            try:
                with open(output_path, "w") as output_file:
                    json.dump(cfg_dict, output_file, indent=4)
            except Exception as e:
                self.logger.error(f"Could not write camera config dump to {output_path}:")
                self.logger.exception(e)

        req = ConfigRequest()
        req.signal.got_config.connect(on_got_config)
        self.camera_worker.commands.get_config.emit(req)


    def on_config_update(self, config: ConfigProtocol):
        # ConfigComboBox.update_from_config diff-updates each combo — items
        # rebuild only when the choices change, selection moves only when the
        # popup is closed — so the 0.5s poll no longer disrupts an open
        # dropdown. The user-pick → set_single_config wiring is connected once
        # in __init__ via value_chosen.
        self.iso_select.update_from_config(config, self.profile.iso_property_name())
        self.f_number_select.update_from_config(config, self.profile.f_number_property_name())
        self.shutter_speed_select.update_from_config(config, self.profile.shutterspeed_property_name())
        self.crop_select.update_from_config(config, self.profile.image_format_property_name())

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

    def _on_live_frame(self, image):
        # The live-view toggle is the single source of truth for "live view on".
        # When it's off we're reviewing a static shot (or paused), so drop any
        # in-flight live frames — they would otherwise overwrite the shown shot.
        if not self.toggle_live_view_button.isChecked():
            return
        # .copy() detaches from the PIL-owned bytes buffer — ImageQt wraps it
        # without owning it, and for RGB32 frames QPixmap.fromImage takes a
        # shallow share instead of converting, so the pixmap would dangle once
        # the ImageQt temporary is collected (segfault on a later repaint).
        self.preview_viewer.show_image(
            QPixmap.fromImage(ImageQt(image.image).copy()), fit=True)

    def _on_preview_capture_selected(self, *_):
        """A preview test shot was chosen for review: switch live view off (its
        frames would overwrite the static shot). The toggle's off state is what
        then reveals the zoom controls — no separate 'reviewing' flag needed."""
        if self.toggle_live_view_button.isChecked():
            self.toggle_live_view_button.setChecked(False)  # → enable_live_view(False)
        self._update_zoom_visibility()

    def trigger_autofocus(self):
        self.camera_worker.commands.trigger_autofocus.emit()

    def _capture_strategy(self, *, is_preview: bool):
        """How a capture runs. The RTI *series* follows the dome's strategy
        (Cologne bursts in lockstep with its LEDs; Paris triggers each shot
        externally; otherwise the app fires each shot). A *preview* is always a
        single, app-triggered frame — the dome's series strategy does not apply
        to it — so it is always APP_PER_SHOT. On a burst-capable body the
        non-burst path resets burstnumber to 1, so a preview never inherits the
        series' burst count (see CameraWorker.captureImages)."""
        if is_preview:
            return CaptureImagesRequest.CaptureStrategy.APP_PER_SHOT
        return self.dome.capture_strategy

    def capture_image(self):
        capture_req: CaptureImagesRequest

        # Capture Previews
        if self.capture_mode == CaptureMode.Preview:
            filename_template = self.session.name.replace(" ", "_") + "_test_" + str(
                self.session.preview_count + 1) + "${extension}"
            file_path_template = os.path.join(self.session.preview_dir, filename_template)
            capture_req = CaptureImagesRequest(file_path_template, num_images=1,
                                               capture_strategy=self._capture_strategy(is_preview=True),
                                               image_quality=QSettings().value(
                                                   "previewCaptureFormat",
                                                   CaptureImagesRequest.CaptureFormat.JPEG))

        # Capture RTI Series
        else:
            # A camera-burst dome needs a body that supports the burst (has a
            # burstnumber property). Paired with one that doesn't (e.g. a Sony)
            # it can't work — refuse clearly here instead of crashing in the
            # worker (get_child_by_name(None)) or hanging on a CAPTURE_COMPLETE
            # the body never sends.
            if (self._capture_strategy(is_preview=False) == CaptureImagesRequest.CaptureStrategy.CAMERA_BURST
                    and self.profile.burstnumber_property_name() is None):
                QMessageBox.warning(
                    self, self.tr("Aufnahmemodus passt nicht zur Kamera"),
                    self.tr("Der Dom-Aufnahmemodus ist „Kamera-Burst“, aber diese "
                            "Kamera unterstützt das nicht. Stelle in den "
                            "Einstellungen den Aufnahmemodus auf „Extern "
                            "getriggert“ oder „Einzelbild per App“."))
                return

            if self.rti_filmstrip.num_files() > 0:
                message_box = QMessageBox(QMessageBox.Icon.Warning, self.tr("RTI-Serie aufnehmen"),
                                          self.tr("Vorhandene Aufnahmen werden gelöscht."))
                message_box.addButton(
                    QPushButton(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogCancelButton), self.tr("Abbrechen")),
                    QMessageBox.ButtonRole.NoRole)
                message_box.addButton(
                    QPushButton(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogOkButton), self.tr("Fortfahren")),
                    QMessageBox.ButtonRole.YesRole)
                if not message_box.exec():
                    return

            existing_files = [os.path.join(self.session.images_dir, f) for f in os.listdir(self.session.images_dir)]
            send2trash(existing_files)

            filename_template = self.session.name.replace(" ", "_") + "_${num}${extension}"
            file_path_template = os.path.join(self.session.images_dir, filename_template)
            capture_req = CaptureImagesRequest(file_path_template, num_images=self.dome.num_positions,
                                               capture_strategy=self._capture_strategy(is_preview=False),
                                               max_burst=self.dome.max_burst,
                                               image_quality=QSettings().value(
                                                   "rtiCaptureFormat",
                                                   CaptureImagesRequest.CaptureFormat.JPEG_AND_RAW))
            self.capture_progress_bar.setMaximum(self.dome.num_positions)
            self.capture_progress_bar.setValue(0)

        def on_file_received(path: str):
            print("Rec: " + path)
        capture_req.signal.file_received.connect(on_file_received)

        def start_capture(show_button_message: bool):
            if self.capture_mode == CaptureMode.RTI and show_button_message:
                press_buttons_dialog = QDialog()
                loadUi(get_ui_path("ui/press-buttons-dialog.ui"), press_buttons_dialog)
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

    # Logging (incl. faulthandler crash reporting) is installed at the top
    # of this module via byzanz_camera.logging_setup.install().

    app = QApplication(sys.argv)
    app.setOrganizationName("CCeH")
    app.setOrganizationDomain("cceh.uni-koeln.de")
    app.setApplicationName("Byzanz RTI")

    logging.info("Using libgphoto2: " + ", ".join(gp.gp_library_version(gp.GP_VERSION_SHORT)))

    # Source strings are German and the only shipped translation is English,
    # so: German UI on German systems, English everywhere else.
    translator = QTranslator()
    if QLocale.system().language() != QLocale.Language.German:
        if translator.load("byzanz_capture_en", get_ui_path("i18n")):
            app.installTranslator(translator)
        else:
            logging.warning("Could not load English translation, falling back to German source strings")

    settings = QSettings()

    # Bring settings up to the current schema (v1 unbundles the old combined
    # "profile" into cameraProfile + dome/*, retires maxBurstNumber /
    # enableBluetooth) before reading or seeding anything below.
    migrate_settings(settings)

    if "workingDirectory" not in settings.allKeys():
        settings.setValue("workingDirectory",
                          QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation))

    if "maxPixmapCache" not in settings.allKeys():
        settings.setValue("maxPixmapCache", 1024)

    # Fresh-install defaults: the historical default rig is the Cologne Nikon
    # (camera + burst/BLE dome). Existing installs get these from migration.
    if "cameraProfile" not in settings.allKeys():
        settings.setValue("cameraProfile", "NikonD800E")
    if dome_config.CAPTURE_STRATEGY not in settings.allKeys():
        dome_config.apply_preset(settings, dome_config.load_presets()["cologne"])

    if "previewCaptureFormat" not in settings.allKeys():
        settings.setValue("previewCaptureFormat", CaptureImagesRequest.CaptureFormat.JPEG)

    if "rtiCaptureFormat" not in settings.allKeys():
        settings.setValue("rtiCaptureFormat", CaptureImagesRequest.CaptureFormat.JPEG_AND_RAW)

    if "enableSecondScreenMirror" not in settings.allKeys():
        settings.setValue("enableSecondScreenMirror", True)

    QPixmapCache.setCacheLimit(int(settings.value("maxPixmapCache")) * 1024)

    # Initialize qasync loop
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    win = RTICaptureMainWindow()
    win.show()

    # BYZANZ_AUTO_OPEN_SESSION=<absolute_session_dir> opens the named
    # session 500ms after the window appears — used for unattended UI
    # debugging where the filmstrip needs real captures to render.
    auto_open = os.environ.get("BYZANZ_AUTO_OPEN_SESSION")
    if auto_open:
        from PyQt6.QtCore import QTimer
        _session_name = os.path.basename(os.path.normpath(auto_open))
        _working_dir = os.path.dirname(os.path.normpath(auto_open))
        QTimer.singleShot(500, lambda: win.set_session(Session(_session_name, _working_dir)))

    # BYZANZ_SMOKE_TEST=1 lets the app fully initialise (all imports, the Qt
    # platform plugin, the bundled ui/ + dome_presets/ data, the camera-worker
    # thread + first detect pass) and then quits cleanly after a few seconds.
    # CI runs this against the frozen bundle to catch an incomplete PyInstaller
    # inclusion: a missing module / plugin / data file crashes at startup with a
    # non-zero exit here, instead of only in front of a user.
    if os.environ.get("BYZANZ_SMOKE_TEST"):
        from PyQt6.QtCore import QTimer
        logging.info("Smoke-test mode: quitting ~5s after startup.")
        QTimer.singleShot(5000, app.quit)

    def excepthook(exc_type, exc_value, exc_traceback):
        logging.exception(msg="Exception", exc_info=(exc_type, exc_value, exc_traceback))
        if win.bt_controller and win.bt_controller.state != BtControllerState.DISCONNECTED:
            win.bt_controller.bt_disconnect()

    sys.excepthook = excepthook

    # Create a coroutine for bluetooth initialization
    async def initialize_bluetooth():
        if BT_AVAILABLE and win.dome.uses_bluetooth:
            try:
                await win.init_bluetooth()
            except Exception as e:
                logging.exception("Failed to initialize bluetooth")
        else:
            logging.info("Bluetooth not available. Is bleak installed?")

    # Run the initialization in the background
    def signal_handler(signum, frame):
        logging.info("Received signal to quit")
        # Use Qt's native close mechanism
        win.close()
        app.quit()

        # Set up signal handlers


    import signal

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


    if BT_AVAILABLE and win.dome.uses_bluetooth:
        asyncio.ensure_future(initialize_bluetooth(), loop=loop)

    # Run the event loop
    try:
        with loop:
            loop.run_forever()
    except Exception as e:
        logging.exception("Error in main event loop")
    finally:
        if win.bt_controller and win.bt_controller.state != BtControllerState.DISCONNECTED:
            win.bt_controller.bt_disconnect()

# if __name__ == "__main__":
#     win: RTICaptureMainWindow
#
#     logging.basicConfig(
#         format='%(levelname)s: %(name)s: %(message)s', level=logging.DEBUG)
#
#     app = QApplication(sys.argv)
#     app.setOrganizationName("CCeH")
#     app.setOrganizationDomain("cceh.uni-koeln.de")
#     app.setApplicationName("Byzanz RTI")
#
#     translator = QTranslator()
#     locale = "de" # QLocale.system().name()
#
#     if translator.load(f"byzanz_capture_{locale}", "i18n"):
#         app.installTranslator(translator)
#
#     settings = QSettings()
#     if "workingDirectory" not in settings.allKeys():
#         settings.setValue("workingDirectory",
#                           QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation))
#
#     if "maxPixmapCache" not in settings.allKeys():
#         settings.setValue("maxPixmapCache", 1024)
#
#     if "maxBurstNumber" not in settings.allKeys():
#         settings.setValue("maxBurstNumber", 60)
#
#     if "enableBluetooth" not in settings.allKeys():
#         settings.setValue("enableBluetooth", True)
#
#     if "enableSecondScreenMirror" not in settings.allKeys():
#         settings.setValue("enableSecondScreenMirror", True)
#
#     QPixmapCache.setCacheLimit(int(settings.value("maxPixmapCache")) * 1024)
#
#     loop: qasync.QEventLoop = qasync.QEventLoop(app)
#     asyncio.set_event_loop(loop)
#
#     app_close_event = asyncio.Event()
#     app.aboutToQuit.connect(app_close_event.set)
#
#     orig_exceptionhook = sys.__excepthook__
#
#     win = RTICaptureMainWindow()
#     win.show()
#
#     def excepthook(exc_type, exc_value, exc_traceback):
#         logging.exception(msg="Exception", exc_info=(exc_type, exc_value, exc_traceback))
#         if win.bt_controller and win.bt_controller.state != BtControllerState.DISCONNECTED:
#             win.bt_controller.bt_disconnect()
#
#         loop.call_soon(lambda _loop: _loop.stop(), loop)
#
#
#     # sys.excepthook = excepthook
#     # threading.excepthook = excepthook
#
#
#     with loop:
#         if BT_AVAILABLE and QSettings().value("enableBluetooth", type=bool):
#             loop.create_task(win.init_bluetooth())
#         else:
#             logging.info("Bluetooth not available. Is bleak installed?")
#
#         loop.run_until_complete(app_close_event.wait())
#
#         # asyncio.get_running_loop().run_forever()


