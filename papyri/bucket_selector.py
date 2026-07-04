"""BucketSelector — grouped-tab replacement for WorkflowStepper.

Two QTabBars side-by-side (Visible | Infrared), each with two tabs
(side A and B). Each tab is a card with a small thumb on the left
(chosen-take thumbnail, when present) and a side label + take count
on the right. The user clicks a tab to activate that bucket;
exactly one tab across both bars is "globally active". The active
tab's bottom edge visually fuses into the FusingPanel below.

Public API: `set_groups`, `set_active`, `set_chosen_thumb`,
`step_clicked` signal. Same WorkflowGroup / WorkflowStep input model
as the legacy WorkflowStepper so MainWindow can pass the same dataclass.

Chosen-thumb support is opt-in via set_chosen_thumb(step_id, pixmap)
— passing None or omitting it leaves the tab in its empty state.
"""
from __future__ import annotations
from typing import Optional, Sequence

from PyQt6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QFontMetricsF, QImage, QMouseEvent, QPainter,
    QPainterPath, QPen, QPixmap, QRadialGradient,
)
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QSizePolicy, QTabBar, QVBoxLayout, QWidget,
)

# We borrow the WorkflowGroup/WorkflowStep input dataclasses so the
# caller (MainWindow) can pass exactly the same data structure used
# with WorkflowStepper. Each group's `base_color` is honored as the
# accent for that group's tab cards + the fusing-panel border when
# one of its tabs is active (so VIS-active draws blue chrome, IR-
# active draws orange) — matches the camera-state pill colours.
from papyri.workflow_stepper import WorkflowGroup, WorkflowStep


# ---- palette / sizing -----------------------------------------------------

# Theme-independent — the slate-teal default accent is used when no
# group has been bound yet (e.g. initial paint before set_groups).
_ACCENT_FALLBACK = QColor("#1c4a48")


def _palette_qcolor(key: str) -> QColor:
    """Read a color from the active app palette at paint time so the
    cards / fusing panel track dark-mode switches automatically. See
    `papyri.styles.current_palette()`."""
    from papyri.styles import current_palette
    return QColor(current_palette()[key])

CARD_W   = 168
CARD_H   = 56
THUMB_W  = 48
THUMB_H  = 32                              # 3:2
THUMB_PAD = 6
INTER_TAB_GAP   = 6                        # gap between adjacent tabs in a bar
INTER_GROUP_GAP = 28                       # gap between the two spectrum groups
GROUP_HEADER_H = 18


# ---- internal helpers -----------------------------------------------------

def _placeholder_thumb(rect: QRectF, side: str, p: QPainter) -> None:
    """Render a gradient placeholder when the bucket has a chosen-thumb
    flag set but no real pixmap has been supplied yet."""
    g = QRadialGradient(rect.center(), rect.width() * 0.7)
    if side == "A":
        g.setColorAt(0.0, QColor("#d2b58d"))
        g.setColorAt(1.0, QColor("#6b5439"))
    else:
        g.setColorAt(0.0, QColor("#bba07c"))
        g.setColorAt(1.0, QColor("#5f4a31"))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(g))
    p.drawRoundedRect(rect, 2, 2)


# ---- BucketTabBar ---------------------------------------------------------

class _BucketTab:
    """Internal per-tab state stored as tabData on the BucketTabBar."""
    __slots__ = ("step_id", "label", "chosen_thumb", "side")

    def __init__(self, step_id: str, label: str, side: str):
        self.step_id = step_id
        self.label = label
        self.side = side
        self.chosen_thumb: Optional[QPixmap] = None


