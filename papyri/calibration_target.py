"""CalibrationTarget — capture target for the calibration sub-mode.

Implements the CaptureTarget contract (see papyri/capture_target.py), so
the normal capture button, filmstrip, viewer and delete all drive it
unchanged. It is a thin sibling of `Object`: one bucket per calibration
target, keyed by (slot, spectrum). Each instance is ONE *run* — a
timestamped folder created per "Calibrate" click — so a mid-day setup
change just starts a fresh run:

    <workingDirectory>/_calibration/<run>/<spectrum>/<folder>/<folder>_<vis|ir>_NNN.{jpg,arw}

e.g. `_calibration/2026-06-21_093015/visible/colorchecker/colorchecker_vis_001.jpg`.

The set of buckets + their folders comes entirely from
`papyri.calibration_spec` — this class has no hard-coded notion of
"ColorChecker" or "Flatfield", so adding/removing a target is a spec edit.
There is no metadata, no keeper file (the card thumbnail just shows the
latest take), and no move-between-buckets.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from PyQt6.QtCore import QObject, QThreadPool, pyqtSignal

from byzanz_camera.filmstrip_widget import get_file_index
from papyri.calibration_spec import (
    CALIBRATION_BUCKETS, CALIBRATION_DIRNAME, folder_for_slot, infix_for,
    is_per_height,
)
from papyri.capture_model import Capture, _CopyRunner
from papyri._layout import JPG_EXTENSIONS, RAW_EXTENSIONS, is_hidden_file


class CalibrationTarget(QObject):
    """Per-(slot, spectrum) calibration capture target rooted at
    `<working_dir>/_calibration/`."""

    state_changed = pyqtSignal()
    import_failed = pyqtSignal(Path)

    def __init__(self, working_dir: str, run_id: str, height_for=None, parent=None):
        super().__init__(parent)
        self._logger = logging.getLogger(self.__class__.__name__)
        self.working_dir = working_dir
        self.run_id = run_id
        # One run = one timestamped folder under _calibration/.
        self.dir = os.path.join(working_dir, CALIBRATION_DIRNAME, run_id)
        # Resolves the current rig height (str) per spectrum for per-height
        # targets (Flatfield). Supplied by MainWindow from the shared
        # `currentHeight` / `irCaptureHeight` settings; default = no height
        # subfolder.
        self._height_for = height_for or (lambda spectrum: "")
        self.dir_loaded = False
        self._captures: dict[tuple[str, str], list[Capture]] = {
            b: [] for b in CALIBRATION_BUCKETS}
        self._chosen: dict[tuple[str, str], Capture | None] = {
            b: None for b in CALIBRATION_BUCKETS}

    @property
    def name(self) -> str:
        return self.run_id

    def discard_if_empty(self) -> None:
        """Remove this run's folder if nothing was captured into it — so a
        Calibrate → Back with no shot leaves no empty run behind."""
        self.refresh()
        if self.total_count() == 0:
            import shutil
            shutil.rmtree(self.dir, ignore_errors=True)

    # --- paths ----------------------------------------------------------

    def dir_for(self, slot: str, spectrum: str) -> str:
        """`_calibration/<run>/<spectrum>/<folder>[/<height>]/`. Per-height
        targets (Flatfield) get a height subfolder so each height's flatfields
        are reviewed separately and switching the rig height swaps the view."""
        base = os.path.join(self.dir, spectrum, folder_for_slot(slot))
        if is_per_height(slot):
            height = self._height_for(spectrum)
            if height:
                return os.path.join(base, height)
        return base

    def next_template(self, slot: str, spectrum: str) -> str:
        """`<folder>_<infix>[_<height>]_NNN${extension}` in the bucket's
        folder. NNN is scanned per bucket so each target keeps its own
        sequence."""
        bucket_dir = self.dir_for(slot, spectrum)
        os.makedirs(bucket_dir, exist_ok=True)
        prefix = f"{folder_for_slot(slot)}_{infix_for(spectrum)}"
        if is_per_height(slot):
            height = self._height_for(spectrum)
            if height:
                prefix = f"{prefix}_{height}"
        n = self._max_index_for(bucket_dir, prefix) + 1
        return os.path.join(bucket_dir, f"{prefix}_{n:03d}${{extension}}")

    # --- read side ------------------------------------------------------

    def captures(self, slot: str, spectrum: str) -> list[Capture]:
        return list(self._captures.get((slot, spectrum), []))

    def chosen(self, slot: str, spectrum: str) -> Capture | None:
        return self._chosen.get((slot, spectrum))

    def count(self, slot: str, spectrum: str) -> int:
        return len(self._captures.get((slot, spectrum), []))

    def total_count(self) -> int:
        return sum(len(v) for v in self._captures.values())

    # --- mutation -------------------------------------------------------

    def ensure_dir(self) -> None:
        for slot, spectrum in CALIBRATION_BUCKETS:
            os.makedirs(self.dir_for(slot, spectrum), exist_ok=True)

    def mark_dir_loaded(self) -> None:
        self.dir_loaded = True
        self.refresh()

    # No keeper / no cross-bucket move for calibration (no-ops keep the
    # filmstrip's generic action plumbing happy — it's delete-only here).
    def set_chosen(self, slot: str, spectrum: str, stem: str) -> None:
        return

    def move(self, src_slot: str, src_spectrum: str, stem: str,
             dest_slot: str) -> None:
        return

    def delete(self, slot: str, spectrum: str, stem: str) -> None:
        from send2trash import send2trash
        cap = next((c for c in self._captures.get((slot, spectrum), [])
                    if c.stem == stem), None)
        if cap is None:
            return
        paths = [p for p in (cap.jpg_path, cap.raw_path) if p is not None]
        if not paths:
            return
        send2trash(paths)
        self.refresh()

    def import_files(self, slot: str, spectrum: str, sources: list) -> list[Path]:
        plan = self._plan_import(slot, spectrum, sources)
        pool = QThreadPool.globalInstance()
        for src, dest in plan:
            runner = _CopyRunner(src, dest)
            runner.signals.failed.connect(self.import_failed)
            pool.start(runner)
        return [dest for _, dest in plan]

    def _plan_import(self, slot: str, spectrum: str,
                     sources: list) -> list[tuple[Path, Path]]:
        groups: dict[str, list[Path]] = {}
        for p in sources:
            p = Path(p)
            if not p.is_file():
                continue
            if p.suffix.lower() not in JPG_EXTENSIONS | RAW_EXTENSIONS:
                continue
            groups.setdefault(p.stem, []).append(p)
        if not groups:
            return []
        out = Path(self.dir_for(slot, spectrum))
        os.makedirs(out, exist_ok=True)
        prefix = f"{folder_for_slot(slot)}_{infix_for(spectrum)}"
        base = self._max_index_for(str(out), prefix)
        plan: list[tuple[Path, Path]] = []
        for i, paths in enumerate(groups.values()):
            stem = f"{prefix}_{base + i + 1:03d}"
            for src in paths:
                plan.append((src, out / f"{stem}{src.suffix}"))
        return plan

    # --- refresh / scan -------------------------------------------------

    def refresh(self) -> None:
        new_caps = {b: self._scan(self.dir_for(*b)) for b in CALIBRATION_BUCKETS}
        new_chosen = {b: self._latest_by_mtime(new_caps[b])
                      for b in CALIBRATION_BUCKETS}
        old_sig = self._signature(self._captures, self._chosen)
        new_sig = self._signature(new_caps, new_chosen)
        self._captures = new_caps
        self._chosen = new_chosen
        if old_sig != new_sig:
            self.state_changed.emit()

    @staticmethod
    def _latest_by_mtime(captures: list[Capture]) -> Capture | None:
        if not captures:
            return None

        def mtime(c: Capture) -> float:
            try:
                return os.path.getmtime(c.primary_path)
            except OSError:
                return float("-inf")
        return max(captures, key=mtime)

    @staticmethod
    def _signature(captures: dict[tuple[str, str], list[Capture]],
                   chosen: dict[tuple[str, str], Capture | None]) -> tuple:
        return tuple(
            (
                bucket,
                tuple((c.stem, c.jpg_path, c.raw_path) for c in captures[bucket]),
                chosen[bucket].stem if chosen[bucket] else None,
            )
            for bucket in CALIBRATION_BUCKETS
        )

    @staticmethod
    def _scan(directory: str) -> list[Capture]:
        """Walk one bucket folder; group JPG/RAW pairs by stem."""
        if not os.path.isdir(directory):
            return []
        jpgs: dict[str, str] = {}
        raws: dict[str, str] = {}
        for entry in os.listdir(directory):
            if is_hidden_file(entry):
                continue
            full = os.path.join(directory, entry)
            if not os.path.isfile(full):
                continue
            stem, ext = os.path.splitext(entry)
            ext_lower = ext.lower()
            if ext_lower in JPG_EXTENSIONS:
                jpgs[stem] = full
            elif ext_lower in RAW_EXTENSIONS:
                raws[stem] = full
        captures = []
        for stem in jpgs.keys() | raws.keys():
            primary = jpgs.get(stem) or raws[stem]
            idx = get_file_index(os.path.basename(primary))
            if idx is None:
                continue
            captures.append(Capture(
                stem=stem,
                jpg_path=jpgs.get(stem),
                raw_path=raws.get(stem),
                index=idx,
            ))
        captures.sort(key=lambda c: c.index)
        return captures

    @staticmethod
    def _max_index_for(directory: str, prefix: str) -> int:
        if not os.path.isdir(directory):
            return 0
        max_idx = 0
        needle = prefix + "_"
        for f in os.listdir(directory):
            if is_hidden_file(f):
                continue
            stem, ext = os.path.splitext(f)
            if ext.lower() not in JPG_EXTENSIONS | RAW_EXTENSIONS:
                continue
            if not stem.startswith(needle):
                continue
            idx = get_file_index(f)
            if idx is not None and idx > max_idx:
                max_idx = idx
        return max_idx
