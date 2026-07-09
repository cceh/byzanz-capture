"""CalibrationBar — slim two-state strip at the top of the FusingPanel.

Calibration reuses the normal capture surface, so this bar does NOT shoot
and does NOT pick the target (that's the bucket tabs). It has two states,
swapped via a QStackedLayout:

  - **idle** (object mode): a per-camera status chip + a "Calibrate ▸"
    button that enters the calibration sub-mode.
  - **active** (calibration mode): a "CALIBRATION" label + a "← Back"
    button that leaves it.

View only: emits `enter_requested` / `exit_requested`; MainWindow owns the
controller, the calibration target, enter/exit, and the (normal) capture.
The buttons use native QPushButton styling on purpose (any QSS property
would strip the macOS native chrome).
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QStackedLayout, QWidget,
)

from byzanz_camera.helpers import set_state


class CalibrationBar(QFrame):
    """Two-state calibration control strip (idle ↔ active)."""

    enter_requested = pyqtSignal()
    exit_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("calibrationBar")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._stack = QStackedLayout(self)
        self._stack.setContentsMargins(4, 2, 4, 6)
        self._build_idle_page()
        self._build_active_page()
        self._stack.setCurrentWidget(self._idle_page)

    # ---- idle page -----------------------------------------------------

    def _build_idle_page(self) -> None:
        self._idle_page = QWidget()
        row = QHBoxLayout(self._idle_page)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self._dot = QLabel("●")
        self._dot.setObjectName("calibrationDot")
        set_state(self._dot, "state", "off")
        row.addWidget(self._dot, 0, Qt.AlignmentFlag.AlignVCenter)

        self._status = QLabel("Calibration")
        self._status.setObjectName("calibrationStatus")
        self._status.setSizePolicy(
            QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Preferred)
        row.addWidget(self._status, 1, Qt.AlignmentFlag.AlignVCenter)

        self._enter_btn = QPushButton("Calibrate ▸")
        self._enter_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._enter_btn.clicked.connect(self.enter_requested)
        row.addWidget(self._enter_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self._stack.addWidget(self._idle_page)

    def set_can_enter(self, enabled: bool) -> None:
        """Enable/disable the Calibrate button. Calibration is always for a
        specific object's height, so it's only offered with an object open."""
        self._enter_btn.setEnabled(enabled)
        self._enter_btn.setToolTip(
            "" if enabled else "Open an object first — calibration is for its height.")

    # ---- active page ---------------------------------------------------

    def _build_active_page(self) -> None:
        self._active_page = QWidget()
        row = QHBoxLayout(self._active_page)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self._mode_label = QLabel("CALIBRATING")
        self._mode_label.setObjectName("calibrationModeLabel")
        row.addWidget(self._mode_label, 0, Qt.AlignmentFlag.AlignVCenter)

        # The height this run calibrates for — comes from the open object, is
        # fixed for the whole run, and reads prominently so it can't be missed.
        self._cal_height = QLabel("")
        self._cal_height.setObjectName("calibrationHeight")
        row.addWidget(self._cal_height, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addStretch(1)

        self._back_btn = QPushButton("← Back")
        self._back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._back_btn.clicked.connect(self.exit_requested)
        row.addWidget(self._back_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self._stack.addWidget(self._active_page)

    # ---- public API ----------------------------------------------------

    def set_idle(self, text: str, level: str) -> None:
        """Show the idle state: per-camera status chip + Calibrate button."""
        self._status.setText(text)
        set_state(self._dot, "state", level)
        self._stack.setCurrentWidget(self._idle_page)

    def set_active(self, back_label: str) -> None:
        """Show the active state. `back_label` is the full button caption
        (e.g. "← Back to P.Köln_123")."""
        self._back_btn.setText(back_label)
        self._stack.setCurrentWidget(self._active_page)

    def set_active_height(self, text: str) -> None:
        """Set the prominent "for height X" caption on the active page."""
        self._cal_height.setText(text)
