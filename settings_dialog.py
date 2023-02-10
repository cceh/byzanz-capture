from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QDialog, QLineEdit
from PyQt6.uic import loadUi


class SettingsDialog(QDialog):
    __settings = QSettings()

    def __init__(self, parent=None):
        super().__init__(parent)
        loadUi('ui/settings_dialog.ui', self)

        self.working_directory_input: QLineEdit = self.findChild(QLineEdit, "workingDirectoryInput")

    def closeEvent(self, event):
        print("Close?")