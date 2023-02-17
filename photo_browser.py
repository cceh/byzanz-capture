import mimetypes
import os
import re
from os import listdir
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import QFileSystemWatcher, Qt, QThreadPool, pyqtSignal, QRunnable, QItemSelectionModel, QMutex, \
    QMutexLocker
from PyQt6.QtGui import QPixmap, QResizeEvent, QPixmapCache, QImage
from PyQt6.QtWidgets import QWidget, QListWidget, QListWidgetItem, QVBoxLayout, QGroupBox
from PyQt6.uic import loadUi

from load_image_worker import LoadImageWorker, LoadImageWorkerResult, LoadImageWorkerSignals
from photo_viewer import PhotoViewer
from spinner import Spinner

def get_file_index(file_path) -> Optional[int]:
    basename = os.path.splitext(file_path)[0]
    numbers_in_basename = re.findall(r'\d+', basename)
    return int(numbers_in_basename[-1]) if numbers_in_basename else None


class ImageFileListItem(QListWidgetItem):
    def __init__(self, path: str, thumbnail: QPixmap):
        super().__init__()
        self.path: str = path
        self.file_name = Path(path).name
        self.index = get_file_index(self.file_name)
        self.thumbnail: QPixmap = thumbnail

    def __lt__(self, other):
        return self.index < other.index

    def data(self, role: Qt.ItemDataRole):
        if role == Qt.ItemDataRole.DecorationRole:
            return self.thumbnail

        return super().data(role)

class PhotoBrowser(QWidget):
    directory_loaded = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        loadUi('ui/photo_browser.ui', self)

        self.__fileSystemWatcher = QFileSystemWatcher()
        self.__threadpool = QThreadPool()
        # self.__threadpool.setMaxThreadCount()
        self.__num_images_to_load = 0

        self.__currentPath: str = None
        self.__currentFileSet: set[str] = set()

        self.photo_viewer: PhotoViewer = self.findChild(QWidget, "photoViewer")
        self.image_file_list: QListWidget = self.findChild(QListWidget, "imageFileList")
        self.viewer_container: QWidget = self.findChild(QWidget, "viewerContainer")

        self.__fileSystemWatcher.directoryChanged.connect(self.__load_directory)

        self.image_file_list.currentItemChanged.connect(self.__on_select_image_file)

        self.spinner = Spinner(self.viewer_container, Spinner.m_light_color)
        self.spinner.isAnimated = False
        self.__center_spinner_over_photo_viewer()
        self.resize(self.size())

        self.__mutex = QMutex()

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
        self.__threadpool.clear()
        self.__threadpool.waitForDone()
        self.__num_images_to_load = 0;
        self.image_file_list.clear()
        QPixmapCache.clear()


    def num_files(self) -> int:
        return self.image_file_list.count()

    def last_index(self) -> int:
        image_count = self.image_file_list.count()
        if image_count > 0:
            return self.image_file_list.item(image_count - 1).index

        return 0

    def resizeEvent(self, event: QResizeEvent):
        self.__center_spinner_over_photo_viewer()

    def __load_directory(self):
        new_files = [f for f in listdir(self.__currentPath)
                     if mimetypes.guess_type(f)[0] == "image/jpeg" and get_file_index(f) is not None]

        new_fileset = set(new_files)
        added_files = new_fileset - self.__currentFileSet
        removed_files = self.__currentFileSet - new_fileset

        if not added_files and not removed_files:
            self.directory_loaded.emit(self.__currentPath)

        if added_files:
            self.__threadpool.waitForDone()
            self.__fileSystemWatcher.removePath(self.__currentPath)
            for f in added_files:
                self.__load_image(f, self.__add_image_item)

        for f in removed_files:
            for i in range(self.image_file_list.count()):
                item = self.image_file_list.item(i)
                if isinstance(item, ImageFileListItem):
                    if item.file_name == f:
                        self.image_file_list.takeItem(i)
                        del item




        self.__currentFileSet = new_fileset

    def __on_directory_loaded(self):
        self.__fileSystemWatcher.addPath(self.__currentPath)
        self.directory_loaded.emit(self.__currentPath)
        # image_count = self.image_file_list.count()
        # if image_count > 0:
        #     self.image_file_list.setCurrentItem(self.image_file_list.item(image_count - 1))

        # just in case there have been changes while loading the files
        self.__load_directory()

    def __load_image(self, file_name: str, on_finished_callback: Callable):
        self.__num_images_to_load +=1

        worker = LoadImageWorker(os.path.join(self.__currentPath, file_name), True, 200)
        worker.signals.finished.connect(lambda result: self.__on_image_loaded(result, on_finished_callback))

        self.spinner.startAnimation()
        self.__threadpool.start(worker)

    def __on_image_loaded(self, result: LoadImageWorkerResult, on_finished_callback: Callable):
        QPixmapCache.insert(result.path, QPixmap.fromImage(result.image))
        on_finished_callback(result)

        self.__num_images_to_load -= 1
        if self.__num_images_to_load == 0:
            self.__on_directory_loaded()

        self.spinner.stopAnimation()

    def __on_select_image_file(self, item: ImageFileListItem):
        if item:
            file_path = item.path
            cached_image = QPixmapCache.find(file_path)
            if cached_image:
                print("cache hit")
                self.photo_viewer.setPhoto(cached_image)
            else:
                print("cache miss")
                self.__load_image(file_path, lambda result: self.photo_viewer.setPhoto(QPixmap.fromImage(result.image)))
        else:
            self.photo_viewer.setPhoto(None)

    def __add_image_item(self, image_worker_result: LoadImageWorkerResult):
        list_item = ImageFileListItem(image_worker_result.path, image_worker_result.thumbnail)

        exposure_time = image_worker_result.exif["ExposureTime"].real
        f_number = image_worker_result.exif["FNumber"]
        list_item.setText("%s\nf/%s | %s" % (list_item.file_name, f_number, exposure_time))

        # Only add the item to the list if a directory is still open. This function
        # can be called asynchronously from a thread so the directory could have been
        # closed in the meantime.
        with QMutexLocker(self.__mutex):
            if self.__currentPath:
                self.image_file_list.addItem(list_item)
                self.image_file_list.sortItems()
                self.image_file_list.scrollToBottom()

                self.image_file_list.currentItemChanged.disconnect()
                self.image_file_list.setCurrentItem(list_item)
                self.image_file_list.currentItemChanged.connect(self.__on_select_image_file)

        image_path = image_worker_result.path
        pixmap: QPixmap = QPixmapCache.find(image_path) or QPixmap.fromImage(image_worker_result.image)
        self.photo_viewer.setPhoto(pixmap)



    def __center_spinner_over_photo_viewer(self):
        spinner_x = (self.viewer_container.width() - 80) / 2
        spinner_y = (self.viewer_container.height() - 80) / 2
        self.spinner.setGeometry(int(spinner_x), int(spinner_y), 80, 80)
