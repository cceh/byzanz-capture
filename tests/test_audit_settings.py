from __future__ import annotations

import tempfile
import unittest

from PyQt6.QtCore import QSettings

from byzanz_camera.capture_audit import SHARPNESS_AUDIT
from byzanz_camera.settings_migration import (
    PAPYRI_SHARPNESS_ENABLED_KEY, PAPYRI_SHARPNESS_IR_THRESHOLD_KEY,
    PAPYRI_SHARPNESS_VIS_THRESHOLD_KEY, migrate_papyri_settings,
)
from papyri.audits import read_audit_settings


class AuditSettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = QSettings(
            self.temp.name + "/settings.ini", QSettings.Format.IniFormat)

    def tearDown(self) -> None:
        self.settings.clear()
        self.temp.cleanup()

    def test_legacy_enable_is_migrated_into_audit_group(self) -> None:
        self.settings.setValue("sharpnessCheckEnabled", False)
        migrate_papyri_settings(self.settings)
        self.assertFalse(self.settings.value(
            PAPYRI_SHARPNESS_ENABLED_KEY, type=bool))
        self.assertIsNone(self.settings.value("sharpnessCheckEnabled"))

    def test_defaults_and_complete_snapshot(self) -> None:
        # A fresh store yields the shipped defaults without any seeding.
        snapshot = read_audit_settings(self.settings)
        self.assertEqual(snapshot.enabled_checks, {SHARPNESS_AUDIT})
        self.assertEqual(snapshot.sharpness.vis_warn_from, 2.60)
        self.assertEqual(snapshot.sharpness.ir_warn_from, 1.75)

        self.settings.setValue(PAPYRI_SHARPNESS_VIS_THRESHOLD_KEY, 3.1)
        self.settings.setValue(PAPYRI_SHARPNESS_IR_THRESHOLD_KEY, 2.2)
        snapshot = read_audit_settings(self.settings)
        self.assertEqual(snapshot.sharpness.vis_warn_from, 3.1)
        self.assertEqual(snapshot.sharpness.ir_warn_from, 2.2)


if __name__ == "__main__":
    unittest.main()
