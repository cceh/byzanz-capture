from enum import Enum
from io import BytesIO
from pathlib import Path

import numpy as np
import rawpy
from PIL import Image, ImageOps
from PIL.ExifTags import TAGS
from PyQt6.QtCore import QObject, pyqtSignal, QRunnable, pyqtSlot, QElapsedTimer, Qt
from PyQt6.QtGui import QImage


JPEG_EXTENSIONS = {".jpg", ".jpeg"}
RAW_EXTENSIONS = {".arw", ".nef", ".cr2", ".cr3", ".dng", ".raf", ".orf", ".rw2"}
SUPPORTED_EXTENSIONS = JPEG_EXTENSIONS | RAW_EXTENSIONS


class DecodeMode(Enum):
    """How to decode RAW source files. JPEGs ignore this — they're loaded the same way either way.

    THUMB: use the camera's embedded JPEG preview via `raw.extract_thumb()` (~50-200 ms).
    FULL : full LibRaw demosaic via `raw.postprocess()` (~1-3 s on a 50 MP file).
    """
    THUMB = "thumb"
    FULL = "full"


def is_raw(path: str) -> bool:
    return Path(path).suffix.lower() in RAW_EXTENSIONS


class LoadImageWorkerResult:
    def __init__(self, image, exif, path, thumbnail):
        self.image: QImage = image
        self.exif: dict = exif
        self.path: str = path
        self.thumbnail: QImage = thumbnail

class LoadImageWorkerSignals(QObject):
    finished = pyqtSignal(LoadImageWorkerResult)

class LoadImageWorker(QRunnable):
    def __init__(self, path, include_thumbnail=None, thumbnail_size=200,
                 decode_mode: DecodeMode = DecodeMode.FULL):
        self.path = path
        self.include_thumbnail = include_thumbnail
        self.thumbnail_size = thumbnail_size
        self.decode_mode = decode_mode

        super(LoadImageWorker, self).__init__()
        self.signals = LoadImageWorkerSignals()

    @pyqtSlot()
    def run(self):
        print("LoadImageWorker started")
        timer = QElapsedTimer()
        timer.start()
        try:
            if is_raw(self.path):
                q_image, exif = self._load_raw()
            else:
                q_image, exif = self._load_jpeg()

            if q_image is None:
                return

            thumbnail_q_image = None
            if self.include_thumbnail:
                thumbnail_q_image = q_image.scaled(self.thumbnail_size, self.thumbnail_size, Qt.AspectRatioMode.KeepAspectRatio)

            self.signals.finished.emit(
                LoadImageWorkerResult(q_image, exif, self.path, thumbnail_q_image)
            )
            print("Loading image %s image took %d ms" % (Path(self.path).name, timer.elapsed()))
        except:
            pass # TODO: Image File Error Handling

    def _load_jpeg(self):
        # exif_transpose applies the camera's EXIF Orientation tag so portrait
        # shots display upright. Without it, all paths show the sensor-native
        # landscape orientation regardless of how the camera was held.
        image = ImageOps.exif_transpose(Image.open(self.path))
        image.load()
        w, h = image.size
        image_data = image.tobytes('raw', 'RGB')
        q_image = QImage(image_data, w, h, QImage.Format.Format_RGB888)
        return q_image, self.get_exif_dict(image)

    def _load_raw(self):
        with rawpy.imread(self.path) as raw:
            exif = self._exif_from_embedded_thumb(raw)

            if self.decode_mode is DecodeMode.THUMB:
                q_image = self._embedded_thumb_qimage(raw)
                if q_image is None:
                    # No usable embedded preview — fall back to full decode.
                    q_image = self._full_raw_qimage(raw)
            else:
                q_image = self._full_raw_qimage(raw)

        return q_image, exif

    def _full_raw_qimage(self, raw) -> QImage:
        rgb = raw.postprocess(
            use_camera_wb=True,
            output_bps=8,
        )  # numpy ndarray HxWx3 uint8 — auto-bright on (rawpy default), so the
           # rendered RAW lands at "naturally exposed" rather than dark/flat.
        rgb = np.ascontiguousarray(rgb)
        h, w, _ = rgb.shape
        # .copy() detaches the QImage from the numpy buffer so it survives
        # after `rgb` is GC'd.
        return QImage(rgb.data, w, h, w * 3, QImage.Format.Format_RGB888).copy()

    def _embedded_thumb_qimage(self, raw):
        try:
            thumb = raw.extract_thumb()
        except rawpy.LibRawNoThumbnailError:
            return None
        if thumb.format == rawpy.ThumbFormat.JPEG:
            # Load via PIL + exif_transpose so the embedded preview agrees on
            # orientation with the postprocess()-rendered full RAW (which honors
            # the camera EXIF flag automatically).
            pil = ImageOps.exif_transpose(Image.open(BytesIO(thumb.data)))
            pil = pil.convert("RGB")
            w, h = pil.size
            return QImage(pil.tobytes('raw', 'RGB'), w, h, w * 3, QImage.Format.Format_RGB888)
        if thumb.format == rawpy.ThumbFormat.BITMAP:
            arr = np.ascontiguousarray(thumb.data)
            h, w, _ = arr.shape
            return QImage(arr.data, w, h, w * 3, QImage.Format.Format_RGB888).copy()
        return None

    def _exif_from_embedded_thumb(self, raw) -> dict:
        try:
            thumb = raw.extract_thumb()
        except rawpy.LibRawNoThumbnailError:
            return {}
        if thumb.format != rawpy.ThumbFormat.JPEG:
            return {}
        return self.get_exif_dict(Image.open(BytesIO(thumb.data)))

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
