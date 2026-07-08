"""CaptureFilmstrip — per-take overlays + capture-management menu.

Extends FilmstripWidget with the capture-workflow UI in two modes, each
with its own delegate (swapped via `set_stitch_mode`, since the two modes
mark takes with different semantics):

  normal (default, `_ChosenStarDelegate`):
    - ★ overlay on the chosen take
    - menu: mark as chosen, optionally move to other side, delete
  stitch (`_StitchDelegate`; segment sets of oversized objects):
    - ◎ overlay on the reference photo (ColorChecker + scale take)
    - per-take connectivity dot fed via `set_connectivity`
    - menu: mark as reference, optionally move to other side, delete

The thumbnail caption (filename + EXIF) is drawn by FilmstripWidget's
CaptionDelegate, which both delegates extend; they only add the overlays.

Model-agnostic — this module paints and emits action signals
(`mark_chosen_requested`, `mark_reference_requested`, `move_requested`,
`delete_requested`); the subclass binds to a specific model and routes
the signals to the model's mutation API.

The "move to other side" entry only appears if the subclass calls
set_other_side(label, value) — capture workflows that don't have a
two-sided structure (RTI, etc.) can skip it.
"""
from __future__ import annotations

from PyQt6.QtCore import QRect, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QMenu, QMessageBox, QStyleOptionViewItem

from .filmstrip_widget import (
    CaptionDelegate, FilmstripWidget, ImageFileListItem, stem_of,
)


_STAR_GLYPH = "★"
_STAR_FILL = QColor("#f59e0b")     # amber-500
_STAR_OUTLINE = QColor("#78350f")  # amber-900 (1px halo for legibility)

_REFERENCE_GLYPH = "◎"
_REFERENCE_FILL = QColor("#3b82f6")     # blue-500 — distinct from the amber ★
_REFERENCE_OUTLINE = QColor("#1e3a8a")  # blue-900

# Connectivity dot per status string; unknown statuses fall back to the
# "unchecked" gray so a not-yet-checked take never looks connected.
_DOT_COLORS = {
    "connected": QColor("#10b981"),  # emerald-500
    "isolated": QColor("#ef4444"),   # red-500
}
_DOT_UNCHECKED = QColor("#94a3b8")   # slate-400
_DOT_OUTLINE = QColor("#1e293b")     # slate-800
_DOT_DIAMETER = 10
_DOT_MARGIN = 5


def _paint_corner_glyph(
    painter: QPainter, thumb_rect: QRect,
    glyph: str, fill: QColor, outline: QColor,
) -> None:
    """Draw a marker glyph in the thumb's top-right corner, halo pass +
    fill pass so it reads against any thumbnail."""
    painter.save()
    font = painter.font()
    font.setPointSize(20)
    font.setBold(True)
    painter.setFont(font)
    target = thumb_rect.adjusted(0, 0, -4, 0)
    align = Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight
    painter.setPen(QPen(outline, 3))
    painter.drawText(target, align, glyph)
    painter.setPen(QPen(fill, 1))
    painter.drawText(target, align, glyph)
    painter.restore()


