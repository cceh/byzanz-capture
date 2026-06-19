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
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QToolButton, QWidget,
)

from byzanz_camera.helpers import get_ui_path, set_themed_icon

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
    # Simple mode only: user clicked the output-folder affordance.
    output_folder_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        uic.loadUi(get_ui_path("papyri/ui/object_title_bar.ui"), self)

        # uic.loadUi installs child widgets as attributes by objectName.
        # Re-declared here for the type checker / IDE.
        self.nameField: QLineEdit
        self.renameButton: QToolButton
        self.closeButton: QToolButton

        # The icons set in the .ui are raw — override with themed
        # versions so they track light/dark, registering for live
        # refresh on scheme change.
        set_themed_icon(self.renameButton.setIcon, get_ui_path("ui/rename.svg"))
        set_themed_icon(self.closeButton.setIcon, get_ui_path("ui/cancel.svg"))

        self._obj: "Object | None" = None
        # Simple-mode state: the name field becomes a free-text filename
        # override and a second row hosts the output-folder picker.
        self._simple = False
        self._folder_row: QHBoxLayout | None = None
        self._folder_label: QLabel | None = None
        self._folder_button: QPushButton | None = None
        self._output_dir = ""
        self._refresh_title_row()

    # ---- simple mode ---------------------------------------------------

    def set_simple_mode(self, simple: bool, output_dir: str = "") -> None:
        """Turn the title bar into the simple-mode layout: the name field
        is a persistent filename override (live), and a folder row below
        shows / changes the output directory. Call once at startup."""
        self._simple = simple
        if simple:
            self.nameField.setPlaceholderText("Filename (empty = camera name)")
            self._ensure_folder_row()
            self.set_output_folder(output_dir)
            # Live override — update on every keystroke (cheap; the slot in
            # main.py just sets SimpleTarget.name_override).
            self.nameField.textChanged.connect(self._on_simple_text_changed)
        self._refresh_title_row()

    def set_output_folder(self, path: str) -> None:
        """Update the folder-row label to reflect the current output dir."""
        self._output_dir = path or ""
        if self._folder_label is None:
            return
        if self._output_dir:
            self._folder_label.setText(f"📁 {self._output_dir}")
            self._folder_button.setText("Change folder…")
        else:
            self._folder_label.setText("No output folder selected")
            self._folder_button.setText("Choose folder…")
        self._folder_label.setToolTip(self._output_dir)

    def current_name(self) -> str:
        """The current filename-override text (simple mode)."""
        return self.nameField.text().strip()

    def _ensure_folder_row(self) -> None:
        if self._folder_row is not None:
            return
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self._folder_label = QLabel()
        self._folder_label.setObjectName("simpleFolderLabel")
        self._folder_button = QPushButton("Choose folder…")
        self._folder_button.setObjectName("simpleFolderButton")
        self._folder_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._folder_button.clicked.connect(self.output_folder_requested.emit)
        row.addWidget(self._folder_label, 1)
        row.addWidget(self._folder_button, 0)
        self.outerLayout.addLayout(row)
        self._folder_row = row

    def _on_simple_text_changed(self, text: str) -> None:
        self.start_object_requested.emit(text)

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
        if self._simple:
            # Persistent override field — never read-only, never cleared on
            # rebind (preserve what the user typed), no rename/close.
            self.nameField.setReadOnly(False)
            self.renameButton.setVisible(False)
            self.closeButton.setVisible(False)
            self.nameField.style().unpolish(self.nameField)
            self.nameField.style().polish(self.nameField)
            return
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
