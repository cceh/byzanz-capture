"""Image loading worker — two modes, single shared disk cache.

ImageMode controls what the worker produces:

  THUMB — cache-aware thumb extraction (DCT-scaled JPEG decode or rawpy
          embedded RAW preview). Sets `result.thumbnail`; `image` is None.
          Cheap (~5–200 ms cold, ~5 ms warm).

  FULL  — full-resolution decode (PIL JPEG / rawpy.postprocess RAW).
          Sets BOTH `result.image` (the decoded full) and
          `result.thumbnail` (cache hit if present, else derived from
          the full image + written to cache). Slow path
          (200–500 ms JPEG, 1–3 s RAW) but populates the cache as a
          side effect — a subsequent THUMB request for the same file
          is a cache hit.

Both modes always populate `result.exif`.

The thumbnail cache (byzanz_camera.thumb_cache) is keyed on
`absolute_path|mtime_ns`, so any file edit invalidates the entry and
falls through to the slow path. Thumb extraction is ALWAYS cache-aware
— there's no uncached variant.
"""
from __future__ import annotations
from enum import Enum
from io import BytesIO
from pathlib import Path
from typing import Optional

import numpy as np
import rawpy
from PIL import Image, ImageOps
from PIL.ExifTags import TAGS
from PyQt6.QtCore import (
    QElapsedTimer, QObject, QRunnable, Qt, pyqtSignal, pyqtSlot,
)
from PyQt6.QtGui import QImage

from .thumb_cache import thumb_cache


JPEG_EXTENSIONS = {".jpg", ".jpeg"}
RAW_EXTENSIONS = {".arw", ".nef", ".cr2", ".cr3", ".dng", ".raf", ".orf", ".rw2"}
SUPPORTED_EXTENSIONS = JPEG_EXTENSIONS | RAW_EXTENSIONS


class ImageMode(Enum):
    """See module docstring."""
    THUMB = "thumb"
    FULL = "full"


def is_raw(path: str) -> bool:
    return Path(path).suffix.lower() in RAW_EXTENSIONS


# ---- EXIF helpers --------------------------------------------------------

def _get_exif_dict(image: Image.Image) -> dict:
    """Flat dict of EXIF + ExifIFD sub-tags. Same shape the filmstrip
    caption code expects (`ExposureTime`, `FNumber`)."""
    exif_dict: dict = {}
    exif_data = image.getexif()
    for tag_id in exif_data:
        _add_tag(tag_id, exif_dict, exif_data)
    ifd = exif_data.get_ifd(0x8769)
    for tag_id in ifd:
        _add_tag(tag_id, exif_dict, ifd)
    return exif_dict


def _add_tag(tag_id, exif_dict, exif_data) -> None:
    tag = TAGS.get(tag_id, tag_id)
    exif_dict[tag] = exif_data.get(tag_id)


# ---- thumb extraction (always cache-aware) -------------------------------

def extract_thumb_with_exif(
    path: str, max_size: int = 256
) -> tuple[Optional[QImage], dict]:
    """Format-aware thumb + EXIF, memoized on disk. Hit returns in ~5 ms.
    Miss decodes (JPEG: PIL `Image.draft` for DCT-level scaled decode;
    RAW: `rawpy.extract_thumb` for the embedded JPEG preview, falling
    back to full demosaic + scale if absent) and stores the result.

    Cache key is `absolute_path|mtime_ns` — file edits auto-invalidate."""
    cache = thumb_cache()
    hit = cache.get(path)
    if hit is not None:
        return hit
    try:
        if is_raw(path):
            img, exif = _extract_raw_thumb(path, max_size)
        else:
            img, exif = _extract_jpeg_thumb(path, max_size)
    except Exception as e:
        import traceback
        print(f"extract_thumb_with_exif FAILED for {Path(path).name}: {e!r}")
        traceback.print_exc()
        return None, {}
    if img is not None and not img.isNull():
        cache.put(path, img, exif)
    return img, exif


def _extract_jpeg_thumb(path: str, max_size: int) -> tuple[QImage, dict]:
    with Image.open(path) as image:
        # Read EXIF before draft (defensive — driver behaviour varies).
        exif = _get_exif_dict(image)
        # JPEG-only fast path: libjpeg performs DCT-level scaled decode,
        # producing a smaller image at a fraction of the cost of decoding
        # at full res then resampling.
        image.draft("RGB", (max_size, max_size))
        image = ImageOps.exif_transpose(image)
        image.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
        image = image.convert("RGB")
        w, h = image.size
        q_image = QImage(
            image.tobytes("raw", "RGB"), w, h,
            w * 3, QImage.Format.Format_RGB888,
        ).copy()
    return q_image, exif


