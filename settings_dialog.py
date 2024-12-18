from typing import Any

from PyQt6.QtCore import QSettings, QVariant, Qt
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import QDialog, QLineEdit, QFileDialog, QToolButton, QSpinBox, QCheckBox
from PyQt6.uic import loadUi

from helpers import get_ui_path

class SettingsDialog(QDialog):

    def __init__(self, q_settings: QSettings, parent=None):
        super(SettingsDialog, self).__init__(parent)
        self.__q_settings = q_settings
        self.settings: dict[str, Any] = dict()

        loadUi(get_ui_path('ui/settings_dialog.ui'), self)

        self.working_directory_input: QLineEdit = self.findChild(QLineEdit, "workingDirectoryInput")
        self.working_directory_input.setText(q_settings.value("workingDirectory"))
        self.working_directory_input.textChanged.connect(
            lambda text: self.set("workingDirectory", text)
        )

        open_action = QAction("Arbeitsverzeichnis wählen", self)
        open_action.setIcon(QIcon(get_ui_path("ui/folder-open.svg")))
        open_action.triggered.connect(self.choose_working_directory)

        self.working_directory_input.addAction(open_action, QLineEdit.ActionPosition.TrailingPosition)
        tool_button: QToolButton
        for tool_button in self.working_directory_input.findChildren(QToolButton):
            tool_button.setCursor(Qt.CursorShape.PointingHandCursor)

        self.max_pixmap_cache_input: QSpinBox = self.findChild(QSpinBox, "maxPixmapCacheInput")
        self.max_pixmap_cache_input.setValue(int(q_settings.value("maxPixmapCache")))
        self.max_pixmap_cache_input.textChanged.connect(
            lambda text: self.set("maxPixmapCache", int(text))
        )

        self.max_burst_number_input: QSpinBox = self.findChild(QSpinBox, "maxBurstNumberInput")
        self.max_burst_number_input.setValue(int(q_settings.value("maxBurstNumber")))
        self.max_burst_number_input.textChanged.connect(
            lambda text: self.set("maxBurstNumber", int(text))
        )

        self.enable_bluetooth_checkbox: QCheckBox = self.findChild(QCheckBox, "enableBluetoothCheckbox")
        self.enable_bluetooth_checkbox.setChecked(q_settings.value("enableBluetooth", type=bool))
        self.enable_bluetooth_checkbox.stateChanged.connect(
            lambda: self.set("enableBluetooth", self.enable_bluetooth_checkbox.isChecked())
        )

        self.enable_second_screen_mirror_checkbox: QCheckBox = self.findChild(QCheckBox, "enableSecondScreenMirrorCheckbox")
        self.enable_second_screen_mirror_checkbox.setChecked(q_settings.value("enableSecondScreenMirror", type=bool))
        self.enable_second_screen_mirror_checkbox.stateChanged.connect(
            lambda: self.set("enableSecondScreenMirror", self.enable_second_screen_mirror_checkbox.isChecked())
        )

    def set(self, name: str, value: QVariant):
        self.settings[name] = value

    def choose_working_directory(self):
        file_dialog = QFileDialog(self,
                                  "Arbeitsverzeichnis wählen",
                                  self.working_directory_input.text())
        file_dialog.setFileMode(QFileDialog.FileMode.Directory)
        if file_dialog.exec():
            print(file_dialog.selectedFiles())

        if file_dialog.selectedFiles():
            self.working_directory_input.setText(file_dialog.selectedFiles()[0])
