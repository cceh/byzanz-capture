from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QColor, QPixmap, QPixmapCache
from PyQt6.QtWidgets import QApplication

from byzanz_camera.capture_audit import (
    AuditRequest, CaptureAuditContext, SHARPNESS_AUDIT,
)
from byzanz_camera.filmstrip_widget import FilmstripWidget
from byzanz_camera.load_image_worker import ImageMode


class FilmstripPixmapCacheTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_closing_directory_preserves_process_global_pixmaps(self) -> None:
        key = "filmstrip-cache-survival-contract"
        pixmap = QPixmap(4, 4)
        pixmap.fill(QColor("red"))
        self.assertTrue(QPixmapCache.insert(key, pixmap))
        widget = FilmstripWidget()
        try:
            widget.close_directory()
            self.assertIsNotNone(QPixmapCache.find(key))
        finally:
            QPixmapCache.remove(key)
            widget.deleteLater()

    def test_initial_display_uses_cached_full_pixmap_without_full_decode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            file_name = "capture_001.jpg"
            path = str(Path(directory) / file_name)
            Path(path).touch()
            cached = QPixmap(4, 4)
            cached.fill(QColor("blue"))
            self.assertTrue(QPixmapCache.insert(path, cached))
            widget = FilmstripWidget()
            widget._FilmstripWidget__currentPath = directory
            widget._preferred_stem = Path(file_name).stem
            context = CaptureAuditContext(
                "/object/_meta.json",
                AuditRequest("vis", frozenset({SHARPNESS_AUDIT})),
            )
            # host gate reports every check as already persisted
            widget.set_capture_audit_binding(context, lambda _path: frozenset())
            try:
                with patch.object(widget, "_FilmstripWidget__load_image") as load:
                    widget._FilmstripWidget__queue_decoders({file_name})
                self.assertEqual(load.call_args.kwargs["mode"], ImageMode.THUMB)
                callback = load.call_args.args[1]
                self.assertIsNotNone(callback.keywords["display_pixmap"])
            finally:
                QPixmapCache.remove(path)
                widget.deleteLater()

    def test_missing_audit_still_requires_full_decode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            file_name = "capture_001.jpg"
            path = str(Path(directory) / file_name)
            Path(path).touch()
            cached = QPixmap(4, 4)
            cached.fill(QColor("blue"))
            self.assertTrue(QPixmapCache.insert(path, cached))
            widget = FilmstripWidget()
            widget._FilmstripWidget__currentPath = directory
            widget._preferred_stem = Path(file_name).stem
            widget.set_capture_audit_binding(CaptureAuditContext(
                "/object/_meta.json",
                AuditRequest("vis", frozenset({SHARPNESS_AUDIT})),
            ))
            try:
                with patch.object(widget, "_FilmstripWidget__load_image") as load:
                    widget._FilmstripWidget__queue_decoders({file_name})
                self.assertEqual(load.call_args.kwargs["mode"], ImageMode.FULL)
            finally:
                QPixmapCache.remove(path)
                widget.deleteLater()

    def test_initial_display_target_is_queued_first_with_priority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            older = "capture_001.jpg"
            preferred = "capture_003.jpg"
            middle = "capture_002.jpg"
            widget = FilmstripWidget()
            widget._FilmstripWidget__currentPath = directory
            widget._preferred_stem = Path(preferred).stem
            try:
                with patch.object(widget, "_FilmstripWidget__load_image") as load:
                    widget._FilmstripWidget__queue_decoders(
                        {preferred, older, middle})

                first = load.call_args_list[0]
                self.assertEqual(first.args[0], preferred)
                self.assertEqual(first.kwargs["mode"], ImageMode.FULL)
                self.assertEqual(first.kwargs["priority"], 1)
                self.assertTrue(all(
                    call.kwargs["priority"] == 0
                    for call in load.call_args_list[1:]
                ))
            finally:
                widget.deleteLater()


if __name__ == "__main__":
    unittest.main()
