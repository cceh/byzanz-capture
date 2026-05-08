"""Chevron-stepper widget for ordered, grouped workflows.

Renders N groups of M steps as a horizontal flow of nested chevrons:

    ┌── Visible ──┐  ┌── Infrared ──┐
    [1│Side A][2│Side B][3│Side A][4│Side B]

Each step shows: step number medallion, group short label pill, step label,
and a textual status (PENDING / WORKING / COMPLETED · count). Step numbers
are auto-generated cumulatively across groups.

Generic enough to drop into any workflow with ordered steps in colored
groups. Click any step to activate it; the widget emits `step_clicked(id)`.

State of each step is derived inside the widget from two inputs:
  - count          (set via `set_count(step_id, n)`)
  - is_active      (set via `set_active(step_id)` — only one at a time)

Derivation:
  count == 0 + not active → pending
  count == 0 + active     → active
  count >  0 + not active → done
  count >  0 + active     → done_active
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Sequence

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont, QMouseEvent, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import QSizePolicy, QWidget


# ---- public data model ---------------------------------------------------

@dataclass
class WorkflowStep:
    """One step in a workflow group.
    `id` is the stable identifier emitted in `step_clicked` and used in
    `set_count` / `set_active`. `label` is the per-step display text
    (e.g. "Side A"). `short_label` overrides the group's pill text for
    this step if set."""
    id: str
    label: str
    short_label: str = ""


@dataclass
class WorkflowGroup:
    """A group of steps sharing a base color and bracket header.
    `short_label` is what appears in each step's pill (e.g. "VIS").
    Tint overrides (bg_active / bg_done / bg_pending / text_dark) accept
    hex strings or QColors; any left None falls back to derived defaults."""
    label: str
    short_label: str
    base_color: str | QColor
    steps: list[WorkflowStep]
    bg_active:   str | QColor | None = None
    bg_done:     str | QColor | None = None
    bg_pending:  str | QColor | None = None
    text_dark:   str | QColor | None = None


# ---- internal palette helpers --------------------------------------------

def _qc(value) -> QColor:
    return value if isinstance(value, QColor) else QColor(value)


@dataclass
class _ResolvedPalette:
    """All the colors needed to paint one step's chevron in any state.
    Built once per group from the WorkflowGroup definition + sensible
    defaults for any tints the caller didn't override."""
    base:        QColor
    bg_active:   QColor
    bg_done:     QColor
    bg_pending:  QColor
    text_dark:   QColor


def _resolve_palette(group: WorkflowGroup) -> _ResolvedPalette:
    base = _qc(group.base_color)
    return _ResolvedPalette(
        base=base,
        # Defaults: bg_active = base color (saturated), bg_done = a light
        # tint, bg_pending = white. Override per-group when the derivation
        # doesn't read well (which is often, for warm hues).
        bg_active=_qc(group.bg_active) if group.bg_active else base,
        bg_done=_qc(group.bg_done) if group.bg_done else base.lighter(180),
        bg_pending=_qc(group.bg_pending) if group.bg_pending else QColor("white"),
        # Dark text used on light fills (done state). Default = darker base.
        text_dark=_qc(group.text_dark) if group.text_dark else base.darker(160),
    )


# Universal colors (independent of group)
_DONE_GREEN     = QColor("#10b981")
_PENDING_GREY   = QColor("#94a3b8")
_PENDING_BORDER = QColor("#cbd5e1")
_TEXT_DIM       = QColor("#64748b")
_WHITE          = QColor("white")


# ---- widget --------------------------------------------------------------