class _ChosenStarDelegate(CaptionDelegate):
    """Adds a ★ on the chosen-take thumb on top of the inherited caption.

    Compares by stem so a JPEG+RAW pair shows the ★ on both rows, and
    RAW-only / JPEG-only takes work without special-casing."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._chosen_stem: str | None = None

    def set_chosen_stem(self, stem: str | None) -> None:
        self._chosen_stem = stem

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        # CaptionDelegate paints thumbnail + caption.
        super().paint(painter, option, index)
        item = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(item, ImageFileListItem):
            return
        if stem_of(item.file_name) == self._chosen_stem:
            _paint_corner_glyph(painter, self._thumb_rect(option),
                                _STAR_GLYPH, _STAR_FILL, _STAR_OUTLINE)


class _StitchDelegate(CaptionDelegate):
    """Overlays for stitch buckets: ◎ on the reference photo and a
    connectivity dot (top-left) on every segment. The reference gets no
    dot — it is excluded from the connectivity check by design.

    Stem-compared like the star delegate, so JPEG+RAW pairs mark
    consistently."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._reference_stem: str | None = None
        self._connectivity: dict[str, str] = {}

    def set_reference_stem(self, stem: str | None) -> None:
        self._reference_stem = stem

    def set_connectivity(self, status_by_stem: dict[str, str] | None) -> None:
        """Per-stem status ("connected" / "isolated"); stems missing from
        the dict paint as unchecked. None clears to all-unchecked."""
        self._connectivity = dict(status_by_stem or {})

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        super().paint(painter, option, index)
        item = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(item, ImageFileListItem):
            return
        stem = stem_of(item.file_name)
        thumb_rect = self._thumb_rect(option)
        if stem == self._reference_stem:
            _paint_corner_glyph(painter, thumb_rect, _REFERENCE_GLYPH,
                                _REFERENCE_FILL, _REFERENCE_OUTLINE)
            return
        color = _DOT_COLORS.get(self._connectivity.get(stem, ""), _DOT_UNCHECKED)
        self._paint_dot(painter, thumb_rect, color)

    @staticmethod
    def _paint_dot(painter: QPainter, thumb_rect: QRect, color: QColor) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(_DOT_OUTLINE, 1))
        painter.setBrush(color)
        painter.drawEllipse(
            thumb_rect.left() + _DOT_MARGIN, thumb_rect.top() + _DOT_MARGIN,
            _DOT_DIAMETER, _DOT_DIAMETER)
        painter.restore()


