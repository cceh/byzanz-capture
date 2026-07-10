#!/usr/bin/env python
import logging
# python-gphoto2 - Python interface to libgphoto2
# http://github.com/jim-easterbrook/python-gphoto2
# Copyright (C) 2014-22  Jim Easterbrook  jim@jim-easterbrook.me.uk
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

# "object oriented" version of camera-config-gui.py
import json
import sys
from datetime import datetime

import gphoto2 as gp
from PyQt6 import QtCore, QtWidgets
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialogButtonBox, QLineEdit

from PyQt6.QtGui import QPalette, QColor

from byzanz_camera.camera_worker import CameraWorker
from byzanz_camera.gphoto2_safe import widget_text_value

_logger = logging.getLogger("CameraConfigDialog")


# Value-carrying widget types. char*-valued ones must be read via the
# NULL-safe helper (see gphoto2_safe); int/float ones can't NULL-segfault.
_CHAR_TYPES = (gp.GP_WIDGET_TEXT, gp.GP_WIDGET_RADIO, gp.GP_WIDGET_MENU)
_TYPE_NAMES = {
    gp.GP_WIDGET_TEXT: "text", gp.GP_WIDGET_RANGE: "range",
    gp.GP_WIDGET_TOGGLE: "toggle", gp.GP_WIDGET_RADIO: "radio",
    gp.GP_WIDGET_MENU: "menu", gp.GP_WIDGET_DATE: "date",
}


def leaf_settings(camera_config) -> dict[str, dict]:
    """Flatten the config tree into `path -> entry` for export/diff.

    Entry: {"label", "type", "value", "readonly", "widget"} — `widget` is
    the live CameraWidget (stripped before JSON export). Sections recurse,
    BUTTON widgets (actions, no value) are skipped. Reads are NULL-safe."""
    out: dict[str, dict] = {}

    def walk(node, path=""):
        for child in node.get_children():
            p = f"{path}/{child.get_name()}"
            child_type = child.get_type()
            if child_type in (gp.GP_WIDGET_WINDOW, gp.GP_WIDGET_SECTION):
                walk(child, p)
                continue
            if child_type not in _TYPE_NAMES:
                continue    # BUTTON etc. — no value to snapshot
            if child_type in _CHAR_TYPES:
                value = widget_text_value(child)
            else:
                value = child.get_value()
            out[p] = {
                "label": child.get_label(), "type": _TYPE_NAMES[child_type],
                "value": value, "readonly": bool(child.get_readonly()),
                "widget": child,
            }

    walk(camera_config)
    return out


def diff_settings(exported: dict[str, dict], current: dict[str, dict]
                  ) -> tuple[list[str], list[tuple[str, dict]]]:
    """Compare an exported settings dict against the current snapshot.

    Returns `(lines, changed)`: human-readable diff lines, and the
    changed entries as `(path, current_entry)` with the file's value in
    `current_entry["file_value"]` — the applicable subset (present on
    both sides, value differs)."""
    lines: list[str] = []
    changed: list[tuple[str, dict]] = []
    for path in sorted(exported.keys() | current.keys()):
        if path not in current:
            lines.append(f"GONE    {path} [{exported[path].get('label', '')}]"
                         f" (in file, not on camera)")
        elif path not in exported:
            entry = current[path]
            lines.append(f"NEW     {path} [{entry['label']}]"
                         f" = {entry['value']!r} (on camera, not in file)")
        elif exported[path].get("value") != current[path]["value"]:
            entry = dict(current[path], file_value=exported[path].get("value"))
            lines.append(f"CHANGED {path} [{entry['label']}]: "
                         f"file {entry['file_value']!r}"
                         f" -> camera {entry['value']!r}")
            changed.append((path, entry))
    return lines, changed


