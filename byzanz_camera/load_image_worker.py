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

from .capture_audit import (
    AuditFinding, AuditRequest, SHARPNESS_AUDIT,
)
from .thumb_cache import thumb_cache
from .sharpness import METRIC_VERSION as SHARPNESS_METRIC_VERSION
from .sharpness import measure as measure_object_sharpness

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


# ---- embedded JPEG extraction --------------------------------------------

def _embedded_jpeg_bytes(raw) -> Optional[bytes]:
    """JPEG payload of an open rawpy handle's embedded preview, or None
    when the RAW has no thumbnail or only a bitmap-format one."""
    try:
        thumb = raw.extract_thumb()
    except rawpy.LibRawNoThumbnailError:
        return None
    if thumb.format != rawpy.ThumbFormat.JPEG:
        return None
    return thumb.data


def read_embedded_jpeg(path: str) -> Optional[bytes]:
    """Decodable JPEG bytes for a capture file, without demosaicing: the
    file itself for JPEGs, the (typically full-res) embedded preview for
    RAWs. None when a RAW carries no JPEG preview."""
    if is_raw(path):
        with rawpy.imread(path) as raw:
            return _embedded_jpeg_bytes(raw)
    with open(path, "rb") as f:
        return f.read()


# ---- live-frame sharpness ----------------------------------------------

def compute_sharpness(source: Image.Image) -> Optional[float]:
    """Laplace variance on a center-crop of the image — the focus/blur
    measure, ~70–110 for sharp papyrus captures and single digits for
    visibly defocused / shaken ones (verified against real ARW samples).

    `source` is an already-decoded in-memory frame (e.g. a live-view
    preview). Low-res live frames land on a smaller absolute scale than
    capture files, so compare live values only to each other. Capture files
    use the unrelated v2 edge-rise metric after their full decode.

    center-cropping 70% × 70% (~50% of pixels) trims background — the
    papyrus is roughly centered — and halves the Laplace cost. Returns
    None on any IO/decode failure — sharpness is advisory, never blocks
    the load."""
    try:
        gray = np.asarray(source.convert("L"))
        if gray.ndim != 2 or gray.size == 0:
            return None
        h, w = gray.shape
        crop_w = int(w * 0.7); crop_h = int(h * 0.7)
        x = (w - crop_w) // 2; y = (h - crop_h) // 2
        crop = gray[y:y + crop_h, x:x + crop_w]
        return float(cv2.Laplacian(crop, cv2.CV_64F).var())
    except (OSError, ValueError):
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
) -> tuple[Optional[QImage], dict]:
    """Format-aware thumb + EXIF, memoized on disk. Hit
    returns in ~5 ms. Miss decodes (JPEG: PIL `Image.draft` for
    DCT-level scaled decode; RAW: `rawpy.extract_thumb` for the
    embedded JPEG preview, falling back to full demosaic + scale if
    absent), and stores both values in the sidecar.

    Cache key is `absolute_path|mtime_ns` — file edits auto-invalidate.

    Capture sharpness intentionally never enters this disposable cache."""
    cache = thumb_cache()
    hit = cache.get(path)
    if hit is not None:
        return hit
    try:
        if is_raw(path):
            img, exif = _extract_raw_thumb(path, max_size)
        else:
            img, exif = _extract_jpeg_thumb(path, max_size)
    except Exception:
        _logger.warning("extract_thumb_with_exif failed for %s",
                        Path(path).name, exc_info=True)
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
        data = _embedded_jpeg_bytes(raw)
        if data is not None:
            pil = Image.open(BytesIO(data))
            exif = _get_exif_dict(pil)
            pil = ImageOps.exif_transpose(pil).convert("RGB")
            pil.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
            w, h = pil.size
            q_image = QImage(
                pil.tobytes("raw", "RGB"), w, h,
                w * 3, QImage.Format.Format_RGB888,
            ).copy()
            return q_image, exif

        # No JPEG preview — a thumbnail must still come out of here:
        # bitmap-format thumb if present, else last-resort full demosaic.
        try:
            thumb = raw.extract_thumb()
        except rawpy.LibRawNoThumbnailError:
            qimg = _qimage_from_rgb(_raw_postprocess(raw))
            return _scale_to_fit(qimg, max_size), {}

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

def _full_decode(path: str) -> tuple[Optional[QImage], dict, np.ndarray]:
    if is_raw(path):
        return _decode_raw_full(path)
    return _decode_jpeg_full(path)


def _decode_jpeg_full(path: str) -> tuple[QImage, dict, np.ndarray]:
    # Honour the file's EXIF Orientation: each capture carries its own
    # orientation (written at capture time / when rotated), so the display
    # reflects the file. Orientation 1 (the dome/RTI case) is a no-op.
    with Image.open(path) as source:
        exif = _get_exif_dict(source)
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.load()
        rgb = np.ascontiguousarray(np.asarray(image))
    return _qimage_from_rgb(rgb), exif, rgb


