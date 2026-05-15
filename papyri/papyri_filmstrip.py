"""PapyriFilmstrip — CaptureFilmstrip bound to papyri's Object model.

Thin papyri-specific wrapper. Knows how to:
  - bind to an Object + (side, spectrum) bucket
  - read the chosen stem from obj.chosen(side, spectrum) and push it to
    CaptureFilmstrip's set_chosen_stem
  - configure the move-to-other-side menu entry per current side
  - route CaptureFilmstrip's action signals to Object mutation methods
  - keep the ★ overlay in sync with obj.state_changed

Replaces the previous PapyriCaptureBrowser (subclass of the monolithic
PhotoBrowser); same external behavior, smaller surface — no viewer code
because the viewer is now ViewerWidget, a separate widget driven
directly from main.py via filmstrip signals.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from byzanz_camera.capture_filmstrip import CaptureFilmstrip
from papyri._layout import SIDE_A, SIDE_B, SPECTRUM_VISIBLE

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
        try:
            self._obj.state_changed.disconnect(self._on_object_state_changed)
        except TypeError:
            pass

    def _on_object_state_changed(self) -> None:
        """Refresh chosen-stem when the bound object's state changes."""
        chosen = self._obj.chosen(self._side, self._spectrum) if self._obj else None
        self.set_chosen_stem(chosen.stem if chosen else None)

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
