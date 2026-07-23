import os
from typing import Any

from PyQt6.QtCore import QSettings, QVariant, Qt
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import QDialog, QLineEdit, QFileDialog, QToolButton, QSpinBox, QCheckBox, QComboBox
from PyQt6.uic import loadUi

from byzanz_camera import dome_config
from byzanz_camera.camera_worker import CaptureImagesRequest
from byzanz_camera.helpers import get_ui_path, themed_icon
from byzanz_camera.profiles.base import Profile


class SettingsDialog(QDialog):

    def __init__(self, q_settings: QSettings, profiles: dict[str, Profile], parent=None):
        super(SettingsDialog, self).__init__(parent)
        self.__q_settings = q_settings
        self.settings: dict[str, Any] = dict()

        loadUi(get_ui_path('ui/settings_dialog.ui'), self)

        self.profile_select: QComboBox = self.findChild(QComboBox, "profileSelect")
        for profile_id, profile in profiles.items():
            self.profile_select.addItem(profile.name(), profile_id)
        current_profile_id = q_settings.value("cameraProfile")
        if current_profile_id:
            index = self.profile_select.findData(current_profile_id)
            if index >= 0:
                self.profile_select.setCurrentIndex(index)
        self.profile_select.currentIndexChanged.connect(
            lambda index: self.set("cameraProfile", self.profile_select.itemData(index))
        )

        self.working_directory_input: QLineEdit = self.findChild(QLineEdit, "workingDirectoryInput")
        self.working_directory_input.setText(q_settings.value("workingDirectory"))
        self.working_directory_input.textChanged.connect(
            lambda text: self.set("workingDirectory", text)
        )

        open_action = QAction(self.tr("Arbeitsverzeichnis wählen"), self)
        # themed_icon (one-shot, no registration): the dialog is short-lived,
        # rebuilt on every open — set_themed_icon would keep it alive forever.
        open_action.setIcon(themed_icon(get_ui_path("ui/folder-open.svg")))
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

        capture_format_options = (
            (self.tr("JPEG + RAW"), CaptureImagesRequest.CaptureFormat.JPEG_AND_RAW),
            (self.tr("Nur JPEG"), CaptureImagesRequest.CaptureFormat.JPEG),
            (self.tr("Nur RAW"), CaptureImagesRequest.CaptureFormat.RAW),
        )
        for combo_name, settings_key in (("previewFormatSelect", "previewCaptureFormat"),
                                         ("captureFormatSelect", "rtiCaptureFormat")):
            combo: QComboBox = self.findChild(QComboBox, combo_name)
            for label, value in capture_format_options:
                combo.addItem(label, value)
            index = combo.findData(q_settings.value(settings_key))
            if index >= 0:
                combo.setCurrentIndex(index)
            combo.currentIndexChanged.connect(
                lambda i, c=combo, key=settings_key: self.set(key, c.itemData(i))
            )

        # LP template: empty = the bundled default (placeholder text says so),
        # a path = a user-chosen .lp file. Resolution happens at capture time
        # via RTICaptureMainWindow.resolved_lp_template_path.
        self.lp_template_path_input: QLineEdit = self.findChild(QLineEdit, "lpTemplatePathInput")
        self.lp_template_path_input.setText(q_settings.value("lpTemplatePath", ""))
        self.lp_template_path_input.textChanged.connect(
            lambda text: self.set("lpTemplatePath", text)
        )

        choose_lp_action = QAction(self.tr("LP-Datei wählen"), self)
        choose_lp_action.setIcon(themed_icon(get_ui_path("ui/folder-open.svg")))
        choose_lp_action.triggered.connect(self.choose_lp_template_file)
        reset_lp_action = QAction(self.tr("Standard verwenden (mitgelieferte Vorlage)"), self)
        reset_lp_action.setIcon(themed_icon(get_ui_path("ui/cancel.svg")))
        reset_lp_action.triggered.connect(lambda: self.lp_template_path_input.setText(""))
        self.lp_template_path_input.addAction(choose_lp_action, QLineEdit.ActionPosition.TrailingPosition)
        self.lp_template_path_input.addAction(reset_lp_action, QLineEdit.ActionPosition.TrailingPosition)
        for tool_button in self.lp_template_path_input.findChildren(QToolButton):
            tool_button.setCursor(Qt.CursorShape.PointingHandCursor)

        # Main-window camera controls: per-control visibility + how exposure
        # times are labeled (raw camera value vs. parsed decimal number).
        for checkbox_name, settings_key in (("showFormatCheckbox", "showFormatControl"),
                                            ("showIsoCheckbox", "showIsoControl"),
                                            ("showExposureTimeCheckbox", "showExposureTimeControl"),
                                            ("showApertureCheckbox", "showApertureControl")):
            checkbox: QCheckBox = self.findChild(QCheckBox, checkbox_name)
            checkbox.setChecked(q_settings.value(settings_key, True, type=bool))
            checkbox.toggled.connect(
                lambda checked, key=settings_key: self.set(key, checked)
            )

        self.exposure_time_display_select: QComboBox = self.findChild(QComboBox, "exposureTimeDisplaySelect")
        for label, value in ((self.tr("Wie von Kamera gemeldet"), "camera"),
                             (self.tr("Dezimalzahl"), "decimal")):
            self.exposure_time_display_select.addItem(label, value)
        self._select_combo_data(self.exposure_time_display_select,
                                q_settings.value("exposureTimeDisplayMode", "camera"))
        self.exposure_time_display_select.currentIndexChanged.connect(
            lambda i: self.set("exposureTimeDisplayMode", self.exposure_time_display_select.itemData(i))
        )

        self.enable_second_screen_mirror_checkbox: QCheckBox = self.findChild(QCheckBox, "enableSecondScreenMirrorCheckbox")
        self.enable_second_screen_mirror_checkbox.setChecked(q_settings.value("enableSecondScreenMirror", type=bool))
        self.enable_second_screen_mirror_checkbox.stateChanged.connect(
            lambda: self.set("enableSecondScreenMirror", self.enable_second_screen_mirror_checkbox.isChecked())
        )

        self._init_dome_group(q_settings)

    def _init_dome_group(self, q_settings: QSettings):
        """Camera and dome are independent. This group edits the dome/* config
        directly; a preset only *loads* values into the fields (they stay
        editable afterwards — there is no stored "active preset")."""
        dome = dome_config.current_dome(q_settings)

        self.dome_num_positions_input: QSpinBox = self.findChild(QSpinBox, "domeNumPositionsInput")
        self.dome_max_burst_input: QSpinBox = self.findChild(QSpinBox, "domeMaxBurstInput")
        self.dome_strategy_select: QComboBox = self.findChild(QComboBox, "domeStrategySelect")
        self.dome_light_select: QComboBox = self.findChild(QComboBox, "domeLightSelect")
        self.dome_preset_select: QComboBox = self.findChild(QComboBox, "domePresetSelect")
        self.dome_show_instructions_checkbox: QCheckBox = self.findChild(
            QCheckBox, "domeShowInstructionsCheckbox")

        S = CaptureImagesRequest.CaptureStrategy
        for label, value in ((self.tr("Kamera-Burst"), S.CAMERA_BURST.value),
                             (self.tr("Extern getriggert"), S.EXTERNAL_PER_SHOT.value),
                             (self.tr("Einzelbild per App"), S.APP_PER_SHOT.value)):
            self.dome_strategy_select.addItem(label, value)

        for label, value in ((self.tr("CCeH-Controller (Bluetooth)"), dome_config.LIGHT_CCEH_BLE),
                             (self.tr("Keine / autonom"), dome_config.LIGHT_NONE)):
            self.dome_light_select.addItem(label, value)

        # Seed the fields from the current config *before* connecting handlers,
        # so only real user edits land in self.settings.
        self.dome_num_positions_input.setValue(dome.num_positions)
        self.dome_max_burst_input.setValue(dome.max_burst)
        self._select_combo_data(self.dome_strategy_select, dome.capture_strategy.value)
        self._select_combo_data(self.dome_light_select, dome.light_controller)
        self.dome_show_instructions_checkbox.setChecked(dome.show_capture_instructions)

        self.dome_num_positions_input.valueChanged.connect(
            lambda v: self.set(dome_config.NUM_POSITIONS, v))
        self.dome_max_burst_input.valueChanged.connect(
            lambda v: self.set(dome_config.MAX_BURST, v))
        self.dome_strategy_select.currentIndexChanged.connect(
            lambda i: self.set(dome_config.CAPTURE_STRATEGY, self.dome_strategy_select.itemData(i)))
        self.dome_light_select.currentIndexChanged.connect(
            lambda i: self.set(dome_config.LIGHT_CONTROLLER, self.dome_light_select.itemData(i)))
        self.dome_show_instructions_checkbox.toggled.connect(
            lambda checked: self.set(dome_config.SHOW_CAPTURE_INSTRUCTIONS, checked))

        self._presets = dome_config.load_presets()
        self.dome_preset_select.addItem(self.tr("— Preset laden —"), None)
        for key, preset in self._presets.items():
            self.dome_preset_select.addItem(preset.name, key)
        self.dome_preset_select.currentIndexChanged.connect(self._apply_dome_preset)

    def _select_combo_data(self, combo: QComboBox, data: Any):
        index = combo.findData(data)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _apply_dome_preset(self, index: int):
        key = self.dome_preset_select.itemData(index)
        if key is None:
            return
        preset = self._presets[key]
        self.dome_num_positions_input.setValue(preset.num_positions)
        self.dome_max_burst_input.setValue(preset.max_burst)
        self._select_combo_data(self.dome_strategy_select, preset.capture_strategy.value)
        self._select_combo_data(self.dome_light_select, preset.light_controller)
        self.dome_show_instructions_checkbox.setChecked(preset.show_capture_instructions)
        self.set(dome_config.NAME, preset.name)
        # Snap the loader back to its placeholder so the same preset can be
        # re-applied (and it isn't mistaken for a stored selection).
        self.dome_preset_select.setCurrentIndex(0)

    def set(self, name: str, value: QVariant):
        self.settings[name] = value

    def choose_lp_template_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr("LP-Datei wählen"),
            os.path.dirname(self.lp_template_path_input.text()),
            self.tr("LP-Dateien (*.lp);;Alle Dateien (*)"))
        if path:
            self.lp_template_path_input.setText(path)

    def choose_working_directory(self):
        file_dialog = QFileDialog(self,
                                  self.tr("Arbeitsverzeichnis wählen"),
                                  self.working_directory_input.text())
        file_dialog.setFileMode(QFileDialog.FileMode.Directory)
        if file_dialog.exec():
            print(file_dialog.selectedFiles())

        if file_dialog.selectedFiles():
            self.working_directory_input.setText(file_dialog.selectedFiles()[0])
