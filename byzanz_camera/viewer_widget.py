"""ViewerWidget — pure photo display with optional state-indicator pill.

Displays whatever pixmap is handed to it (live frame or decoded capture).
Doesn't know about directories, files, async loading, or the session —
pure sink. All logic about *what* to display lives in the caller.

Companion to FilmstripWidget; together they replace the older monolithic
PhotoBrowser so the parent layout can interleave other widgets (e.g.
capture controls) between the viewer and the thumbnail strip.
"""
from __future__ import annotations

from PyQt6.QtCore import QEvent, QRectF, QSize, Qt
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QFontMetrics, QPainter, QPen, QPixmap,
)
from PyQt6.QtWidgets import QVBoxLayout, QWidget

from .photo_viewer import PhotoViewer


# ---- view-state pill (corner indicator) ---------------------------------

class _ViewStatePill(QWidget):
    """Custom-painted pill badge for the viewer mode indicator.

    QSS-styled QLabel proved unreliable across Qt6/macOS — `border-radius`
    + transparent rgba background combinations either render inconsistently
    or silently drop the chrome entirely. A self-painting widget bypasses
    QSS entirely, which is the only reliable way to get the rounded dark
    pill look across platforms.
    """
    PAD_X = 12
    PAD_Y = 6
    RADIUS = 11

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ""
        self._bg = QColor(15, 23, 42, 220)         # semi-transparent slate-900
        self._fg = QColor("white")
        self._border_color: QColor | None = None
        self._border_w = 1.5
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.hide()

    def set_border_color(self, color: str | QColor | None) -> None:
        new = QColor(color) if color and not isinstance(color, QColor) else color
        if (self._border_color is None) == (new is None) and new == self._border_color:
            return
        self._border_color = new
        self.update()

    def setText(self, text: str) -> None:
        if self._text == text:
            return
        self._text = text
        self.adjustSize()
        self.update()

    def _font(self) -> QFont:
        font = QFont()
        font.setBold(True)
        font.setPointSize(9)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
        return font

    def sizeHint(self) -> QSize:
        if not self._text:
            return QSize(0, 0)
        fm = QFontMetrics(self._font())
        return QSize(
            fm.horizontalAdvance(self._text) + 2 * self.PAD_X,
            fm.height() + 2 * self.PAD_Y,
        )

    def paintEvent(self, event) -> None:
        if not self._text:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bw = self._border_w if self._border_color is not None else 0
        half = bw / 2
        rect = QRectF(half, half, self.width() - bw, self.height() - bw)
        p.setBrush(QBrush(self._bg))
        if self._border_color is not None:
            p.setPen(QPen(self._border_color, bw))
        else:
            p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(rect, self.RADIUS, self.RADIUS)
        p.setFont(self._font())
        p.setPen(QPen(self._fg))
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._text)


# ---- the widget --------------------------------------------------------

class ViewerWidget(QWidget):
    """Pure photo display. Show a pixmap, optionally with a state pill.

    No state beyond what's currently rendered. Sink only — doesn't emit
    signals, doesn't know about directories, files, or async loading.
    Caller handles all decision logic about what to display.

    Pill colors per state — cyan for live (red would collide with IR's
    amber identity), amber for paused, grey for preview.
    """
    _VIEW_STATE_BORDERS = {
        "live":    "1.5px solid #06b6d4",
        "paused":  "1px dashed #cbd5e1",
        "preview": "1.5px solid #94a3b8",
        "empty":   "1px solid #e2e8f0",
    }
    _LIVE_DOT_COLOR    = "#06b6d4"
    _PAUSED_ICON_COLOR = "#fbbf24"
    _PILL_INSET = 12

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.photo_viewer = PhotoViewer(self)
        self.photo_viewer.setObjectName("photoViewer")
        layout.addWidget(self.photo_viewer)

        # Corner pill — parented to the QGraphicsView's viewport so the
        # pill's local coords are just (viewport.width - pill.width - inset,
        # inset), never overlapping a scrollbar and clipped to the visible
        # image area automatically.
        self._view_state: str = "empty"
        self._view_state_label: str = ""
        self._pill = _ViewStatePill(self.photo_viewer.viewport())
        self.photo_viewer.viewport().installEventFilter(self)
        self._refresh_indicator()

    # ---- public API -----------------------------------------------------

    def show_image(self, pixmap: QPixmap | None, *, fit: bool = False) -> None:
        """Display a pixmap (or clear with None). `fit=True` re-scales to
        the viewport — appropriate for live frames where the viewer should
        always show the full frame. `fit=False` (default) preserves the
        user's current zoom — appropriate for clicked thumbnails."""
        self.photo_viewer.setPhoto(pixmap)
        if pixmap is not None and fit:
            self.photo_viewer.fitInView()

    def set_view_state(self, mode: str, label: str = "") -> None:
        """Update the corner-pill / border-tint indicator. `mode` ∈
        {live, paused, preview, empty}. `label` is shown inside the pill
        for "preview" (typically the file stem); ignored for other modes."""
        if mode not in self._VIEW_STATE_BORDERS:
            return
        self._view_state = mode
        self._view_state_label = label
        self._refresh_indicator()

    def clear(self) -> None:
        """Reset to defaults: blank viewer + hide pill. Single chokepoint
        for "wipe everything"."""
        self.show_image(None)
        self.set_view_state("empty")

    # ---- internals ------------------------------------------------------

    def _refresh_indicator(self) -> None:
        # Border tint on the QGraphicsView itself.
        border = self._VIEW_STATE_BORDERS[self._view_state]
        self.photo_viewer.setStyleSheet(
            f"QGraphicsView#photoViewer {{ border: {border}; border-radius: 3px; }}"
        )
        # Pill content + accent border.
        if self._view_state == "empty":
            self._pill.hide()
            return
        if self._view_state == "live":
            self._pill.setText("● LIVE")
            self._pill.set_border_color(self._LIVE_DOT_COLOR)
        elif self._view_state == "paused":
            self._pill.setText("⏸ PAUSED")
            self._pill.set_border_color(self._PAUSED_ICON_COLOR)
        elif self._view_state == "preview":
            label = self._view_state_label or "Preview"
            self._pill.setText(f"📷 {label}")
            self._pill.set_border_color("#94a3b8")
        self._pill.adjustSize()
        self._pill.show()
        self._pill.raise_()
        self._reposition_pill()

    def _reposition_pill(self) -> None:
        if not self._pill.isVisible():
            return
        viewport = self.photo_viewer.viewport()
        self._pill.move(
            max(0, viewport.width() - self._pill.width() - self._PILL_INSET),
            self._PILL_INSET,
        )

    def eventFilter(self, obj, event):
        if (obj is self.photo_viewer.viewport()
                and event.type() == QEvent.Type.Resize):
            self._reposition_pill()
        return super().eventFilter(obj, event)