def _trace_widget(child) -> None:
    """Debug-only safety net: log a widget's identity + flush to disk
    BEFORE its value is read, so the last log line names the culprit if a
    widget read ever hard-crashes (segfaults are uncatchable from Python).

    The known crash — char*-valued widgets (TEXT/RADIO/MENU) with a NULL
    value, e.g. Sony's `d2c1` — is now handled by `widget_text_value`;
    this trace stays as a net for future surprises but is a no-op unless
    debug logging is enabled (it floods at one line per widget)."""
    if not _logger.isEnabledFor(logging.DEBUG):
        return
    try:
        name = child.get_name()
        ctype = child.get_type()
    except Exception as e:  # noqa: BLE001 - diagnostic must never raise
        name, ctype = "?", repr(e)
    _logger.debug("config-dialog: about to read widget name=%r type=%s", name, ctype)
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:  # noqa: BLE001
            pass


class CameraConfigDialog(QtWidgets.QDialog):
    def __init__(self, camera_config: gp.CameraWidget, camera_worker: CameraWorker, parent=None):
        self.__logger = logging.getLogger(self.__class__.__name__)
        self.camera_config = camera_config
        self.camera_worker = camera_worker
        self.do_init = QtCore.QEvent.registerEventType()
        super(CameraConfigDialog, self).__init__(parent)
        self.setWindowTitle("Camera config")
        self.setMinimumWidth(600)

        # main widget
        self.setLayout(QtWidgets.QGridLayout())
        self.layout().setColumnStretch(0, 1)

        # Add search field
        self.search_field = QLineEdit()
        self.search_field.setPlaceholderText("Search properties...")
        self.search_field.textChanged.connect(self.search_properties)
        self.layout().addWidget(self.search_field, 0, 0, 1, 3)

        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        # Settings export / import-diff (e.g. to identify which PTP property
        # a camera-menu setting maps to: export, change the setting offline,
        # reconnect, import → the diff names it).
        export_button = self.button_box.addButton(
            "Export…", QDialogButtonBox.ButtonRole.ActionRole)
        export_button.clicked.connect(self.export_settings)
        import_button = self.button_box.addButton(
            "Import / Diff…", QDialogButtonBox.ButtonRole.ActionRole)
        import_button.clicked.connect(self.import_settings)
        self.layout().addWidget(self.button_box, 2, 2)

        parent_width = self.parent().frameGeometry().width()
        parent_height = self.parent().frameGeometry().height()
        self.resize(int(parent_width * 0.6), int(parent_height * 0.6))

        # Store reference to top widget for searching
        self.top_widget = None
        self.scroll_area = None

        # defer full initialisation (slow operation) until gui is visible
        QtWidgets.QApplication.postEvent(
            self, QtCore.QEvent(self.do_init), Qt.EventPriority.LowEventPriority.value - 1)

    def event(self, event):
        if event.type() != self.do_init:
            return QtWidgets.QDialog.event(self, event)
        event.accept()
        QtWidgets.QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.initialise()
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        return True

    def initialise(self):
        self.setWindowTitle(self.camera_config.get_label())
        self.top_widget = SectionWidget(self.config_changed, self.camera_config)
        self.scroll_area = QtWidgets.QScrollArea()
        self.scroll_area.setWidget(self.top_widget)
        self.scroll_area.setWidgetResizable(True)
        self.layout().addWidget(self.scroll_area, 1, 0, 1, 3)

    def search_properties(self, search_text):
        if not self.top_widget:
            return

        # Reset all highlighting first
        self.reset_highlighting(self.top_widget)

        if not search_text:
            return

        # Search and highlight matching items
        self.highlight_matching_items(self.top_widget, search_text.lower())

    def reset_highlighting(self, widget):
        """Reset the background color of all form rows"""
        if isinstance(widget, SectionWidget):
            form_layout = widget.layout()
            if isinstance(form_layout, QtWidgets.QFormLayout):
                for i in range(form_layout.rowCount()):
                    label_item = form_layout.itemAt(i, QtWidgets.QFormLayout.ItemRole.LabelRole)
                    if label_item:
                        label_widget = label_item.widget()
                        if label_widget:
                            label_widget.setStyleSheet("")

            # Reset highlighting in child tabs
            for child in widget.findChildren(SectionWidget):
                self.reset_highlighting(child)

    def highlight_matching_items(self, widget, search_text):
        """Search through the widget hierarchy and highlight matching items"""
        found = False
        if isinstance(widget, SectionWidget):
            form_layout = widget.layout()
            if isinstance(form_layout, QtWidgets.QFormLayout):
                for i in range(form_layout.rowCount()):
                    label_item = form_layout.itemAt(i, QtWidgets.QFormLayout.ItemRole.LabelRole)
                    if label_item:
                        label_widget = label_item.widget()
                        if label_widget and hasattr(label_widget, 'text'):
                            if search_text in label_widget.text().lower():
                                # Highlight the matching row
                                label_widget.setStyleSheet("background-color: yellow;")
                                # Scroll to the matching item
                                self.scroll_to_widget(label_widget)
                                found = True

            # Search in child tabs
            for child in widget.findChildren(SectionWidget):
                if self.highlight_matching_items(child, search_text):
                    found = True

        return found

    def scroll_to_widget(self, widget):
        """Scroll the scroll area to make the widget visible"""
        if self.scroll_area and widget:
            self.scroll_area.ensureWidgetVisible(widget)

    def config_changed(self):
        self.camera_worker.commands.set_config.emit(self.camera_config)
        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)
        self.__logger.info("Camera config changed")

    # ---- settings export / import-diff -----------------------------------

    def export_settings(self):
        """Dump every value-carrying config entry to a JSON file."""
        default_name = "camera-settings-{}-{}.json".format(
            self.camera_config.get_label().replace(" ", "_"),
            datetime.now().strftime("%Y%m%d-%H%M%S"))
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export camera settings", default_name, "JSON (*.json)")
        if not path:
            return
        snapshot = leaf_settings(self.camera_config)
        data = {
            "camera": self.camera_config.get_label(),
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "settings": {p: {k: v for k, v in entry.items() if k != "widget"}
                         for p, entry in snapshot.items()},
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        self.__logger.info("exported %d settings to %s", len(snapshot), path)
        QtWidgets.QMessageBox.information(
            self, "Export", f"Exported {len(snapshot)} settings to\n{path}")

    def import_settings(self):
        """Load an exported settings file and DIFF it against the camera's
        current values (printed to the console + shown in a dialog). If any
        differ, offers to apply the file's values back to the camera —
        readonly entries are diffed but never applied."""
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Import camera settings (diff)", "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                exported = json.load(f)["settings"]
        except (OSError, json.JSONDecodeError, KeyError) as e:
            QtWidgets.QMessageBox.warning(
                self, "Import", f"Could not read settings file:\n{e}")
            return
        current = leaf_settings(self.camera_config)
        lines, changed = diff_settings(exported, current)

        header = (f"diff {path} (file) vs. camera — "
                  f"{len(lines)} difference(s)")
        print(header, flush=True)
        for line in lines:
            print("  " + line, flush=True)
        self.__logger.info("%s\n%s", header, "\n".join(lines))
        if not lines:
            QtWidgets.QMessageBox.information(
                self, "Import / Diff", "No differences — the camera matches "
                "the exported settings.")
            return
        self._show_diff_dialog(header, lines, changed)

    def _show_diff_dialog(self, header: str, lines: list[str],
                          changed: list[tuple[str, dict]]) -> None:
        applicable = [(p, e) for p, e in changed if not e["readonly"]]
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Settings diff")
        dialog.setMinimumSize(700, 400)
        layout = QtWidgets.QVBoxLayout(dialog)
        layout.addWidget(QtWidgets.QLabel(header))
        text = QtWidgets.QPlainTextEdit("\n".join(lines))
        text.setReadOnly(True)
        text.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        if applicable:
            apply_button = buttons.addButton(
                f"Apply {len(applicable)} file value(s) to camera",
                QDialogButtonBox.ButtonRole.ActionRole)
            apply_button.clicked.connect(
                lambda: (self._apply_file_values(applicable), dialog.accept()))
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _apply_file_values(self, applicable: list[tuple[str, dict]]) -> None:
        """Write the file's values onto the in-memory config tree, then push
        the whole tree to the camera via the existing choke point."""
        applied = 0
        for path, entry in applicable:
            value = entry["file_value"]
            if value is None:
                continue    # a NULL the camera exposed at export time
            widget = entry["widget"]
            try:
                if entry["type"] == "range":
                    widget.set_value(float(value))
                elif entry["type"] in ("toggle", "date"):
                    widget.set_value(int(value))
                else:
                    widget.set_value(str(value))
                applied += 1
            except gp.GPhoto2Error as e:
                self.__logger.warning("could not set %s = %r: %s",
                                      path, value, e)
        if applied:
            self.config_changed()
        self.__logger.info("applied %d imported setting(s)", applied)

    def accept(self):
        super().accept()


class SectionWidget(QtWidgets.QWidget):
    def __init__(self, config_changed, camera_config, parent=None):
        QtWidgets.QWidget.__init__(self, parent)
        self.setLayout(QtWidgets.QFormLayout())
        if camera_config.get_readonly():
            self.setDisabled(True)
        child_count = camera_config.count_children()
        if child_count < 1:
            return
        tabs = None
        for child in camera_config.get_children():
            _trace_widget(child)  # diagnostic: names the culprit on a get_value segfault
            label = '{} ({})'.format(child.get_label(), child.get_name())
            label_widget = QtWidgets.QLabel(label)
            child_type = child.get_type()
            if child_type == gp.GP_WIDGET_SECTION:
                if not tabs:
                    tabs = QtWidgets.QTabWidget()
                    self.layout().insertRow(0, tabs)
                tabs.addTab(SectionWidget(config_changed, child), label)
            elif child_type == gp.GP_WIDGET_TEXT:
                self.layout().addRow(label_widget, TextWidget(config_changed, child))
            elif child_type == gp.GP_WIDGET_RANGE:
                self.layout().addRow(label_widget, RangeWidget(config_changed, child))
            elif child_type == gp.GP_WIDGET_TOGGLE:
                self.layout().addRow(label_widget, ToggleWidget(config_changed, child))
            elif child_type == gp.GP_WIDGET_RADIO:
                if child.count_choices() > 3:
                    widget = MenuWidget(config_changed, child)
                else:
                    widget = RadioWidget(config_changed, child)
                self.layout().addRow(label_widget, widget)
            elif child_type == gp.GP_WIDGET_MENU:
                self.layout().addRow(label_widget, MenuWidget(config_changed, child))
            elif child_type == gp.GP_WIDGET_DATE:
                self.layout().addRow(label_widget, DateWidget(config_changed, child))
            else:
                print('Cannot make widget type %d for %s' % (child_type, label))


class TextWidget(QtWidgets.QLineEdit):
    def __init__(self, config_changed, config, parent=None):
        QtWidgets.QLineEdit.__init__(self, parent)
        self.config_changed = config_changed
        self.config = config
        if self.config.get_readonly():
            self.setDisabled(True)
        assert self.config.count_children() == 0
        # NULL-safe: a TEXT widget with a NULL value segfaults the binding's
        # get_value() (PyUnicode_FromString(NULL)) — read via ctypes instead.
        value = widget_text_value(self.config)
        if value:
            self.setText(value)
        self.editingFinished.connect(self.new_value)

    def new_value(self):
        if sys.version_info[0] < 3:
            value = unicode(self.text()).encode('utf-8')  # noqa: F821
        else:
            value = str(self.text())
        self.config.set_value(value)
        self.config_changed()
        print("TEXTFIELD CHANGEd")

class RangeWidget(QtWidgets.QSlider):
    def __init__(self, config_changed, config, parent=None):
        QtWidgets.QSlider.__init__(self, Qt.Orientation.Horizontal, parent)
        self.config_changed = config_changed
        self.config = config
        if self.config.get_readonly():
            self.setDisabled(True)
        assert self.config.count_children() == 0
        lo, hi, inc = self.config.get_range()
        # Some cameras (e.g. Sony) report inc=0 for continuous/unspecified-step
        # range properties. QSlider needs a positive step to map float ↔ int,
        # so fabricate one: 1000 positions across the range, or 1.0 if degenerate.
        if inc <= 0:
            inc = (hi - lo) / 1000 if hi > lo else 1.0
        self.inc = inc
        value = self.config.get_value()
        self.setRange(max(int(lo / self.inc), -0x80000000),
                      min(int(hi / self.inc), 0x7fffffff))
        self.setValue(max(min(int(value / self.inc), 0x7fffffff), -0x80000000))
        self.sliderReleased.connect(self.new_value)

    def new_value(self):
        value = float(self.value()) * self.inc
        self.config.set_value(value)
        self.config_changed()


class ToggleWidget(QtWidgets.QCheckBox):
    def __init__(self, config_changed, config, parent=None):
        QtWidgets.QCheckBox.__init__(self, parent)
        self.config_changed = config_changed
        self.config = config
        if self.config.get_readonly():
            self.setDisabled(True)
        assert self.config.count_children() == 0
        value = self.config.get_value()
        self.setChecked(value != 0)
        self.clicked.connect(self.new_value)

    def new_value(self):
        value = self.isChecked()
        self.config.set_value((0, 1)[value])
        self.config_changed()


class RadioWidget(QtWidgets.QWidget):
    def __init__(self, config_changed, config, parent=None):
        QtWidgets.QWidget.__init__(self, parent)
        self.config_changed = config_changed
        self.config = config
        if self.config.get_readonly():
            self.setDisabled(True)
        assert self.config.count_children() == 0
        self.setLayout(QtWidgets.QHBoxLayout())
        value = widget_text_value(self.config)  # NULL-safe (see TextWidget)
        self.buttons = []
        for choice in self.config.get_choices():
            if choice:
                button = QtWidgets.QRadioButton(choice)
                self.layout().addWidget(button)
                if choice == value:
                    button.setChecked(True)
                self.buttons.append((button, choice))
                button.clicked.connect(self.new_value)

    def new_value(self):
        for button, choice in self.buttons:
            if button.isChecked():
                self.config.set_value(choice)
                self.config_changed()
                return


class MenuWidget(QtWidgets.QComboBox):
    def __init__(self, config_changed, config, parent=None):
        QtWidgets.QComboBox.__init__(self, parent)
        self.config_changed = config_changed
        self.config = config
        if self.config.get_readonly():
            self.setDisabled(True)
        assert self.config.count_children() == 0
        value = widget_text_value(self.config)  # NULL-safe (see TextWidget)
        choice_count = self.config.count_choices()
        for n in range(choice_count):
            choice = self.config.get_choice(n)
            if choice:
                self.addItem(choice)
                if choice == value:
                    self.setCurrentIndex(n)
        self.currentIndexChanged.connect(self.new_value)

    def new_value(self, value):
        print(self.config.get_name())
        print(value)
        value = str(self.itemText(value))
        self.config.set_value(value)
        self.config_changed()


class DateWidget(QtWidgets.QDateTimeEdit):
    def __init__(self, config_changed, config, parent=None):
        QtWidgets.QDateTimeEdit.__init__(self, parent)
        self.config_changed = config_changed
        self.config = config
        if self.config.get_readonly():
            self.setDisabled(True)
        assert self.config.count_children() == 0
        value = self.config.get_value()
        if value:
            self.setDateTime(datetime.fromtimestamp(value))
        self.dateTimeChanged.connect(self.new_value)
        self.setDisplayFormat('yyyy-MM-dd hh:mm:ss')

    def new_value(self, value):
        value = value.toPyDateTime() - datetime.fromtimestamp(0)
        value = int(value.total_seconds())
        self.config.set_value(value)
        self.config_changed()
