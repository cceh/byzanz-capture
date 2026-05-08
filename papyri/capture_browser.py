"""Papyri-flavored PhotoBrowser: adds chosen-take ★ overlay and per-item
right-click menu (mark as chosen / delete). Bound to an `Object` *and a side*
("visible" or "infrared") so it stays in sync with the object's per-side
chosen-take state via `state_changed`.

Captures are identified by stem, so JPEG+RAW pairs, RAW-only and JPEG-only
all work the same way. PhotoBrowser still lists each side as its own row;
when both sides exist for a take, both rows show the ★ and either row's
right-click "Delete" trashes the whole pair.

Uses only PhotoBrowser's public extension API (`set_item_delegate`,
`set_context_menu_provider`, `repaint_items`, `open_directory`,
`close_directory`) — does not reach into the inner list widget.
"""
from __future__ import annotations
import os
from typing import TYPE_CHECKING

from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QColor, QLinearGradient, QPainter, QPen
from PyQt6.QtWidgets import QMenu, QMessageBox, QStyleOptionViewItem, QStyledItemDelegate

from byzanz_camera.photo_browser import ImageFileListItem, PhotoBrowser
from papyri._layout import SIDE_A, SIDE_B, SPECTRUM_VISIBLE

if TYPE_CHECKING:
    from papyri.main import Object


def _stem_of(file_name: str) -> str:
    """Filename stem (extension stripped). Safe for names with embedded dots."""
    return os.path.splitext(file_name)[0]


_STAR_GLYPH = "★"
_STAR_FILL = QColor("#f59e0b")     # amber-500
_STAR_OUTLINE = QColor("#78350f")  # amber-900 (1px halo for legibility)

# Caption overlay tunables
_CAPTION_HEIGHT = 34
_CAPTION_GRADIENT_TOP = QColor(0, 0, 0, 0)     # transparent at the top of the strip
_CAPTION_GRADIENT_BOTTOM = QColor(0, 0, 0, 150)  # ~60% black at the bottom


