from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from PyQt6.QtGui import QImage

from byzanz_camera.capture_audit import AuditRequest, SHARPNESS_AUDIT
from byzanz_camera.load_image_worker import ImageMode, LoadImageWorker
from byzanz_camera.thumb_cache import ThumbCache


class AuditWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.image_path = Path(self.temp.name) / "capture.jpg"
        Image.new("RGB", (32, 24), (100, 120, 140)).save(self.image_path)
        self.cache = ThumbCache(Path(self.temp.name) / "worker-cache")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_image_is_published_before_audit(self) -> None:
        events: list[str] = []
        worker = LoadImageWorker(
            str(self.image_path),
            mode=ImageMode.FULL,
            audit_request=AuditRequest("vis", frozenset({SHARPNESS_AUDIT})),
        )
        worker.signals.finished.connect(lambda _result: events.append("image"))
        worker.signals.audit_finished.connect(
            lambda _path, _finding: events.append("audit"))
        with (
            patch("byzanz_camera.load_image_worker.thumb_cache",
                  return_value=self.cache),
            patch(
                "byzanz_camera.load_image_worker.measure_object_sharpness",
                return_value={"sharp_px": 1.2},
            ),
        ):
            worker.run()
        self.assertEqual(events, ["image", "audit"])

    def test_no_request_means_no_audit(self) -> None:
        audits = []
        worker = LoadImageWorker(str(self.image_path), mode=ImageMode.FULL)
        worker.signals.audit_finished.connect(audits.append)
        with patch("byzanz_camera.load_image_worker.thumb_cache",
                   return_value=self.cache):
            worker.run()
        self.assertEqual(audits, [])

    def test_legacy_thumb_cache_sharpness_is_ignored(self) -> None:
        cache = ThumbCache(Path(self.temp.name) / "cache")
        thumb = QImage(4, 4, QImage.Format.Format_RGB888)
        thumb.fill(0)
        cache.put(str(self.image_path), thumb, {"ISO": 100})
        key = cache._key(str(self.image_path))
        sidecar = cache.cache_dir / f"{key}.json"
        payload = json.loads(sidecar.read_text())
        payload["sharpness"] = 9999
        sidecar.write_text(json.dumps(payload))

        hit = cache.get(str(self.image_path))
        self.assertIsNotNone(hit)
        _image, exif = hit
        self.assertEqual(exif, {"ISO": 100})


if __name__ == "__main__":
    unittest.main()
