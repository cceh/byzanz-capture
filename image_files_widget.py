from PyQt6.QtWidgets import QWidget
from PyQt6.uic import loadUi


class ImageFilesWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        loadUi('ui/image_files_widget.ui', self)