class _ChosenStarDelegate(QStyledItemDelegate):
    """Custom paint for filmstrip items: thumbnail + bottom-gradient caption
    overlay (filename) + ★ on the chosen take.

    The gradient strip works against both bright and dark thumbnails — text
    sits inside the dark band so contrast holds either way.

    Compares by stem so a JPEG+RAW pair shows the ★ on both rows, and
    RAW-only / JPEG-only takes work without special-casing.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._chosen_stem: str | None = None

    def set_chosen_stem(self, stem: str | None) -> None:
        self._chosen_stem = stem

    def displayText(self, value, locale) -> str:
        # Suppress the standard text-below-thumb caption — we paint our own
        # caption as a gradient overlay in paint().
        return ""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        # Selection background + thumbnail come from the default paint path.
        # Only the (now-empty) text portion is omitted thanks to displayText.
        super().paint(painter, option, index)

        item = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(item, ImageFileListItem):
            return

        thumb_rect = self._thumb_rect(option)
        self._paint_caption(painter, thumb_rect, item)

        if _stem_of(item.file_name) == self._chosen_stem:
            self._paint_star(painter, thumb_rect)

    @staticmethod
    def _thumb_rect(option: QStyleOptionViewItem) -> QRect:
        """Compute where the thumbnail actually lives inside the cell.
        Qt's IconMode centers the icon horizontally near the top of the
        cell — we mirror that placement so the overlay lands on the thumb."""
        cell = option.rect
        icon = option.decorationSize  # QSize set via list.setIconSize
        x = cell.x() + (cell.width() - icon.width()) // 2
        y = cell.y() + 2
        return QRect(x, y, icon.width(), icon.height())

    @staticmethod
    def _paint_caption(
        painter: QPainter, thumb_rect: QRect, item: ImageFileListItem
    ) -> None:
        # Two-line caption: stem on top (bold), EXIF line below (regular).
        # The base PhotoBrowser stores the EXIF line as the second line of
        # `item.text()` ("filename\nf/X | 1/Y"); we read it back from there
        # rather than re-parsing EXIF in the delegate.
        stem = _stem_of(item.file_name)
        text_lines = item.text().split("\n")
        exif_line = text_lines[1] if len(text_lines) > 1 else ""

        strip = QRect(
            thumb_rect.x(),
            thumb_rect.bottom() - _CAPTION_HEIGHT + 1,
            thumb_rect.width(),
            _CAPTION_HEIGHT,
        )

        painter.save()
        # Vertical fade from transparent → ~60% black so the caption reads
        # over any thumb without a hard color stripe. (QLinearGradient
        # takes float coords, not QPoint — be explicit so we don't trip
        # on the QPoint→QPointF overload mismatch.)
        gradient = QLinearGradient(
            float(strip.x()), float(strip.y()),
            float(strip.x()), float(strip.bottom()),
        )
        gradient.setColorAt(0.0, _CAPTION_GRADIENT_TOP)
        gradient.setColorAt(1.0, _CAPTION_GRADIENT_BOTTOM)
        painter.fillRect(strip, gradient)

        painter.setPen(QColor("white"))
        font = painter.font()
        font.setPointSize(10)
        font.setBold(True)
        painter.setFont(font)

        # Stem line (top of strip): elide in the middle so the object name
        # and the take index both stay visible.
        elided_stem = painter.fontMetrics().elidedText(
            stem, Qt.TextElideMode.ElideMiddle, strip.width() - 8
        )
        stem_rect = QRect(strip.x() + 4, strip.y() + 2,
                          strip.width() - 8, 16)
        painter.drawText(
            stem_rect,
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter,
            elided_stem,
        )

        # EXIF line (bottom of strip): smaller, regular weight.
        font.setPointSize(8)
        font.setBold(False)
        painter.setFont(font)
        exif_rect = QRect(strip.x() + 4, strip.y() + 18,
                          strip.width() - 8, 14)
        painter.drawText(
            exif_rect,
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter,
            exif_line,
        )
        painter.restore()

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


class PapyriCaptureBrowser(PhotoBrowser):
    """PhotoBrowser bound to a papyri Object and one (side, spectrum) bucket.

    Adds a ★ overlay on the chosen take and a right-click menu to mark
    chosen / delete. Stays in sync with the bound object's `state_changed`.
    Re-bind with a different (side, spectrum) to swap the listed directory
    and chosen overlay (used when the user toggles the side or spectrum).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._obj: "Object | None" = None
        self._side: str = SIDE_A
        self._spectrum: str = SPECTRUM_VISIBLE
        self._delegate = _ChosenStarDelegate(self)
        self.set_item_delegate(self._delegate)
        self.set_context_menu_provider(self._build_context_menu)
        # Loupe layout: viewer on top, horizontal filmstrip below — papyri
        # workflow is "watch the live image; glance at prior takes" so the
        # viewer should get the most pixels.
        self.use_loupe_layout()
        # Top-right corner pill + viewer-border tint that tells the
        # operator at a glance whether they're looking at live frames,
        # a paused live view, or a selected capture from the filmstrip.
        # main.py drives state transitions via set_view_state().
        self.enable_view_state_indicator()

    # ---- public API ----------------------------------------------------

    def bind_object(
        self,
        obj: "Object | None",
        side: str = SIDE_A,
        spectrum: str = SPECTRUM_VISIBLE,
    ) -> None:
        """Track one (side, spectrum) bucket of an object. Pass `obj=None`
        to clear; pass a different `side` or `spectrum` (with the same
        object) to swap which bucket is shown."""
        self._unbind_previous()
        self._obj = obj
        self._side = side
        self._spectrum = spectrum

        if obj is None:
            self.close_directory()
            self._delegate.set_chosen_stem(None)
            self.repaint_items()
            return

        obj.state_changed.connect(self._on_object_state_changed)
        self._on_object_state_changed()
        self.open_directory(obj.dir_for(side, spectrum))

    # ---- internals -----------------------------------------------------

    def _on_object_state_changed(self) -> None:
        chosen = self._obj.chosen(self._side, self._spectrum) if self._obj else None
        self._delegate.set_chosen_stem(chosen.stem if chosen else None)
        self.repaint_items()

    def _unbind_previous(self) -> None:
        if self._obj is None:
            return
        try:
            self._obj.state_changed.disconnect(self._on_object_state_changed)
        except TypeError:
            pass

    def _build_context_menu(self, item: ImageFileListItem) -> QMenu | None:
        if self._obj is None:
            return None
        stem = _stem_of(item.file_name)
        menu = QMenu(self)

        chosen = self._obj.chosen(self._side, self._spectrum)
        is_chosen = chosen is not None and chosen.stem == stem

        mark_action = menu.addAction(f"Mark as chosen  {_STAR_GLYPH}")
        mark_action.setEnabled(not is_chosen)
        mark_action.triggered.connect(
            lambda *_: self._obj.set_chosen(self._side, self._spectrum, stem)
        )

        # Move to the OTHER side (within the same spectrum) — recovery for
        # captures that were taken on the wrong side of the papyrus. The
        # destination renumbers automatically.
        other_side = SIDE_B if self._side == SIDE_A else SIDE_A
        other_side_label = "B" if other_side == SIDE_B else "A"
        move_action = menu.addAction(f"Move to side {other_side_label}")
        move_action.triggered.connect(
            lambda *_: self._confirm_and_move(stem, other_side, other_side_label)
        )

        menu.addSeparator()

        delete_action = menu.addAction("Delete capture…")
        delete_action.triggered.connect(
            lambda *_: self._confirm_and_delete(stem)
        )

        return menu

    def _confirm_and_move(self, stem: str, other_side: str, other_side_label: str) -> None:
        if self._obj is None:
            return
        result = QMessageBox.question(
            self,
            "Move capture",
            f"Move {stem!r} (both JPG and RAW, whichever exist) to side "
            f"{other_side_label}? It will be renumbered as the next take "
            f"in that side.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if result == QMessageBox.StandardButton.Yes:
            self._obj.move(self._side, self._spectrum, stem, other_side)

    def _confirm_and_delete(self, stem: str) -> None:
        if self._obj is None:
            return
        result = QMessageBox.warning(
            self,
            "Delete capture",
            f"Move {stem!r} (both JPG and RAW, whichever exist) to the Trash?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if result == QMessageBox.StandardButton.Yes:
            self._obj.delete(self._side, self._spectrum, stem)
