from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from byzanz_camera.capture_audit import SHARPNESS_AUDIT
from byzanz_camera.sharpness import METRIC_VERSION
from papyri.audits import CaptureAuditSettings, bucket_effective_warnings
from papyri.audits.sharpness import SharpnessAuditSettings
from papyri.capture_vocab import SIDE_A, SPECTRUM_VISIBLE
from papyri.object_layout import (
    MetaKey, bucket_key, dir_for_bucket, locate_capture, write_meta,
)

WARN_ENTRY = {"sharp_px": 9.9, "metric_version": METRIC_VERSION}
OK_ENTRY = {"sharp_px": 1.2, "metric_version": METRIC_VERSION}


class BucketEffectiveWarningsTest(unittest.TestCase):
    """The resolution semantics of the warning rollup: only the take that
    REPRESENTS a bucket (pinned chosen, else newest) counts — retaking,
    re-choosing, or deleting a warned take clears the rollup by itself."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.object_dir = str(Path(self.temp.name) / "object")
        self.bucket = Path(dir_for_bucket(
            self.object_dir, SIDE_A, SPECTRUM_VISIBLE))
        self.bucket.mkdir(parents=True)
        self.settings = CaptureAuditSettings(SharpnessAuditSettings(
            enabled=True, vis_warn_from=2.60, ir_warn_from=1.75))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_meta(self, audits: dict, markers: dict | None = None) -> None:
        meta = {MetaKey.AUDITS: audits}
        if markers is not None:
            meta[MetaKey.MARKERS] = markers
        write_meta(str(Path(self.object_dir) / "_meta.json"), meta)

    def _touch(self, stem: str) -> None:
        (self.bucket / f"{stem}.jpg").touch()

    def test_warned_newest_take_flags_the_bucket(self) -> None:
        self._touch("obj_a_vis_001")
        self._write_meta({"obj_a_vis_001": {SHARPNESS_AUDIT: WARN_ENTRY}})
        self.assertEqual(
            bucket_effective_warnings(self.object_dir, self.settings),
            {(SIDE_A, SPECTRUM_VISIBLE)},
        )

    def test_retaking_supersedes_the_warned_take(self) -> None:
        self._touch("obj_a_vis_001")
        self._touch("obj_a_vis_002")
        self._write_meta({
            "obj_a_vis_001": {SHARPNESS_AUDIT: WARN_ENTRY},
            "obj_a_vis_002": {SHARPNESS_AUDIT: OK_ENTRY},
        })
        self.assertEqual(
            bucket_effective_warnings(self.object_dir, self.settings), set())

    def test_pinning_a_warned_take_flags_the_bucket_again(self) -> None:
        self._touch("obj_a_vis_001")
        self._touch("obj_a_vis_002")
        self._write_meta(
            {
                "obj_a_vis_001": {SHARPNESS_AUDIT: WARN_ENTRY},
                "obj_a_vis_002": {SHARPNESS_AUDIT: OK_ENTRY},
            },
            markers={bucket_key(SIDE_A, SPECTRUM_VISIBLE): {
                "chosen": "obj_a_vis_001"}},
        )
        self.assertEqual(
            bucket_effective_warnings(self.object_dir, self.settings),
            {(SIDE_A, SPECTRUM_VISIBLE)},
        )

    def test_disabled_audits_never_warn(self) -> None:
        self._touch("obj_a_vis_001")
        self._write_meta({"obj_a_vis_001": {SHARPNESS_AUDIT: WARN_ENTRY}})
        disabled = CaptureAuditSettings(SharpnessAuditSettings(
            enabled=False, vis_warn_from=2.60, ir_warn_from=1.75))
        self.assertEqual(
            bucket_effective_warnings(self.object_dir, disabled), set())


class LocateCaptureTest(unittest.TestCase):
    def test_round_trip_through_the_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            object_dir = str(Path(directory) / "P.Tebt. 0959")
            bucket = Path(dir_for_bucket(object_dir, SIDE_A, SPECTRUM_VISIBLE))
            bucket.mkdir(parents=True)
            write_meta(str(Path(object_dir) / "_meta.json"), {})
            path = bucket / "P.Tebt. 0959_a_vis_001.jpg"
            path.touch()
            self.assertEqual(
                locate_capture(str(path)),
                (object_dir, SIDE_A, SPECTRUM_VISIBLE),
            )
            # outside the managed layout -> honest None
            self.assertIsNone(locate_capture(str(Path(directory) / "x.jpg")))


if __name__ == "__main__":
    unittest.main()