class BucketTabBar(QTabBar):
    """QTabBar with each tab custom-painted as a bucket card.

    `inactive_group` makes ALL tabs render as inactive regardless of
    currentIndex — used by BucketSelector for the non-active group so
    only one tab globally appears active across both bars.
    """

    user_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDrawBase(False)
        self.setExpanding(False)
        self.setDocumentMode(True)
        self.setMouseTracking(True)
        self._inactive_group = False
        # Whether to render the chosen-take thumb slot. Off in simple mode,
        # where the cards are a plain VIS/IR camera switch (no buckets).
        self._show_thumbs = True
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # The group's accent — used for the ACTIVE card's border + text
        # + VIS/IR badge fill, and queried by FusingPanel for its
        # frame. Set by BucketSelector.set_groups from
        # `WorkflowGroup.base_color`.
        self._accent_color: QColor = _ACCENT_FALLBACK
        # Soft variants for INACTIVE card badges — same hue family,
        # low weight so the four-tab strip doesn't pulse with
        # competing saturated badges.
        self._soft_color: QColor = QColor("#e2e8f0")
        self._soft_text: QColor = QColor("#475569")
        # Short spectrum label ("VIS" / "IR") rendered as a small pill
        # on each card, matching the camera-state widget's badge.
        self._short_label: str = ""

    def accent_color(self) -> QColor:
        return self._accent_color

    def set_accent_color(self, color: QColor) -> None:
        self._accent_color = color
        self.update()

    def set_soft_colors(self, bg: QColor, text: QColor) -> None:
        """Inactive-state badge colors (low-emphasis variant of the
        group accent). Typically `WorkflowGroup.bg_done` + `text_dark`."""
        self._soft_color = bg
        self._soft_text = text
        self.update()

    def short_label(self) -> str:
        return self._short_label

    def set_short_label(self, label: str) -> None:
        self._short_label = label
        self.update()

    def set_show_thumbs(self, show: bool) -> None:
        self._show_thumbs = show
        self.update()

    def set_inactive_group(self, inactive: bool) -> None:
        if inactive == self._inactive_group:
            return
        self._inactive_group = inactive
        self.update()

    def is_inactive_group(self) -> bool:
        return self._inactive_group

    def tabSizeHint(self, index: int) -> QSize:
        # First tab: width == CARD_W (no leading gap — its card sits flush
        # with the bar's left edge, which aligns with the panel's left
        # border). Subsequent tabs: width == CARD_W + INTER_TAB_GAP, with
        # the gap painted on the LEFT of the card so a visible gap appears
        # between cards.
        return QSize(CARD_W if index == 0 else CARD_W + INTER_TAB_GAP, CARD_H)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self.user_clicked.emit()

    # ---- queries for the panel -----------------------------------------

    def active_tab_card_rect(self) -> Optional[QRectF]:
        idx = self.currentIndex()
        if idx < 0:
            return None
        return self._card_rect_in_bar(idx)

    def _card_rect_in_bar(self, index: int) -> QRectF:
        """Visible card rect for `index` in bar-local coords. The inter-tab
        gap is painted on the LEFT of non-first tabs (so the first tab
        sits flush with the bar's left edge)."""
        r = QRectF(self.tabRect(index))
        left_inset = 0 if index == 0 else INTER_TAB_GAP
        return QRectF(r.x() + left_inset, r.y(), CARD_W, r.height())

    def step_id_at(self, index: int) -> Optional[str]:
        data = self.tabData(index)
        return data.step_id if isinstance(data, _BucketTab) else None

    def index_of_step(self, step_id: str) -> int:
        for i in range(self.count()):
            data = self.tabData(i)
            if isinstance(data, _BucketTab) and data.step_id == step_id:
                return i
        return -1

    # ---- painting ------------------------------------------------------

    def paintEvent(self, _evt) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Whole-bar dim when the host disables the selector (e.g. no
        # object loaded). Qt already blocks input on disabled widgets;
        # this just gives a visual cue.
        if not self.isEnabled():
            p.setOpacity(0.45)

        active_idx = -1 if self._inactive_group else self.currentIndex()
        for i in range(self.count()):
            data = self.tabData(i)
            if not isinstance(data, _BucketTab):
                continue
            self._paint_card(p, data, self._card_rect_in_bar(i),
                             active=(i == active_idx))

    def _paint_card(self, p: QPainter, data: _BucketTab, rect: QRectF,
                    *, active: bool) -> None:
        has_thumb = (data.chosen_thumb is not None
                     and not data.chosen_thumb.isNull())
        # In no-thumb (camera-switch) mode, inactive cards must read as
        # full-strength labels, not dimmed empty buckets.
        dimmed = (not has_thumb and not active) if self._show_thumbs else False

        radius = 5
        stroke = 1.5 if active else 1.0
        body = QRectF(
            rect.x() + stroke / 2,
            rect.y() + stroke / 2,
            rect.width() - stroke,
            rect.height(),         # bottom flush — fuses with the panel
        )

        # Theme-aware palette — fetched once per paint so dark/light
        # toggles are picked up automatically.
        bg_card = _palette_qcolor("bg_card")
        line = _palette_qcolor("line")
        ink = _palette_qcolor("ink")
        ink_3 = _palette_qcolor("ink_3")

        # Fill.
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(bg_card))
        p.drawRoundedRect(body, radius, radius)

        # Border: rounded top, two verticals, NO bottom segment.
        path = QPainterPath()
        path.moveTo(body.left(), body.bottom())
        path.lineTo(body.left(), body.top() + radius)
        path.arcTo(QRectF(body.left(), body.top(), 2 * radius, 2 * radius),
                   180, -90)
        path.lineTo(body.right() - radius, body.top())
        path.arcTo(QRectF(body.right() - 2 * radius, body.top(),
                          2 * radius, 2 * radius), 90, -90)
        path.lineTo(body.right(), body.bottom())

        p.setPen(QPen(self._accent_color if active else line, stroke))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        if not active:
            p.drawLine(QPointF(body.left(), body.bottom()),
                       QPointF(body.right(), body.bottom()))

        # Thumb area (left) — omitted in camera-switch mode, where the
        # labels start flush at the card's left padding instead.
        if self._show_thumbs:
            thumb_rect = QRectF(rect.x() + THUMB_PAD,
                                rect.y() + (rect.height() - THUMB_H) / 2,
                                THUMB_W, THUMB_H)
            if not has_thumb:
                p.setPen(QPen(line, 1.0, Qt.PenStyle.DashLine))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(thumb_rect, 2, 2)
            else:
                pix: QPixmap = data.chosen_thumb
                # Empty placeholder (zero alpha) → render gradient stand-in.
                if (pix.size() == QSize(THUMB_W, THUMB_H)
                        and pix.toImage().pixelColor(0, 0).alpha() == 0):
                    _placeholder_thumb(thumb_rect, data.side, p)
                else:
                    p.drawPixmap(thumb_rect.toRect(),
                                 pix.scaled(THUMB_W, THUMB_H,
                                            Qt.AspectRatioMode.KeepAspectRatio,
                                            Qt.TransformationMode.SmoothTransformation))

        # Labels (right) — badge + side label, both vertically centered
        # in the card. Active card gets saturated accent badge; inactive
        # cards get the soft variant so the four-tab strip doesn't pulse
        # with competing saturated badges.
        text_x = (thumb_rect.right() + 10) if self._show_thumbs else (rect.x() + 14)
        badge_h = 16
        badge_label = self._short_label
        if badge_label:
            badge_font = p.font()
            badge_font.setPointSize(9)
            badge_font.setBold(True)
            badge_text_w = QFontMetricsF(badge_font).horizontalAdvance(badge_label)
            badge_rect = QRectF(
                text_x,
                rect.y() + (rect.height() - badge_h) / 2,
                badge_text_w + 12, badge_h,
            )
            if active:
                badge_bg, badge_fg = self._accent_color, QColor("white")
            else:
                badge_bg, badge_fg = self._soft_color, self._soft_text
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(badge_bg))
            p.drawRoundedRect(badge_rect, 4, 4)
            p.setPen(badge_fg)
            p.setFont(badge_font)
            p.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, badge_label)
            text_x = badge_rect.right() + 6

        text_w = rect.right() - text_x - 8
        font = p.font()
        font.setPointSize(12)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QPen(self._accent_color if active
                      else (ink_3 if dimmed else ink)))
        p.drawText(QRectF(text_x, rect.y(), text_w, rect.height()),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   data.label)


