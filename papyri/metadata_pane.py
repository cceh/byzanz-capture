"""Metadata pane for the current object.

Schema-driven metadata form that auto-saves to `<obj>/_meta.json`.
Bound to an Object via `bind_object`. Lives on the right side of the
splitter; the object's name + rename/close buttons live in the
separate ObjectTitleBar at the top of the window.

Schema definition + completeness check live in `papyri._metadata` so
the sidebar can derive its `??` vs `✓` badge from the same source of
truth, and the form header can show "X/N required".
"""
from __future__ import annotations
import json
import os
from typing import TYPE_CHECKING

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QFormLayout, QFrame, QLabel, QLineEdit, QPlainTextEdit,
    QSizePolicy, QVBoxLayout, QWidget,
)

from papyri._metadata import DEFAULT_SCHEMA, FieldSchema

if TYPE_CHECKING:
    from papyri.main import Object


_DEBOUNCE_MS = 500   # for longtext save coalescing


class MetadataPane(QFrame):
    """Schema-driven metadata form. Bind to an Object via `bind_object`.

    State propagation:
        Object change   →  bind_object()  →  load _meta.json, populate widgets
        User edits      →  field commit   →  collect values, write _meta.json
                          (text fields debounced; line/choice immediate)
    """

    metadata_changed = pyqtSignal()   # emitted after a successful write

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

    # ---- public API ----------------------------------------------------

    def bind_object(self, obj: "Object | None") -> None:
        """Switch to a different object's metadata. Flushes any pending
        debounced writes from the previous object first."""
        self._flush_pending_save()
        self._obj = obj
        self._populate_from_disk()
        self._set_form_enabled(obj is not None)
        self._update_header()

    # ---- ui construction ----------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

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
        self.setStyleSheet("""
            #metadataPane {
                background: #f8fafc;
                border-left: 1px solid #cbd5e1;
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
