"""CaptureFilmstrip — chosen-take overlay + capture-management menu.

Extends FilmstripWidget with the capture-workflow UI:
  - ★ overlay on the chosen take (on top of the inherited caption)
  - right-click menu: mark as chosen, optionally move to other side, delete
  - confirmation dialogs for destructive operations (move, delete)

The thumbnail caption (filename + EXIF) is now drawn by FilmstripWidget's
default CaptionDelegate; this delegate only adds the chosen-take star.

Model-agnostic. Emits action signals (`mark_chosen_requested`,
`move_requested`, `delete_requested`); the subclass binds to a specific
model and routes those signals to the model's mutation API.

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
            self._paint_star(painter, self._thumb_rect(option))

    @staticmethod
    def _paint_star(painter: QPainter, thumb_rect: QRect) -> None:
        painter.save()
        font = painter.font()
        font.setPointSize(20)
        font.setBold(True)
        painter.setFont(font)
        target = thumb_rect.adjusted(0, 0, -4, 0)
        align = Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight
        # Halo + fill so the star reads against any thumbnail.
        painter.setPen(QPen(_STAR_OUTLINE, 3))
        painter.drawText(target, align, _STAR_GLYPH)
        painter.setPen(QPen(_STAR_FILL, 1))
        painter.drawText(target, align, _STAR_GLYPH)
        painter.restore()


class CaptureFilmstrip(FilmstripWidget):
    """FilmstripWidget + chosen-take overlay + capture-management menu.

    Model-agnostic: subclass binds to a specific model and routes the
    emitted action signals to model mutations.

    Public API (in addition to FilmstripWidget's):
        set_chosen_stem(stem)            update the ★ marker (or clear)
        set_other_side(label, value)     enable the "Move to side X"
                                         menu entry; subclass receives
                                         `value` back in move_requested
    """

    # User picked "Mark as chosen ★" — emitted directly (no confirmation
    # dialog; this is reversible by marking a different take).
    mark_chosen_requested = pyqtSignal(str)              # stem

    # User confirmed "Move to side X" — emitted only after the
    # confirmation dialog returns Yes. `dest_side` is whatever value
    # the subclass passed to set_other_side.
    move_requested = pyqtSignal(str, str)                # stem, dest_side

    # User confirmed "Delete capture…" — emitted only after the
    # confirmation dialog returns Yes.
    delete_requested = pyqtSignal(str)                   # stem

    def __init__(self, parent=None):
        super().__init__(parent)
        self._delegate = _ChosenStarDelegate(self)
        self.set_item_delegate(self._delegate)
        self.set_context_menu_provider(self._build_context_menu)
        # Tracked here too so the menu builder can check is_chosen without
        # reaching into the delegate's private state.
        self._chosen_stem: str | None = None
        # Subclass-configured "move to other side" target. If left None,
        # the move entry is hidden.
        self._other_side_label: str | None = None
        self._other_side_value: str | None = None

    # ---- public API ----------------------------------------------------

    def set_chosen_stem(self, stem: str | None) -> None:
        """Update the ★ marker. Pass None to clear. Triggers a repaint."""
        self._chosen_stem = stem
        self._delegate.set_chosen_stem(stem)
        self.repaint_items()

    def set_other_side(self, label: str, value: str) -> None:
        """Enable the "Move to side {label}" menu entry. `label` is the
        user-facing text ('B' / 'A'); `value` is echoed back in the
        move_requested signal so the subclass knows where to move."""
        self._other_side_label = label
        self._other_side_value = value

    # ---- context menu --------------------------------------------------

    def _build_context_menu(self, item: ImageFileListItem) -> QMenu | None:
        """Default context menu: mark / (move) / delete. Subclasses can
        override to add or replace entries — set_context_menu_provider
        is wired to self._build_context_menu, so subclass override is
        picked up via normal Python method dispatch."""
        stem = stem_of(item.file_name)
        menu = QMenu(self)

        is_chosen = (stem == self._chosen_stem)
        mark_action = menu.addAction(f"Mark as chosen  {_STAR_GLYPH}")
        mark_action.setEnabled(not is_chosen)
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
