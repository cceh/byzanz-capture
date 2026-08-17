from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from byzanz_camera.capture_audit import (
    AuditFinding, AuditRequest, CaptureAuditContext, SHARPNESS_AUDIT,
)
from byzanz_camera.filmstrip_widget import FilmstripWidget


class FilmstripAuditContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.widget = FilmstripWidget()
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "capture.jpg")
        Path(self.path).touch()
        self.context = CaptureAuditContext(
            "/object/_meta.json",
            AuditRequest("vis", frozenset({SHARPNESS_AUDIT})),
        )
        self.finding = AuditFinding(
            SHARPNESS_AUDIT, "v2-erf", {"sharp_px": 1.2})

    def tearDown(self) -> None:
        self.widget.deleteLater()
        self.temp.cleanup()

    def test_missing_checks_gate_restricts_the_request(self) -> None:
        self.widget.set_capture_audit_binding(
            self.context, lambda _path: frozenset())
        self.assertIsNone(self.widget._audit_request_for_path(self.path))

        self.widget.set_capture_audit_binding(
            self.context, lambda _path: frozenset({SHARPNESS_AUDIT}))
        request = self.widget._audit_request_for_path(self.path)
        self.assertEqual(request.checks, frozenset({SHARPNESS_AUDIT}))

    def test_without_gate_the_full_request_runs(self) -> None:
        self.widget.set_capture_audit_binding(self.context)
        request = self.widget._audit_request_for_path(self.path)
        self.assertEqual(request, self.context.request)

    def test_fresh_result_is_forwarded_with_its_frozen_context(self) -> None:
        forwarded = []
        self.widget.audit_finished.connect(
            lambda *args: forwarded.append(args))
        # Navigation happened long before the worker finished: the widget
        # is bound to a DIFFERENT context now — the event must still carry
        # its frozen origin so persistence can target the right object.
        other = CaptureAuditContext(
            "/other/_meta.json",
            AuditRequest("ir", frozenset({SHARPNESS_AUDIT})),
        )
        self.widget.set_capture_audit_binding(other)

        self.widget._FilmstripWidget__on_audit_finished(
            self.path, self.finding, self.context)

        self.assertEqual(forwarded, [(self.path, self.finding, self.context)])

    def test_result_without_context_is_dropped(self) -> None:
        forwarded = []
        self.widget.audit_finished.connect(
            lambda *args: forwarded.append(args))

        self.widget._FilmstripWidget__on_audit_finished(
            self.path, self.finding, None)

        self.assertEqual(forwarded, [])


if __name__ == "__main__":
    unittest.main()
