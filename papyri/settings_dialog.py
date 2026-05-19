"""Papyri settings dialog. Loads `papyri/ui/settings_dialog.ui`.

Adds an IR camera profile slot (`irProfile` key) on top of the visible-camera
profile. Drops the Bluetooth and max-burst fields the byzanz dialog has —
neither applies to the papyri workflow.

API mirrors the byzanz dialog: `exec()` returns truthy on save,
`self.settings` holds the dict of changed values for the caller to apply.
"""
from __future__ import annotations
from typing import Any

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QLineEdit, QSpinBox, QToolButton,
)
from PyQt6.uic import loadUi

from byzanz_camera.helpers import get_ui_path
from byzanz_camera.profiles.base import Profile


_NO_IR_LABEL = "(none — IR disabled)"


class PapyriSettingsDialog(QDialog):
    def __init__(
        self,
        q_settings: QSettings,
        profiles: dict[str, Profile],
        parent=None,
    ):
        super().__init__(parent)
        loadUi(get_ui_path("papyri/ui/settings_dialog.ui"), self)

        self._q_settings = q_settings
        self._profiles = profiles
        # Caller reads this after exec() returns truthy and applies to QSettings.
        self.settings: dict[str, Any] = {}

        self._bind_widgets()
        self._populate_profiles()
        self._wire_actions()
        self._load_current()

    # ---- setup ---------------------------------------------------------

    def _bind_widgets(self) -> None:
        self.workdir_input: QLineEdit = self.findChild(
            QLineEdit, "workingDirectoryInput"
        )
        self.visible_profile_combo: QComboBox = self.findChild(
            QComboBox, "visibleProfileSelect"
        )
        self.ir_profile_combo: QComboBox = self.findChild(
            QComboBox, "irProfileSelect"
        )
        self.pixmap_cache_input: QSpinBox = self.findChild(
            QSpinBox, "maxPixmapCacheInput"
        )
        self.second_screen_checkbox: QCheckBox = self.findChild(
            QCheckBox, "enableSecondScreenMirrorCheckbox"
        )
        self.sharpness_check_checkbox: QCheckBox = self.findChild(
            QCheckBox, "enableSharpnessCheckCheckbox"
        )

    def _populate_profiles(self) -> None:
        # Visible: required, all profiles available.
        for profile_id, profile in self._profiles.items():
            self.visible_profile_combo.addItem(profile.name(), profile_id)
        # IR: optional, "none" first.
        self.ir_profile_combo.addItem(_NO_IR_LABEL, None)
        for profile_id, profile in self._profiles.items():
            self.ir_profile_combo.addItem(profile.name(), profile_id)

    def _wire_actions(self) -> None:
        # Folder picker as a trailing action on the workdir field
        choose = QAction("Choose…", self)
        choose.setIcon(QIcon(get_ui_path("ui/folder-open.svg")))
        choose.triggered.connect(self._choose_working_directory)
        self.workdir_input.addAction(
            choose, QLineEdit.ActionPosition.TrailingPosition
        )
        for tool_button in self.workdir_input.findChildren(QToolButton):
            tool_button.setCursor(Qt.CursorShape.PointingHandCursor)

        # Each widget records its change into self.settings; caller applies on accept.
        self.workdir_input.textChanged.connect(
            lambda t: self._set("workingDirectory", t)
        )
        self.visible_profile_combo.currentIndexChanged.connect(
            lambda idx: self._set("profile", self.visible_profile_combo.itemData(idx))
        )
        self.ir_profile_combo.currentIndexChanged.connect(
            lambda idx: self._set("irProfile", self.ir_profile_combo.itemData(idx))
        )
        self.pixmap_cache_input.valueChanged.connect(
            lambda v: self._set("maxPixmapCache", v)
        )
        self.second_screen_checkbox.stateChanged.connect(
            lambda: self._set(
                "enableSecondScreenMirror", self.second_screen_checkbox.isChecked()
            )
        )
        self.sharpness_check_checkbox.stateChanged.connect(
            lambda: self._set(
                "sharpnessCheckEnabled", self.sharpness_check_checkbox.isChecked()
            )
        )

    def _load_current(self) -> None:
        self.workdir_input.setText(self._q_settings.value("workingDirectory", ""))

        self._select_profile(
            self.visible_profile_combo, self._q_settings.value("profile")
        )
        self._select_profile(
            self.ir_profile_combo, self._q_settings.value("irProfile")
        )

        self.pixmap_cache_input.setValue(
            int(self._q_settings.value("maxPixmapCache", 256))
        )
        self.second_screen_checkbox.setChecked(
            self._q_settings.value("enableSecondScreenMirror", False, type=bool)
        )
        self.sharpness_check_checkbox.setChecked(
            self._q_settings.value("sharpnessCheckEnabled", True, type=bool)
        )

    @staticmethod
    def _select_profile(combo: QComboBox, profile_id: str | None) -> None:
        if profile_id is None:
            combo.setCurrentIndex(0)
            return
        idx = combo.findData(profile_id)
        combo.setCurrentIndex(idx if idx >= 0 else 0)

    # ---- handlers ------------------------------------------------------

    def _set(self, name: str, value: Any) -> None:
        self.settings[name] = value

    def _choose_working_directory(self) -> None:
        chooser = QFileDialog(
            self,
            "Choose working directory",
            self.workdir_input.text(),
        )
        chooser.setFileMode(QFileDialog.FileMode.Directory)
        if chooser.exec() and chooser.selectedFiles():
            self.workdir_input.setText(chooser.selectedFiles()[0])
