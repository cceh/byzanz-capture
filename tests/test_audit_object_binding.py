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
    AuditRequest, CaptureAuditContext, SCALECARD_AUDIT, SHARPNESS_AUDIT,
)
from byzanz_camera.sharpness import METRIC_VERSION
from byzanz_camera.sharpness.edge_blur import (
    METRIC_VERSION as SCALECARD_METRIC_VERSION,
)
from papyri.audits import CaptureAuditSettings
from papyri.audits.scalecard import ScalecardAuditSettings
from papyri.audits.sharpness import SharpnessAuditSettings
from papyri.capture_model import Capture
from papyri.capture_vocab import SIDE_A, SPECTRUM_VISIBLE
from papyri.object_layout import (
    MetaKey, read_capture_audits, store_capture_audit, write_meta,
)
from byzanz_camera.filmstrip_widget import ImageFileListItem
from papyri.papyri_filmstrip import PapyriFilmstrip


class _Target(QObject):
    state_changed = pyqtSignal()
    import_failed = pyqtSignal(Path)

    def __init__(self, root: Path, *captures: Capture) -> None:
        super().__init__()
        self.name = "object"
        self.dir = str(root)
        self.meta_path = str(root / "_meta.json")
        self._all = list(captures)
        self._captures: list[Capture] = []
        self._chosen: Capture | None = None
        self._stitching = False
        self._reference: Capture | None = None

    def refresh(self) -> None:
        self._captures = list(self._all)
        self._chosen = self._all[0]

    def captures(self, _side: str, _spectrum: str) -> list[Capture]:
        return list(self._captures)

    def chosen(self, _side: str, _spectrum: str) -> Capture | None:
        return self._chosen

    def dir_for(self, _side: str, _spectrum: str) -> str:
        return self.dir

    def is_stitching(self) -> bool:
        return self._stitching

    def reference(self, _side: str, _spectrum: str) -> Capture | None:
        return self._reference


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
            settings = CaptureAuditSettings(
                SharpnessAuditSettings(
                    enabled=True, vis_warn_from=2.60, ir_warn_from=1.75),
                ScalecardAuditSettings(enabled=True))
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


class ReferenceAuditFollowupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_scalecard_audit_follows_the_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "obj_a_vis_001.jpg"
            second = root / "obj_a_vis_002.jpg"
            first.touch()
            second.touch()
            cap1 = Capture(first.stem, str(first), None, 1)
            cap2 = Capture(second.stem, str(second), None, 2)
            write_meta(str(root / "_meta.json"), {MetaKey.AUDITS: {
                cap1.stem: {
                    SHARPNESS_AUDIT: {"metric_version": METRIC_VERSION},
                    SCALECARD_AUDIT: {
                        "metric_version": SCALECARD_METRIC_VERSION},
                },
                cap2.stem: {
                    SHARPNESS_AUDIT: {"metric_version": METRIC_VERSION},
                },
            }})
            target = _Target(root, cap1, cap2)
            target._stitching = True
            target._reference = cap1
            context = CaptureAuditContext(
                target.meta_path,
                AuditRequest("vis", frozenset(
                    {SHARPNESS_AUDIT, SCALECARD_AUDIT})),
            )
            settings = CaptureAuditSettings(
                SharpnessAuditSettings(
                    enabled=True, vis_warn_from=2.60, ir_warn_from=1.75),
                ScalecardAuditSettings(enabled=True))
            filmstrip = PapyriFilmstrip()
            try:
                target.refresh()
                with patch.object(filmstrip, "open_directory"):
                    filmstrip.bind_object(
                        target, SIDE_A, SPECTRUM_VISIBLE, context, settings)

                # Re-pin the reference to the last capture: the old
                # reference's now-inapplicable scalecard entry goes, the
                # new one is measured through the display-decode path.
                previous, target._reference = target._reference, cap2
                with patch.object(filmstrip, "redecode") as redecode:
                    filmstrip.ensure_reference_audits(previous)
                redecode.assert_called_once_with(str(second))
                audits = read_capture_audits(target.meta_path)
                self.assertEqual(
                    set(audits[cap1.stem]), {SHARPNESS_AUDIT})

                # Stitching off: the reference's scalecard entry goes,
                # and nothing is decoded (no check is missing).
                store_capture_audit(
                    target.meta_path, cap2.stem, SCALECARD_AUDIT,
                    {"metric_version": SCALECARD_METRIC_VERSION})
                target._stitching = False
                with patch.object(filmstrip, "redecode") as redecode:
                    filmstrip.ensure_reference_audits()
                redecode.assert_not_called()
                audits = read_capture_audits(target.meta_path)
                self.assertEqual(
                    set(audits[cap2.stem]), {SHARPNESS_AUDIT})
            finally:
                filmstrip.deleteLater()

    def test_second_capture_makes_the_first_shot_a_measured_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "obj_a_vis_001.jpg"
            second = root / "obj_a_vis_002.jpg"
            first.touch()
            second.touch()
            cap1 = Capture(first.stem, str(first), None, 1)
            cap2 = Capture(second.stem, str(second), None, 2)
            write_meta(str(root / "_meta.json"), {MetaKey.AUDITS: {
                cap1.stem: {SHARPNESS_AUDIT: {"metric_version": METRIC_VERSION}},
            }})
            target = _Target(root, cap1)
            target._stitching = True
            target._reference = cap1
            context = CaptureAuditContext(
                target.meta_path,
                AuditRequest("vis", frozenset(
                    {SHARPNESS_AUDIT, SCALECARD_AUDIT})),
            )
            settings = CaptureAuditSettings(
                SharpnessAuditSettings(
                    enabled=True, vis_warn_from=2.60, ir_warn_from=1.75),
                ScalecardAuditSettings(enabled=True))
            filmstrip = PapyriFilmstrip()
            try:
                target.refresh()
                with patch.object(filmstrip, "open_directory"):
                    filmstrip.bind_object(
                        target, SIDE_A, SPECTRUM_VISIBLE, context, settings)

                # One capture: an ordinary shot, nothing to measure.
                with patch.object(filmstrip, "redecode") as redecode:
                    filmstrip.ensure_reference_audits()
                redecode.assert_not_called()

                # The first segment arrives: the card shot is now the
                # reference and gets its scalecard measured.
                target._all.append(cap2)
                target.refresh()
                with patch.object(filmstrip, "redecode") as redecode:
                    filmstrip.ensure_reference_audits()
                redecode.assert_called_once_with(str(first))
            finally:
                filmstrip.deleteLater()


if __name__ == "__main__":
    unittest.main()
