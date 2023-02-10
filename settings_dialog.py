from PyQt6.QtCore import QSettings, QVariant
from PyQt6.QtWidgets import QDialog, QLineEdit, QFileDialog
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