# ---- BucketSelector (drop-in for WorkflowStepper) -------------------------

class BucketSelector(QWidget):
    """Two BucketTabBars (Visible | Infrared) coordinated so one tab is
    globally active. Public API:

        set_groups(groups)          configure the buckets
        set_active(step_id | None)  set/clear the active tab
        set_chosen_thumb(step_id, pixmap | None)
                                    update the thumb on a tab
        step_clicked(str)           signal emitted on user click

    Calls into FusingPanel via set_fusing_panel() to drive the panel's
    border-with-gap repaint when the active tab changes.
    """

    step_clicked = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._bars: list[BucketTabBar] = []
        self._panel: Optional["FusingPanel"] = None
        # Whether the cards show chosen-take thumbs. Off → camera-switch
        # look. Set before set_groups so new bars pick it up.
        self._show_thumbs = True
        self._build_chrome()

    # ---- public API ----------------------------------------------------

    def set_show_thumbs(self, show: bool) -> None:
        """Toggle chosen-take thumbs on the cards. Off turns the selector
        into a plain VIS/IR camera switch (simple mode). Call before
        set_groups; also applied to any already-built bars."""
        self._show_thumbs = show
        for bar in self._bars:
            bar.set_show_thumbs(show)

    def set_groups(self, groups: Sequence[WorkflowGroup]) -> None:
        """Configure the bars from the same WorkflowGroup list used
        with WorkflowStepper. One column per group; each column has
        a colored header label stacked above the tab bar so the label
        always sits above its bar's first tab regardless of how the
        window resizes."""
        # Wipe existing group columns (children of self._groups_layout).
        while self._groups_layout.count():
            item = self._groups_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._bars = []

        for group in groups:
            accent = QColor(group.base_color)
            self._groups_layout.addWidget(self._build_group_column(group, accent))
        self._groups_layout.addStretch(1)

        # Initialize: first tab of first bar is the globally active one,
        # but no step_clicked is emitted (caller drives set_active).
        if self._bars:
            for bar in self._bars[1:]:
                bar.set_inactive_group(True)

    def _build_group_column(
        self, group: WorkflowGroup, accent: QColor,
    ) -> QWidget:
        """A [header label, tab bar] column for one group. The column
        widget hugs its content (fixed width = bar width) so columns
        stay put when the parent resizes."""
        column = QWidget(self)
        column.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred,
        )
        col_layout = QVBoxLayout(column)
        col_layout.setContentsMargins(0, 0, 0, 0)
        col_layout.setSpacing(0)

        header = QLabel(
            f"<span style='color:{accent.name()};letter-spacing:1.5px;"
            f"font-weight:600;'>{group.label.upper()}</span>"
        )
        # Font size + padding rules live in papyri/ui/app.qss against
        # the #bucketGroupHeader object name. The accent color stays
        # inline in the HTML span because it's per-instance and
        # bucket-selector painting is custom (not QSS-themable).
        header.setObjectName("bucketGroupHeader")
        col_layout.addWidget(header)

        bar = BucketTabBar(self)
        bar.set_accent_color(accent)
        bar.set_soft_colors(QColor(group.bg_done), QColor(group.text_dark))
        bar.set_short_label(group.short_label)
        bar.set_show_thumbs(self._show_thumbs)
        for step in group.steps:
            side = "A" if "A" in step.label else "B"
            idx = bar.addTab("")
            bar.setTabData(idx, _BucketTab(step.id, step.label, side))
        bar.user_clicked.connect(lambda b=bar: self._on_user_clicked(b))
        bar.currentChanged.connect(
            lambda _i, b=bar: self._on_current_changed(b)
        )
        self._bars.append(bar)
        col_layout.addWidget(bar)
        return column

    def set_active(self, step_id: Optional[str]) -> None:
        if step_id is None:
            for bar in self._bars:
                bar.set_inactive_group(True)
            self._refresh_panel()
            return
        for bar in self._bars:
            idx = bar.index_of_step(step_id)
            if idx < 0:
                bar.set_inactive_group(True)
                continue
            bar.set_inactive_group(False)
            # Avoid bouncing through currentChanged → step_clicked when
            # the caller is just reflecting state.
            blocked = bar.blockSignals(True)
            bar.setCurrentIndex(idx)
            bar.blockSignals(blocked)
        self._refresh_panel()

    def set_chosen_thumb(self, step_id: str, pixmap: Optional[QPixmap]) -> None:
        for bar in self._bars:
            idx = bar.index_of_step(step_id)
            if idx < 0:
                continue
            data = bar.tabData(idx)
            if isinstance(data, _BucketTab):
                data.chosen_thumb = pixmap
                bar.update()
            return

    def set_fusing_panel(self, panel: "FusingPanel") -> None:
        """The panel asks the selector for the active tab's geometry to
        paint the gap in its top border. Pair them via this setter."""
        self._panel = panel
        self._refresh_panel()

    def active_bar(self) -> Optional[BucketTabBar]:
        for bar in self._bars:
            if not bar.is_inactive_group():
                return bar
        return None

    def active_accent_color(self) -> QColor:
        """Accent color of the active group (VIS-blue / IR-orange).
        Falls back to the default slate when nothing is active."""
        bar = self.active_bar()
        return bar.accent_color() if bar is not None else _ACCENT_FALLBACK

    # ---- internals -----------------------------------------------------

    def _build_chrome(self) -> None:
        """One horizontal row of vertical group-columns. Each column is
        a QWidget holding [header label, tab bar] stacked, so the
        header always sits flush above its own bar's first tab — no
        resize-time math needed. A trailing stretch packs all columns
        to the left."""
        self._groups_layout = QHBoxLayout(self)
        self._groups_layout.setContentsMargins(0, 0, 0, 0)
        self._groups_layout.setSpacing(INTER_GROUP_GAP)

    def _on_user_clicked(self, clicked_bar: BucketTabBar) -> None:
        # The clicked bar becomes globally active. The other bars deactivate.
        for bar in self._bars:
            bar.set_inactive_group(bar is not clicked_bar)
        # Emit step_clicked for the (already-updated) current tab.
        step_id = clicked_bar.step_id_at(clicked_bar.currentIndex())
        if step_id is not None:
            self.step_clicked.emit(step_id)
        self._refresh_panel()

    def _on_current_changed(self, bar: BucketTabBar) -> None:
        # Only emit step_clicked if THIS bar is the active group (arrow-key
        # navigation within the active bar). Inactive bars' currentChanged
        # is suppressed.
        if bar.is_inactive_group():
            self._refresh_panel()
            return
        step_id = bar.step_id_at(bar.currentIndex())
        if step_id is not None:
            self.step_clicked.emit(step_id)
        self._refresh_panel()

    def _refresh_panel(self) -> None:
        if self._panel is not None:
            self._panel.update()


