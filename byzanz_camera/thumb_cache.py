"""Disk-cached thumbnail extraction.

A small persistent cache that maps source-file paths to scaled-down
thumbnails and the EXIF dict the filmstrip uses for captions.

Why both: the only consumers right now are the filmstrip (which paints
captions from EXIF) and the bucket selector (which doesn't). Caching the
thumb alone wouldn't actually avoid file I/O on the filmstrip path — it
would still have to reopen the source for EXIF, which for RAW means a
full `rawpy.imread` again. Caching both makes a cache hit truly free.

Cache layout (one entry per source file):
    <cache_root>/thumbs/<sha1(abs_path + "|" + mtime_ns)[:16]>.png
    <cache_root>/thumbs/<sha1(...)>.json

PNG holds the thumbnail; JSON holds the EXIF dict (with `Fraction` values
preserved via a custom encoder so the filmstrip caption code keeps
formatting "1/250" instead of "0.004").

Key is path + mtime, so any file edit invalidates the entry automatically.
No need to checksum file content.

Eviction is LRU-by-atime when the directory's total size exceeds the
configured cap. Both files for an entry are deleted together.
"""
from __future__ import annotations
import hashlib
import json
import os
from fractions import Fraction
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QStandardPaths
from PyQt6.QtGui import QImage


# ---- EXIF (de)serialization preserving Fraction --------------------------

_FRACTION_TAG = "__frac__"


def _exif_default(o):
    """JSON serialization fallback. Preserves Fraction-like rationals
    (PIL's IFDRational, stdlib Fraction) so they re-hydrate as Fractions
    on load — keeping the filmstrip caption's "1/250" formatting intact."""
    n = getattr(o, "numerator", None)
    d = getattr(o, "denominator", None)
    if isinstance(n, int) and isinstance(d, int) and d != 0:
        return {_FRACTION_TAG: [n, d]}
    try:
        return float(o)
    except (TypeError, ValueError):
        return str(o)


def _exif_object_hook(obj):
    if isinstance(obj, dict) and len(obj) == 1 and _FRACTION_TAG in obj:
        n, d = obj[_FRACTION_TAG]
        return Fraction(n, d)
    return obj


# ---- ThumbCache ----------------------------------------------------------

class ThumbCache:
    """Stateless wrt the rest of the app — instantiate one per process
    and let the workers use it. Thread-safe for get/put against distinct
    keys (relies on the filesystem for cross-thread coherence)."""

    def __init__(self, cache_dir: Optional[Path] = None,
                 max_bytes: int = 500_000_000):
        if cache_dir is None:
            base = QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.CacheLocation
            )
            cache_dir = Path(base) / "thumbs"
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = cache_dir
        self.max_bytes = max_bytes

    # ---- key ------------------------------------------------------------

    def _key(self, path: str) -> Optional[str]:
        try:
            mtime_ns = os.stat(path).st_mtime_ns
        except OSError:
            return None
        abs_path = os.path.abspath(path)
        h = hashlib.sha1(f"{abs_path}|{mtime_ns}".encode()).hexdigest()
        return h[:16]

    # ---- get / put ------------------------------------------------------

    def get(self, path: str) -> Optional[tuple[QImage, dict, Optional[float]]]:
        key = self._key(path)
        if key is None:
            return None
        png = self.cache_dir / f"{key}.png"
        if not png.exists():
            return None
        img = QImage(str(png))
        if img.isNull():
            return None
        # Touch atime for LRU.
        for f in (png, self.cache_dir / f"{key}.json"):
            try:
                os.utime(f)
            except OSError:
                pass
        # Sidecar shape: {"exif": <dict>, "sharpness": <float|null>}.
        # Any pre-existing flat-exif sidecars were one-shot converted
        # to this shape via `jq` when the column was added.
        exif: dict = {}
        sharpness: Optional[float] = None
        ejson = self.cache_dir / f"{key}.json"
        if ejson.exists():
            try:
                raw = json.loads(ejson.read_text(),
                                 object_hook=_exif_object_hook)
            except (OSError, json.JSONDecodeError):
                raw = {}
            exif = raw.get("exif") or {}
            s = raw.get("sharpness")
            if isinstance(s, (int, float)):
                sharpness = float(s)
        return img, exif, sharpness

    def put(self, path: str, thumb: QImage, exif: dict,
            sharpness: Optional[float] = None) -> None:
        key = self._key(path)
        if key is None or thumb.isNull():
            return
        png = self.cache_dir / f"{key}.png"
        if not thumb.save(str(png), "PNG"):
            return
        ejson = self.cache_dir / f"{key}.json"
        try:
            ejson.write_text(json.dumps(
                {"exif": exif, "sharpness": sharpness},
                default=_exif_default,
            ))
        except OSError:
            pass
        self._evict_if_needed()

    # ---- eviction -------------------------------------------------------

    def _evict_if_needed(self) -> None:
        files = [f for f in self.cache_dir.glob("*") if f.is_file()]
        total = sum(f.stat().st_size for f in files)
        if total <= self.max_bytes:
            return
        files.sort(key=lambda f: f.stat().st_atime)
        for f in files:
            if total <= self.max_bytes:
                return
            try:
                total -= f.stat().st_size
                f.unlink()
            except OSError:
                pass


# ---- module-level singleton ---------------------------------------------

_singleton: Optional[ThumbCache] = None


def thumb_cache() -> ThumbCache:
    """Lazily-initialized process-wide thumbnail cache. Both filmstrip
    workers and the bucket-selector worker funnel through this."""
    global _singleton
    if _singleton is None:
        _singleton = ThumbCache()
    return _singleton
