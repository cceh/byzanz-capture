"""Write the EXIF Orientation tag into a captured file, in place and
losslessly, for a 0/90/180/270° clockwise display rotation.

Why two code paths: there is no single pure-Python library that writes
Orientation to *both* JPEG and RAW. So:

  - JPEG            -> piexif (pure Python; rewrites only the EXIF segment,
                      pixels untouched; creates the tag if it's missing).
  - TIFF-based RAW  -> a 2-byte in-place patch of the IFD0 Orientation tag
    (NEF, ARW, CR2…)  (no re-encode). NEF and ARW are both TIFF/EP
                      containers, so the same patch works for both; libraw
                      (rawpy.postprocess) honours the value, as do external
                      tools. The tag already exists on these bodies' files
                      (value 1), so we only overwrite it.

EXIF Orientation values for a clockwise display rotation:
    0° -> 1   90° -> 6   180° -> 3   270° -> 8
"""
from __future__ import annotations

import struct

import piexif

# Clockwise display angle -> EXIF Orientation value.
ANGLE_TO_EXIF = {0: 1, 90: 6, 180: 3, 270: 8}
EXIF_TO_ANGLE = {v: k for k, v in ANGLE_TO_EXIF.items()}

_JPEG_EXTS = (".jpg", ".jpeg")
_ORIENTATION_TAG = 0x0112


def read_orientation(path: str) -> int:
    """Return the file's current clockwise display rotation (0/90/180/270)
    from its EXIF Orientation, or 0 if absent/unknown/unsupported."""
    if path.lower().endswith(_JPEG_EXTS):
        try:
            value = piexif.load(path)["0th"].get(piexif.ImageIFD.Orientation)
        except Exception:
            value = None
    else:
        value = _read_tiff_orientation(path)
    return EXIF_TO_ANGLE.get(value, 0)


def write_orientation(path: str, angle: int) -> bool:
    """Set the file's EXIF Orientation for a clockwise display rotation of
    `angle` degrees (0/90/180/270). Returns True if written, False if the
    tag couldn't be set (e.g. a RAW with no Orientation entry). Never raises
    on an unsupported file — orientation is best-effort metadata."""
    value = ANGLE_TO_EXIF.get(angle % 360)
    if value is None:
        return False
    if path.lower().endswith(_JPEG_EXTS):
        return _write_jpeg(path, value)
    return _patch_tiff_ifd0(path, value)


def _write_jpeg(path: str, value: int) -> bool:
    try:
        exif = piexif.load(path)
        exif["0th"][piexif.ImageIFD.Orientation] = value
        piexif.insert(piexif.dump(exif), path)
        return True
    except Exception:
        return False


def _read_tiff_orientation(path: str) -> int | None:
    """Read the IFD0 Orientation SHORT value of a TIFF-based file, or None."""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
            if head[:2] not in (b"II", b"MM"):
                return None
            bo = "<" if head[:2] == b"II" else ">"
            ifd_off = struct.unpack(bo + "I", head[4:8])[0]
            f.seek(ifd_off)
            count = struct.unpack(bo + "H", f.read(2))[0]
            for _ in range(count):
                entry = f.read(12)
                tag = struct.unpack(bo + "H", entry[:2])[0]
                if tag == _ORIENTATION_TAG:
                    return struct.unpack(bo + "H", entry[8:10])[0]
    except OSError:
        return None
    return None


def _patch_tiff_ifd0(path: str, value: int) -> bool:
    """Overwrite the SHORT value of the IFD0 Orientation tag in a TIFF-based
    file (NEF/ARW/CR2/TIFF). Only patches an existing entry; returns False
    if absent."""
    try:
        with open(path, "r+b") as f:
            head = f.read(8)
            if head[:2] not in (b"II", b"MM"):
                return False
            bo = "<" if head[:2] == b"II" else ">"
            ifd_off = struct.unpack(bo + "I", head[4:8])[0]
            f.seek(ifd_off)
            count = struct.unpack(bo + "H", f.read(2))[0]
            for i in range(count):
                tag = struct.unpack(bo + "H", f.read(2))[0]
                f.read(10)  # type(2) + count(4) + value/offset(4)
                if tag == _ORIENTATION_TAG:
                    # SHORT value is stored inline in the first 2 bytes of the
                    # entry's 4-byte value field, at entry offset +8.
                    f.seek(ifd_off + 2 + i * 12 + 8)
                    f.write(struct.pack(bo + "H", value))
                    return True
    except OSError:
        return False
    return False
