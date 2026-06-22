"""Objects sidebar — left rail. Top: the open BOX (a working directory =
one physical box of papyri); below it, the OBJECTS in that box with a
status badge per row.

Box no. is the box directory's name, not a per-object field — switching or
creating a box is just opening/creating a folder (the box header's menu).

Status:
    · empty                 → no captures yet
    ?? has captures, metadata incomplete (per the schema in `papyri._metadata`)
    ✓  has captures + metadata complete
    (active row uses Qt's standard list selection highlight)

The sidebar walks the box directory on demand (cheap; ~100–200 objects max
per box). It does NOT hold per-object QObject instances — the canonical
state of a single in-focus object lives in the `Object` model that main.py
manages.
"""
from __future__ import annotations
import os
from dataclasses import dataclass

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QFrame, QLabel, QListWidget, QListWidgetItem, QMenu, QPushButton,
    QSizePolicy, QToolButton, QVBoxLayout,
)

from papyri._layout import has_any_captures_for, list_managed_objects
from papyri._metadata import is_metadata_complete_for


_BADGE_EMPTY = "·"
_BADGE_INCOMPLETE = "??"
_BADGE_COMPLETE = "✓"


@dataclass(frozen=True)
class ObjectListEntry:
    name: str
    has_captures: bool
    metadata_complete: bool

    @property
    def badge(self) -> str:
        if not self.has_captures:
            return _BADGE_EMPTY
        return _BADGE_COMPLETE if self.metadata_complete else _BADGE_INCOMPLETE


class ObjectsSidebar(QFrame):
    """Left-rail box header + object list.

    Public API:
        set_working_directory(path)   — point at a box dir, refresh
        set_recent_boxes(paths)       — populate the box menu's recents
        set_active_object_name(name)  — highlight the row for `name` (or clear)
        refresh()                     — re-scan disk and rebuild the list

    Signals:
        object_selected(str)          — object name on row click
        new_object_requested()        — "+ New object" clicked
        new_box_requested()           — "New box directory…" chosen
        open_box_requested()          — "Open box directory…" chosen
        recent_box_chosen(str)        — a recent box path chosen
    """

    object_selected = pyqtSignal(str)
    new_object_requested = pyqtSignal()
    new_box_requested = pyqtSignal()
    open_box_requested = pyqtSignal()
    recent_box_chosen = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("objectsSidebar")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.setMinimumWidth(160)
        self.setMaximumWidth(320)

        self._working_dir: str | None = None
        self._recent_boxes: list[str] = []
        self._entries: list[ObjectListEntry] = []
        self._active_name: str | None = None

        self._build_ui()

    # ---- public API --------------------------------------------------

    def set_working_directory(self, path: str | None) -> None:
        if path == self._working_dir:
            return
        self._working_dir = path
        self._refresh_box_label()
        self.refresh()

    def set_recent_boxes(self, paths: list[str]) -> None:
        """Recent box directories shown in the box menu (most-recent first)."""
        self._recent_boxes = list(paths)

    def set_active_object_name(self, name: str | None) -> None:
        """Visually mark the row for `name` as the active one."""
        self._active_name = name
        self._sync_selection()

    def refresh(self) -> None:
        """Re-scan the box dir and rebuild the list (preserves active highlight)."""
        self._entries = self._scan(self._working_dir)
        self._populate()
        self._sync_selection()
        self._refresh_objects_header()

    # ---- internals ---------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 12, 8, 8)
        layout.setSpacing(6)

        # Box header — the open box + a menu to switch (recents) / open / new.
        self._box_button = QToolButton()
        self._box_button.setObjectName("sidebarBoxButton")
        self._box_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._box_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._box_button.setSizePolicy(QSizePolicy.Policy.Expanding,
                                       QSizePolicy.Policy.Fixed)
        self._box_menu = QMenu(self._box_button)
        self._box_menu.aboutToShow.connect(self._rebuild_box_menu)
        self._box_button.setMenu(self._box_menu)
        layout.addWidget(self._box_button)
        self._refresh_box_label()

        self._header = QLabel("OBJECTS")
        self._header.setObjectName("sidebarHeader")
        layout.addWidget(self._header)

        self._list = QListWidget()
        self._list.setObjectName("sidebarList")
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        self._list.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self._list, 1)

        self._new_button = QPushButton("+ New object")
        self._new_button.setObjectName("sidebarNewButton")
        self._new_button.clicked.connect(self.new_object_requested.emit)
        layout.addWidget(self._new_button)

    # Styles for #objectsSidebar / #sidebarBoxButton / #sidebarHeader /
    # #sidebarList / #sidebarNewButton live in papyri/ui/app.qss —
    # installed once at app startup.

    def _refresh_box_label(self) -> None:
        if self._working_dir:
            name = os.path.basename(os.path.normpath(self._working_dir))
            self._box_button.setText(f"📦  {name}  ▾")
            self._box_button.setToolTip(self._working_dir)
        else:
            self._box_button.setText("📦  Open a box  ▾")
            self._box_button.setToolTip("")

    def _refresh_objects_header(self) -> None:
        n = len(self._entries)
        self._header.setText(f"OBJECTS · {n}" if n else "OBJECTS")

    def _rebuild_box_menu(self) -> None:
        """Rebuilt on each open so recents / current-box checkmark stay fresh.
        Commands first, then a divider, then recent boxes underneath."""
        self._box_menu.clear()
        self._box_menu.addAction("New box directory…", self.new_box_requested.emit)
        self._box_menu.addAction("Open existing box directory…",
                                 self.open_box_requested.emit)
        if self._recent_boxes:
            self._box_menu.addSeparator()
            current = (os.path.normpath(self._working_dir)
                       if self._working_dir else None)
            for path in self._recent_boxes:
                name = os.path.basename(os.path.normpath(path))
                act = QAction(f"📦  {name}", self._box_menu)
                act.setCheckable(True)
                act.setChecked(os.path.normpath(path) == current)
                act.setToolTip(path)
                act.triggered.connect(
                    lambda _checked, p=path: self.recent_box_chosen.emit(p))
                self._box_menu.addAction(act)

    def _populate(self) -> None:
        self._list.blockSignals(True)
        self._list.clear()
        for entry in self._entries:
            item = QListWidgetItem(f" {entry.badge}    {entry.name}")
            item.setData(Qt.ItemDataRole.UserRole, entry.name)
            self._list.addItem(item)
        self._list.blockSignals(False)

    def _sync_selection(self) -> None:
        """Highlight the row matching the active object name."""
        self._list.blockSignals(True)
        self._list.clearSelection()
        if self._active_name is not None:
            for i in range(self._list.count()):
                if self._list.item(i).data(Qt.ItemDataRole.UserRole) == self._active_name:
                    self._list.setCurrentRow(i)
                    break
        self._list.blockSignals(False)

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        name = item.data(Qt.ItemDataRole.UserRole)
        if name and name != self._active_name:
            self.object_selected.emit(name)

    @staticmethod
    def _scan(working_dir: str | None) -> list[ObjectListEntry]:
        if working_dir is None:
            return []
        return [
            ObjectListEntry(
                name=name,
                has_captures=has_any_captures_for(working_dir, name),
                metadata_complete=is_metadata_complete_for(working_dir, name),
            )
            for name in list_managed_objects(working_dir)
        ]
