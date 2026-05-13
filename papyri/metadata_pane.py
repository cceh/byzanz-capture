"""Metadata pane for the current object.

Lives in the middle column (left of the workspace). Hosts:
  - the object's *name* as a fat title at the top: doubles as the "new object"
    input when nothing is loaded, and as a read-only display + ✏ rename / ×
    close button when an object is bound;
  - a small subtitle showing per-side capture counts;
  - a busy spinner shown while the object dir is loading;
  - the schema-driven metadata form (auto-saves to `<obj>/_meta.json`).

Schema definition + completeness check live in `papyri._metadata` so the
sidebar can derive its `??` vs `✓` badge from the same source of truth.
"""
from __future__ import annotations
import json
import os
from typing import TYPE_CHECKING

from PyQt6.QtCore import QSize, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QComboBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from byzanz_camera.helpers import get_ui_path
from byzanz_camera.spinner import Spinner
from papyri._layout import BUCKETS
from papyri._metadata import DEFAULT_SCHEMA, FieldSchema

if TYPE_CHECKING:
    from papyri.main import Object


_DEBOUNCE_MS = 500   # for longtext save coalescing


class MetadataPane(QFrame):
    """Schema-driven metadata form + name title row. Bind to an Object via `bind_object`.

    State propagation:
        Object change   →  bind_object()  →  load _meta.json, populate widgets, update title
        User edits      →  field commit   →  collect values, write _meta.json
                          (text fields debounced; line/choice immediate)

    Title row signals (wired up by main.py):
        start_object_requested(str)  — Enter pressed in the name field with no object bound
        rename_requested()           — ✏ button clicked (handler runs the rename flow)
        close_requested()            — × button clicked
    """

    metadata_changed = pyqtSignal()   # emitted after a successful write
    start_object_requested = pyqtSignal(str)
    rename_requested = pyqtSignal()
    close_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("metadataPane")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        # Width is owned by the splitter in main_window.ui (default 200, min 150).

        self._obj: "Object | None" = None
        self._schema: tuple[FieldSchema, ...] = DEFAULT_SCHEMA
        self._widgets: dict[str, QWidget] = {}
        self._loading = False  # suppresses save during programmatic populate

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(_DEBOUNCE_MS)
        self._save_timer.timeout.connect(self._save_now)

        self._build_ui()
        self._apply_styles()
        self._set_form_enabled(False)
        self._refresh_title_row()

    # ---- public API ----------------------------------------------------

    def bind_object(self, obj: "Object | None") -> None:
        """Switch to a different object's metadata. Flushes any pending
        debounced writes from the previous object first; disconnects the
        previous object's state_changed connection so a stale instance
        can't keep firing into _refresh_subtitle."""
        self._flush_pending_save()
        self._unbind_previous()
        self._obj = obj
        self._populate_from_disk()
        self._set_form_enabled(obj is not None)
        self._update_header()
        self._refresh_title_row()
        # Object change can flip the subtitle (capture counts) — re-render it
        # whenever the object's state_changed fires.
        if obj is not None:
            obj.state_changed.connect(self._refresh_subtitle)
        self._refresh_subtitle()

    def _unbind_previous(self) -> None:
        """Mirror of PapyriFilmstrip._unbind_previous — disconnect the
        prior object's state_changed connection (F-LEAK fix). No-op when
        nothing was bound."""
        if self._obj is None:
            return
        try:
            self._obj.state_changed.disconnect(self._refresh_subtitle)
        except TypeError:
            pass

    def set_loading_busy(self, busy: bool) -> None:
        """Drive the inline spinner in the title row.
        Called by main.py while the photo browser is opening the directory."""
        self._spinner.isAnimated = busy

    def focus_name_input(self) -> None:
        """Move keyboard focus to the name field (used by 'New object' affordance)."""
        if not self._name_field.isReadOnly():
            self._name_field.setFocus()
            self._name_field.selectAll()

    # ---- ui construction ----------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        # ---- title row: name field (fat target) + ✏ + × + spinner ----
        title_row = QHBoxLayout()
        title_row.setSpacing(2)
        title_row.setContentsMargins(0, 0, 0, 0)

        self._name_field = QLineEdit()
        self._name_field.setObjectName("metadataNameField")
        self._name_field.setPlaceholderText("Object name")
        # Plays double duty: typing + Enter creates the object; once an
        # object is bound, becomes read-only and shows the name as title.
        self._name_field.returnPressed.connect(self._on_name_return)
        font = self._name_field.font()
        font.setPointSize(20)
        font.setBold(True)
        self._name_field.setFont(font)
        title_row.addWidget(self._name_field, 1)

        self._spinner = Spinner(self)
        self._spinner.setFixedSize(18, 18)
        self._spinner.isAnimated = False
        title_row.addWidget(self._spinner, 0, Qt.AlignmentFlag.AlignVCenter)

        self._rename_btn = self._make_icon_button(
            "ui/rename.svg", fallback_text="✏", tooltip="Rename object"
        )
        self._rename_btn.clicked.connect(self.rename_requested.emit)
        title_row.addWidget(self._rename_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self._close_btn = self._make_icon_button(
            "ui/cancel.svg", fallback_text="×", tooltip="Close object"
        )
        self._close_btn.clicked.connect(self.close_requested.emit)
        title_row.addWidget(self._close_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        outer.addLayout(title_row)

        # subtitle: per-side capture summary
        self._subtitle = QLabel("")
        self._subtitle.setObjectName("metadataSubtitle")
        outer.addWidget(self._subtitle)

        outer.addSpacing(6)

        # ---- metadata header + form ----
        self._header = QLabel("METADATA")
        self._header.setObjectName("metadataHeader")
        outer.addWidget(self._header)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(8)
        # Labels stack above their fields — at 200px wide there isn't room
        # for a side-by-side label/field row.
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        outer.addLayout(form)

        for schema in self._schema:
            label_text = schema.label + (" *" if schema.required else "")
            label = QLabel(label_text)
            label.setObjectName("metadataLabel")

            widget = self._create_widget(schema)
            self._widgets[schema.name] = widget
            form.addRow(label, widget)

        outer.addStretch(1)

    @staticmethod
    def _make_icon_button(
        icon_path: str, *, fallback_text: str, tooltip: str
    ) -> QToolButton:
        """Build a flat icon tool button. Falls back to a unicode glyph if
        the SVG isn't bundled (so the layout still works during sketches)."""
        btn = QToolButton()
        btn.setToolTip(tooltip)
        btn.setAutoRaise(True)
        btn.setFixedSize(24, 24)
        try:
            icon = QIcon(get_ui_path(icon_path))
            if not icon.isNull():
                btn.setIcon(icon)
                btn.setIconSize(QSize(16, 16))
            else:
                btn.setText(fallback_text)
        except Exception:
            btn.setText(fallback_text)
        return btn

    def _create_widget(self, schema: FieldSchema) -> QWidget:
        if schema.type == "string":
            w = QLineEdit()
            w.editingFinished.connect(self._save_now)  # focus-loss
            return w
        if schema.type == "choice":
            w = QComboBox()
            w.addItem("")  # placeholder for "unset"
            for c in schema.choices:
                w.addItem(c)
            w.currentTextChanged.connect(self._on_choice_changed)
            return w
        if schema.type == "longtext":
            w = QPlainTextEdit()
            w.setFixedHeight(140)
            w.textChanged.connect(self._on_text_changed)  # debounced
            return w
        raise ValueError(f"unknown field type: {schema.type!r}")

    def _apply_styles(self) -> None:
        # Name field gets a visible text-field outline only when editable
        # (no object bound). Read-only state strips the chrome so it reads
        # as a title. Padding stays equal in both states so the text doesn't
        # jump on bind/unbind.
        self.setStyleSheet("""
            #metadataPane {
                background: #f8fafc;
                border-right: 1px solid #cbd5e1;
            }
            #metadataNameField {
                background: white;
                color: #0f172a;
                border: 1.5px solid #cbd5e1;
                border-radius: 6px;
                padding: 4px 8px;
            }
            #metadataNameField:focus {
                border-color: #3b82f6;
                outline: none;
            }
            #metadataNameField[readOnly="true"] {
                background: transparent;
                border: 1.5px solid transparent;
            }
            #metadataSubtitle {
                color: #94a3b8;
                font-size: 9pt;
            }
            #metadataHeader {
                color: #475569;
                font-weight: 700;
                font-size: 10pt;
                letter-spacing: 1px;
                padding: 0 0 4px 0;
            }
            #metadataLabel {
                color: #475569;
                font-size: 10pt;
            }
        """)

    def sizeHint(self) -> QSize:
        """Default to 200px wide. Combined with the splitter's stretchFactor
        (0 for this pane, 1 for the workspace), this is the initial width
        users see; they can drag down to the 150px minimum or wider."""
        base = super().sizeHint()
        return QSize(200, base.height())

    # ---- save plumbing -------------------------------------------------

    def _on_text_changed(self) -> None:
        if self._loading:
            return
        self._save_timer.start()  # restart debounce window

    def _on_choice_changed(self, _value: str) -> None:
        if self._loading:
            return
        self._save_now()

    def _save_now(self) -> None:
        """Flush any pending debounced save AND save current state."""
        self._save_timer.stop()
        self._flush_pending_save()

    def flush_pending_save(self) -> None:
        """Public alias for callers that need to commit edits before a
        filesystem operation (e.g. main.py before renaming the object dir)."""
        self._save_now()

    def _flush_pending_save(self) -> None:
        """Write the current form values to `<obj>/_meta.json`. No-op if no obj
        or if the object's directory no longer exists (e.g. it was just
        renamed/deleted under us — the in-memory edits are dropped)."""
        if self._obj is None or self._loading:
            return
        # Guard against the dir being renamed/removed between bind_object calls
        # — otherwise the open() below raises FileNotFoundError and the unhandled
        # exception inside the slot makes PyQt qFatal-abort the process.
        if not os.path.isdir(self._obj.dir):
            return
        data = self._collect_values()
        with open(self._obj.meta_path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        self._update_header()
        self.metadata_changed.emit()

    def _collect_values(self) -> dict:
        out: dict[str, str] = {}
        for schema in self._schema:
            w = self._widgets[schema.name]
            value = self._read_widget(w, schema)
            if value:  # omit empty values from JSON for cleanliness
                out[schema.name] = value
        return out

    @staticmethod
    def _read_widget(w: QWidget, schema: FieldSchema) -> str:
        if schema.type == "string":
            return w.text().strip()
        if schema.type == "choice":
            return w.currentText().strip()
        if schema.type == "longtext":
            return w.toPlainText().strip()
        return ""

    # ---- load plumbing -------------------------------------------------

    def _populate_from_disk(self) -> None:
        self._loading = True
        try:
            data = self._read_meta() if self._obj is not None else {}
            for schema in self._schema:
                w = self._widgets[schema.name]
                self._write_widget(w, schema, data.get(schema.name, ""))
        finally:
            self._loading = False

    def _read_meta(self) -> dict:
        try:
            with open(self._obj.meta_path) as f:
                return json.load(f) or {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _write_widget(w: QWidget, schema: FieldSchema, value: str) -> None:
        if schema.type == "string":
            w.setText(value)
        elif schema.type == "choice":
            idx = w.findText(value)
            w.setCurrentIndex(idx if idx >= 0 else 0)
        elif schema.type == "longtext":
            w.setPlainText(value)

    # ---- header / state ------------------------------------------------

    def _update_header(self) -> None:
        if self._obj is None:
            self._header.setText("METADATA")
            return
        required = [s for s in self._schema if s.required]
        if not required:
            self._header.setText("METADATA")
            return
        data = self._collect_values()
        filled = sum(1 for s in required if data.get(s.name))
        self._header.setText(f"METADATA  {filled}/{len(required)} required")

    def _set_form_enabled(self, enabled: bool) -> None:
        for w in self._widgets.values():
            w.setEnabled(enabled)

    # ---- title row -----------------------------------------------------

    def _refresh_title_row(self) -> None:
        """Sync name field + buttons to current bound state."""
        if self._obj is None:
            self._name_field.setReadOnly(False)
            self._name_field.clear()
            self._rename_btn.setVisible(False)
            self._close_btn.setVisible(False)
        else:
            self._name_field.setReadOnly(True)
            self._name_field.setText(self._obj.name)
            self._rename_btn.setVisible(True)
            self._close_btn.setVisible(True)
        # Force re-evaluation of the [readOnly] QSS attribute selector.
        self._name_field.style().unpolish(self._name_field)
        self._name_field.style().polish(self._name_field)

    def _refresh_subtitle(self) -> None:
        # Placeholder summary until D.3 lands the proper 2×2 grid: shows
        # "X/4 buckets" — how many of the four (side, spectrum) buckets
        # contain at least one capture.
        if self._obj is None:
            self._subtitle.setText("")
            return
        filled = sum(
            1 for side, spectrum in BUCKETS
            if self._obj.count(side, spectrum) > 0
        )
        self._subtitle.setText(f"{filled}/4 buckets")

    def _on_name_return(self) -> None:
        if self._obj is not None:
            return  # name is read-only when an object is bound
        text = self._name_field.text().strip().replace(" ", "_")
        if text:
            self.start_object_requested.emit(text)