class CaptureFilmstrip(FilmstripWidget):
    """FilmstripWidget + per-take overlays + capture-management menu.

    Model-agnostic: subclass binds to a specific model and routes the
    emitted action signals to model mutations.

    Public API (in addition to FilmstripWidget's):
        set_stitch_mode(enabled)         swap normal ↔ stitch delegate/menu
        set_chosen_stem(stem)            update the ★ marker (or clear)
        set_reference_stem(stem)         update the ◎ marker (stitch mode)
        set_connectivity(status_by_stem) update the dots (stitch mode)
        set_other_side(label, value)     enable the "Move to side X"
                                         menu entry; subclass receives
                                         `value` back in move_requested
    """

    # User picked "Mark as chosen ★" — emitted directly (no confirmation
    # dialog; this is reversible by marking a different take).
    mark_chosen_requested = pyqtSignal(str)              # stem

    # User picked "Mark as reference ◎" (stitch mode) — also reversible.
    mark_reference_requested = pyqtSignal(str)           # stem

    # User picked "Unmark as reference" on the current reference (stitch
    # mode) — the bucket is left with no reference. No stem: there is at
    # most one reference.
    unmark_reference_requested = pyqtSignal()

    # User confirmed "Move to side X" — emitted only after the
    # confirmation dialog returns Yes. `dest_side` is whatever value
    # the subclass passed to set_other_side.
    move_requested = pyqtSignal(str, str)                # stem, dest_side

    # User confirmed "Delete capture…" — emitted only after the
    # confirmation dialog returns Yes.
    delete_requested = pyqtSignal(str)                   # stem

    def __init__(self, parent=None):
        super().__init__(parent)
        # One delegate per mode; set_stitch_mode swaps them. Both are
        # created up front so their marker state survives mode flips.
        self._star_delegate = _ChosenStarDelegate(self)
        self._stitch_delegate = _StitchDelegate(self)
        self._stitch_mode = False
        self.set_item_delegate(self._star_delegate)
        self.set_context_menu_provider(self._build_context_menu)
        # Tracked here too so the menu builder can check is_chosen /
        # is_reference without reaching into delegate private state.
        self._chosen_stem: str | None = None
        self._reference_stem: str | None = None
        # Subclass-configured "move to other side" target. If left None,
        # the move entry is hidden.
        self._other_side_label: str | None = None
        self._other_side_value: str | None = None

    # ---- public API ----------------------------------------------------

    def set_stitch_mode(self, enabled: bool) -> None:
        """Swap between the normal (★ chosen) and stitch (◎ reference +
        connectivity dots) delegate and context menu."""
        enabled = bool(enabled)
        if enabled == self._stitch_mode:
            return
        self._stitch_mode = enabled
        self.set_item_delegate(
            self._stitch_delegate if enabled else self._star_delegate)
        self.repaint_items()

    def set_chosen_stem(self, stem: str | None) -> None:
        """Update the ★ marker. Pass None to clear. Triggers a repaint."""
        self._chosen_stem = stem
        self._star_delegate.set_chosen_stem(stem)
        self.repaint_items()

    def set_reference_stem(self, stem: str | None) -> None:
        """Update the ◎ reference marker (stitch mode). Triggers a repaint."""
        self._reference_stem = stem
        self._stitch_delegate.set_reference_stem(stem)
        self.repaint_items()

    def set_connectivity(self, status_by_stem: dict[str, str] | None) -> None:
        """Update the connectivity dots (stitch mode). Triggers a repaint."""
        self._stitch_delegate.set_connectivity(status_by_stem)
        self.repaint_items()

    def set_other_side(self, label: str, value: str) -> None:
        """Enable the "Move to side {label}" menu entry. `label` is the
        user-facing text ('B' / 'A'); `value` is echoed back in the
        move_requested signal so the subclass knows where to move."""
        self._other_side_label = label
        self._other_side_value = value

    # ---- context menu --------------------------------------------------

    def _build_context_menu(self, item: ImageFileListItem) -> QMenu | None:
        """Default context menu: mark / (move) / delete. The mark entry is
        mode-specific: chosen ★ normally, reference ◎ in stitch mode.
        Subclasses can override — set_context_menu_provider is wired to
        self._build_context_menu, so subclass override is picked up via
        normal Python method dispatch."""
        stem = stem_of(item.file_name)
        menu = QMenu(self)

        if self._stitch_mode:
            if stem == self._reference_stem:
                # Right-clicking the current reference offers to clear it,
                # leaving the bucket with no reference (all captures checked).
                unmark_action = menu.addAction("Unmark as reference")
                unmark_action.triggered.connect(
                    lambda *_: self.unmark_reference_requested.emit()
                )
            else:
                mark_action = menu.addAction(f"Mark as reference  {_REFERENCE_GLYPH}")
                mark_action.triggered.connect(
                    lambda *_: self.mark_reference_requested.emit(stem)
                )
        else:
            mark_action = menu.addAction(f"Mark as chosen  {_STAR_GLYPH}")
            mark_action.setEnabled(stem != self._chosen_stem)
            mark_action.triggered.connect(
                lambda *_: self.mark_chosen_requested.emit(stem)
            )

        if self._other_side_label is not None and self._other_side_value is not None:
            move_action = menu.addAction(f"Move to side {self._other_side_label}")
            move_action.triggered.connect(
                lambda *_: self._confirm_and_move(stem)
            )

        menu.addSeparator()

        delete_action = menu.addAction("Delete capture…")
        delete_action.triggered.connect(
            lambda *_: self._confirm_and_delete(stem)
        )

        return menu

    def _confirm_and_move(self, stem: str) -> None:
        result = QMessageBox.question(
            self,
            "Move capture",
            f"Move {stem!r} (both JPG and RAW, whichever exist) to side "
            f"{self._other_side_label}? It will be renumbered as the next "
            f"take in that side.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if result == QMessageBox.StandardButton.Yes:
            self.move_requested.emit(stem, self._other_side_value)

    def _confirm_and_delete(self, stem: str) -> None:
        result = QMessageBox.warning(
            self,
            "Delete capture",
            f"Move {stem!r} (both JPG and RAW, whichever exist) to the Trash?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if result == QMessageBox.StandardButton.Yes:
            self.delete_requested.emit(stem)
