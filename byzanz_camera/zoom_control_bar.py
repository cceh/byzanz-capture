"""Zoom control bar for ViewerWidget.

A thin horizontal row of zoom controls rendered under the photo
viewer:

    [ Fit | 1:1 ]   [ − ]──────●──────[ + ]   42%

Layout groups:
  - Left: named-level pills (Fit, 1:1). Active level highlights when
    the current scale matches it exactly (within a tolerance).
  - Center: log-scale slider with ± step buttons. Step size matches
    the photo viewer's ZOOMFACT (1.25) so wheel and button steps
    align.
  - Right: live percentage readout, monospace-aligned so it doesn't
    jitter on each scroll tick.

Signals are emitted as plain *requests* — the parent layer wires them
to PhotoViewer methods. The bar holds no zoom state itself; it
mirrors what `set_current_zoom(...)` is told. This keeps the
viewer's transform the single source of truth.
"""
from __future__ import annotations

import math

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSlider, QWidget,
)


# Slider has integer ticks; we map them log-linearly across the active
# zoom range. 1000 ticks gives smooth dragging without slider snap.
_SLIDER_TICKS = 1000

# Step factor — matches PhotoViewer.ZOOMFACT so wheel and ± clicks
# move the same amount. Mirrored here so the bar can compute "next"
# levels for button enable/disable.
_ZOOM_STEP = 1.25

# Tolerance for "is current scale equal to Fit / 1:1?" matching.
# Floating-point round-trips through QGraphicsView's transform make
# strict equality unreliable.
_EQ_EPSILON = 1e-3


class ZoomControlBar(QWidget):
    """Stateless zoom controls — emits requests, displays whatever
    `set_current_zoom` was last told."""

    # All scales are absolute (1.0 == 1:1).
    fit_requested        = pyqtSignal()
    one_to_one_requested = pyqtSignal()
    zoom_in_requested    = pyqtSignal()
    zoom_out_requested   = pyqtSignal()
    absolute_zoom_requested = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("zoomControlBar")

        # Mirror state. Updated only by set_current_zoom; the bar
        # never reads from the viewer directly.
        self._current: float = 1.0
        self._fit: float = 1.0
        self._max: float = 1.0
        # Suppress slider's valueChanged emission while we're
        # programmatically updating it to mirror the viewer.
        self._suppress_slider = False

        self._build_ui()
        self._refresh()

    # ---- public API ----------------------------------------------------

    def set_current_zoom(self, current: float, fit: float) -> None:
        """Update the displayed state. `current` and `fit` are
        absolute scales (1.0 == 1:1). `fit` may be > 1.0 when the
        image is smaller than the viewport. Zoom-in past 1:1 isn't
        supported by the viewer, so max = max(1.0, fit)."""
        self._current = max(1e-6, current)
        self._fit = max(1e-6, fit)
        self._max = max(1.0, self._fit)
        self._refresh()

    def set_photo_present(self, present: bool) -> None:
        """Enable/disable the whole bar based on whether there's a
        photo to zoom. Called by the parent when setPhoto(None)."""
        self.setEnabled(present)

    # ---- ui construction ----------------------------------------------

    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)

        # All buttons + slider use native styling — the bar is system
        # chrome, not branded. Fit/1:1 are checkable so the OS shows
        # which level the current zoom matches; clicks always emit the
        # action signal (`_refresh` re-syncs the checked state).
        self._fit_btn = QPushButton("Fit")
        self._fit_btn.setCheckable(True)
        self._fit_btn.setToolTip("Fit image to viewport")
        self._fit_btn.clicked.connect(self.fit_requested.emit)
        self._one_btn = QPushButton("1:1")
        self._one_btn.setCheckable(True)
        self._one_btn.setToolTip("Zoom to 100% (1 image px = 1 screen px)")
        self._one_btn.clicked.connect(self.one_to_one_requested.emit)
        layout.addWidget(self._fit_btn)
        layout.addWidget(self._one_btn)

        layout.addStretch(1)

        self._minus_btn = QPushButton("−")
        self._minus_btn.setToolTip("Zoom out (one step)")
        self._minus_btn.clicked.connect(self.zoom_out_requested.emit)
        layout.addWidget(self._minus_btn)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, _SLIDER_TICKS)
        self._slider.setFixedWidth(180)
        self._slider.setToolTip("Drag to zoom")
        self._slider.valueChanged.connect(self._on_slider_changed)
        layout.addWidget(self._slider)

        self._plus_btn = QPushButton("+")
        self._plus_btn.setToolTip("Zoom in (one step)")
        self._plus_btn.clicked.connect(self.zoom_in_requested.emit)
        layout.addWidget(self._plus_btn)

        layout.addStretch(1)

        self._pct_label = QLabel("100%")
        self._pct_label.setMinimumWidth(48)
        self._pct_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        # Monospace digits → no width jitter on each scroll tick.
        pf = QFont("Menlo")
        pf.setStyleHint(QFont.StyleHint.Monospace)
        pf.setPointSize(10)
        self._pct_label.setFont(pf)
        layout.addWidget(self._pct_label)

        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(32)

    # ---- refresh ------------------------------------------------------

    def _refresh(self) -> None:
        # Percent — floor instead of round so the displayed value
        # doesn't bounce between e.g. 8% / 9% when the true scale sits
        # near a half-integer percent boundary and viewport-size jitter
        # (splitter drag, scrollbar appearing) nudges it across.
        self._pct_label.setText(f"{math.floor(self._current * 100)}%")

        # Slider position
        self._suppress_slider = True
        self._slider.setValue(self._scale_to_tick(self._current))
        self._suppress_slider = False
        # If fit == max (i.e. fit >= 1.0, image smaller than viewport),
        # the slider has no usable range. Disable to communicate that.
        self._slider.setEnabled(self._max > self._fit + _EQ_EPSILON)

        # Step buttons
        self._minus_btn.setEnabled(self._current > self._fit + _EQ_EPSILON)
        self._plus_btn.setEnabled(self._current < self._max - _EQ_EPSILON)

        # Named-level active highlights — native checkable button state.
        self._fit_btn.setChecked(abs(self._current - self._fit) < _EQ_EPSILON)
        self._one_btn.setChecked(abs(self._current - 1.0) < _EQ_EPSILON)

    # ---- slider <-> log-scale mapping --------------------------------

    def _scale_to_tick(self, scale: float) -> int:
        if self._max <= self._fit:
            return 0
        # Log-linear interpolation across [fit, max].
        lo = math.log(self._fit)
        hi = math.log(self._max)
        t = (math.log(max(scale, 1e-6)) - lo) / (hi - lo)
        return int(round(max(0.0, min(1.0, t)) * _SLIDER_TICKS))

    def _tick_to_scale(self, tick: int) -> float:
        if self._max <= self._fit:
            return self._fit
        t = tick / _SLIDER_TICKS
        lo = math.log(self._fit)
        hi = math.log(self._max)
        return math.exp(lo + t * (hi - lo))

    # ---- slot ---------------------------------------------------------

    def _on_slider_changed(self, value: int) -> None:
        if self._suppress_slider:
            return
        self.absolute_zoom_requested.emit(self._tick_to_scale(value))
