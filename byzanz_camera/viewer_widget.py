"""ViewerWidget — pure photo display with optional state-indicator pill.

Displays whatever pixmap is handed to it (live frame or decoded capture).
Doesn't know about directories, files, async loading, or the session —
pure sink. All logic about *what* to display lives in the caller.

Companion to FilmstripWidget; together they replace the older monolithic
PhotoBrowser so the parent layout can interleave other widgets (e.g.
capture controls) between the viewer and the thumbnail strip.
"""
from __future__ import annotations

from PyQt6.QtCore import QEvent, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QFontMetrics, QPainter, QPen, QPixmap,
)
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QStackedWidget,
    QVBoxLayout, QWidget,
)

from .photo_viewer import PhotoViewer
from .spinner import Spinner
from .zoom_control_bar import ZoomControlBar


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


# ---- no-object overlay card ---------------------------------------------

class _NoObjectCard(QFrame):
    """Centered card shown when the host app has no current object.
    Replaces the photo with a headline + CTA so the user has an
    obvious next step rather than staring at a blank viewer."""

    new_object_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("noObjectCard")
        self.setStyleSheet("""
            QFrame#noObjectCard {
                background: white;
                border: 1px solid #e2e8f0;
                border-radius: 12px;
            }
            QLabel#noObjectTitle {
                font-size: 18pt;
                font-weight: 600;
                color: #1c1c1c;
            }
            QLabel#noObjectSubtitle {
                font-size: 11pt;
                color: #5a5a5a;
            }
            QPushButton#noObjectCta {
                background: #1c4a48;
                color: white;
                padding: 8px 22px;
                border-radius: 6px;
                font-weight: 600;
                font-size: 11pt;
                border: none;
            }
            QPushButton#noObjectCta:hover {
                background: #2c5f5c;
            }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 32, 40, 32)
        layout.setSpacing(10)

        title = QLabel("No object open")
        title.setObjectName("noObjectTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel(
            "Start a new one or pick an existing one from the sidebar →"
        )
        subtitle.setObjectName("noObjectSubtitle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(subtitle)

        layout.addSpacing(8)

        button = QPushButton("Start new object")
        button.setObjectName("noObjectCta")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(self.new_object_clicked)
        layout.addWidget(button, 0, Qt.AlignmentFlag.AlignCenter)
        # Card hugs its content (don't stretch to fill the page).
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)


class _NoObjectPage(QWidget):
    """Fills the stacked-widget page; centers the card horizontally
    and vertically via stretches — pure layout, no geometry math.

    Matches the empty PhotoViewer's dark background + 1px border so
    the page swap is seamless (only the card differs visually)."""

    def __init__(self, card: _NoObjectCard, parent=None):
        super().__init__(parent)
        self.setObjectName("noObjectPage")
        # Plain QWidget doesn't paint stylesheet backgrounds without
        # this attribute (it defaults to "let the system style draw").
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("""
            QWidget#noObjectPage {
                background: rgb(30, 30, 30);
                border: 1px solid #e2e8f0;
                border-radius: 3px;
            }
        """)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(card)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(1)


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
        "live":      "1.5px solid #06b6d4",
        "paused":    "1px dashed #cbd5e1",
        "preview":   "1.5px solid #94a3b8",
        "empty":     "1px solid #e2e8f0",
        "no_object": "1px solid #e2e8f0",
    }
    _LIVE_DOT_COLOR    = "#06b6d4"
    _PAUSED_ICON_COLOR = "#fbbf24"
    _PILL_INSET = 12
    _SPINNER_SIZE = 120

    # Emitted when the user clicks the "Start new object" CTA in the
    # no_object overlay. The host wires this to whatever action focuses
    # the new-object input (e.g. title bar).
    new_object_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Photo + no-object CTA live as siblings in a QStackedWidget so
        # we can swap pages declaratively — no manual geometry sync for
        # the CTA card (its centering is layout-driven inside its page).
        self.photo_viewer = PhotoViewer(self)
        self.photo_viewer.setObjectName("photoViewer")

        self._no_object_card = _NoObjectCard()
        self._no_object_page = _NoObjectPage(self._no_object_card)

        self._viewer_stack = QStackedWidget(self)
        self._viewer_stack.addWidget(self.photo_viewer)   # index 0
        self._viewer_stack.addWidget(self._no_object_page)  # index 1
        layout.addWidget(self._viewer_stack, 1)

        # Zoom control bar — embedded so any host gets it for free.
        # Wired bidirectionally to the photo viewer so the bar mirrors
        # whatever transform the viewer holds (scroll wheel, fitInView,
        # ± click, slider drag — all go through the same source of
        # truth).
        self.zoom_bar = ZoomControlBar(self)
        layout.addWidget(self.zoom_bar, 0)
        self.photo_viewer.zoom_changed.connect(self._sync_zoom_bar)
        # Fit / 1:1 buttons get the animated variants — they're
        # discrete jumps where the transition helps orientation. The
        # ± step buttons and slider drag stay instant (animating
        # those feels laggy when the user is actively driving them).
        self.zoom_bar.fit_requested.connect(self.photo_viewer.animated_fit_in_view)
        self.zoom_bar.one_to_one_requested.connect(
            self.photo_viewer.animated_to_one_to_one
        )
        self.zoom_bar.zoom_in_requested.connect(self.photo_viewer.zoomPlus)
        self.zoom_bar.zoom_out_requested.connect(self.photo_viewer.zoomMinus)
        self.zoom_bar.absolute_zoom_requested.connect(
            self.photo_viewer.set_absolute_scale
        )

        # Corner pill — parented to the PhotoViewer itself (the scroll
        # area), NOT to its viewport. Parenting to the viewport would
        # make `QAbstractScrollArea.scrollContentsBy()` translate the
        # pill along with the scene during trackpad pan, drifting it
        # out of position. Parenting to the scroll area keeps it pinned
        # regardless of pan. We still use the viewport's geometry as
        # the position reference (the pill sits inside the viewport's
        # rect, not over the scrollbars).
        self._view_state: str = "empty"
        self._view_state_label: str = ""
        self._pill = _ViewStatePill(self.photo_viewer)

        # Busy spinner — same reasoning as the pill: child of the scroll
        # area, positioned relative to the viewport rect.
        self._spinner = Spinner(self.photo_viewer, Spinner.m_light_color)
        self._spinner.isAnimated = False
        self._reposition_spinner()

        # CTA button click → re-emit at the widget level.
        self._no_object_card.new_object_clicked.connect(
            self.new_object_requested
        )

        self.photo_viewer.viewport().installEventFilter(self)
        self._refresh_indicator()
        self.zoom_bar.set_photo_present(False)

    # ---- public API -----------------------------------------------------

    def show_image(self, pixmap: QPixmap | None, *, fit: bool = False) -> None:
        """Display a pixmap (or clear with None). `fit=True` re-scales to
        the viewport — appropriate for live frames where the viewer should
        always show the full frame. `fit=False` (default) preserves the
        user's current zoom — appropriate for clicked thumbnails. Also
        hides the busy spinner — an image arriving means the load done."""
        self.photo_viewer.setPhoto(pixmap)
        if pixmap is not None and fit:
            self.photo_viewer.fitInView()
        self._spinner.stopAnimation()
        self.zoom_bar.set_photo_present(pixmap is not None)
        # setPhoto / fitInView already emit zoom_changed, but cover the
        # no-photo and same-size cases (where setPhoto doesn't re-fit)
        # so the bar's mirror stays consistent.
        self._sync_zoom_bar()

    def show_busy(self) -> None:
        """Show the centered spinner overlay. Use during async loads
        (e.g. full-image decode of a clicked thumbnail) so the user
        gets feedback that something's happening."""
        self._reposition_spinner()
        self._spinner.startAnimation()

    def hide_busy(self) -> None:
        """Hide the spinner. Usually called implicitly by show_image."""
        self._spinner.stopAnimation()

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

    def set_mirror_graphics_view(self, view) -> None:
        """Route the same scene to a second QGraphicsView (e.g. an external
        screen mirror for dome alignment). Thin pass-through to the
        underlying PhotoViewer."""
        self.photo_viewer.setMirrorView(view)

    # ---- internals ------------------------------------------------------

    def _refresh_indicator(self) -> None:
        # Page swap: no_object → CTA page; anything else → photo page.
        # Done first so the pill below doesn't briefly show over the
        # wrong page during a transition.
        self._viewer_stack.setCurrentWidget(
            self._no_object_page if self._view_state == "no_object"
            else self.photo_viewer
        )
        # Border tint on the QGraphicsView itself.
        border = self._VIEW_STATE_BORDERS[self._view_state]
        self.photo_viewer.setStyleSheet(
            f"QGraphicsView#photoViewer {{ border: {border}; border-radius: 3px; }}"
        )
        # Pill content + accent border. Hidden for empty / no_object.
        if self._view_state in ("empty", "no_object"):
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
        # The pill is now a child of PhotoViewer, not its viewport, so
        # we position it in PhotoViewer coords using the viewport rect
        # as the reference (keeps the pill inside the image area and
        # off the scrollbars).
        vp_rect = self.photo_viewer.viewport().geometry()
        self._pill.move(
            max(0, vp_rect.right() - self._pill.width() - self._PILL_INSET),
            vp_rect.top() + self._PILL_INSET,
        )

    def _sync_zoom_bar(self, scale: float | None = None) -> None:
        """Push the photo viewer's current scale + fit-scale into the
        zoom bar. Called from photo_viewer.zoom_changed and also
        on-demand from show_image (which covers the no-photo case
        and the same-size-pixmap path where fitInView isn't fired)."""
        if scale is None:
            scale = self.photo_viewer.current_scale()
        self.zoom_bar.set_current_zoom(scale, self.photo_viewer.fit_scale())

    def _reposition_spinner(self) -> None:
        # Spinner is also a child of PhotoViewer now — center it on
        # the viewport rect (which excludes the scrollbars), expressed
        # in PhotoViewer coords.
        vp_rect = self.photo_viewer.viewport().geometry()
        size = self._SPINNER_SIZE
        x = vp_rect.left() + max(0, (vp_rect.width() - size) // 2)
        y = vp_rect.top() + max(0, (vp_rect.height() - size) // 2)
        self._spinner.setGeometry(x, y, size, size)
        self._spinner.raise_()

    def eventFilter(self, obj, event):
        if (obj is self.photo_viewer.viewport()
                and event.type() == QEvent.Type.Resize):
            self._reposition_pill()
            self._reposition_spinner()
        return super().eventFilter(obj, event)
