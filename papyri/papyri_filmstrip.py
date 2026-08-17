"""PapyriFilmstrip — CaptureFilmstrip bound to papyri's Object model.

Thin papyri-specific wrapper. Knows how to:
  - bind to an Object + (side, spectrum) bucket
  - drive the filmstrip's normal vs stitch mode from obj.is_stitching():
    normal → ★ chosen overlay; stitch → ◎ reference overlay + connectivity
    dots (dots are pushed from main.py when a check completes)
  - configure the move-to-other-side menu entry per current side
  - route CaptureFilmstrip's action signals to Object mutation methods
  - keep the overlays in sync with obj.state_changed
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

from PyQt6.QtCore import QEvent, pyqtSignal
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QWidget

from PyQt6.QtWidgets import QMenu

from byzanz_camera.capture_audit import CaptureAuditContext
from byzanz_camera.capture_filmstrip import CaptureFilmstrip
from byzanz_camera.filmstrip_widget import THUMB_GAP, stem_of
from byzanz_camera.load_image_worker import SUPPORTED_EXTENSIONS
from papyri.audits import CaptureAuditSettings, entry_is_current, warned_checks
from papyri.capture_vocab import SIDE_A, SIDE_B, SPECTRUM_VISIBLE
from papyri.object_layout import read_capture_audits
from papyri.styles import AUDIT_STATUS_COLORS


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

    # Menu action "Re-run capture check" on a thumb. Emits the stem; main
    # drops the persisted findings and queues the re-measure.
    audit_recheck_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._obj: "Object | None" = None
        self._side: str = SIDE_A
        self._spectrum: str = SPECTRUM_VISIBLE
        # Simple capture mode: whole-folder view, no chosen ★ / move action.
        self._simple: bool = False
        # Resolved directory currently open — lets bind_object skip a
        # needless reload when only the spectrum changes but the storage
        # dir doesn't (the simple-mode VIS/IR switch).
        self._bound_dir: str | None = None
        self._bound_audit_context: CaptureAuditContext | None = None
        self._audit_settings: CaptureAuditSettings | None = None

        # Route the generic capture-action signals from CaptureFilmstrip
        # to Object's per-bucket mutation API. Greppable named slots
        # rather than lambdas (rule #3 from session refactor).
        self.mark_chosen_requested.connect(self._on_mark_chosen_requested)
        self.mark_reference_requested.connect(self._on_mark_reference_requested)
        self.unmark_reference_requested.connect(self._on_unmark_reference_requested)
        self.move_requested.connect(self._on_move_requested)
        self.delete_requested.connect(self._on_delete_requested)

        # Papyri filenames carry meaning (name + side + spectrum + index),
        # so show the full filename on the thumb (left-elided) rather than
        # the bare index.
        self.set_caption_mode("name")

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

    def set_simple_mode(self, simple: bool) -> None:
        """Simple capture mode: the strip shows the whole output folder,
        has no chosen ★ and no move-to-other-side entry (only delete),
        and is ordered chronologically (mtime) — the folder mixes two
        cameras' native naming schemes, so the filename index isn't a
        capture order there. Set before bind_object (a live mode switch
        rebinds to a different directory, so the strip reloads with the
        new order)."""
        self._simple = simple
        self.set_sort_by_mtime(simple)

    def bind_object(
        self,
        obj: "Object | None",
        side: str = SIDE_A,
        spectrum: str = SPECTRUM_VISIBLE,
        audit_context: CaptureAuditContext | None = None,
        audit_settings: CaptureAuditSettings | None = None,
    ) -> None:
        """Track one (side, spectrum) bucket of an object. Pass obj=None
        to clear; pass a different side or spectrum (with the same
        object) to swap which bucket is shown."""
        target_dir = obj.dir_for(side, spectrum) if obj is not None else None
        # Idempotent re-bind: same object + same resolved directory just
        # updates the active bucket without tearing down + reloading the
        # strip. This is what keeps the simple-mode VIS/IR switch (shared
        # storage dir) from flashing the strip; in full mode the dir
        # differs per bucket so this never triggers.
        if (obj is not None and obj is self._obj
                and target_dir == self._bound_dir):
            context_changed = audit_context != self._bound_audit_context
            self._side = side
            self._spectrum = spectrum
            self._set_audit_binding(obj, audit_context, audit_settings)
            current_path = self.current_file_path()
            # A changed binding (e.g. audits newly enabled, or the simple-
            # mode VIS/IR switch changing the modality) can leave the
            # currently shown file without its measurements — re-decode it
            # so the worker computes exactly the missing checks.
            if (context_changed and audit_context is not None
                    and current_path is not None
                    and self._missing_audit_checks(current_path)):
                self.reload_current()
            return

        self._unbind_previous()
        self._obj = obj
        self._side = side
        self._spectrum = spectrum
        self._bound_dir = target_dir
        self._set_audit_binding(obj, audit_context, audit_settings)
        # Connectivity dots belong to the bucket we're leaving — clear them
        # so they never linger on the new bucket. Fresh dots (if this is a
        # stitch bucket) arrive from main.py once its check completes.
        self.set_connectivity(None)

        if obj is None:
            self.close_directory()
            self.set_stitch_mode(False)
            self.set_chosen_stem(None)
            return

        # Configure the "Move to side X" menu entry for this side. With
        # only two sides, "other" is unambiguous. Simple mode has no sides
        # — leaving _other_side_* unset hides the move entry entirely.
        if not self._simple:
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
            audit_context=audit_context,
            missing_checks=(self._missing_audit_checks
                            if audit_context is not None else None),
        )

    def _build_context_menu(self, item):
        """Simple mode strips the menu down to Delete (no chosen / move).
        Full mode extends CaptureFilmstrip's default builder with the
        re-run-check action while an audit binding is active."""
        stem = stem_of(item.file_name)
        if self._simple:
            menu = QMenu(self)
            delete_action = menu.addAction("Delete capture…")
            delete_action.triggered.connect(
                lambda *_: self._confirm_and_delete(stem))
            return menu
        menu = super()._build_context_menu(item)
        if menu is not None and self._bound_audit_context is not None:
            menu.addSeparator()
            recheck = menu.addAction("Re-run capture check")
            recheck.triggered.connect(
                lambda *_: self.audit_recheck_requested.emit(stem))
        return menu

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

    def _set_audit_binding(
        self,
        obj: "Object | None",
        context: CaptureAuditContext | None,
        audit_settings: CaptureAuditSettings | None,
    ) -> None:
        """Adopt the host-supplied audit binding and repaint the badges.
        Persisted `_meta.json` entries are the only display source; this
        widget keeps no findings of its own."""
        if obj is not None and context is not None and audit_settings is None:
            raise ValueError("audit settings required for an enabled audit context")
        self._bound_audit_context = context
        self._audit_settings = audit_settings
        self.set_capture_audit_binding(
            context if obj is not None else None,
            self._missing_audit_checks if context is not None else None,
        )
        self.refresh_audit_badges()

    def refresh_audit_badges(self) -> None:
        """Repaint per-stem WARNING badges from the object's persisted
        audits — warn-only by design: a ✓ would claim "verified good",
        which the metrics cannot promise. Called on binding changes,
        object state changes, and by main.py after a fresh finding was
        persisted."""
        context = self._bound_audit_context
        if (self._obj is None or context is None
                or self._audit_settings is None):
            self.set_audit_badges(None, AUDIT_STATUS_COLORS)
            return
        persisted = read_capture_audits(self._obj.meta_path)
        status_by_stem = {}
        for capture in self._obj.captures(self._side, self._spectrum):
            warned = warned_checks(
                persisted.get(capture.stem, {}),
                context.request.modality, self._audit_settings)
            if warned & context.request.checks:
                status_by_stem[capture.stem] = "warn"
        self.set_audit_badges(status_by_stem, AUDIT_STATUS_COLORS)

    def _missing_audit_checks(self, path: str) -> frozenset[str]:
        """Which of the binding's checks have no current persisted entry
        for this capture. The filename stem is the established per-capture
        storage key (same key `persist_fresh_capture_audit` writes)."""
        context = self._bound_audit_context
        if context is None or self._obj is None:
            return frozenset()
        entries = read_capture_audits(self._obj.meta_path).get(
            Path(path).stem, {})
        present = frozenset(
            check for check, entry in entries.items()
            if entry_is_current(check, entry))
        return context.request.checks - present

    def _is_stitch_bucket(self) -> bool:
        """This bucket shows stitch overlays: a bound papyri object flagged
        for stitching. Simple mode never stitches."""
        return (not self._simple and self._obj is not None
                and self._obj.is_stitching())

    def _on_object_state_changed(self) -> None:
        """Re-drive the overlays when the bound object's state changes:
        the mode (normal ★ vs stitch ◎) and the marker stem. Simple mode
        has no markers. Connectivity dots are NOT touched here — they come
        from the async check via main.py."""
        self.refresh_audit_badges()
        if self._is_stitch_bucket():
            self.set_stitch_mode(True)
            reference = self._obj.reference(self._side, self._spectrum)
            self.set_reference_stem(reference.stem if reference else None)
            return
        self.set_stitch_mode(False)
        chosen = (self._obj.chosen(self._side, self._spectrum)
                  if self._obj and not self._simple else None)
        self.set_chosen_stem(chosen.stem if chosen else None)

    def _on_import_failed(self, dest) -> None:
        """A queued drop-import copy raised — drop its placeholder so
        the user doesn't stare at an orphaned spinner forever."""
        self.remove_placeholder(str(dest))

    # ---- action handlers (route CaptureFilmstrip signals → Object) ----

    def _on_mark_chosen_requested(self, stem: str) -> None:
        if self._obj is not None:
            self._obj.set_chosen(self._side, self._spectrum, stem)

    def _on_mark_reference_requested(self, stem: str) -> None:
        if self._obj is not None:
            self._obj.set_reference(self._side, self._spectrum, stem)

    def _on_unmark_reference_requested(self) -> None:
        if self._obj is not None:
            self._obj.clear_reference(self._side, self._spectrum)

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
