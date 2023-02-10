from pathlib import Path

from PIL import Image
from PIL.ExifTags import TAGS
from PyQt6.QtCore import QObject, pyqtSignal, QRunnable, pyqtSlot, QElapsedTimer
from PyQt6.QtGui import QPixmap, QImage


class LoadImageWorkerResult:
    def __init__(self, image, exif, path):
        self.image: QImage = image
        self.exif: dict = exif
        self.path: str = path

class LoadImageWorkerSignals(QObject):
    finished = pyqtSignal(LoadImageWorkerResult)

class LoadImageWorker(QRunnable):
    def __init__(self, path):
        self.path = path
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

            self.signals.finished.emit(LoadImageWorkerResult(q_image, self.get_exif_dict(image), self.path))
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