from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from byzanz_camera.viewer_widget import PillStack


class PillStackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_status_pill_grows_with_longer_text(self) -> None:
        stack = PillStack()
        try:
            stack.set_content([], "Schärfe …")
            width_pending = stack._status_pill.width()

            stack.set_content([], "Schärfe 22,96 px")
            pill = stack._status_pill
            # The persistent pill must re-negotiate its layout size when
            # its text changes — a frozen width clips the text.
            self.assertEqual(pill.width(), pill.sizeHint().width())
            self.assertGreater(pill.width(), width_pending)
        finally:
            stack.deleteLater()

    def test_empty_content_hides_the_stack(self) -> None:
        stack = PillStack()
        try:
            stack.set_content([("warn", "#f59e0b")], "status")
            stack.set_content([], None)
            self.assertFalse(stack.isVisible())
        finally:
            stack.deleteLater()


if __name__ == "__main__":
    unittest.main()
