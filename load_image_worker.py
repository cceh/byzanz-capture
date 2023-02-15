from pathlib import Path

from PIL import Image
from PIL.ExifTags import TAGS
from PyQt6.QtCore import QObject, pyqtSignal, QRunnable, pyqtSlot, QElapsedTimer, Qt
from PyQt6.QtGui import QPixmap, QImage


class LoadImageWorkerResult:
    def __init__(self, image, exif, path, thumbnail):
        self.image: QImage = image
        self.exif: dict = exif
        self.path: str = path
        self.thumbnail: QImage = thumbnail

class LoadImageWorkerSignals(QObject):
    finished = pyqtSignal(LoadImageWorkerResult)

class LoadImageWorker(QRunnable):
    def __init__(self, path, include_thumbnail=None, thumbnail_size=200):
        self.path = path
        self.include_thumbnail = include_thumbnail
        self.thumbnail_size = thumbnail_size

        super(LoadImageWorker, self).__init__()
        self.signals = LoadImageWorkerSignals()

    @pyqtSlot()
    def run(self):
        print("LoadImageWorker started")
        timer = QElapsedTimer()
        timer.start()
        image = Image.open(self.path)
        try:
            image.load()
            w, h = image.size
            image_data = image.tobytes('raw', 'RGB')
            q_image = QImage(image_data, w, h, QImage.Format.Format_RGB888)

            if not q_image:
                return

            thumbnail_q_image = None
            if self.include_thumbnail:
                thumbnail_q_image = q_image.scaled(self.thumbnail_size, self.thumbnail_size, Qt.AspectRatioMode.KeepAspectRatio)

            self.signals.finished.emit(
                LoadImageWorkerResult(q_image, self.get_exif_dict(image), self.path, thumbnail_q_image)
            )
            print("Loading image %s image took %d ms" % (Path(self.path).name, timer.elapsed()))
        except:
            pass # TODO: Image File Error Handling

    def get_exif_dict(self, image: Image) -> dict:
        exif_dict = dict()
        exif_data = image.getexif()
        for tag_id in exif_data:
            self.add_tag_to_exif_dict(tag_id, exif_dict, exif_data)

        ifd = exif_data.get_ifd(0x8769)
        for tag_id in ifd:
            self.add_tag_to_exif_dict(tag_id, exif_dict, ifd)

        return exif_dict

    def add_tag_to_exif_dict(self, tag_id, exif_dict, exif_data):
        tag = TAGS.get(tag_id, tag_id)
        content = exif_data.get(tag_id)
        exif_dict[tag] = content