"""Papyri settings dialog. Loads `papyri/ui/settings_dialog.ui`.

Adds an IR camera profile slot (`irProfile` key) on top of the visible-camera
profile. Drops the Bluetooth and max-burst fields the byzanz dialog has —
neither applies to the papyri workflow.

API mirrors the byzanz dialog: `exec()` returns truthy on save,
`self.settings` holds the dict of changed values for the caller to apply.
"""
from __future__ import annotations
from typing import Any

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QLineEdit, QSpinBox,
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
        self.calibration_trigger_combo: QComboBox = self.findChild(
            QComboBox, "calibrationTriggerSelect"
        )
        self.calibration_interval_input: QSpinBox = self.findChild(
            QSpinBox, "calibrationIntervalInput"
        )
        self.capture_heights_input: QLineEdit = self.findChild(
            QLineEdit, "captureHeightsInput"
        )
        self.ir_capture_height_input: QLineEdit = self.findChild(
            QLineEdit, "irCaptureHeightInput"
        )
        # rotated-sample nudge
        self.rotated_sample_nudge_enabled_checkbox: QCheckBox = self.findChild(
            QCheckBox, "rotatedSampleNudgeEnabledCheckbox"
        )
        self.rotated_sample_nudge_interval_input: QSpinBox = self.findChild(
            QSpinBox, "rotatedSampleNudgeIntervalInput"
        )

    def _populate_profiles(self) -> None:
        # Visible: required, all profiles available.
        for profile_id, profile in self._profiles.items():
            self.visible_profile_combo.addItem(profile.name(), profile_id)
        # IR: optional, "none" first.
        self.ir_profile_combo.addItem(_NO_IR_LABEL, None)
        for profile_id, profile in self._profiles.items():
            self.ir_profile_combo.addItem(profile.name(), profile_id)

        # Calibration-reminder trigger (label, stored value).
        for label, value in (
            ("Off", "off"),
            ("Time-based", "time"),
            ("At session start", "session"),
        ):
            self.calibration_trigger_combo.addItem(label, value)

    def _wire_actions(self) -> None:
        # The box (working directory) is chosen in the sidebar's box menu, not
        # here — Settings holds only rig/app config now.
        # Each widget records its change into self.settings; caller applies on accept.
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
        self.calibration_trigger_combo.currentIndexChanged.connect(
            self._on_calibration_trigger_changed
        )
        self.calibration_interval_input.valueChanged.connect(
            lambda v: self._set("calibrationIntervalMinutes", v)
        )
        self.capture_heights_input.textChanged.connect(
            lambda t: self._set("captureHeightChoices", t)
        )
        self.ir_capture_height_input.textChanged.connect(
            lambda t: self._set("irCaptureHeight", t.strip())
        )
        # rotated-sample nudge
        self.rotated_sample_nudge_enabled_checkbox.stateChanged.connect(
            lambda: self._set(
                "rotatedSampleNudge/enabled",
                self.rotated_sample_nudge_enabled_checkbox.isChecked(),
            )
        )
        self.rotated_sample_nudge_interval_input.valueChanged.connect(
            lambda v: self._set("rotatedSampleNudge/interval", v)
        )

    def _on_calibration_trigger_changed(self, idx: int) -> None:
        trigger = self.calibration_trigger_combo.itemData(idx)
        self._set("calibrationTrigger", trigger)
        # The interval only applies to the time-based trigger.
        self.calibration_interval_input.setEnabled(trigger == "time")

    def _load_current(self) -> None:
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

        trigger = self._q_settings.value("calibrationTrigger", "time") or "time"
        t_idx = self.calibration_trigger_combo.findData(trigger)
        self.calibration_trigger_combo.setCurrentIndex(t_idx if t_idx >= 0 else 0)
        self.calibration_interval_input.setValue(
            int(self._q_settings.value("calibrationIntervalMinutes", 60))
        )
        # findData/setCurrentIndex above won't fire the changed-handler when
        # the value already maps to index 0, so set the dependent enable here.
        self.calibration_interval_input.setEnabled(trigger == "time")

        self.capture_heights_input.setText(
            self._q_settings.value("captureHeightChoices", "30,45,60,75,90")
        )
        self.ir_capture_height_input.setText(
            str(self._q_settings.value("irCaptureHeight", "45"))
        )
        # rotated-sample nudge
        self.rotated_sample_nudge_enabled_checkbox.setChecked(
            self._q_settings.value("rotatedSampleNudge/enabled", True, type=bool)
        )
        self.rotated_sample_nudge_interval_input.setValue(
            int(self._q_settings.value("rotatedSampleNudge/interval", 20))
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