def _decode_raw_full(path: str) -> tuple[QImage, dict, np.ndarray]:
    with rawpy.imread(path) as raw:
        exif = _exif_from_raw_embedded_jpeg(raw)
        rgb = _raw_postprocess(raw)
    return _qimage_from_rgb(rgb), exif, rgb


def _raw_postprocess(raw) -> np.ndarray:
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
    return np.ascontiguousarray(rgb)


def _qimage_from_rgb(rgb: np.ndarray) -> QImage:
    """Detach a tightly packed uint8 RGB array into an owning QImage."""
    h, w, _ = rgb.shape
    # .copy() detaches the QImage from the numpy buffer so it survives
    # after `rgb` is garbage-collected.
    return QImage(rgb.data, w, h, w * 3, QImage.Format.Format_RGB888).copy()


def _measure_capture_array(path: str, rgb: np.ndarray, modality: str):
    """Run v2 on the already-decoded full-resolution array."""
    def gray_loader(_path: str) -> np.ndarray:
        if modality == "ir":
            # The IR body clips red; validation uses the green channel.
            return rgb[:, :, 1].astype(np.float32)
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)

    return measure_object_sharpness(path, gray_loader=gray_loader)


def _exif_from_raw_embedded_jpeg(raw) -> dict:
    """RAW EXIF lives in the embedded JPEG preview's metadata (LibRaw
    doesn't expose EXIF directly). Returns empty dict if no JPEG preview
    is embedded."""
    data = _embedded_jpeg_bytes(raw)
    if data is None:
        return {}
    return _get_exif_dict(Image.open(BytesIO(data)))


# ---- worker -------------------------------------------------------------

class LoadImageWorkerResult:
    """Either `image` or `thumbnail` is set, depending on `ImageMode`.
    FULL mode sets both. Capture audits arrive later on the separate,
    path-bound `audit_finished` signal."""
    def __init__(self, image: Optional[QImage], thumbnail: Optional[QImage],
                 exif: dict, path: str):
        self.image = image
        self.thumbnail = thumbnail
        self.exif = exif
        self.path = path


class LoadImageWorkerSignals(QObject):
    finished = pyqtSignal(LoadImageWorkerResult)
    audit_finished = pyqtSignal(str, object)  # path, AuditFinding


class LoadImageWorker(QRunnable):
    def __init__(self, path: str, *, mode: ImageMode = ImageMode.FULL,
                 thumb_max_size: int = 256,
                 audit_request: AuditRequest | None = None):
        super().__init__()
        self.path = path
        self.mode = mode
        self.thumb_max_size = thumb_max_size
        # Explicit semantic request from the capture-mode coordinator. The
        # worker never reconstructs target type or modality from disk names.
        self.audit_request = audit_request
        self.signals = LoadImageWorkerSignals()

    @pyqtSlot()
    def run(self):
        timer = QElapsedTimer()
        timer.start()
        rgb: np.ndarray | None = None
        try:
            image: Optional[QImage] = None
            thumbnail: Optional[QImage] = None
            exif: dict = {}

            if self.mode is ImageMode.THUMB:
                thumbnail, exif = extract_thumb_with_exif(
                    self.path, self.thumb_max_size
                )
            else:  # FULL
                image, exif, rgb = _full_decode(self.path)
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
            _logger.debug("load(%s, %s) took %d ms",
                          Path(self.path).name, self.mode.value, timer.elapsed())
        except Exception:
            _logger.warning("load failed for %s",
                            Path(self.path).name, exc_info=True)
            return

        # Display is already queued above. Expensive audits start only now,
        # so a 2–5 s 61 MP analysis never delays the viewer.
        if (self.mode is not ImageMode.FULL or self.audit_request is None
                or rgb is None):
            return
        self._run_audits(rgb, self.audit_request)

    def _run_audits(self, rgb: np.ndarray, request: AuditRequest) -> None:
        """Canonical FULL-array audit dispatcher; each check fails closed."""
        for check in sorted(request.checks):
            timer = QElapsedTimer()
            timer.start()
            if check != SHARPNESS_AUDIT:
                _logger.warning("unknown capture audit %r for %s",
                                check, Path(self.path).name)
                continue
            try:
                result = _measure_capture_array(
                    self.path, rgb, request.modality)
            except Exception:
                result = None
                _logger.warning("sharpness v2 failed for %s",
                                Path(self.path).name, exc_info=True)
            finding = AuditFinding(
                check=SHARPNESS_AUDIT,
                metric_version=SHARPNESS_METRIC_VERSION,
                data=result,
            )
            self.signals.audit_finished.emit(self.path, finding)
            _logger.debug("audit(%s, %s) took %d ms (result=%s)",
                          Path(self.path).name, check, timer.elapsed(),
                          "none" if result is None else
                          f"{result.get('sharp_px', float('nan')):.2f}px")
