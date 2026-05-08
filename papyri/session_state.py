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

from papyri._layout import SIDE_A, SPECTRUM_VISIBLE

if TYPE_CHECKING:
    from camera_config_dialog import CameraConfigDialog


class SessionState(QObject):
    # ---- signals ------------------------------------------------------

    # B1+B2 — active bucket. Atomic: side and spectrum always change
    # together. IR-fallback (when caller asks for IR but no IR worker is
    # configured) is enforced caller-side, not in this setter — keeps
    # SessionState ignorant of worker availability.
    active_bucket_changed = pyqtSignal(str, str)  # side, spectrum

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._logger = logging.getLogger(self.__class__.__name__)

        # B1+B2 — workflow active bucket.
        self._active_side: str = SIDE_A
        self._active_spectrum: str = SPECTRUM_VISIBLE

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
