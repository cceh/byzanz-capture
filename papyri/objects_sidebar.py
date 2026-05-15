"""Objects sidebar — left rail listing all object directories in the working
directory with a status badge per row.

Status:
    · empty                 → no captures yet
    ?? has captures, metadata incomplete (per the schema in `papyri._metadata`)
    ✓  has captures + metadata complete
    (active row uses Qt's standard list selection highlight)

Phase B will add `✓✓` vs `●✓` for visible+IR completion.

The sidebar walks the working directory on demand (cheap; ~50 objects max
in typical use). It does NOT hold per-object QObject instances — the
canonical state of a single in-focus object lives in the `Object` model
that main.py manages.
"""
from __future__ import annotations
from dataclasses import dataclass

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame, QLabel, QListWidget, QListWidgetItem, QPushButton, QSizePolicy,
    QVBoxLayout,
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
    """Left-rail list of object directories in the current working dir.

    Public API:
        set_working_directory(path)   — point at a workdir, refresh
        set_active_object_name(name)  — highlight the row for `name` (or clear)
        refresh()                     — re-scan disk and rebuild the list

    Signals:
        object_selected(str)          — emitted with object name on row click
        new_object_requested()        — emitted when the "+ New object" button is clicked
    """

    object_selected = pyqtSignal(str)
    new_object_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("objectsSidebar")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.setMinimumWidth(160)
        self.setMaximumWidth(320)

        self._working_dir: str | None = None
        self._entries: list[ObjectListEntry] = []
        self._active_name: str | None = None

        self._build_ui()
        self._apply_styles()

    # ---- public API --------------------------------------------------

    def set_working_directory(self, path: str | None) -> None:
        if path == self._working_dir:
            return
        self._working_dir = path
        self._refresh_workdir_label()
        self.refresh()

    def _refresh_workdir_label(self) -> None:
        if self._working_dir:
            self._workdir_label.setText(self._working_dir)
            self._workdir_label.setToolTip(self._working_dir)
        else:
            self._workdir_label.setText("No working directory selected")
            self._workdir_label.setToolTip("")

    def set_active_object_name(self, name: str | None) -> None:
        """Visually mark the row for `name` as the active one."""
        self._active_name = name
        self._sync_selection()

    def refresh(self) -> None:
        """Re-scan the working dir and rebuild the list (preserves active highlight)."""
        self._entries = self._scan(self._working_dir)
        self._populate()
        self._sync_selection()

    # ---- internals ---------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 12, 8, 8)
        layout.setSpacing(6)

        self._header = QLabel("OBJECTS")
        self._header.setObjectName("sidebarHeader")
        layout.addWidget(self._header)

        self._list = QListWidget()
        self._list.setObjectName("sidebarList")
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        self._list.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self._list, 1)

        # Workdir display sits just above the + New button — the workdir is
        # the parent of every row in the list, so it belongs here.
        self._workdir_label = QLabel("No working directory selected")
        self._workdir_label.setObjectName("sidebarWorkdir")
        self._workdir_label.setWordWrap(True)
        layout.addWidget(self._workdir_label)

        self._new_button = QPushButton("+ New object")
        self._new_button.setObjectName("sidebarNewButton")
        self._new_button.clicked.connect(self.new_object_requested.emit)
        layout.addWidget(self._new_button)

    def _apply_styles(self) -> None:
        self.setStyleSheet("""
            #objectsSidebar {
                background: #f1f5f9;
                border-right: 1px solid #cbd5e1;
            }
            #sidebarHeader {
                color: #475569;
                font-weight: 700;
                font-size: 10pt;
                letter-spacing: 1px;
                padding: 0 6px 4px 6px;
            }
            #sidebarList {
                background: transparent;
                border: none;
                font-size: 11pt;
            }
            #sidebarList::item {
                padding: 6px 6px;
                border-radius: 4px;
            }
            #sidebarList::item:selected {
                background: #2563eb;
                color: white;
            }
            #sidebarWorkdir {
                color: #94a3b8;
                font-size: 8pt;
                padding: 4px 6px 0 6px;
            }
            #sidebarNewButton {
                padding: 6px 10px;
                color: #475569;
            }
        """)

    def _populate(self) -> None:
        self._list.blockSignals(True)
        self._list.clear()
        for entry in self._entries:
            item = QListWidgetItem(f" {entry.badge}    {entry.name}")
            item.setData(Qt.ItemDataRole.UserRole, entry.name)
            self._list.addItem(item)
        self._list.blockSignals(False)
        self._update_header_count()

    def _update_header_count(self) -> None:
        total = len(self._entries)
        # "Done" = captures landed AND required metadata filled in
        # (matches the ✓ badge's meaning).
        done = sum(
            1 for e in self._entries
            if e.has_captures and e.metadata_complete
        )
        if total == 0:
            self._header.setText("OBJECTS")
        else:
            self._header.setText(f"OBJECTS  {done}/{total}")

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
