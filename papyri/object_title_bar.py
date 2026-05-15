"""Object title bar — the fat name field at the top of the window.

Hosts the object name (also doubles as input for new objects) and
rename / close buttons. Layout, sizes, fonts, QSS, and button
signal-forwards live in `papyri/ui/object_title_bar.ui` — see the .ui
for everything static. This module carries only the dynamic state
logic (bind_object, name-input return handling).

The .ui wires:
    renameButton.clicked  → rename_requested
    closeButton.clicked   → close_requested
    nameField.returnPressed → _on_name_return  (then emits
        start_object_requested if the field is editable + non-empty)
"""
from __future__ import annotations
from typing import TYPE_CHECKING

from PyQt6 import uic
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QLineEdit, QToolButton, QWidget

from byzanz_camera.helpers import get_ui_path

if TYPE_CHECKING:
    from papyri.main import Object


class ObjectTitleBar(QWidget):
    """Top-of-window title row, bound to the current object.

    State propagation:
        None      → name field editable + empty + placeholder;
                    rename/close hidden.
        Object    → name field read-only and showing obj.name;
                    rename/close visible.
    """

    rename_requested = pyqtSignal()
    close_requested = pyqtSignal()
    start_object_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        uic.loadUi(get_ui_path("papyri/ui/object_title_bar.ui"), self)

        # uic.loadUi installs child widgets as attributes by objectName.
        # Re-declared here for the type checker / IDE.
        self.nameField: QLineEdit
        self.renameButton: QToolButton
        self.closeButton: QToolButton

        self._obj: "Object | None" = None
        self._refresh_title_row()

    # ---- public API ----------------------------------------------------

    def bind_object(self, obj: "Object | None") -> None:
        """Switch to a different object (or None)."""
        self._obj = obj
        self._refresh_title_row()

    def focus_name_input(self) -> None:
        """Move keyboard focus to the name field (used by the sidebar's
        'New object' affordance). No-op when read-only (object bound)."""
        if not self.nameField.isReadOnly():
            self.nameField.setFocus()
            self.nameField.selectAll()

    # ---- internals -----------------------------------------------------

    def _refresh_title_row(self) -> None:
        if self._obj is None:
            self.nameField.setReadOnly(False)
            self.nameField.clear()
            self.renameButton.setVisible(False)
            self.closeButton.setVisible(False)
        else:
            self.nameField.setReadOnly(True)
            self.nameField.setText(self._obj.name)
            self.renameButton.setVisible(True)
            self.closeButton.setVisible(True)
        # Force re-evaluation of the [readOnly="true"] QSS attribute
        # selector — Qt doesn't repolish on dynamic-property change.
        self.nameField.style().unpolish(self.nameField)
        self.nameField.style().polish(self.nameField)

    def _on_name_return(self) -> None:
        """Slot wired in the .ui to nameField.returnPressed. Only fires
        a start_object_requested when no object is bound (the field is
        read-only otherwise, so returnPressed wouldn't normally arrive,
        but guard anyway)."""
        if self._obj is not None:
            return
        text = self.nameField.text().strip()
        if text:
            self.start_object_requested.emit(text)
