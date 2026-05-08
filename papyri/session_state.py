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

from PyQt6.QtCore import QObject

if TYPE_CHECKING:
    from camera_config_dialog import CameraConfigDialog


class SessionState(QObject):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._logger = logging.getLogger(self.__class__.__name__)

        # B8 — per-camera advanced-config dialog handle. At most one open at
        # a time (option A in the design discussion); spectrum tracks which
        # worker the dialog is for so the inline Disconnecting auto-reject
        # in _on_camera_state_changed only fires for the matching spectrum.
        # No signal: only the inline gate observes; that's a side-effect of
        # the camera-state handler, not a UI repaint.
        self._cam_config_dialog: "CameraConfigDialog | None" = None
        self._cam_config_dialog_spectrum: str | None = None

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
