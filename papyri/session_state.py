"""Centralized orchestrator state for papyri.

Owns the cross-cutting state axes that drive UI reactivity (active bucket,
current object, camera states per spectrum, live-view paused intent, viewer
mode, advanced-config dialog handle). Each reactive axis exposes a single
setter and a single signal; receivers in MainWindow subscribe and read back
from the session — never from signal arguments — so that any receiver can
be invoked anytime to (re-)render the correct state.

Per-axis migration is tracked in the 7-stage refactor plan; until each
axis lands here it remains on MainWindow.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PyQt6.QtCore import QObject, pyqtSignal

from papyri._layout import SIDE_A, SPECTRUM_INFRARED, SPECTRUM_VISIBLE

if TYPE_CHECKING:
    from byzanz_camera.camera_worker import CameraStates
    from camera_config_dialog import CameraConfigDialog
    from papyri.main import Object


class SessionState(QObject):
    # ---- signals ------------------------------------------------------

    # B1+B2 — active bucket. Atomic: side and spectrum always change
    # together. IR-fallback (when caller asks for IR but no IR worker is
    # configured) is enforced caller-side, not in this setter — keeps
    # SessionState ignorant of worker availability.
    active_bucket_changed = pyqtSignal(str, str)  # side, spectrum

    # B5 — current object reference. Emits the new value (or None);
    # receivers either ignore the arg and read from session, or use the
    # arg as a convenience. Identity comparison in the setter — a
    # re-bind of the SAME instance is a no-op.
    current_object_changed = pyqtSignal(object)  # Object | None

    # B6 — live-view paused intent. Single source of truth — the pause
    # button is a UI mirror (F-DUP fix) wired via _refresh_pause_button_text.
    # Action handlers (button toggle, thumb selection, directory load) call
    # the setter; receivers do the rest.
    live_view_paused_changed = pyqtSignal(bool)

    # B7 — viewer mode. Atomic (mode, label): the label is meaningful only
    # for "preview" mode but always travels with the mode change.
    # Receivers use the args for convenience but should still read from
    # session for idempotency.
    view_mode_changed = pyqtSignal(str, str)  # mode, label

    # B3+B4 — per-spectrum camera state. One signal for both spectra; the
    # spectrum arg distinguishes. Receivers either gate on
    # `spectrum == active_spectrum` (active-only effects) or fire for any
    # spectrum (per-camera lifecycle effects).
    camera_state_changed = pyqtSignal(str, object)  # spectrum, CameraStates.StateType

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._logger = logging.getLogger(self.__class__.__name__)

        # B1+B2 — workflow active bucket.
        self._active_side: str = SIDE_A
        self._active_spectrum: str = SPECTRUM_VISIBLE

        # B5 — current object reference.
        self._current_object: "Object | None" = None

        # B6 — live-view paused intent.
        self._live_view_paused: bool = False

        # B7 — viewer mode (one of "live" / "paused" / "preview" / "empty").
        # Label is the per-state extra (typically a stem for "preview").
        self._view_mode: str = "empty"
        self._view_mode_label: str = ""

        # B3+B4 — per-spectrum camera state. Both default to None (workers
        # haven't initialized yet); first emission for each is Waiting.
        self._camera_states: dict[str, "CameraStates.StateType | None"] = {
            SPECTRUM_VISIBLE: None,
            SPECTRUM_INFRARED: None,
        }

        # B8 — per-camera advanced-config dialog handle. At most one open at
        # a time (option A in the design discussion); spectrum tracks which
        # worker the dialog is for so the inline Disconnecting auto-reject
        # in _on_camera_state_changed only fires for the matching spectrum.
        # No signal: only the inline gate observes; that's a side-effect of
        # the camera-state handler, not a UI repaint.
        self._cam_config_dialog: "CameraConfigDialog | None" = None
        self._cam_config_dialog_spectrum: str | None = None

    # ---- B1 + B2 active_bucket ----------------------------------------

    @property
    def active_side(self) -> str:
        return self._active_side

    @property
    def active_spectrum(self) -> str:
        return self._active_spectrum

    def set_active_bucket(self, side: str, spectrum: str) -> None:
        """Atomic — side and spectrum always change together. No-op when
        the new value matches the old (keeps emissions clean and stops
        receivers from running for nothing)."""
        if (side, spectrum) == (self._active_side, self._active_spectrum):
            return
        self._active_side = side
        self._active_spectrum = spectrum
        self._logger.info("active_bucket = (%s, %s)", side, spectrum)
        self.active_bucket_changed.emit(side, spectrum)

    # ---- B5 current_object --------------------------------------------

    @property
    def current_object(self) -> "Object | None":
        return self._current_object

    def set_current_object(self, obj: "Object | None") -> None:
        """Identity-compare — re-binding the same instance is a no-op.
        A new Object with the same name (e.g. after rename re-construction)
        IS a different reference and DOES emit."""
        if obj is self._current_object:
            return
        self._current_object = obj
        self._logger.info("current_object = %s",
                          obj.name if obj is not None else None)
        self.current_object_changed.emit(obj)

    # ---- B6 live_view_paused ------------------------------------------

    @property
    def live_view_paused(self) -> bool:
        return self._live_view_paused

    def set_live_view_paused(self, paused: bool) -> None:
        if paused == self._live_view_paused:
            return
        self._live_view_paused = paused
        self._logger.info("live_view_paused = %s", paused)
        self.live_view_paused_changed.emit(paused)

    # ---- B7 view_mode -------------------------------------------------

    @property
    def view_mode(self) -> str:
        return self._view_mode

    @property
    def view_mode_label(self) -> str:
        return self._view_mode_label

    def set_view_mode(self, mode: str, label: str = "") -> None:
        """Atomic (mode, label). Caller is responsible for valid modes
        (one of "live" / "paused" / "preview" / "empty") — no validation
        here per the setters-mutate-and-emit-only rule."""
        if mode == self._view_mode and label == self._view_mode_label:
            return
        self._view_mode = mode
        self._view_mode_label = label
        self._logger.info(
            "view_mode = %s%s", mode, f" ({label})" if label else ""
        )
        self.view_mode_changed.emit(mode, label)

    # ---- B3+B4 camera_state per spectrum ------------------------------

    def camera_state(self, spectrum: str) -> "CameraStates.StateType | None":
        """Per-spectrum accessor."""
        return self._camera_states[spectrum]

    @property
    def active_camera_state(self) -> "CameraStates.StateType | None":
        """Convenience: the active spectrum's camera state."""
        return self._camera_states[self._active_spectrum]

    def set_camera_state(
        self, spectrum: str, state: "CameraStates.StateType"
    ) -> None:
        """Identity-compare in the no-op guard — different state instances
        with the same class always emit (e.g. CaptureInProgress with a
        new num_captured count is a meaningful re-emit)."""
        if state is self._camera_states[spectrum]:
            return
        self._camera_states[spectrum] = state
        short = "VIS" if spectrum == SPECTRUM_VISIBLE else "IR"
        self._logger.info("[%s] %s", short, state.__class__.__name__)
        self.camera_state_changed.emit(spectrum, state)

    # ---- B8 cam_config_dialog -----------------------------------------

    @property
    def cam_config_dialog(self) -> "CameraConfigDialog | None":
        return self._cam_config_dialog

    @property
    def cam_config_dialog_spectrum(self) -> str | None:
        return self._cam_config_dialog_spectrum

    def set_cam_config_dialog(
        self,
        dialog: "CameraConfigDialog | None",
        spectrum: str | None,
    ) -> None:
        """Atomic setter: pass `(dialog, spectrum)` when opening,
        `(None, None)` when clearing. The two fields always change together
        so they share one entry point per the locked-in atomicity rule."""
        if (dialog is self._cam_config_dialog
                and spectrum == self._cam_config_dialog_spectrum):
            return
        self._cam_config_dialog = dialog
        self._cam_config_dialog_spectrum = spectrum
        if dialog is None:
            self._logger.info("cam_config_dialog = closed")
        else:
            self._logger.info("cam_config_dialog = open (%s)", spectrum)
