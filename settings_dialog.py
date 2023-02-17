from PyQt6.QtCore import QSettings, QVariant, QEvent, QObject, Qt
from PyQt6.QtGui import QAction, QIcon, QCursor
from PyQt6.QtWidgets import QDialog, QLineEdit, QFileDialog, QToolButton, QSpinBox
from PyQt6.uic import loadUi


class SettingsDialog(QDialog):
    settings: dict[str, QVariant] = dict()

    def __init__(self, parent=None):
        super().__init__(parent)
        loadUi('ui/settings_dialog.ui', self)

        settings = QSettings()

        self.working_directory_input: QLineEdit = self.findChild(QLineEdit, "workingDirectoryInput")
        self.working_directory_input.setText(settings.value("workingDirectory"))
        self.working_directory_input.textChanged.connect(
            lambda text: self.set("workingDirectory", text)
        )

        open_action = QAction("Arbeitsverzeichnis wählen", self)
        open_action.setIcon(QIcon("ui/folder-open.svg"))
        open_action.triggered.connect(self.choose_working_directory)

        self.working_directory_input.addAction(open_action, QLineEdit.ActionPosition.TrailingPosition)
        tool_button: QToolButton
        for tool_button in self.working_directory_input.findChildren(QToolButton):
            tool_button.setCursor(Qt.CursorShape.PointingHandCursor)

        self.max_pixmap_cache_input: QSpinBox = self.findChild(QSpinBox, "maxPixmapCacheInput")
        self.max_pixmap_cache_input.setValue(int(settings.value("maxPixmapCache")))
        self.max_pixmap_cache_input.textChanged.connect(
            lambda text: self.set("maxPixmapCache", int(text))
        )

    def set(self, name: str, value: QVariant):
        self.settings[name] = value

    def choose_working_directory(self):
        file_dialog = QFileDialog(self,
                                  "Arbeitsverzeichnis wählen",
                                  self.working_directory_input.text())
        file_dialog.setFileMode(QFileDialog.FileMode.Directory)
        if file_dialog.exec():
            print(file_dialog.selectedFiles())

        if file_dialog.selectedFiles():
            self.working_directory_input.setText(file_dialog.selectedFiles()[0])
