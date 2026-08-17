from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from byzanz_camera.capture_audit import (
    AuditFinding, AuditRequest, CaptureAuditContext, SHARPNESS_AUDIT,
)
from byzanz_camera.sharpness import METRIC_VERSION
from papyri.audits import CaptureAuditSettings, persist_fresh_capture_audit
from papyri.audits.sharpness import SharpnessAuditSettings


class AuditPersistenceCoordinatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.meta_path = str(Path(self.temp.name) / "_meta.json")
        Path(self.meta_path).touch()
        self.context = CaptureAuditContext(
            self.meta_path,
            AuditRequest("vis", frozenset({SHARPNESS_AUDIT})),
        )
        self.finding = AuditFinding(
            SHARPNESS_AUDIT,
            METRIC_VERSION,
            {"sharp_px": 1.2, "n_edges": 100},
        )
        self.settings = CaptureAuditSettings(
            sharpness=SharpnessAuditSettings(
                enabled=True, vis_warn_from=2.60, ir_warn_from=1.75))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_late_result_does_not_resurrect_a_removed_capture(self) -> None:
        removed_path = str(Path(self.temp.name) / "removed_vis_001.jpg")
        with patch("papyri.audits.store_capture_audit") as store:
            persisted = persist_fresh_capture_audit(
                removed_path, self.finding, self.context, self.settings)
        self.assertFalse(persisted)
        store.assert_not_called()

    def test_late_result_does_not_recreate_a_removed_target(self) -> None:
        capture_path = Path(self.temp.name) / "capture_vis_001.jpg"
        capture_path.touch()
        Path(self.meta_path).unlink()
        with patch("papyri.audits.store_capture_audit") as store:
            persisted = persist_fresh_capture_audit(
                str(capture_path), self.finding, self.context, self.settings)
        self.assertFalse(persisted)
        store.assert_not_called()

    def test_live_result_uses_frozen_target_and_exact_capture_identity(self) -> None:
        capture_path = Path(self.temp.name) / "capture_vis_001.jpg"
        capture_path.touch()
        with patch("papyri.audits.store_capture_audit") as store:
            persisted = persist_fresh_capture_audit(
                str(capture_path), self.finding, self.context, self.settings)
        self.assertTrue(persisted)
        self.assertEqual(store.call_args.args[:3], (
            self.meta_path, capture_path.stem, SHARPNESS_AUDIT))


if __name__ == "__main__":
    unittest.main()
