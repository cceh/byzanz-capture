from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication, QGroupBox

from byzanz_camera.settings_migration import (
    PAPYRI_SHARPNESS_ENABLED_KEY,
    PAPYRI_SHARPNESS_IR_THRESHOLD_KEY,
    PAPYRI_SHARPNESS_VIS_THRESHOLD_KEY,
)
from papyri.settings_dialog import PapyriSettingsDialog


class AuditSettingsDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.q_settings = QSettings(
            self.temp.name + "/settings.ini", QSettings.Format.IniFormat)
        self.dialog = PapyriSettingsDialog(self.q_settings, {})

    def tearDown(self) -> None:
        self.dialog.deleteLater()
        self.q_settings.clear()
        self.temp.cleanup()

    def test_load_is_silent_and_group_is_visible(self) -> None:
        self.assertEqual(self.dialog.settings, {})
        group = self.dialog.findChild(QGroupBox, "auditSettingsGroup")
        self.assertIsNotNone(group)
        self.assertEqual(group.title(), "Audit settings")

    def test_sharpness_gate_controls_its_subsettings(self) -> None:
        self.dialog.sharpness_check_checkbox.setChecked(False)
        self.assertFalse(self.dialog.sharpness_vis_threshold_input.isEnabled())
        self.assertFalse(self.dialog.sharpness_ir_threshold_input.isEnabled())
        self.assertFalse(
            self.dialog.settings[PAPYRI_SHARPNESS_ENABLED_KEY])

        self.dialog.sharpness_check_checkbox.setChecked(True)
        self.dialog.sharpness_vis_threshold_input.setValue(3.15)
        self.dialog.sharpness_ir_threshold_input.setValue(2.05)
        self.assertEqual(
            self.dialog.settings[PAPYRI_SHARPNESS_VIS_THRESHOLD_KEY], 3.15)
        self.assertEqual(
            self.dialog.settings[PAPYRI_SHARPNESS_IR_THRESHOLD_KEY], 2.05)


if __name__ == "__main__":
    unittest.main()
