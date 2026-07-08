"""Objects sidebar — left rail. Top: the open BOX (a working directory =
one physical box of papyri); below it, the OBJECTS in that box, one
two-line row each:

    name                     ⚠ [VIS] [IR]
    date of newest capture

    [VIS] / [IR]  — small spectrum pills (blue/orange, same QSS pattern as
                    the camera-state badge). Filled = both sides captured;
                    outlined = only one side so far; no pill = no captures
                    for that spectrum yet
    ⚠             — amber: has captures but required metadata is missing
                    (per the schema in `papyri._metadata`)
    no captures   — no pills, "no captures" as the (muted) date line
    (active row uses Qt's standard list selection highlight)

Box no. is the box directory's name, not a per-object field — switching or
creating a box is just opening/creating a folder (the box header's menu).

The sidebar walks the box directory on demand (cheap; ~100–200 objects max
per box). Rows are plain label widgets (`_ObjectRowWidget`), rebuilt on
every refresh and mouse-transparent so clicks and the context menu keep
hitting the QListWidget itself. It does NOT hold per-object QObject
instances — the canonical state of a single in-focus object lives in the
`Object` model that main.py manages.
"""
from __future__ import annotations
import os
from dataclasses import dataclass
from datetime import date, timedelta

from PyQt6.QtCore import QSize, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QDesktopServices, QPainter, QPalette
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMenu,
    QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from byzanz_camera.helpers import set_state
from papyri.capture_vocab import SIDES, SPECTRUM_INFRARED, SPECTRUM_VISIBLE
from papyri.object_layout import (
    captured_sides_for_spectrum, is_spectrum_complete, is_stitching_object,
    list_managed_objects, newest_capture_mtime,
)
from papyri._metadata import is_metadata_complete_for


@dataclass(frozen=True)
class ObjectListEntry:
    name: str
    vis_sides: int                  # sides with ≥1 visible capture (0–2)
    ir_sides: int                   # sides with ≥1 infrared capture (0–2)
    vis_complete: bool              # per object_layout.is_spectrum_complete
    ir_complete: bool
    metadata_complete: bool
    stitching: bool                 # oversized object captured as segments
    last_capture_ts: float | None   # newest capture mtime; None = no captures

    @property
    def has_captures(self) -> bool:
        return (self.vis_sides + self.ir_sides) > 0

    @property
    def needs_metadata(self) -> bool:
        """Amber ⚠: captures exist but required metadata is missing. Empty
        objects don't warn — a just-created object isn't a problem yet."""
        return self.has_captures and not self.metadata_complete


def _capture_date_text(ts: float | None) -> str:
    """'Today' / 'Yesterday' / '12 Jun' / '12 Jun 2025'. The day number is
    formatted manually because strftime's no-padding flag is platform-
    specific ('%-d' POSIX, '%#d' Windows) and there is a Windows build."""
    if ts is None:
        return "—"
    d = date.fromtimestamp(ts)
    today = date.today()
    if d == today:
        return "Today"
    if d == today - timedelta(days=1):
        return "Yesterday"
    fmt = "%b" if d.year == today.year else "%b %Y"
    return f"{d.day} {d.strftime(fmt)}"


def _spectrum_status(sides: int) -> str:
    """Tooltip wording for one spectrum: '—' / '1 of 2 sides' / 'both sides'."""
    if sides == 0:
        return "—"
    if sides < len(SIDES):
        return f"{sides} of {len(SIDES)} sides"
    return "both sides"


def _tooltip_text(entry: ObjectListEntry) -> str:
    """Row tooltip — full name (rows elide long ones) + status detail."""
    lines = [entry.name]
    if entry.stitching:
        lines.append("🪡 Stitching object")
    lines.append(
        f"VIS: {_spectrum_status(entry.vis_sides)}"
        f"    IR: {_spectrum_status(entry.ir_sides)}")
    if entry.has_captures:
        lines.append("Metadata: complete" if entry.metadata_complete
                     else "Metadata: incomplete")
        lines.append(f"Last capture: {_capture_date_text(entry.last_capture_ts)}")
    return "\n".join(lines)


