"""Write the EXIF Orientation tag into a captured file, losslessly, for a
0/90/180/270° clockwise display rotation.

Writes are atomic (build the new bytes, then os.replace) — never in place.
The target is a freshly-captured file the filmstrip's QFileSystemWatcher
is about to decode on a pool thread, so an in-place rewrite would race that
decode and hand it a truncated file. See _atomic_write.

Why two code paths: there is no single pure-Python library that writes
Orientation to *both* JPEG and RAW. So:

  - JPEG            -> piexif (pure Python; rewrites only the EXIF segment,
                      pixels untouched; creates the tag if it's missing).
  - TIFF-based RAW  -> patch the 2-byte IFD0 Orientation SHORT (NEF, ARW,
    (NEF, ARW, CR2…)  CR2…) in the file's bytes (no re-encode). NEF and ARW
                      are both TIFF/EP containers, so the same patch works
                      for both; libraw (rawpy.postprocess) honours the value,
                      as do external tools. The tag already exists on these
                      bodies' files (value 1), so we only overwrite it.

EXIF Orientation values for a clockwise display rotation:
    0° -> 1   90° -> 6   180° -> 3   270° -> 8
"""
from __future__ import annotations

import io
import os
import struct

import piexif

# Clockwise display angle -> EXIF Orientation value.
ANGLE_TO_EXIF = {0: 1, 90: 6, 180: 3, 270: 8}
EXIF_TO_ANGLE = {v: k for k, v in ANGLE_TO_EXIF.items()}

_JPEG_EXTS = (".jpg", ".jpeg")
_ORIENTATION_TAG = 0x0112


def _atomic_write(path: str, data: bytes) -> None:
    """Replace `path` with `data` atomically: write a sibling `.part` file,
    flush+fsync it, then os.replace() it over the target.

    The whole reason this module exists in its current form: orientation is
    written to a file that was *just* captured, and the filmstrip's
    QFileSystemWatcher fires a LoadImageWorker to decode that same file on a
    pool thread the instant it lands. An in-place rewrite (piexif's default
    open("wb+")) truncates the file to zero before writing the new bytes, so
    a concurrent decode catches a truncated file -> "image file is
    truncated". os.replace() is atomic on POSIX and NTFS (Python uses
    MoveFileEx with REPLACE_EXISTING), so a reader sees either the old file
    or the new one — never a partial one. The `.part` extension is not an
    image extension, so the watcher ignores it (same convention as the
    capture-save path in camera_worker)."""
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


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
        # Have piexif build the new JPEG into a buffer (it reads pixels from
        # `path`, merges the new EXIF segment) rather than rewrite `path` in
        # place, then swap it in atomically. See _atomic_write for why.
        buf = io.BytesIO()
        piexif.insert(piexif.dump(exif), path, buf)
        _atomic_write(path, buf.getvalue())
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
    if absent.

    Reads the file, patches the 2-byte SHORT in memory, and swaps the result
    in atomically (see _atomic_write) — same race-safety as the JPEG path, so
    a concurrent rawpy decode never sees a partial file."""
    try:
        with open(path, "rb") as f:
            data = bytearray(f.read())
    except OSError:
        return False
    if data[:2] not in (b"II", b"MM"):
        return False
    bo = "<" if data[:2] == b"II" else ">"
    try:
        ifd_off = struct.unpack_from(bo + "I", data, 4)[0]
        count = struct.unpack_from(bo + "H", data, ifd_off)[0]
        for i in range(count):
            entry_off = ifd_off + 2 + i * 12
            tag = struct.unpack_from(bo + "H", data, entry_off)[0]
            if tag == _ORIENTATION_TAG:
                # SHORT value is stored inline in the first 2 bytes of the
                # entry's 4-byte value field, at entry offset +8.
                struct.pack_into(bo + "H", data, entry_off + 8, value)
                _atomic_write(path, bytes(data))
                return True
    except (struct.error, IndexError):
        return False
    return False