class WorkflowStepper(QWidget):
    """A horizontal chevron-stepper. Groups of ordered steps with brackets
    above each group, color-coded per group, click-to-activate.

    Construct with a list of `WorkflowGroup`s OR construct empty (e.g. via
    Qt Designer) and call `set_groups(...)` later.

    Signals:
        step_clicked(str)   — emitted with the step id on left-click
    """

    step_clicked = pyqtSignal(str)

    # Geometry
    CHEVRON_W = 220
    CHEVRON_H = 62
    NOTCH = 14
    GAP = 8
    MEDALLION = 30
    MED_INSET = 10
    BRACKET_LABEL_H = 18
    BRACKET_GAP = 14
    LEFT_PADDING = 4
    BRACKET_GROUP_GAP = 8   # extra horizontal space between consecutive groups

    def __init__(self, groups: Sequence[WorkflowGroup] | None = None, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        # Per-group resolved palettes; per-step state.
        self._groups: list[WorkflowGroup] = []
        self._palettes: list[_ResolvedPalette] = []
        self._counts: dict[str, int] = {}
        self._active_id: str | None = None
        # Click-target rects, populated during paintEvent.
        self._step_rects: list[tuple[str, QRectF]] = []

        if groups:
            self.set_groups(groups)

    # ---- public API --------------------------------------------------

    def set_groups(self, groups: Sequence[WorkflowGroup]) -> None:
        """Configure (or reconfigure) the stepper's groups. Resets all
        per-step state to count=0, no active."""
        self._groups = list(groups)
        self._palettes = [_resolve_palette(g) for g in self._groups]
        self._counts = {s.id: 0 for g in self._groups for s in g.steps}
        self._active_id = None
        self._recompute_size()
        self.update()

    def set_count(self, step_id: str, count: int) -> None:
        if step_id in self._counts and self._counts[step_id] != count:
            self._counts[step_id] = count
            self.update()

    def set_active(self, step_id: str | None) -> None:
        """Mark `step_id` as active. Pass None to clear. Other steps' state
        is unaffected (they remain done/pending depending on their counts)."""
        if step_id is not None and step_id not in self._counts:
            return
        if step_id == self._active_id:
            return
        self._active_id = step_id
        self.update()

    # ---- size --------------------------------------------------------

    def _total_step_count(self) -> int:
        return sum(len(g.steps) for g in self._groups)

    def _recompute_size(self) -> None:
        n = self._total_step_count()
        if n == 0:
            self.setFixedHeight(0)
            self.setMinimumWidth(0)
            return
        n_groups = len(self._groups)
        bracket_total = self.BRACKET_LABEL_H + self.BRACKET_GAP
        self.setFixedHeight(self.CHEVRON_H + bracket_total + 12)
        # Width: first chevron full-width, each subsequent one steps by
        # (CHEVRON_W - NOTCH + GAP). Then add inter-group extra spacing.
        per_step_stride = self.CHEVRON_W - self.NOTCH + self.GAP
        width = (
            self.LEFT_PADDING
            + self.CHEVRON_W
            + (n - 1) * per_step_stride
            + (n_groups - 1) * self.BRACKET_GROUP_GAP
            + 16
        )
        self.setMinimumWidth(int(width))

    # ---- layout: compute each chevron's x position -------------------

    def _layout(self) -> list[tuple[int, int, int, int, WorkflowStep, _ResolvedPalette]]:
        """Returns one tuple per step:
            (x, y, group_index, step_number_1based, step, palette)
        x is the chevron's left edge; y is its top.
        """
        out = []
        chevron_y = self.BRACKET_LABEL_H + self.BRACKET_GAP
        x = self.LEFT_PADDING
        step_n = 0
        per_step_stride = self.CHEVRON_W - self.NOTCH + self.GAP
        for g_idx, group in enumerate(self._groups):
            pal = self._palettes[g_idx]
            for s_idx, step in enumerate(group.steps):
                step_n += 1
                out.append((x, chevron_y, g_idx, step_n, step, pal))
                # Last step in group: jump to next group with extra spacing.
                is_last_in_group = (s_idx == len(group.steps) - 1)
                is_last_overall  = (g_idx == len(self._groups) - 1
                                    and is_last_in_group)
                if not is_last_overall:
                    x += per_step_stride
                    if is_last_in_group:
                        x += self.BRACKET_GROUP_GAP
        return out

    # ---- painting ----------------------------------------------------

    def paintEvent(self, _evt):
        if not self._groups:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        layout = self._layout()
        # Reset click-target list; refilled per step below.
        self._step_rects = []

        # Bracket above each group.
        bracket_y = self.BRACKET_LABEL_H + 2
        for g_idx, group in enumerate(self._groups):
            group_steps = [t for t in layout if t[2] == g_idx]
            x_left = self._chevron_top_left_x(group_steps[0])
            x_right = self._chevron_top_right_x(group_steps[-1], layout)
            self._paint_bracket(p, x_left, x_right, bracket_y, self._palettes[g_idx], group.label)

        # Each chevron.
        n_total = len(layout)
        for i, (x, y, g_idx, step_n, step, pal) in enumerate(layout):
            self._paint_chevron(
                p, x, y, step_n, step, pal,
                first=(i == 0),
                last=(i == n_total - 1),
                short_label=step.short_label or self._groups[g_idx].short_label,
            )

    def _chevron_top_left_x(self, layout_entry) -> int:
        """Top-left corner x of a chevron — always = its x (the V-notch
        carves only the side edges, not the top)."""
        x, _y, _g, _n, _step, _pal = layout_entry
        return x

    def _chevron_top_right_x(self, layout_entry, full_layout) -> int:
        """Top-right corner x — before the arrow point on non-last
        chevrons, all the way to x+W on the last."""
        x, _y, _g, _n, _step, _pal = layout_entry
        is_last = (layout_entry is full_layout[-1])
        return x + (self.CHEVRON_W if is_last else self.CHEVRON_W - self.NOTCH)

    # ---- bracket -----------------------------------------------------

    def _paint_bracket(self, p, x_left, x_right, baseline_y, pal, label):
        font = p.font()
        font.setPointSize(8); font.setBold(True)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2.0)
        p.setFont(font)
        text = label.upper()
        fm = p.fontMetrics()
        text_w = fm.horizontalAdvance(text) + 14
        cx = (x_left + x_right) / 2
        text_l = cx - text_w / 2
        text_r = cx + text_w / 2

        line_y = baseline_y - 2
        p.setPen(QPen(pal.base, 1.5))
        p.drawLine(int(x_left), int(line_y), int(text_l), int(line_y))
        p.drawLine(int(text_r), int(line_y), int(x_right), int(line_y))
        # Downward end-caps
        p.drawLine(int(x_left), int(line_y), int(x_left), int(line_y + 8))
        p.drawLine(int(x_right), int(line_y), int(x_right), int(line_y + 8))

        p.setPen(QPen(pal.base))
        p.drawText(QRectF(text_l, line_y - 11, text_w, 18),
                   Qt.AlignmentFlag.AlignCenter, text)

    # ---- chevron ----------------------------------------------------

    def _paint_chevron(self, p, x, y, step_n, step, pal, *, first, last, short_label):
        is_active = step.id == self._active_id
        count = self._counts.get(step.id, 0)
        is_done = count > 0

        # Color choices per state.
        if is_active:
            fill = pal.bg_active
            border = pal.base.darker(120)
            border_w = 2
            text_color = _WHITE
            sub_color = _WHITE
            pill_bg = _WHITE
            pill_text = pal.base
            med_bg = _WHITE
            med_fg = pal.bg_active
            med_border = _WHITE
        elif is_done:
            fill = pal.bg_done
            border = pal.bg_done.darker(105)
            border_w = 1
            text_color = pal.text_dark
            sub_color = _DONE_GREEN
            pill_bg = pal.base
            pill_text = _WHITE
            med_bg = _WHITE
            med_fg = pal.text_dark
            med_border = pal.base
        else:  # pending
            fill = pal.bg_pending
            border = _PENDING_BORDER
            border_w = 1
            text_color = _TEXT_DIM
            sub_color = _PENDING_GREY
            pill_bg = pal.base
            pill_text = _WHITE
            med_bg = _WHITE
            med_fg = pal.base
            med_border = pal.base

        # Polygon
        poly = self._chevron_polygon(x, y, first=first, last=last)
        p.setPen(QPen(border, border_w))
        p.setBrush(QBrush(fill))
        p.drawPolygon(poly)

        # Click-target = the polygon's bounding rect; good enough for our purposes.
        self._step_rects.append((step.id, poly.boundingRect()))

        # Medallion (anchored to flat-left so it clears the V-notch).
        flat_left = x if first else (x + self.NOTCH)
        med_x = flat_left + self.MED_INSET
        med_y = y + (self.CHEVRON_H - self.MEDALLION) / 2
        med_rect = QRectF(med_x, med_y, self.MEDALLION, self.MEDALLION)
        p.setBrush(QBrush(med_bg))
        p.setPen(QPen(med_border, 2))
        p.drawEllipse(med_rect)
        font = p.font()
        font.setPointSize(12); font.setBold(True)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.0)
        p.setFont(font)
        p.setPen(QPen(med_fg))
        p.drawText(med_rect, Qt.AlignmentFlag.AlignCenter, str(step_n))

        # Right-of-medallion content (pill + title row, status row below).
        content_x = med_x + self.MEDALLION + 12
        title_row_y = y + 8

        # Pill
        pill_w, pill_h = 36, 22
        pill_rect = QRectF(content_x, title_row_y, pill_w, pill_h)
        p.setBrush(QBrush(pill_bg))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(pill_rect, 4, 4)
        p.setPen(QPen(pill_text))
        font = p.font(); font.setPointSize(8); font.setBold(True)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
        p.setFont(font)
        p.drawText(pill_rect, Qt.AlignmentFlag.AlignCenter, short_label)

        # Step label
        side_x = content_x + pill_w + 10
        font = p.font(); font.setPointSize(13); font.setBold(True)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.0)
        p.setFont(font)
        p.setPen(QPen(text_color))
        p.drawText(QRectF(side_x, title_row_y - 1, 100, pill_h + 2),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   step.label)

        # Status row
        if is_active and is_done:
            status_text = f"● WORKING · {count} done"
        elif is_active:
            status_text = "● WORKING"
        elif is_done:
            status_text = f"✓ COMPLETED · {count}"
        else:
            status_text = "○ PENDING"
        status_y = title_row_y + pill_h + 4
        font = p.font(); font.setPointSize(8); font.setBold(True)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
        p.setFont(font)
        p.setPen(QPen(sub_color))
        avail_w = (x + self.CHEVRON_W - self.NOTCH - 6) - content_x
        p.drawText(QRectF(content_x, status_y, avail_w, 16),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   status_text)

    def _chevron_polygon(self, x, y, *, first, last) -> QPolygonF:
        w, h, n = self.CHEVRON_W, self.CHEVRON_H, self.NOTCH
        pts = [QPointF(x, y), QPointF(x + w - n, y)]
        if last:
            pts.append(QPointF(x + w, y))
            pts.append(QPointF(x + w, y + h))
        else:
            pts.append(QPointF(x + w, y + h / 2))
        pts.append(QPointF(x + w - n, y + h))
        pts.append(QPointF(x, y + h))
        if not first:
            pts.append(QPointF(x + n, y + h / 2))
        return QPolygonF(pts)

    # ---- click handling ---------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        pos = event.position()
        for step_id, rect in self._step_rects:
            if rect.contains(pos):
                self.step_clicked.emit(step_id)
                return
        super().mousePressEvent(event)
