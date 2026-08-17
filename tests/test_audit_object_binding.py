from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from byzanz_camera.capture_audit import (
    AuditRequest, CaptureAuditContext, SHARPNESS_AUDIT,
)
from byzanz_camera.sharpness import METRIC_VERSION
from papyri.audits import CaptureAuditSettings
from papyri.audits.sharpness import SharpnessAuditSettings
from papyri.capture_model import Capture
from papyri.capture_vocab import SIDE_A, SPECTRUM_VISIBLE
from papyri.object_layout import MetaKey, write_meta
from byzanz_camera.filmstrip_widget import ImageFileListItem
from papyri.papyri_filmstrip import PapyriFilmstrip


class _Target(QObject):
    state_changed = pyqtSignal()
    import_failed = pyqtSignal(Path)

    def __init__(self, root: Path, capture: Capture) -> None:
        super().__init__()
        self.name = "object"
        self.dir = str(root)
        self.meta_path = str(root / "_meta.json")
        self._capture = capture
        self._captures: list[Capture] = []
        self._chosen: Capture | None = None

    def refresh(self) -> None:
        self._captures = [self._capture]
        self._chosen = self._capture

    def captures(self, _side: str, _spectrum: str) -> list[Capture]:
        return list(self._captures)

    def chosen(self, _side: str, _spectrum: str) -> Capture | None:
        return self._chosen

    def dir_for(self, _side: str, _spectrum: str) -> str:
        return self.dir

    def is_stitching(self) -> bool:
        return False


class AuditObjectBindingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_first_binding_renders_persisted_audit_and_wires_the_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "object_a_vis_001.jpg"
            path.touch()
            capture = Capture(path.stem, str(path), None, 1)
            write_meta(str(root / "_meta.json"), {
                MetaKey.AUDITS: {
                    capture.stem: {
                        SHARPNESS_AUDIT: {
                            "sharp_px": 9.9,   # far over the warn threshold
                            "metric_version": METRIC_VERSION,
                            "status": "warn",
                        },
                    },
                },
            })
            target = _Target(root, capture)
            context = CaptureAuditContext(
                target.meta_path,
                AuditRequest("vis", frozenset({SHARPNESS_AUDIT})),
            )
            settings = CaptureAuditSettings(SharpnessAuditSettings(
                enabled=True, vis_warn_from=2.60, ir_warn_from=1.75))
            filmstrip = PapyriFilmstrip()
            try:
                target.refresh()   # hydrate-then-publish contract
                with patch.object(filmstrip, "open_directory") as open_directory:
                    filmstrip.bind_object(
                        target, SIDE_A, SPECTRUM_VISIBLE, context, settings)

                # Badges come straight from the persisted meta entries —
                # and only warnings badge at all (no ✓ overclaim).
                self.assertEqual(
                    filmstrip._audit_badges.status_by_stem,
                    {capture.stem: "warn"},
                )
                self.assertEqual(
                    open_directory.call_args.kwargs["preferred_stem"],
                    capture.stem,
                )
                # The gate reports the persisted check as already covered,
                # and an unknown capture as fully missing.
                gate = open_directory.call_args.kwargs["missing_checks"]
                self.assertEqual(gate(str(path)), frozenset())
                self.assertEqual(
                    gate(str(root / "object_a_vis_002.jpg")),
                    frozenset({SHARPNESS_AUDIT}),
                )

                # Bound audit context adds the re-run entry to the menu,
                # and triggering it reports the capture's stem.
                item = ImageFileListItem(str(path))
                menu = filmstrip._build_context_menu(item)
                recheck = [a for a in menu.actions()
                           if a.text() == "Re-run capture check"]
                self.assertEqual(len(recheck), 1)
                requested = []
                filmstrip.audit_recheck_requested.connect(requested.append)
                recheck[0].trigger()
                self.assertEqual(requested, [capture.stem])
            finally:
                filmstrip.deleteLater()


if __name__ == "__main__":
    unittest.main()
