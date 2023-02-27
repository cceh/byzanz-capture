import logging
import os
import sys
from enum import Enum
from pathlib import Path

import gphoto2 as gp
from PIL.ImageQt import ImageQt
from PyQt6.QtCore import QThread, QSettings, QStandardPaths, pyqtSignal
from PyQt6.QtGui import QPixmap, QAction, QPixmapCache, QIcon
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QPushButton, QWidget, QFrame, QLineEdit,
    QComboBox, QLabel, QToolBox, QProgressBar, QMenu, QAbstractButton, QInputDialog, QMessageBox, QStyle, QDialog
)
from PyQt6.uic import loadUi
from send2trash import send2trash

from camera_worker import CameraWorker, CaptureImagesRequest, CameraStates
from open_session_dialog import OpenSessionDialog
from photo_browser import PhotoBrowser
from settings_dialog import SettingsDialog
from spinner import Spinner


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

        self.__logger = logging.getLogger(self.__class__.__name__)

        self.camera_worker = CameraWorker()
        self.__session: Session = None
        self.camera_state: CameraStates.StateType = None

        # Set up UI and find controls
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
        self.session_loading_spinner: Spinner = self.findChild(QWidget, "sessionLoadingSpinner")
        self.session_menu_button: QAbstractButton = self.findChild(QWidget, "sessionMenuButton")

        self.live_view_controls: QWidget = self.findChild(QWidget, "liveViewControls")
        self.toggle_live_view_button: QPushButton = self.findChild(QPushButton, "toggleLiveViewButton")
        self.autofocus_button: QPushButton = self.findChild(QPushButton, "autofocusButton")

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

        self.session_menu = QMenu(self)
        self.open_session_action = QAction('Vorherige Sitzung öffnen...', self)
        self.open_session_action.triggered.connect(self.open_existing_session_directory)
        self.rename_session_action = QAction('Sitzung umbenennen...', self)
        self.rename_session_action.triggered.connect(self.rename_current_session)
        self.session_menu.addActions([self.open_session_action, self.rename_session_action])

        self.session_name_edit.textChanged.connect(
            lambda text: self.start_session_button.setEnabled(
                True if len(text) > 0 else False
            ))

        self.cancel_capture_button.setVisible(False)

        self.set_camera_connection_busy(True)
        self.capture_mode = CaptureMode.Preview
        self.set_session(None)

        self.camera_thread = QThread()
        self.camera_worker.moveToThread(self.camera_thread)
        self.camera_worker.state_changed.connect(self.set_camera_state)
        self.camera_worker.events.config_updated.connect(self.on_config_update)
        self.camera_worker.preview_image.connect(lambda image: self.previewImageBrowser.show_preview(ImageQt(image.image)))
        self.camera_worker.initialized.connect(lambda: self.camera_worker.commands.find_camera.emit())
        self.camera_thread.started.connect(self.camera_worker.initialize)
        self.camera_thread.start()



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
        self.__logger.debug("Handle camera state:" + state.__class__.__name__)
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

            case CameraStates.ConnectionError():
                self.__logger.error(state.error)

            case CameraStates.Ready():
                pass

            case CameraStates.LiveViewStarted():
                pass

            case CameraStates.LiveViewStopped():
                pass

            case CameraStates.Disconnecting():
                pass

            case CameraStates.CaptureInProgress():
                pass

            case CameraStates.CaptureFinished():
                if self.capture_mode == CaptureMode.Preview:
                    self.session.preview_count += 1

                self.write_lp()

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

                self.connect_camera_button.setEnabled(False)
                self.disconnect_camera_button.setVisible(False)
                self.camera_busy_spinner.isAnimated = True
                self.capture_status_label.setText("")

                self.live_view_controls.setEnabled(False)

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

                self.live_view_controls.setEnabled(False)

                self.camera_controls.setEnabled(False)
                self.camera_config_controls.setEnabled(False)
                self.capture_button.setText("Nicht verbunden")

            case CameraStates.LiveViewStarted():
                self.camera_config_controls.setEnabled(False)
                self.autofocus_button.setEnabled(True)

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
                # print(camera_state.num_captured, " / ", camera_state.capture_request.num_images)

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
        print(self.capture_mode)
        if self.capture_mode == CaptureMode.RTI and self.camera_state == CameraStates.LiveViewStarted:
            self.camera_worker.commands.live_view.emit(False)
        self.update_ui()

    def open_settings(self):
        q_settings = QSettings()
        dialog = SettingsDialog(q_settings, self)
        dialog.setModal(True)
        if dialog.exec():
            for name, value in dialog.settings.items():
                q_settings.setValue(name, value)
                QPixmapCache.setCacheLimit(int(settings.value("maxPixmapCache")) * 1024)

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
        self.write_lp()
        self.session_menu.exec(self.session_menu_button.mapToGlobal(self.session_menu_button.rect().bottomLeft()))

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

        combo_box.currentIndexChanged.connect(lambda: self.camera_worker.commands.set_config.emit(
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

            press_buttons_dialog = QDialog()
            loadUi("ui/press-buttons-dialog.ui", press_buttons_dialog)
            if not press_buttons_dialog.exec():
                return

            existing_files = [os.path.join(self.session.images_dir, f) for f in os.listdir(self.session.images_dir)]
            send2trash(existing_files)

            filename_template = self.session.name.replace(" ", "_") + "_${num}${extension}"
            file_path_template = os.path.join(self.session.images_dir, filename_template)
            capture_req = CaptureImagesRequest(file_path_template, num_images=60, expect_files=2,
                                               max_burst=int(QSettings().value("maxBurstNumber")), skip=0, image_quality="NEF+Fine")
            self.capture_progress_bar.setMaximum(119)
            self.capture_progress_bar.setValue(0)

        self.camera_worker.commands.capture_images.emit(capture_req)

    def on_capture_cancelled(self):
        logging.info("Capture cancelled")

    def cancel_capture(self):
        self.cancel_capture_button.setEnabled(False)
        self.camera_worker.commands.cancel.emit()

if __name__ == "__main__":
    logging.basicConfig(
        format='%(levelname)s: %(name)s: %(message)s', level=logging.DEBUG)

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

    QPixmapCache.setCacheLimit(int(settings.value("maxPixmapCache")) * 1024)

    win = RTICaptureMainWindow()
    win.show()
    sys.exit(app.exec())
