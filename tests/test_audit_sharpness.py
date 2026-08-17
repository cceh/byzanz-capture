from __future__ import annotations

import unittest

from byzanz_camera.capture_audit import AuditFinding, SHARPNESS_AUDIT
from byzanz_camera.sharpness import METRIC_VERSION
from papyri.audits import entry_is_current
from papyri.audits.sharpness import (
    SharpnessAuditSettings, finding_to_entry, is_current_entry,
    presentation_for_entry, status_for, summary_for_entry,
)


class CaptureAuditPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = SharpnessAuditSettings(
            enabled=True, vis_warn_from=2.60, ir_warn_from=1.75)

    @staticmethod
    def finding(sharp_px: float | None, balance: float = 0.5) -> AuditFinding:
        data = None if sharp_px is None else {
            "sharp_px": sharp_px,
            "median_px": sharp_px + 0.5,
            "n_edges": 200,
            "orientation_balance": balance,
            "excluded": {"cc": False, "scale": True},
        }
        return AuditFinding(SHARPNESS_AUDIT, METRIC_VERSION, data)

    def entry(self, sharp_px: float | None, balance: float = 0.5) -> dict:
        return finding_to_entry(
            self.finding(sharp_px, balance), "vis", self.settings)

    def test_single_vis_warning_threshold(self) -> None:
        self.assertEqual(self.entry(2.59)["status"], "ok")
        self.assertEqual(self.entry(2.60)["status"], "warn")

    def test_single_ir_warning_threshold(self) -> None:
        entry_ok = finding_to_entry(self.finding(1.74), "ir", self.settings)
        entry_warn = finding_to_entry(self.finding(1.75), "ir", self.settings)
        self.assertEqual(entry_ok["status"], "ok")
        self.assertEqual(entry_warn["status"], "warn")

    def test_none_is_not_ok(self) -> None:
        status, text = presentation_for_entry(
            self.entry(None), "vis", self.settings)
        self.assertEqual(status, "none")
        self.assertIn("NICHT MESSBAR", text)

    def test_balance_never_reaches_the_operator_text(self) -> None:
        # Measured + persisted, but deliberately not presented — see the
        # rationale in presentation_for_entry.
        _, warn_text = presentation_for_entry(
            self.entry(3.0, 0.01), "vis", self.settings)
        self.assertNotIn("VERWACKLUNG", warn_text)
        self.assertIn("MÖGLICHE UNSCHÄRFE", warn_text)

    def test_only_exact_metric_version_is_current(self) -> None:
        entry = self.entry(2.0)
        self.assertTrue(is_current_entry(entry))
        self.assertTrue(entry_is_current(SHARPNESS_AUDIT, entry))
        entry["metric_version"] = "old"
        self.assertFalse(is_current_entry(entry))
        self.assertFalse(is_current_entry("not-a-dict"))
        self.assertFalse(entry_is_current("unknown-check", self.entry(2.0)))

    def test_status_reclassifies_against_current_settings(self) -> None:
        # Persisted with a 2.60 threshold, re-read after the user tightened
        # it: the stored status snapshot must not win over the live rule.
        entry = self.entry(2.0)
        self.assertEqual(entry["status"], "ok")
        tightened = SharpnessAuditSettings(
            enabled=True, vis_warn_from=1.90, ir_warn_from=1.75)
        self.assertEqual(status_for(entry, "vis", tightened), "warn")


class SummaryTest(CaptureAuditPolicyTest):
    def test_summary_states_measurement_without_verdict(self) -> None:
        self.assertEqual(summary_for_entry(self.entry(1.18)),
                         "Schärfe 1,18 px")
        self.assertEqual(summary_for_entry(self.entry(None)), "Schärfe –")
        self.assertEqual(summary_for_entry(None), "Schärfe …")


if __name__ == "__main__":
    unittest.main()
