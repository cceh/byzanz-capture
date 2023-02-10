import mimetypes
from os import listdir, path
from pathlib import Path

from PyQt6.QtCore import QFileSystemWatcher, Qt, QThreadPool
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QWidget, QListWidget, QListWidgetItem
from PyQt6.uic import loadUi

from load_image_worker import LoadImageWorker, LoadImageWorkerResult
from photo_viewer import PhotoViewer


class PhotoBrowser(QWidget):
    __currentPath: str = None
    __currentFileSet: set[str] = set()
    __fileSystemWatcher: QFileSystemWatcher

    def __init__(self, parent=None):
        super().__init__(parent)
        loadUi('ui/photo_browser.ui', self)

        self.__fileSystemWatcher = QFileSystemWatcher()

        self.photo_viewer: PhotoViewer = self.findChild(QWidget, "photoViewer")
        self.image_file_list: QListWidget = self.findChild(QListWidget, "imageFileList")

        self.__fileSystemWatcher.directoryChanged.connect(self.__load_directory)

        self.image_file_list.currentItemChanged.connect(self.__on_select_image_file)

    def open_directory(self, dir_path):
        if self.__currentPath:
            self.close_directory()

        self.__currentPath = dir_path
        self.__fileSystemWatcher.addPath(self.__currentPath)
        print("Watching " + self.__currentPath)
        self.__load_directory()

    def close_directory(self):
        self.__fileSystemWatcher.removePath(self.__currentPath)
        self.__currentPath = None
        self.__currentFileSet.clear()
        self.image_file_list.clear()

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
        print(self.__currentPath)
        print(file_name)
        print(self.thread().currentThreadId())
        worker = LoadImageWorker(path.join(self.__currentPath, file_name))
        worker.signals.finished.connect(on_finished_slot)

        QThreadPool.globalInstance().start(worker)

    def __on_select_image_file(self, item: QListWidgetItem):
        file_path = item.data(Qt.ItemDataRole.UserRole)
        self.__load_image(file_path, lambda result: self.photo_viewer.setPhoto(QPixmap.fromImage(result.image)))

    def __add_image_item(self, image_worker_result: LoadImageWorkerResult):
        print("Dr changed")
        list_item = QListWidgetItem()
        file_name = Path(image_worker_result.path).name
        list_item.setData(Qt.ItemDataRole.UserRole, image_worker_result.path)
        list_item.setData(Qt.ItemDataRole.DecorationRole,
                          QPixmap.fromImage(image_worker_result.image).scaled(200, 200, Qt.AspectRatioMode.KeepAspectRatio))

        exposure_time = image_worker_result.exif["ExposureTime"].real
        f_number = image_worker_result.exif["FNumber"]
        list_item.setText("%s\nf/%s | %s" % (file_name, f_number, exposure_time))

        self.image_file_list.addItem(list_item)

        # self.image_file_list.currentItemChanged.disconnect()
        # self.preview_list.setCurrentItem(list_item)
        # self.image_file_list.currentItemChanged.connect(self.__on_select_image_file)