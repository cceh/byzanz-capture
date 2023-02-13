import mimetypes
from os import listdir, path
from pathlib import Path

from PyQt6.QtCore import QFileSystemWatcher, Qt, QThreadPool
from PyQt6.QtGui import QPixmap, QResizeEvent
from PyQt6.QtWidgets import QWidget, QListWidget, QListWidgetItem, QVBoxLayout, QGroupBox
from PyQt6.uic import loadUi

from load_image_worker import LoadImageWorker, LoadImageWorkerResult
from photo_viewer import PhotoViewer
from spinner import Spinner


class PhotoBrowser(QWidget):
    # TODO: stop loading image when directory is closed!

    __currentPath: str = None
    __currentFileSet: set[str] = set()
    __fileSystemWatcher: QFileSystemWatcher

    def __init__(self, parent=None):
        super().__init__(parent)
        loadUi('ui/photo_browser.ui', self)

        self.__fileSystemWatcher = QFileSystemWatcher()
        self.__threadpool = QThreadPool()

        self.photo_viewer: PhotoViewer = self.findChild(QWidget, "photoViewer")
        self.image_file_list: QListWidget = self.findChild(QListWidget, "imageFileList")
        self.viewer_container: QWidget = self.findChild(QWidget, "viewerContainer")

        self.__fileSystemWatcher.directoryChanged.connect(self.__load_directory)

        self.image_file_list.currentItemChanged.connect(self.__on_select_image_file)

        self.spinner = Spinner(self.viewer_container, Spinner.m_light_color)
        self.spinner.isAnimated = False
        self.__center_spinner_over_photo_viewer()


    def open_directory(self, dir_path):
        if self.__currentPath:
            self.close_directory()

        self.__currentPath = dir_path
        self.__fileSystemWatcher.addPath(self.__currentPath)
        self.__load_directory()

    def close_directory(self):
        self.__fileSystemWatcher.removePath(self.__currentPath)
        self.__currentPath = None
        self.__currentFileSet.clear()
        self.image_file_list.clear()
        self.__threadpool.clear()

    def resizeEvent(self, event: QResizeEvent):
        self.__center_spinner_over_photo_viewer()

    def __load_directory(self):
        new_files = [f for f in listdir(self.__currentPath) if mimetypes.guess_type(f)[0] == "image/jpeg"]
        new_fileset = set(new_files)
        added_files = new_fileset - self.__currentFileSet
        removed_files = self.__currentFileSet - new_fileset

        for f in added_files:
            self.__load_image(f, self.__add_image_item)

        for f in removed_files:
            pass

        self.__currentFileSet = new_fileset

    def __load_image(self, file_name: str, on_finished_slot):
        worker = LoadImageWorker(path.join(self.__currentPath, file_name), True, 200)
        worker.signals.finished.connect(lambda result: self.__on_image_loaded(result, on_finished_slot))

        self.spinner.startAnimation()
        self.__threadpool.start(worker)

    def __on_image_loaded(self, result: LoadImageWorkerResult, on_finished_slot):
        self.spinner.stopAnimation()
        on_finished_slot(result)

    def __on_select_image_file(self, item: QListWidgetItem):
        if item:
            file_path = item.data(Qt.ItemDataRole.UserRole)
            self.__load_image(file_path, lambda result: self.photo_viewer.setPhoto(QPixmap.fromImage(result.image)))
        else:
            self.photo_viewer.setPhoto(None)

    def __add_image_item(self, image_worker_result: LoadImageWorkerResult):
        list_item = QListWidgetItem()
        file_name = Path(image_worker_result.path).name
        list_item.setData(Qt.ItemDataRole.UserRole, image_worker_result.path)
        list_item.setData(Qt.ItemDataRole.DecorationRole, image_worker_result.thumbnail)

        exposure_time = image_worker_result.exif["ExposureTime"].real
        f_number = image_worker_result.exif["FNumber"]
        list_item.setText("%s\nf/%s | %s" % (file_name, f_number, exposure_time))

        # Only add the item to the list if a directory is still open. This function
        # can be called asynchronously from a thread so the directory could have been
        # closed in the meantime.
        if self.__currentPath:
            self.image_file_list.addItem(list_item)

        # self.image_file_list.currentItemChanged.disconnect()
        # self.preview_list.setCurrentItem(list_item)
        # self.image_file_list.currentItemChanged.connect(self.__on_select_image_file)

    def __center_spinner_over_photo_viewer(self):
        spinner_x = (self.viewer_container.width() - 80) / 2
        spinner_y = (self.viewer_container.height() - 80) / 2
        self.spinner.setGeometry(int(spinner_x), int(spinner_y), 80, 80)
