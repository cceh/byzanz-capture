"""CaptureTarget — the contract every "place captures live" implements.

There are three implementations, all duck-typed (no inheritance):
  - `Object`            (main.py)            full papyri: 4 side×spectrum buckets
  - `SimpleTarget`      (simple_target.py)   simple mode: one flat folder
  - `CalibrationTarget` (calibration_target.py) calibration: per (slot, spectrum)

The map of where each family's on-disk layout is written down:
  - objects     → object_layout.py       (tree, naming, completeness)
  - calibration → calibration_layout.py  (tree, naming, completeness)
  - simple      → no layout module: flat folder, naming inline in simple_target.py
  - shared axes + file primitives for all families → capture_vocab.py

`main.py` and the filmstrip drive a target purely through this surface, so
the capture pipeline / live view / filmstrip never branch on the mode — the
target object decides where files go and what comes back. That mode-blind
core is what keeps the multi-mode app readable.

This Protocol is the one place the contract is written down. It is
documentation first (and an optional static check via pyright): the three
classes satisfy it structurally without importing or subclassing it. When
you add a new target type, implement these.

Both axes are passed positionally as `(slot, spectrum)`:
  slot     — first bucket axis. SIDE_A/SIDE_B for objects; calibration
             slot tokens ("cal_cc", …) for calibration. Opaque to callers.
  spectrum — SPECTRUM_VISIBLE | SPECTRUM_INFRARED (which camera).
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from PyQt6.QtCore import pyqtSignal

from papyri.capture_model import Capture


@runtime_checkable
class CaptureTarget(Protocol):
    # --- attributes ----------------------------------------------------
    state_changed: pyqtSignal      # emitted when on-disk captures change
    name: str                      # identifier (object name / "_calibration")
    dir: str                       # the target's root directory
    dir_loaded: bool               # set once the filmstrip has scanned it

    # --- read side (parametric over the bucket) ------------------------
    def dir_for(self, slot: str, spectrum: str) -> str: ...
    def captures(self, slot: str, spectrum: str) -> list[Capture]: ...
    def chosen(self, slot: str, spectrum: str) -> Capture | None: ...
    def count(self, slot: str, spectrum: str) -> int: ...

    # --- capture / naming ----------------------------------------------
    def next_template(self, slot: str, spectrum: str) -> str: ...

    # --- mutation ------------------------------------------------------
    def set_chosen(self, slot: str, spectrum: str, stem: str) -> None: ...
    def delete(self, slot: str, spectrum: str, stem: str) -> None: ...
    def import_files(self, slot: str, spectrum: str, sources: list) -> list[Path]: ...

    # --- lifecycle -----------------------------------------------------
    def ensure_dir(self) -> None: ...
    def mark_dir_loaded(self) -> None: ...
    def refresh(self) -> None: ...
