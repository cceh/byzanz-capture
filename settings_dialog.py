from typing import Any

from PyQt6.QtCore import QSettings, QVariant, Qt
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import QDialog, QLineEdit, QFileDialog, QToolButton, QSpinBox, QCheckBox, QComboBox
from PyQt6.uic import loadUi

from helpers import get_ui_path
from profiles.base import Profile


class SettingsDialog(QDialog):

    def __init__(self, q_settings: QSettings, profiles: dict[str, Profile], parent=None):
        super(SettingsDialog, self).__init__(parent)
        self.__q_settings = q_settings
        self.settings: dict[str, Any] = dict()

        loadUi(get_ui_path('ui/settings_dialog.ui'), self)

        self.profile_select: QComboBox = self.findChild(QComboBox, "profileSelect")
        for profile_id, profile in profiles.items():
            self.profile_select.addItem(profile.name(), profile_id)
        current_profile_id = q_settings.value("profile")
        if current_profile_id:
            index = self.profile_select.findData(current_profile_id)
            if index >= 0:
                self.profile_select.setCurrentIndex(index)
        self.profile_select.currentIndexChanged.connect(
            lambda index: self.set("profile", self.profile_select.itemData(index))
        )

        self.working_directory_input: QLineEdit = self.findChild(QLineEdit, "workingDirectoryInput")
        self.working_directory_input.setText(q_settings.value("workingDirectory"))
        self.working_directory_input.textChanged.connect(
            lambda text: self.set("workingDirectory", text)
        )

        open_action = QAction(self.tr("Arbeitsverzeichnis wählen"), self)
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
                                  self.tr("Arbeitsverzeichnis wählen"),
                                  self.working_directory_input.text())
        file_dialog.setFileMode(QFileDialog.FileMode.Directory)
        if file_dialog.exec():
            print(file_dialog.selectedFiles())

        if file_dialog.selectedFiles():
            self.working_directory_input.setText(file_dialog.selectedFiles()[0])