# ---- FusingPanel ---------------------------------------------------------

class FusingPanel(QFrame):
    """QFrame container whose top border has a gap where the active tab
    of the paired BucketSelector sits — making the active tab visually
    flow into the panel as one continuous surface.

    Hosts the existing capture-area widgets (viewer + capture-controls
    row + filmstrip) via a normal QVBoxLayout, so the panel doesn't
    care about what's inside — only the painted frame is special.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAutoFillBackground(False)
        self._selector: Optional[BucketSelector] = None

    def set_bucket_selector(self, selector: BucketSelector) -> None:
        self._selector = selector

    # ---- painting ------------------------------------------------------

    def paintEvent(self, _evt) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), _palette_qcolor("bg_card"))

        y_top = 0.75
        accent = (self._selector.active_accent_color()
                  if self._selector is not None else _ACCENT_FALLBACK)
        p.setPen(QPen(accent, 1.5))
        x_range = self._active_tab_x_range()
        if x_range is None:
            p.drawLine(QPointF(0, y_top), QPointF(self.width(), y_top))
        else:
            gap_l, gap_r = x_range
            p.drawLine(QPointF(0, y_top), QPointF(gap_l, y_top))
            p.drawLine(QPointF(gap_r, y_top), QPointF(self.width(), y_top))
        p.drawLine(QPointF(0.75, 0), QPointF(0.75, self.height()))
        p.drawLine(QPointF(self.width() - 0.75, 0),
                   QPointF(self.width() - 0.75, self.height()))
        p.drawLine(QPointF(0, self.height() - 0.75),
                   QPointF(self.width(), self.height() - 0.75))

    def _active_tab_x_range(self) -> Optional[tuple[float, float]]:
        if self._selector is None:
            return None
        bar = self._selector.active_bar()
        if bar is None or not bar.isVisible():
            return None
        rect = bar.active_tab_card_rect()
        if rect is None:
            return None
        # Bar and panel are NOT in an ancestor/descendant relationship —
        # they're siblings under the same main column. mapTo() would
        # crash; use the global-coords roundtrip instead.
        topl_g = bar.mapToGlobal(QPoint(int(rect.left()), 0))
        topr_g = bar.mapToGlobal(QPoint(int(rect.right()), 0))
        topl = self.mapFromGlobal(topl_g)
        topr = self.mapFromGlobal(topr_g)
        return float(topl.x()), float(topr.x())
