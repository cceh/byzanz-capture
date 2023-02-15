from pathlib import Path

from PyQt6.QtCore import QObject
from PyQt6.QtWidgets import QFileDialog, QToolButton, QWidget, QMessageBox


class OpenSessionDialog(QObject):
    """
    A wrapper around a QFileDialog that is restricted to a single directory
    """

    def __init__(self, restrict_path, parent=None):
        super().__init__(parent)
        self.__dialog = QFileDialog(parent, "Bestehende Sitzung öffnen", restrict_path)
        self.__selected_session_path = None

        self.restrict_path = Path(restrict_path).absolute()

        self.__dialog.setOptions(QFileDialog.Option.DontUseNativeDialog | QFileDialog.Option.ReadOnly | QFileDialog.Option.ShowDirsOnly)
        self.__dialog.setFileMode(QFileDialog.FileMode.Directory)
        self.__dialog.setViewMode(QFileDialog.ViewMode.Detail)
        self.__dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
        self.__dialog.setSidebarUrls([])

        self.__dialog.directoryEntered.connect(self.__on_directory_entered)

        self.__modify_dialog_controls()

    def __modify_dialog_controls(self):
        sidebar = self.__dialog.findChild(QWidget, "sidebar")
        if sidebar:
            sidebar.hide()

        look_in_combo = self.__dialog.findChild(QWidget, "lookInCombo")
        if look_in_combo:
            look_in_combo.setEnabled(False)

        for tool_button in self.__dialog.findChildren(QToolButton):
            tool_button.hide()

    def __on_directory_entered(self, path):
        if Path(path).parent.absolute() == Path(self.restrict_path):
            self.__selected_session_path = path
            self.__dialog.close()
        else:
            self.__dialog.setDirectory(str(self.restrict_path))

    def get_session_path(self):
        result = self.__dialog.exec()
        print("execed")
        if not self.__selected_session_path:
            return None
        elif not Path(self.__selected_session_path).parent.absolute() == Path(self.restrict_path):
            QMessageBox.critical(None, "Fehler", "Gewähltes Verzeichnis befindet sich nicht im Arbeitsverzeichnis.")
            return None

        return self.__selected_session_path


