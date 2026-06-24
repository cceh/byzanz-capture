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
import logging
from enum import Enum
from io import BytesIO
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import rawpy
from PIL import Image, ImageOps
from PIL.ExifTags import TAGS
from PyQt6.QtCore import (
    QElapsedTimer, QObject, QRunnable, Qt, pyqtSignal, pyqtSlot,
)
from PyQt6.QtGui import QImage

from .thumb_cache import thumb_cache

_logger = logging.getLogger("LoadImageWorker")

JPEG_EXTENSIONS = {".jpg", ".jpeg"}
RAW_EXTENSIONS = {".arw", ".nef", ".cr2", ".cr3", ".dng", ".raf", ".orf", ".rw2"}
SUPPORTED_EXTENSIONS = JPEG_EXTENSIONS | RAW_EXTENSIONS


class ImageMode(Enum):
    """See module docstring."""
    THUMB = "thumb"
    FULL = "full"


def is_raw(path: str) -> bool:
    return Path(path).suffix.lower() in RAW_EXTENSIONS


# ---- sharpness ----------------------------------------------------------

# Toggled from app startup based on the QSettings flag
# `sharpnessCheckEnabled`. When False, workers skip the Laplace
# computation entirely AND return None for sharpness even on cache
# hits, so the user gets a guaranteed clean experience after flipping
# the setting off.
_SHARPNESS_ENABLED = True


def set_sharpness_enabled(enabled: bool) -> None:
    """Global on/off for the per-image sharpness measurement. Called
    by main.py at startup and whenever the setting changes."""
    global _SHARPNESS_ENABLED
    _SHARPNESS_ENABLED = bool(enabled)


def _resolved_sharpness(
    path: str, thumb: Optional[QImage], exif: dict,
    cached: Optional[float],
) -> Optional[float]:
    """Return the sharpness value to surface in a load result, given
    whatever was found in the cache for this path:

      - feature globally disabled → return None (any cached value is
        suppressed; caller should not propagate it)
      - cached value present → return it as-is
      - cached value absent → compute now, top up the sidecar, return
        the new value (or None if compute failed)

    The thumb/exif arguments are only used when the function has to
    write back to the cache; pass the freshly-decoded versions you
    already have on hand."""
    if not _SHARPNESS_ENABLED:
        return None
    if cached is not None:
        return cached
    sharp = compute_sharpness(path)
    if sharp is not None and thumb is not None:
        thumb_cache().put(path, thumb, exif, sharp)
    return sharp


def compute_sharpness(source: "str | Image.Image") -> Optional[float]:
    """Laplace variance on a center-crop of the image — the focus/blur
    measure, ~70–110 for sharp papyrus captures and single digits for
    visibly defocused / shaken ones (verified against real ARW samples).

    `source` is either a file path or an already-decoded frame, so the
    same metric backs both the post-capture check and the live-view
    focus readout:
      - str path → JPEG is decoded at half res (`IMREAD_REDUCED_COLOR_2`,
        keeps enough high-frequency content for half-pixel-blur
        sensitivity without the full 60 MP demosaic); RAW pulls the
        embedded full-res JPEG thumb via rawpy instead of demosaicing.
      - PIL Image → an in-memory frame (e.g. a live-view preview). Low-res
        live frames land on a smaller absolute scale than capture files,
        so compare live values to each other, not to capture numbers.

    center-cropping 70% × 70% (~50% of pixels) trims background — the
    papyrus is roughly centered — and halves the Laplace cost. Returns
    None on any IO/decode failure — sharpness is advisory, never blocks
    the load."""
    try:
        if isinstance(source, Image.Image):
            gray = np.asarray(source.convert("L"))
        else:
            if is_raw(source):
                with rawpy.imread(source) as raw:
                    thumb = raw.extract_thumb()
                if thumb.format != rawpy.ThumbFormat.JPEG:
                    return None
                data = thumb.data
            else:
                with open(source, "rb") as f:
                    data = f.read()
            img = cv2.imdecode(np.frombuffer(data, np.uint8),
                               cv2.IMREAD_REDUCED_COLOR_2)
            if img is None:
                return None
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        if gray.ndim != 2 or gray.size == 0:
            return None
        h, w = gray.shape
        crop_w = int(w * 0.7); crop_h = int(h * 0.7)
        x = (w - crop_w) // 2; y = (h - crop_h) // 2
        crop = gray[y:y + crop_h, x:x + crop_w]
        return float(cv2.Laplacian(crop, cv2.CV_64F).var())
    except (rawpy.LibRawNoThumbnailError, rawpy.LibRawIOError,
            OSError, ValueError):
        return None


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
) -> tuple[Optional[QImage], dict, Optional[float]]:
    """Format-aware thumb + EXIF + sharpness, memoized on disk. Hit
    returns in ~5 ms. Miss decodes (JPEG: PIL `Image.draft` for
    DCT-level scaled decode; RAW: `rawpy.extract_thumb` for the
    embedded JPEG preview, falling back to full demosaic + scale if
    absent), measures sharpness (when globally enabled — see
    `set_sharpness_enabled`), and stores everything in the sidecar.

    Cache key is `absolute_path|mtime_ns` — file edits auto-invalidate.

    `sharpness` is None when: globally disabled, the capture couldn't
    be decoded for measurement, or this is a legacy cache entry
    (created before the sharpness column was added)."""
    cache = thumb_cache()
    hit = cache.get(path)
    if hit is not None:
        img, exif, cached_sharp = hit
        return img, exif, _resolved_sharpness(path, img, exif, cached_sharp)
    try:
        if is_raw(path):
            img, exif = _extract_raw_thumb(path, max_size)
        else:
            img, exif = _extract_jpeg_thumb(path, max_size)
    except Exception:
        _logger.warning("extract_thumb_with_exif failed for %s",
                        Path(path).name, exc_info=True)
        return None, {}, None
    sharp = compute_sharpness(path) if _SHARPNESS_ENABLED else None
    if img is not None and not img.isNull():
        cache.put(path, img, exif, sharp)
    return img, exif, sharp


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
    # Honour the file's EXIF Orientation: each capture carries its own
    # orientation (written at capture time / when rotated), so the display
    # reflects the file. Orientation 1 (the dome/RTI case) is a no-op.
    image = ImageOps.exif_transpose(Image.open(path))
    image.load()
    w, h = image.size
    image_data = image.tobytes("raw", "RGB")
    # .copy() detaches the QImage from the Python `image_data` bytes; without
    # it the QImage holds a borrowed pointer that is freed once image_data
    # goes out of scope, so painting the resulting pixmap later dereferences
    # freed memory and segfaults. The explicit w*3 stride matches tobytes'
    # tight packing (the 4-arg ctor assumes 32-bit-aligned scanlines, which
    # raw RGB888 is not).
    q_image = QImage(
        image_data, w, h, w * 3, QImage.Format.Format_RGB888,
    ).copy()
    return q_image, _get_exif_dict(image)


