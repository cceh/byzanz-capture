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
import sys
from datetime import datetime

import gphoto2 as gp
from PyQt6 import QtCore, QtWidgets
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialogButtonBox, QLineEdit

from PyQt6.QtGui import QPalette, QColor

from camera_worker import CameraWorker


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
        value = self.config.get_value()
        if value:
            if sys.version_info[0] < 3:
                value = value.decode('utf-8')
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
        lo, hi, self.inc = self.config.get_range()
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
        value = self.config.get_value()
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
        value = self.config.get_value()
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
