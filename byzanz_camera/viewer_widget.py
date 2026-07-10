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
from PyQt6.QtWidgets import QStackedWidget, QVBoxLayout, QWidget

from .helpers import set_state
from .photo_viewer import PhotoViewer
from .spinner import Spinner
from .zoom_control_bar import ZoomControlBar  # type: ignore  # used in attach_zoom_bar


# ---- view-state pill (corner indicator) ---------------------------------

class PillBadge(QWidget):
    """Custom-painted pill badge — the viewer mode indicator, and reusable
    by host apps for their own corner overlays (see `add_corner_overlay`;
    e.g. papyri's overlap-coach readout).

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

    Host apps can inject an extra "overlay" page (e.g. a "no object
    open" CTA) via `set_overlay_widget(...)` and toggle visibility
    with `show_overlay()` / `show_photo()`. Border-tint per viewState
    is property-driven against the host's stylesheet — see
    `#photoViewer[viewState="live"]` etc.
    """
    # Pill colors — cyan for live (red would collide with IR's amber
    # identity), amber for paused, grey for preview. Kept here because
    # the pill is a self-painted widget (not QSS-themable).
    _LIVE_DOT_COLOR    = "#06b6d4"
    _PAUSED_ICON_COLOR = "#fbbf24"
    _PREVIEW_BORDER    = "#94a3b8"
    _PILL_INSET = 12
    _SPINNER_SIZE = 120
    _VALID_VIEW_STATES = ("live", "paused", "preview", "empty")

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Photo lives in a QStackedWidget so host apps can inject
        # an optional "overlay" page (e.g. a no-object CTA) via
        # set_overlay_widget — show via show_overlay / show_photo.
        self.photo_viewer = PhotoViewer(self)
        self.photo_viewer.setObjectName("photoViewer")
        self._viewer_stack = QStackedWidget(self)
        self._viewer_stack.addWidget(self.photo_viewer)
        layout.addWidget(self._viewer_stack, 1)
        self._overlay_widget: QWidget | None = None

        # Zoom bar is hosted externally — the .ui declares a
        # ZoomControlBar somewhere in the surrounding layout and the
        # host wires it via `attach_zoom_bar`.
        self.zoom_bar: ZoomControlBar | None = None

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
        self._pill = PillBadge(self.photo_viewer)

        # Host-supplied corner overlays (see add_corner_overlay).
        self._corner_overlays: list[tuple[QWidget, str]] = []

        # Busy spinner — same reasoning as the pill: child of the scroll
        # area, positioned relative to the viewport rect.
        self._spinner = Spinner(self.photo_viewer, Spinner.m_light_color)
        self._spinner.isAnimated = False
        self._reposition_spinner()

        self.photo_viewer.viewport().installEventFilter(self)
        # Initial view-state paint — sets viewState property + pill.
        self.set_view_state("empty")

    # ---- public API -----------------------------------------------------

    def attach_zoom_bar(self, zoom_bar: ZoomControlBar) -> None:
        """Wire a `ZoomControlBar` placed elsewhere in the host's
        layout to this viewer's photo_viewer. Bidirectional: the bar
        mirrors whatever transform the viewer holds, and bar actions
        drive the viewer's scale. Call once after `loadUi`.

        Fit / 1:1 use the animated variants — discrete jumps where
        the transition helps orientation. ± step and slider drag stay
        instant (animating those feels laggy when driven actively)."""
        self.zoom_bar = zoom_bar
        self.photo_viewer.zoom_changed.connect(self._sync_zoom_bar)
        zoom_bar.fit_requested.connect(self.photo_viewer.animated_fit_in_view)
        zoom_bar.one_to_one_requested.connect(
            self.photo_viewer.animated_to_one_to_one
        )
        zoom_bar.zoom_in_requested.connect(self.photo_viewer.zoomPlus)
        zoom_bar.zoom_out_requested.connect(self.photo_viewer.zoomMinus)
        zoom_bar.absolute_zoom_requested.connect(
            self.photo_viewer.set_absolute_scale
        )
        zoom_bar.set_photo_present(False)

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
        if self.zoom_bar is not None:
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
        """Update the corner-pill + border tint. `mode` ∈
        {live, paused, preview, empty}. `label` is shown inside the
        pill for "preview" (typically the file stem); ignored
        otherwise. Border styles live in the host app's stylesheet
        against `#photoViewer[viewState="..."]`."""
        if mode not in self._VALID_VIEW_STATES:
            return
        self._view_state = mode
        self._view_state_label = label
        set_state(self.photo_viewer, "viewState", mode)
        self._refresh_pill()

    def set_overlay_widget(self, widget: QWidget) -> None:
        """Install a host-supplied widget as the "non-photo" page of
        the viewer (e.g. a "no object open" CTA). Shown via
        `show_overlay()`, hidden via `show_photo()`. Pass the same
        widget again to replace; pass `None` to remove."""
        if self._overlay_widget is not None:
            self._viewer_stack.removeWidget(self._overlay_widget)
            self._overlay_widget.setParent(None)
        self._overlay_widget = widget
        if widget is not None:
            self._viewer_stack.addWidget(widget)

    def show_overlay(self) -> None:
        """Switch to the host-installed overlay page (no-op if none
        installed). Hides the corner pill while shown."""
        if self._overlay_widget is None:
            return
        self._viewer_stack.setCurrentWidget(self._overlay_widget)
        self._pill.hide()

    def show_photo(self) -> None:
        """Switch back to the photo page. Pill reappears per the
        current view-state."""
        self._viewer_stack.setCurrentWidget(self.photo_viewer)
        self._refresh_pill()

    def clear(self) -> None:
        """Reset to defaults: blank viewer + hide pill. Single chokepoint
        for "wipe everything"."""
        self.show_image(None)
        self.set_view_state("empty")

    def add_corner_overlay(self, widget: QWidget, corner: str = "top-left") -> None:
        """Pin a host-supplied widget to a corner of the viewport (e.g.
        papyri's overlap-coach pill). Mechanism only — content and
        show/hide stay with the host. `corner` ∈ {top-left, top-right,
        bottom-left, bottom-right}; top-right is where the built-in
        view-state pill lives. Repositioned on viewport resize and on the
        widget's own resize/show (so a setText → adjustSize re-pins it)."""
        widget.setParent(self.photo_viewer)
        widget.installEventFilter(self)
        self._corner_overlays.append((widget, corner))
        self._reposition_corner_overlays()

    def set_mirror_graphics_view(self, view) -> None:
        """Route the same scene to a second QGraphicsView (e.g. an external
        screen mirror for dome alignment). Thin pass-through to the
        underlying PhotoViewer."""
        self.photo_viewer.setMirrorView(view)

    # ---- internals ------------------------------------------------------

    def _refresh_pill(self) -> None:
        """Sync pill visibility/content with the current view state.
        Border tint on the photo viewer is QSS-driven (see set_view_state)."""
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
            self._pill.set_border_color(self._PREVIEW_BORDER)
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
        if self.zoom_bar is None:
            return
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

    def _reposition_corner_overlays(self) -> None:
        vp_rect = self.photo_viewer.viewport().geometry()
        inset = self._PILL_INSET
        for widget, corner in self._corner_overlays:
            x = (vp_rect.left() + inset if corner.endswith("left")
                 else max(0, vp_rect.right() - widget.width() - inset))
            y = (vp_rect.top() + inset if corner.startswith("top")
                 else max(0, vp_rect.bottom() - widget.height() - inset))
            widget.move(x, y)
            widget.raise_()

    def eventFilter(self, obj, event):
        if (obj is self.photo_viewer.viewport()
                and event.type() == QEvent.Type.Resize):
            self._reposition_pill()
            self._reposition_spinner()
            self._reposition_corner_overlays()
        elif (any(obj is w for w, _ in self._corner_overlays)
                and event.type() in (QEvent.Type.Resize, QEvent.Type.Show)):
            self._reposition_corner_overlays()
        return super().eventFilter(obj, event)
