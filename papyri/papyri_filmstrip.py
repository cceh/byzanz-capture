"""PapyriFilmstrip — CaptureFilmstrip bound to papyri's Object model.

Thin papyri-specific wrapper. Knows how to:
  - bind to an Object + (side, spectrum) bucket
  - read the chosen stem from obj.chosen(side, spectrum) and push it to
    CaptureFilmstrip's set_chosen_stem
  - configure the move-to-other-side menu entry per current side
  - route CaptureFilmstrip's action signals to Object mutation methods
  - keep the ★ overlay in sync with obj.state_changed
  - accept Finder drag-and-drop of image files as if they had been
    captured via tethering — copies + renames using the same naming
    logic as the camera worker (see `Object.next_stem`).

Replaces the previous PapyriCaptureBrowser (subclass of the monolithic
PhotoBrowser); same external behavior, smaller surface — no viewer code
because the viewer is now ViewerWidget, a separate widget driven
directly from main.py via filmstrip signals.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PyQt6.QtCore import QEvent
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QWidget

from byzanz_camera.capture_filmstrip import CaptureFilmstrip
from byzanz_camera.filmstrip_widget import THUMB_GAP
from byzanz_camera.load_image_worker import SUPPORTED_EXTENSIONS
from papyri._layout import SIDE_A, SIDE_B, SPECTRUM_VISIBLE


class _DropMarker(QWidget):
    """Slate-900 vertical bar painted via paintEvent — more reliable
    than QFrame+stylesheet at 3px wide on macOS."""
    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#0f172a"))


if TYPE_CHECKING:
    from papyri.main import Object


class PapyriFilmstrip(CaptureFilmstrip):
    """CaptureFilmstrip bound to a papyri (Object, side, spectrum) bucket."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._obj: "Object | None" = None
        self._side: str = SIDE_A
        self._spectrum: str = SPECTRUM_VISIBLE

        # Route the generic capture-action signals from CaptureFilmstrip
        # to Object's per-bucket mutation API. Greppable named slots
        # rather than lambdas (rule #3 from session refactor).
        self.mark_chosen_requested.connect(self._on_mark_chosen_requested)
        self.move_requested.connect(self._on_move_requested)
        self.delete_requested.connect(self._on_delete_requested)

        # Tethering-fallback: accept image files dragged in from Finder
        # so an assistant whose USB has dropped can shoot to the
        # camera's card and just drag the take into the bucket.
        self.setAcceptDrops(True)
        # The QListWidget swallows drag events without bubbling them
        # to the parent — install an event filter on its viewport so
        # we receive drags wherever the user hovers, not just on the
        # strip's exposed margin.
        self.image_file_list.viewport().setAcceptDrops(True)
        self.image_file_list.viewport().installEventFilter(self)
        # Drop-position indicator: thin slate-900 bar parented to the
        # list viewport so it paints ON TOP of items.
        self._drop_marker = _DropMarker(self.image_file_list.viewport())
        self._drop_marker.setFixedWidth(3)
        self._drop_marker.hide()

    # ---- public API ----------------------------------------------------

    def bind_object(
        self,
        obj: "Object | None",
        side: str = SIDE_A,
        spectrum: str = SPECTRUM_VISIBLE,
    ) -> None:
        """Track one (side, spectrum) bucket of an object. Pass obj=None
        to clear; pass a different side or spectrum (with the same
        object) to swap which bucket is shown."""
        self._unbind_previous()
        self._obj = obj
        self._side = side
        self._spectrum = spectrum

        if obj is None:
            self.close_directory()
            self.set_chosen_stem(None)
            return

        # Configure the "Move to side X" menu entry for this side. With
        # only two sides, "other" is unambiguous.
        other_side = SIDE_B if side == SIDE_A else SIDE_A
        other_side_label = "B" if other_side == SIDE_B else "A"
        self.set_other_side(other_side_label, other_side)

        obj.state_changed.connect(self._on_object_state_changed)
        obj.import_failed.connect(self._on_import_failed)
        self._on_object_state_changed()  # initial chosen-stem paint
        # Tell the strip which file should land in the viewer at end of
        # load — the bucket's chosen-take if there is one. If not,
        # FilmstripWidget falls back to the highest-indexed file.
        chosen = obj.chosen(side, spectrum)
        self.open_directory(
            obj.dir_for(side, spectrum),
            preferred_stem=chosen.stem if chosen else None,
        )

    # ---- internals -----------------------------------------------------

    def _unbind_previous(self) -> None:
        if self._obj is None:
            return
        for signal, slot in (
            (self._obj.state_changed, self._on_object_state_changed),
            (self._obj.import_failed, self._on_import_failed),
        ):
            try:
                signal.disconnect(slot)
            except TypeError:
                pass

    def _on_object_state_changed(self) -> None:
        """Refresh chosen-stem when the bound object's state changes."""
        chosen = self._obj.chosen(self._side, self._spectrum) if self._obj else None
        self.set_chosen_stem(chosen.stem if chosen else None)

    def _on_import_failed(self, dest) -> None:
        """A queued drop-import copy raised — drop its placeholder so
        the user doesn't stare at an orphaned spinner forever."""
        self.remove_placeholder(str(dest))

    # ---- action handlers (route CaptureFilmstrip signals → Object) ----

    def _on_mark_chosen_requested(self, stem: str) -> None:
        if self._obj is not None:
            self._obj.set_chosen(self._side, self._spectrum, stem)

    def _on_move_requested(self, stem: str, dest_side: str) -> None:
        if self._obj is not None:
            self._obj.move(self._side, self._spectrum, stem, dest_side)

    def _on_delete_requested(self, stem: str) -> None:
        if self._obj is not None:
            self._obj.delete(self._side, self._spectrum, stem)

    # ---- drag & drop import -------------------------------------------

    # Drag events that land on the viewport (because the QListWidget
    # fills the strip and gets the mouse first) are funneled here via
    # the eventFilter installed in __init__.
    _DRAG_EVENT_TYPES = (
        QEvent.Type.DragEnter, QEvent.Type.DragMove,
        QEvent.Type.DragLeave, QEvent.Type.Drop,
    )

    def eventFilter(self, obj, event):
        if (obj is self.image_file_list.viewport()
                and event.type() in self._DRAG_EVENT_TYPES):
            {
                QEvent.Type.DragEnter: self.dragEnterEvent,
                QEvent.Type.DragMove:  self.dragMoveEvent,
                QEvent.Type.DragLeave: self.dragLeaveEvent,
                QEvent.Type.Drop:      self.dropEvent,
            }[event.type()](event)
            return event.isAccepted()
        return super().eventFilter(obj, event)

    def dragEnterEvent(self, event) -> None:
        ok = self._drop_is_acceptable(event)
        self._set_drop_marker(ok)
        event.acceptProposedAction() if ok else event.ignore()

    def dragMoveEvent(self, event) -> None:
        ok = self._drop_is_acceptable(event)
        self._set_drop_marker(ok)
        event.acceptProposedAction() if ok else event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self._set_drop_marker(False)

    def dropEvent(self, event) -> None:
        self._set_drop_marker(False)
        if not self._drop_is_acceptable(event):
            event.ignore()
            return
        sources = [Path(u.toLocalFile()) for u in event.mimeData().urls()]
        dests = self._obj.import_files(self._side, self._spectrum, sources)
        if not dests:
            event.ignore()
            return
        # Synchronous pre-seed: bridges the worker-thread copy duration
        # so the user sees feedback before the watcher catches up.
        # __load_directory will idempotently skip these on the eventual
        # post-rename watcher fire. `add_placeholder` scrolls to end
        # itself, so the last-seeded placeholder ends up onscreen.
        for dest in dests:
            self.add_placeholder(str(dest))
        event.acceptProposedAction()

    def _set_drop_marker(self, visible: bool) -> None:
        if not visible:
            self._drop_marker.hide()
            return
        lw = self.image_file_list
        vp = lw.viewport()
        if lw.count() > 0:
            rect = lw.visualItemRect(lw.item(lw.count() - 1))
            x = rect.right() + 1 + THUMB_GAP // 2
        else:
            x = THUMB_GAP // 2
        # Clamp into the visible viewport — items past the right edge
        # leave the geometric insertion point off-screen; show the
        # marker at the trailing edge so the user still sees feedback.
        x = max(0, min(x, vp.width() - 3))
        self._drop_marker.setGeometry(x, 0, 3, vp.height())
        self._drop_marker.show()
        self._drop_marker.raise_()

    def _drop_is_acceptable(self, event) -> bool:
        """True when there's a bound object AND the drag carries at
        least one file with a supported image extension. Returning
        False makes Qt show the system's 'no-drop' cursor."""
        if self._obj is None:
            return False
        mime = event.mimeData()
        if not mime.hasUrls():
            return False
        for url in mime.urls():
            suffix = Path(url.toLocalFile()).suffix.lower()
            if suffix in SUPPORTED_EXTENSIONS:
                return True
        return False
