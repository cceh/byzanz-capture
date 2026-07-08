"""StitchBar — slim status strip between the capture row and the filmstrip.

Shown only for stitch buckets (MainWindow toggles visibility). It reports
the connectivity verdict for the filmstrip's segment set right where the
eye already is — above the thumbnails whose dots it explains. The message
carries its own ✓ / △ / ⚠ / ✗ glyph; the `state` QSS property tints it
ok / warn / error / neutral.

View only: MainWindow owns the StitchController and drives this via
`show_checking` (while a check runs) and `show_message` (its result).
The preview button arrives in phase S2.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy

from byzanz_camera.helpers import set_state
from byzanz_camera.spinner import Spinner


class StitchBar(QFrame):
    """Connectivity-verdict strip for stitch buckets."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("stitchBar")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        row = QHBoxLayout(self)
        row.setContentsMargins(4, 2, 4, 4)
        row.setSpacing(8)

        self._spinner = Spinner(self)
        self._spinner.setFixedSize(14, 14)
        self._spinner.stopAnimation()
        self._spinner.hide()
        row.addWidget(self._spinner, 0, Qt.AlignmentFlag.AlignVCenter)

        self._status = QLabel("")
        self._status.setObjectName("stitchStatus")
        self._status.setWordWrap(True)
        self._status.setSizePolicy(
            QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Preferred)
        set_state(self._status, "state", "neutral")
        row.addWidget(self._status, 1, Qt.AlignmentFlag.AlignVCenter)

    def show_checking(self) -> None:
        """A check is running — spinner + neutral 'checking' line."""
        self._spinner.show()
        self._spinner.startAnimation()
        set_state(self._status, "state", "neutral")
        self._status.setText("Checking segment overlap…")

    def show_message(self, text: str, level: str) -> None:
        """A check finished — its verdict message, tinted by `level`
        (ok / warn / error / neutral)."""
        self._spinner.stopAnimation()
        self._spinner.hide()
        set_state(self._status, "state", level)
        self._status.setText(text)
