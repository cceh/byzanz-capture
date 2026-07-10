"""StitchBar — slim status strip between the capture row and the filmstrip.

Shown only for stitch buckets (MainWindow toggles visibility). It reports
the connectivity verdict for the filmstrip's segment set right where the
eye already is — above the thumbnails whose dots it explains. The message
carries its own ✓ / △ / ⚠ / ✗ glyph; the `state` QSS property tints it
ok / warn / error / neutral.

A "Stitch preview" button sits at the right, enabled only when the set is
green (its enabled state is a direct function of the verdict beside it).

View only: MainWindow owns the StitchController and drives this via
`show_checking` / `show_message` / `show_previewing` / `set_preview_enabled`,
and reacts to `preview_requested`.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
)

from byzanz_camera.helpers import get_ui_path, set_state, set_themed_icon
from byzanz_camera.spinner import Spinner


class StitchBar(QFrame):
    """Connectivity-verdict strip + preview trigger for stitch buckets."""

    # User clicked "Stitch preview" (only possible while enabled = green).
    preview_requested = pyqtSignal()

    # User toggled the ghost overlay (translucent previous segment over the
    # live view — see the overlap coach). MainWindow persists + applies it.
    ghost_toggled = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("stitchBar")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        row = QHBoxLayout(self)
        row.setContentsMargins(4, 2, 4, 4)
        row.setSpacing(8)

        # Needle identity marker (matches the Stitch toggle + sidebar badge).
        marker = QLabel("🪡")
        marker.setObjectName("stitchBarMarker")
        row.addWidget(marker, 0, Qt.AlignmentFlag.AlignVCenter)

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

        # Ghost overlay toggle — checkable, next to the verdict it
        # visualizes. Native styling on purpose (QSS on a QPushButton
        # strips macOS chrome — same reason the calibration bar's buttons
        # stay native).
        self._ghost_btn = QPushButton("Ghost")
        self._ghost_btn.setCheckable(True)
        set_themed_icon(self._ghost_btn.setIcon, get_ui_path("ui/ghost.svg"))
        self._ghost_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._ghost_btn.setToolTip(
            "Overlay the last segment translucently on the live view —\n"
            "green = spacing is right, shoot")
        self._ghost_btn.toggled.connect(self.ghost_toggled)
        row.addWidget(self._ghost_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self._preview_btn = QPushButton("Stitch preview")
        set_themed_icon(self._preview_btn.setIcon, get_ui_path("ui/image.svg"))
        self._preview_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._preview_btn.setToolTip(
            "Stitch the segments into a preview to check coverage")
        self._preview_btn.setEnabled(False)
        self._preview_btn.clicked.connect(self.preview_requested)
        row.addWidget(self._preview_btn, 0, Qt.AlignmentFlag.AlignVCenter)

    def show_checking(self) -> None:
        """A check is running — spinner + neutral line; preview not offered
        until the set is known to be green."""
        self._spinner.show()
        self._spinner.startAnimation()
        set_state(self._status, "state", "neutral")
        self._status.setText("Checking segment overlap…")
        self._preview_btn.setEnabled(False)

    def show_message(self, text: str, level: str) -> None:
        """A check finished — its verdict message, tinted by `level`
        (ok / warn / error / neutral). The caller sets preview-enabled
        from the verdict via `set_preview_enabled`."""
        self._spinner.stopAnimation()
        self._spinner.hide()
        set_state(self._status, "state", level)
        self._status.setText(text)

    def show_previewing(self) -> None:
        """The composite is running — spinner + neutral line, button off."""
        self._spinner.show()
        self._spinner.startAnimation()
        set_state(self._status, "state", "neutral")
        self._status.setText("Stitching preview…")
        self._preview_btn.setEnabled(False)

    def set_preview_enabled(self, enabled: bool) -> None:
        """Enable the preview button (green set) or grey it out."""
        self._preview_btn.setEnabled(enabled)

    def set_ghost_checked(self, checked: bool) -> None:
        """Reflect the persisted ghost-overlay state without re-emitting."""
        self._ghost_btn.blockSignals(True)
        self._ghost_btn.setChecked(checked)
        self._ghost_btn.blockSignals(False)
