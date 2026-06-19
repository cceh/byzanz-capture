"""Shared capture-storage primitives.

`Capture` (one take on disk) and the async `_CopyRunner` used for
drag-and-drop import live here so that both storage models — the
full-mode `Object` (main.py) and the flat `SimpleTarget`
(simple_target.py) — can import them without an import cycle through
main.py.

These were extracted verbatim from main.py; behaviour is unchanged.
"""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QObject, QRunnable, pyqtSignal


@dataclass(frozen=True)
class Capture:
    """One capture take in a storage bucket.

    Identity is the stem (filename minus extension), so a capture exists
    whenever any side (JPG, RAW, or both) is on disk. At least one of
    `jpg_path` / `raw_path` is always set.
    """
    stem: str               # identity, e.g. "P.Köln_8821_vis_001"
    jpg_path: str | None    # absolute jpg path if present
    raw_path: str | None    # absolute raw path if present
    index: int              # parsed NNN

    @property
    def primary_path(self) -> str:
        """Whichever side exists, preferring JPG (faster to display).
        At least one is always non-None by construction."""
        return self.jpg_path or self.raw_path  # type: ignore[return-value]

    @property
    def display_name(self) -> str:
        """Basename of the primary file — what the user sees in the browser."""
        return os.path.basename(self.primary_path)


class _CopyRunnerSignals(QObject):
    """QRunnable can't host signals directly — pyqtSignal needs a
    QObject base, and inheriting both QObject + QRunnable confuses
    Qt's metaobject system. Standard workaround: a one-purpose
    QObject sidecar that holds the signals."""
    failed = pyqtSignal(Path)  # the dest path whose copy failed


class _CopyRunner(QRunnable):
    """Atomic copy on a worker thread: src → dest.part → rename dest.
    The atomic rename means the FS watcher sees the final file all-at-
    once instead of mid-write partial states (which the load worker
    would otherwise try to decode and fail on).

    On OSError, `signals.failed` fires with the dest path so the
    caller can clean up its placeholder."""
    def __init__(self, src: Path, dest: Path):
        super().__init__()
        self._src = src
        self._dest = dest
        self.signals = _CopyRunnerSignals()

    def run(self):
        tmp = self._dest.with_suffix(self._dest.suffix + ".part")
        try:
            shutil.copy2(self._src, tmp)
            tmp.replace(self._dest)
        except OSError as e:
            logging.getLogger("CopyRunner").warning(
                "drop-copy failed for %s: %r", self._src.name, e)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            self.signals.failed.emit(self._dest)
