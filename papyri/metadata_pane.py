"""Metadata pane for the current object.

Schema-driven metadata form that auto-saves to `<obj>/_meta.json`.
Bound to an Object via `bind_object`. Lives on the right side of the
splitter; the object's name + rename/close buttons live in the
separate ObjectTitleBar at the top of the window.

Schema definition + completeness check live in `papyri._metadata` so
the sidebar can derive its `??` vs `✓` badge from the same source of
truth.
"""
from __future__ import annotations
import json
import os
from typing import TYPE_CHECKING

from PyQt6.QtCore import QPoint, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QIntValidator, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QComboBox, QFormLayout, QFrame, QLabel, QLineEdit, QPlainTextEdit,
    QSizePolicy, QSpinBox, QVBoxLayout, QWidget,
)

from byzanz_camera.helpers import get_ui_path
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

    # Bottom-right mascot (sits behind every child widget because
    # paintEvent runs before child rendering). Constants are the
    # watermark's max side (px), the corner inset, and the alpha
    # blend (1.0 = fully opaque).
    _MASCOT_MAX_PX = 200
    _MASCOT_MARGIN = 0
    _MASCOT_OPACITY = 0.5

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

        # Watermark pixmap loaded once at native (1031×948) resolution.
        # `get_ui_path` handles both dev (relative to cwd) and
        # PyInstaller-frozen (_MEIPASS) cases. We cache a DPR-aware
        # pre-scaled copy on first paint and invalidate it whenever
        # the target size or screen DPR changes — see
        # `_get_scaled_mascot`.
        self._mascot_pixmap = QPixmap(get_ui_path("papyri/ui/mascot.png"))
        self._scaled_mascot: QPixmap | None = None
        self._scaled_mascot_key: tuple | None = None

        self._build_ui()
        self._set_form_enabled(False)

    # ---- watermark -----------------------------------------------------

    def paintEvent(self, event) -> None:
        """Draw the corner mascot after the QFrame's background. Child
        widgets paint after this, so they sit on top of the watermark
        automatically — no z-order plumbing needed."""
        super().paintEvent(event)
        if self._mascot_pixmap.isNull():
            return
        # Logical target size, aspect-preserved.
        aspect = self._mascot_pixmap.width() / self._mascot_pixmap.height()
        if aspect >= 1:
            w = self._MASCOT_MAX_PX
            h = int(round(self._MASCOT_MAX_PX / aspect))
        else:
            h = self._MASCOT_MAX_PX
            w = int(round(self._MASCOT_MAX_PX * aspect))
        scaled = self._get_scaled_mascot(w, h)
        x = self.width() - w - self._MASCOT_MARGIN
        y = self.height() - h - self._MASCOT_MARGIN
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setOpacity(self._MASCOT_OPACITY)
        # drawPixmap(QPoint, QPixmap) uses the pixmap's own DPR-aware
        # logical size — no further scaling done by the painter, so
        # we get a single high-quality resample (in _get_scaled_mascot)
        # instead of two (resample-on-load + resample-on-draw).
        painter.drawPixmap(QPoint(x, y), scaled)

    def _get_scaled_mascot(self, w: int, h: int) -> QPixmap:
        """Return a cached, DPR-aware pre-scaled mascot. Re-scales on
        first call and whenever target size or screen DPR changes.

        Why this matters: on HiDPI, the widget renders to a 2× (or
        higher) backing surface. If we hand QPainter a full-res 1031×948
        pixmap and ask it to draw into a 200×183 logical rect, the
        painter scales once for the screen and the result reads
        slightly soft. Pre-scaling at the device-pixel resolution and
        tagging the pixmap with that DPR sidesteps the painter's
        on-draw scaling — Qt renders the pre-scaled bitmap 1:1 in
        device pixels."""
        dpr = self.devicePixelRatioF()
        key = (w, h, dpr)
        if self._scaled_mascot is None or self._scaled_mascot_key != key:
            device_w = int(round(w * dpr))
            device_h = int(round(h * dpr))
            pm = self._mascot_pixmap.scaled(
                QSize(device_w, device_h),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            pm.setDevicePixelRatio(dpr)
            self._scaled_mascot = pm
            self._scaled_mascot_key = key
        return self._scaled_mascot

    # ---- public API ----------------------------------------------------

    def bind_object(self, obj: "Object | None") -> None:
        """Switch to a different object's metadata. Flushes any pending
        debounced writes from the previous object first."""
        self._flush_pending_save()
        self._obj = obj
        self._populate_from_disk()
        self._set_form_enabled(obj is not None)

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
            if schema.editable:
                # Free-text combo: keep the dropdown as presets but let
                # the user type custom values. `currentTextChanged`
                # already covers both typed input and dropdown picks.
                w.setEditable(True)
                w.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
                if schema.numeric:
                    w.lineEdit().setValidator(QIntValidator(0, 99999, w))
            w.currentTextChanged.connect(self._on_choice_changed)
            return w
        if schema.type == "longtext":
            w = QPlainTextEdit()
            w.setFixedHeight(140)
            w.textChanged.connect(self._on_text_changed)  # debounced
            return w
        if schema.type == "number":
            w = QSpinBox()
            w.setRange(0, 99999)        # mm — enough for any single sheet
            w.setSpecialValueText(" ")  # value 0 shown as blank (== unset)
            w.valueChanged.connect(self._on_choice_changed)  # immediate save
            return w
        raise ValueError(f"unknown field type: {schema.type!r}")

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
        # Merge into existing JSON so keys NOT rendered as form fields — e.g.
        # the `capture_height_*` that MainWindow stamps on capture — survive.
        data = self._read_meta()
        collected = self._collect_values()
        for schema in self._schema:
            if schema.name in collected:
                data[schema.name] = collected[schema.name]
            else:
                data.pop(schema.name, None)   # cleared field → drop the key
        with open(self._obj.meta_path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
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
        if schema.type == "number":
            return str(w.value()) if w.value() > 0 else ""
        return ""

    # ---- load plumbing -------------------------------------------------

    def _populate_from_disk(self) -> None:
        self._loading = True
        try:
            data = self._read_meta() if self._obj is not None else {}
            for schema in self._schema:
                w = self._widgets[schema.name]
                raw = data.get(schema.name) or schema.default or ""
                # default may be int (e.g. capture_height=45) — widgets
                # all take strings, so coerce here once.
                self._write_widget(w, schema, str(raw))
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
            if idx >= 0:
                w.setCurrentIndex(idx)
            elif schema.editable and value:
                # Custom text the user typed previously — show it even
                # though it's not in the predefined dropdown.
                w.setCurrentText(value)
            else:
                w.setCurrentIndex(0)
        elif schema.type == "longtext":
            w.setPlainText(value)
        elif schema.type == "number":
            try:
                w.setValue(int(value) if value else 0)
            except ValueError:
                w.setValue(0)

    # ---- header / state ------------------------------------------------

    def _set_form_enabled(self, enabled: bool) -> None:
        for w in self._widgets.values():
            w.setEnabled(enabled)