class _ElidedLabel(QLabel):
    """QLabel that elides with '…' instead of hard-clipping when the sidebar
    is narrower than the text. Text color still comes from QSS — stylesheet
    `color:` lands in the widget palette, which the painter reads."""

    def paintEvent(self, _evt) -> None:
        p = QPainter(self)
        p.setPen(self.palette().color(QPalette.ColorRole.WindowText))
        p.setFont(self.font())
        elided = self.fontMetrics().elidedText(
            self.text(), Qt.TextElideMode.ElideRight, self.width())
        p.drawText(self.rect(),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   elided)


class _ObjectRowWidget(QWidget):
    """One object row: name + right-aligned ⚠/VIS/IR pills on the first
    line, capture date on the second. Plain labels in layouts — every
    visual property lives in app.qss under #sidebarRow* /
    #sidebarSpectrumPill."""

    def __init__(self, entry: ObjectListEntry, parent=None):
        super().__init__(parent)
        # Let clicks fall through to the QListWidget viewport so selection
        # and the context menu behave exactly as with plain items. Tooltips
        # therefore live on the QListWidgetItem, not on this widget.
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 4, 6, 4)
        outer.setSpacing(1)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(4)

        name = _ElidedLabel(entry.name)
        name.setObjectName("sidebarRowName")
        # Ignored: the label never forces the sidebar wider — long names
        # elide instead.
        name.setSizePolicy(QSizePolicy.Policy.Ignored,
                           QSizePolicy.Policy.Preferred)
        top.addWidget(name, 1)

        if entry.stitching:
            stitch = QLabel("🪡")
            stitch.setObjectName("sidebarRowStitch")
            stitch.setToolTip("Stitching object (captured as segments)")
            top.addWidget(stitch, 0)
        if entry.needs_metadata:
            warn = QLabel("⚠")
            warn.setObjectName("sidebarRowWarn")
            top.addWidget(warn, 0)
        for spectrum_label, sides, complete in (
                ("VIS", entry.vis_sides, entry.vis_complete),
                ("IR", entry.ir_sides, entry.ir_complete)):
            if sides == 0:
                continue
            pill = QLabel(spectrum_label)
            pill.setObjectName("sidebarSpectrumPill")
            set_state(pill, "spectrum", spectrum_label)
            # Filled pill = spectrum complete (both sides); outlined
            # ("partial") = one side still missing.
            set_state(pill, "partial", not complete)
            top.addWidget(pill, 0)
        outer.addLayout(top)

        date_label = QLabel(_capture_date_text(entry.last_capture_ts)
                            if entry.has_captures else "no captures")
        date_label.setObjectName("sidebarRowDate")
        outer.addWidget(date_label)


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
        new_box_requested()           — "New box folder…" chosen
        open_box_requested()          — "Open box folder…" chosen
        recent_box_chosen(str)        — a recent box path chosen
        delete_object_requested(str)  — "Move to Trash" on a row (main.py
                                        confirms + trashes the object dir)
    """

    object_selected = pyqtSignal(str)
    new_object_requested = pyqtSignal()
    new_box_requested = pyqtSignal()
    open_box_requested = pyqtSignal()
    recent_box_chosen = pyqtSignal(str)
    delete_object_requested = pyqtSignal(str)   # object name (confirm + trash in main.py)

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

        # Box header — the open box + a menu to switch (recents) / open / new,
        # with an "open in Finder" button alongside it.
        box_row = QHBoxLayout()
        box_row.setContentsMargins(0, 0, 0, 0)
        box_row.setSpacing(6)
        self._box_button = QToolButton()
        self._box_button.setObjectName("sidebarBoxButton")
        self._box_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._box_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._box_button.setSizePolicy(QSizePolicy.Policy.Expanding,
                                       QSizePolicy.Policy.Fixed)
        self._box_menu = QMenu(self._box_button)
        self._box_menu.aboutToShow.connect(self._rebuild_box_menu)
        self._box_button.setMenu(self._box_menu)
        box_row.addWidget(self._box_button, 1)

        self._box_finder_button = QToolButton()
        self._box_finder_button.setObjectName("sidebarBoxFinderButton")
        self._box_finder_button.setText("📂")
        self._box_finder_button.setToolTip("Open box folder in Finder")
        self._box_finder_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._box_finder_button.clicked.connect(self._reveal_box)
        box_row.addWidget(self._box_finder_button, 0)
        layout.addLayout(box_row)
        self._refresh_box_label()

        self._header = QLabel("OBJECTS")
        self._header.setObjectName("sidebarHeader")
        layout.addWidget(self._header)

        self._list = QListWidget()
        self._list.setObjectName("sidebarList")
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        self._list.itemClicked.connect(self._on_item_clicked)
        # Right-click any object row → "Open in Finder".
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_list_context_menu)
        layout.addWidget(self._list, 1)

        self._new_button = QPushButton("+ New object")
        self._new_button.setObjectName("sidebarNewButton")
        self._new_button.clicked.connect(self.new_object_requested.emit)
        layout.addWidget(self._new_button)

    # Styles for #objectsSidebar / #sidebarBoxButton / #sidebarHeader /
    # #sidebarList / #sidebarRow* / #sidebarSpectrumPill /
    # #sidebarNewButton live in papyri/ui/app.qss — installed once at
    # app startup.

    def _refresh_box_label(self) -> None:
        if self._working_dir:
            name = os.path.basename(os.path.normpath(self._working_dir))
            self._box_button.setText(f"📦  {name}  ▾")
            self._box_button.setToolTip(self._working_dir)
        else:
            self._box_button.setText("📦  Open a box  ▾")
            self._box_button.setToolTip("")
        # Finder button only makes sense when a box is open.
        self._box_finder_button.setEnabled(bool(self._working_dir))

    def _refresh_objects_header(self) -> None:
        n = len(self._entries)
        self._header.setText(f"OBJECTS · {n}" if n else "OBJECTS")

    def _rebuild_box_menu(self) -> None:
        """Rebuilt on each open so recents / current-box checkmark stay fresh.
        Commands first, then a divider, then recent boxes underneath."""
        self._box_menu.clear()
        self._box_menu.addAction("New box folder…", self.new_box_requested.emit)
        self._box_menu.addAction("Open existing box folder…",
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
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, entry.name)
            item.setToolTip(_tooltip_text(entry))
            row = _ObjectRowWidget(entry)
            # Width 0: only the height matters — the list stretches rows
            # to the viewport width, and names elide rather than scroll.
            item.setSizeHint(QSize(0, row.sizeHint().height()))
            self._list.addItem(item)
            self._list.setItemWidget(item, row)
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

    def _on_list_context_menu(self, pos) -> None:
        """Right-click on an object row → "Open in Finder" for that object."""
        item = self._list.itemAt(pos)
        if item is None:
            return
        name = item.data(Qt.ItemDataRole.UserRole)
        if not name:
            return
        menu = QMenu(self._list)
        menu.addAction("Open in Finder", lambda: self._reveal_object(name))
        menu.addSeparator()
        menu.addAction("Move to Trash",
                       lambda: self.delete_object_requested.emit(name))
        menu.exec(self._list.viewport().mapToGlobal(pos))

    # ---- reveal-in-Finder --------------------------------------------

    def _reveal_box(self) -> None:
        self._reveal_in_finder(self._working_dir)

    def _reveal_object(self, name: str) -> None:
        if self._working_dir:
            self._reveal_in_finder(os.path.join(self._working_dir, name))

    @staticmethod
    def _reveal_in_finder(path: str | None) -> None:
        """Open a folder in the OS file manager (Finder / Explorer). No-op if
        the path is missing — reveal is best-effort."""
        if path and os.path.isdir(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    @staticmethod
    def _scan(working_dir: str | None) -> list[ObjectListEntry]:
        if working_dir is None:
            return []
        entries = []
        for name in list_managed_objects(working_dir):
            obj_dir = os.path.join(working_dir, name)
            entries.append(ObjectListEntry(
                name=name,
                vis_sides=captured_sides_for_spectrum(obj_dir, SPECTRUM_VISIBLE),
                ir_sides=captured_sides_for_spectrum(obj_dir, SPECTRUM_INFRARED),
                vis_complete=is_spectrum_complete(obj_dir, SPECTRUM_VISIBLE),
                ir_complete=is_spectrum_complete(obj_dir, SPECTRUM_INFRARED),
                metadata_complete=is_metadata_complete_for(working_dir, name),
                stitching=is_stitching_object(obj_dir),
                last_capture_ts=newest_capture_mtime(obj_dir),
            ))
        return entries