def _decode_raw_full(path: str) -> tuple[QImage, dict]:
    with rawpy.imread(path) as raw:
        exif = _exif_from_raw_embedded_jpeg(raw)
        q_image = _raw_full_qimage(raw)
    return q_image, exif


def _raw_full_qimage(raw) -> QImage:
    # Default user_flip: libraw applies the RAW's Orientation flag, so the
    # decode reflects the file's own orientation (written at capture / on
    # rotate). RTI/dome files carry flip 0, so this is a no-op for them.
    #
    # no_auto_bright=True: libraw's default auto-brightness stretches each
    # image's histogram independently, which silently equalises exposure
    # differences between shots. The preview is meant to let the user judge
    # the lighting/exposure settings they dialled in (proper development
    # happens later), so we keep camera WB + sRGB gamma for a natural look
    # but switch the per-image auto-exposure off so relative brightness is
    # faithful.
    rgb = raw.postprocess(use_camera_wb=True, output_bps=8, no_auto_bright=True)
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
    BOTH mode sets both. `sharpness` is the Laplace-variance metric
    when computed, None when disabled or unavailable."""
    def __init__(self, image: Optional[QImage], thumbnail: Optional[QImage],
                 exif: dict, path: str, sharpness: Optional[float] = None):
        self.image = image
        self.thumbnail = thumbnail
        self.exif = exif
        self.path = path
        self.sharpness = sharpness


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
            sharpness: Optional[float] = None

            if self.mode is ImageMode.THUMB:
                thumbnail, exif, sharpness = extract_thumb_with_exif(
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
                        thumbnail, _, sharpness = hit
                    else:
                        thumbnail = _scale_to_fit(image, self.thumb_max_size)
                        sharpness = (compute_sharpness(self.path)
                                     if _SHARPNESS_ENABLED else None)
                        cache.put(self.path, thumbnail, exif, sharpness)
                    sharpness = _resolved_sharpness(
                        self.path, thumbnail, exif, sharpness,
                    )

            self.signals.finished.emit(LoadImageWorkerResult(
                image=image, thumbnail=thumbnail, exif=exif, path=self.path,
                sharpness=sharpness,
            ))
            _logger.debug("load(%s, %s) took %d ms (sharpness=%s)",
                          Path(self.path).name, self.mode.value, timer.elapsed(),
                          f"{sharpness:.1f}" if sharpness is not None else "—")
        except Exception:
            _logger.warning("load failed for %s",
                            Path(self.path).name, exc_info=True)
