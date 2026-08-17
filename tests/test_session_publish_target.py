from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from papyri.session_state import SessionState


class _Target:
    def __init__(self) -> None:
        self.name = "target"
        self.hydrated = False

    def refresh(self) -> None:
        self.hydrated = True


class SessionPublishTargetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_target_is_hydrated_before_receivers_run(self) -> None:
        session = SessionState()
        target = _Target()
        seen_hydrated = []
        session.current_object_changed.connect(
            lambda obj: seen_hydrated.append(obj.hydrated))

        session.publish_target(target)

        self.assertEqual(seen_hydrated, [True])
        self.assertIs(session.current_object, target)

    def test_none_clears_without_refresh(self) -> None:
        session = SessionState()
        session.publish_target(_Target())

        session.publish_target(None)

        self.assertIsNone(session.current_object)


if __name__ == "__main__":
    unittest.main()
