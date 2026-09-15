from __future__ import annotations

import unittest

from byzanz_camera.capture_audit import AuditFinding, SCALECARD_AUDIT
from byzanz_camera.sharpness.edge_blur import METRIC_VERSION
from papyri.audits import applicable_checks
from papyri.audits.scalecard import (
    ScalecardAuditSettings, applies_to, finding_to_entry, is_current_entry,
    presentation_for_entry, status_for, summary_for_entry,
)


class ScalecardAuditPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = ScalecardAuditSettings(enabled=True)

    @staticmethod
    def finding(edge_px: float | None) -> AuditFinding:
        # The shape edge_blur.measure_rgb returns (see its docstring).
        data = {
            "edge_px": edge_px,
            "edges": 14 if edge_px is not None else 0,
            "px_per_cm": 305.3 if edge_px is not None else None,
            "ticks": 11 if edge_px is not None else 0,
            "metric_version": METRIC_VERSION,
        }
        return AuditFinding(SCALECARD_AUDIT, METRIC_VERSION, data)

    def entry(self, edge_px: float | None) -> dict:
        return finding_to_entry(self.finding(edge_px), "vis", self.settings)

    def test_entry_is_the_metric_result_plus_status(self) -> None:
        entry = self.entry(2.8)
        self.assertEqual(entry["edge_px"], 2.8)
        self.assertEqual(entry["edges"], 14)
        self.assertEqual(entry["px_per_cm"], 305.3)
        self.assertEqual(entry["ticks"], 11)
        self.assertEqual(entry["status"], "ok")
        self.assertNotIn("warn_threshold", entry)   # no thresholds by design

    def test_never_warns(self) -> None:
        self.assertEqual(status_for(self.entry(9.9), "vis", self.settings), "ok")
        self.assertEqual(status_for(self.entry(None), "vis", self.settings), "none")

    def test_only_exact_metric_version_is_current(self) -> None:
        entry = self.entry(2.8)
        self.assertTrue(is_current_entry(entry))
        entry["metric_version"] = "card-v2"
        self.assertFalse(is_current_entry(entry))

    def test_summary_and_presentation_report_micrometres(self) -> None:
        # 2.8 px at 305.3 px/cm = 91.7 µm — the height-comparable number,
        # derived at display time.
        self.assertEqual(summary_for_entry(self.entry(2.8)), "Maßstab 92 µm")
        self.assertEqual(summary_for_entry(self.entry(None)), "Maßstab –")
        self.assertEqual(summary_for_entry(None), "Maßstab …")
        status, text = presentation_for_entry(
            self.entry(None), "vis", self.settings)
        self.assertEqual(status, "none")
        self.assertIn("NICHT MESSBAR", text)

    def test_applies_only_to_the_stitch_reference(self) -> None:
        self.assertTrue(applies_to(
            "obj_a_vis_001", stitching=True, reference_stem="obj_a_vis_001"))
        self.assertFalse(applies_to(
            "obj_a_vis_002", stitching=True, reference_stem="obj_a_vis_001"))
        self.assertFalse(applies_to(
            "obj_a_vis_001", stitching=False, reference_stem="obj_a_vis_001"))

    def test_applicable_checks_routes_through_the_modules(self) -> None:
        requested = frozenset({"sharpness", "scalecard"})
        self.assertEqual(
            applicable_checks(requested, "obj_a_vis_001",
                              stitching=True, reference_stem="obj_a_vis_001"),
            {"sharpness", "scalecard"})
        self.assertEqual(
            applicable_checks(requested, "obj_a_vis_002",
                              stitching=True, reference_stem="obj_a_vis_001"),
            {"sharpness"})


if __name__ == "__main__":
    unittest.main()
