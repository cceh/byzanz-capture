from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QColor, QPixmap, QPixmapCache
from PyQt6.QtWidgets import QApplication

from byzanz_camera.filmstrip_widget import FilmstripWidget


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


if __name__ == "__main__":
    unittest.main()