def _extract_raw_thumb(path: str, max_size: int) -> tuple[Optional[QImage], dict]:
    with rawpy.imread(path) as raw:
        try:
            thumb = raw.extract_thumb()
        except rawpy.LibRawNoThumbnailError:
            # No embedded preview — last-resort full demosaic + scale.
            qimg = _raw_full_qimage(raw)
            return _scale_to_fit(qimg, max_size), {}

        if thumb.format == rawpy.ThumbFormat.JPEG:
            pil = Image.open(BytesIO(thumb.data))
            exif = _get_exif_dict(pil)
            pil = ImageOps.exif_transpose(pil).convert("RGB")
            pil.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
            w, h = pil.size
            q_image = QImage(
                pil.tobytes("raw", "RGB"), w, h,
                w * 3, QImage.Format.Format_RGB888,
            ).copy()
            return q_image, exif

        if thumb.format == rawpy.ThumbFormat.BITMAP:
            arr = np.ascontiguousarray(thumb.data)
            h, w, _ = arr.shape
            qimg = QImage(
                arr.data, w, h, w * 3, QImage.Format.Format_RGB888,
            ).copy()
            return _scale_to_fit(qimg, max_size), {}

    return None, {}


def _scale_to_fit(img: QImage, max_size: int) -> QImage:
    if max(img.width(), img.height()) <= max_size:
        return img
    return img.scaled(
        max_size, max_size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


# ---- full decode --------------------------------------------------------

def _full_decode(path: str) -> tuple[Optional[QImage], dict]:
    if is_raw(path):
        return _decode_raw_full(path)
    return _decode_jpeg_full(path)


def _decode_jpeg_full(path: str) -> tuple[QImage, dict]:
    image = ImageOps.exif_transpose(Image.open(path))
    image.load()
    w, h = image.size
    image_data = image.tobytes("raw", "RGB")
    q_image = QImage(image_data, w, h, QImage.Format.Format_RGB888)
    return q_image, _get_exif_dict(image)


def _decode_raw_full(path: str) -> tuple[QImage, dict]:
    with rawpy.imread(path) as raw:
        exif = _exif_from_raw_embedded_jpeg(raw)
        q_image = _raw_full_qimage(raw)
    return q_image, exif


def _raw_full_qimage(raw) -> QImage:
    rgb = raw.postprocess(use_camera_wb=True, output_bps=8)
    rgb = np.ascontiguousarray(rgb)
    h, w, _ = rgb.shape
    # .copy() detaches the QImage from the numpy buffer so it survives
    # after `rgb` is garbage-collected.
    return QImage(rgb.data, w, h, w * 3, QImage.Format.Format_RGB888).copy()


def _exif_from_raw_embedded_jpeg(raw) -> dict:
    """RAW EXIF lives in the embedded JPEG preview's metadata (LibRaw
    doesn't expose EXIF directly). Returns empty dict if no JPEG preview
    is embedded."""
    try:
        thumb = raw.extract_thumb()
    except rawpy.LibRawNoThumbnailError:
        return {}
    if thumb.format != rawpy.ThumbFormat.JPEG:
        return {}
    return _get_exif_dict(Image.open(BytesIO(thumb.data)))


# ---- worker -------------------------------------------------------------

class LoadImageWorkerResult:
    """Either `image` or `thumbnail` is set, depending on `ImageMode`.
    BOTH mode sets both."""
    def __init__(self, image: Optional[QImage], thumbnail: Optional[QImage],
                 exif: dict, path: str):
        self.image = image
        self.thumbnail = thumbnail
        self.exif = exif
        self.path = path


class LoadImageWorkerSignals(QObject):
    finished = pyqtSignal(LoadImageWorkerResult)


class LoadImageWorker(QRunnable):
    def __init__(self, path: str, *, mode: ImageMode = ImageMode.FULL,
                 thumb_max_size: int = 256):
        super().__init__()
        self.path = path
        self.mode = mode
        self.thumb_max_size = thumb_max_size
        self.signals = LoadImageWorkerSignals()

    @pyqtSlot()
    def run(self):
        timer = QElapsedTimer()
        timer.start()
        try:
            image: Optional[QImage] = None
            thumbnail: Optional[QImage] = None
            exif: dict = {}

            if self.mode is ImageMode.THUMB:
                thumbnail, exif = extract_thumb_with_exif(
                    self.path, self.thumb_max_size
                )
            else:  # FULL
                image, exif = _full_decode(self.path)
                # Always also populate the thumb (cache hit if present,
                # else derived from the just-decoded full image + cached
                # for future THUMB requests). Cheap relative to the full
                # decode that just ran, and keeps the cache warm.
                if image is not None and not image.isNull():
                    cache = thumb_cache()
                    hit = cache.get(self.path)
                    if hit is not None:
                        thumbnail, _ = hit
                    else:
                        thumbnail = _scale_to_fit(image, self.thumb_max_size)
                        cache.put(self.path, thumbnail, exif)

            self.signals.finished.emit(LoadImageWorkerResult(
                image=image, thumbnail=thumbnail, exif=exif, path=self.path,
            ))
            print("LoadImageWorker(%s, %s) took %d ms" % (
                Path(self.path).name, self.mode.value, timer.elapsed()
            ))
        except Exception as e:
            # Was a bare `except: pass` historically — silently lost RAWs
            # that rawpy couldn't read mid-flight (FS watcher fires before
            # the file is fully flushed). Log + traceback so the next
            # failure is diagnosable.
            import traceback
            print(f"LoadImageWorker FAILED for {Path(self.path).name}: {e!r}")
            traceback.print_exc()
