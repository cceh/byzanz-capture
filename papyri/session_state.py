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

from PyQt6.QtCore import QObject


class SessionState(QObject):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._logger = logging.getLogger(self.__class__.__name__)
