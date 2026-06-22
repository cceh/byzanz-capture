"""SimpleTarget — the flat-folder storage model for simple capture mode.

Where full mode's `Object` (main.py) organises captures into four
nested `<side>/<spectrum>/` buckets with metadata and chosen-take
files, the simple mode degenerates to *one output folder*: every
capture lands directly in it. There is no side axis, no metadata, no
keeper/chosen concept and no move-between-buckets.

It implements the same duck-typed surface that `main.py` and the
filmstrip call on `Object` — `dir_for / captures / chosen / count /
next_template / set_chosen / move / delete / import_files / refresh /
ensure_dir / mark_dir_loaded` plus the `state_changed` / `import_failed`
signals — so the rest of the app drives it unchanged. The `(side,
spectrum)` parameters are accepted for signature compatibility; `side`
is ignored entirely (always SIDE_A), `spectrum` only feeds the optional
filename infix when a name override is set.

Naming:
  - no override  → `${basename}${extension}` template, i.e. the
    camera keeps its own filename (Sony/Nikon names rarely collide).
  - override set → `<override>_<vis|ir>_NNN.<ext>`, NNN scanned from
    the folder so VIS/IR stay distinguishable and sequences don't clash.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from PyQt6.QtCore import QObject, QThreadPool, pyqtSignal

from byzanz_camera.filmstrip_widget import get_file_index
from papyri.capture_model import Capture, _CopyRunner
from papyri._layout import (
    JPG_EXTENSIONS, RAW_EXTENSIONS, SPECTRUM_INFRARED, SPECTRUM_VISIBLE,
)


class SimpleTarget(QObject):
    """Flat capture target bound to a single output folder.

    Implements the CaptureTarget contract (papyri/capture_target.py).

    `output_dir` is both the storage folder and `dir`. `name_override`
    (possibly empty) is the user-typed filename prefix; empty means
    "keep the camera's own filename".
    """

    state_changed = pyqtSignal()
    import_failed = pyqtSignal(Path)

    _SPECTRUM_INFIX = {SPECTRUM_VISIBLE: "vis", SPECTRUM_INFRARED: "ir"}

    def __init__(self, output_dir: str, name_override: str = "", parent=None):
        super().__init__(parent)
        self._logger = logging.getLogger(self.__class__.__name__)
        self.output_dir = output_dir
        self.working_dir = output_dir
        self.dir = output_dir
        self._name_override = self._sanitize(name_override)
        self.dir_loaded = False
        # Whole-folder capture list + the latest take (display default).
        self._captures: list[Capture] = []
        self._chosen: Capture | None = None

    # --- identity / naming ---------------------------------------------

    @property
    def name(self) -> str:
        """The override prefix, or "" when captures keep camera names.
        Used only for logging / the (hidden) sidebar in simple mode."""
        return self._name_override

    def set_name_override(self, text: str) -> None:
        """Update the filename prefix used for subsequent captures.
        No rebind / refresh — it only affects `next_template`."""
        self._name_override = self._sanitize(text)

    @staticmethod
    def _sanitize(text: str) -> str:
        return (text or "").strip().replace(" ", "_")

    # --- (side, spectrum)-parametric read side (side ignored) ----------

    def dir_for(self, side: str, spectrum: str) -> str:
        return self.output_dir

    def captures(self, side: str, spectrum: str) -> list[Capture]:
        return list(self._captures)

    def chosen(self, side: str, spectrum: str) -> Capture | None:
        return self._chosen

    def count(self, side: str, spectrum: str) -> int:
        return len(self._captures)

    def total_count(self) -> int:
        return len(self._captures)

    # --- mutation ------------------------------------------------------

    def ensure_dir(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)

    def mark_dir_loaded(self) -> None:
        self.dir_loaded = True
        self.refresh()

    def next_template(self, side: str, spectrum: str) -> str:
        """File-path template for the camera worker. With no override the
        camera's own filename is preserved (`${basename}`); with an
        override the next `<override>_<infix>_NNN` is reserved by scanning
        the folder. `${extension}` is substituted per file by the worker."""
        self.ensure_dir()
        if not self._name_override:
            return os.path.join(self.output_dir, "${basename}${extension}")
        infix = self._SPECTRUM_INFIX.get(spectrum, "vis")
        prefix = f"{self._name_override}_{infix}"
        n = self._max_index_for(prefix) + 1
        stem = f"{prefix}_{n:03d}"
        return os.path.join(self.output_dir, f"{stem}${{extension}}")

    # No keeper / no second bucket in simple mode — both are no-ops so
    # the filmstrip's generic action plumbing stays happy.
    def set_chosen(self, side: str, spectrum: str, stem: str) -> None:
        return

    def move(self, src_side: str, src_spectrum: str, stem: str,
             dest_side: str) -> None:
        return

    def delete(self, side: str, spectrum: str, stem: str) -> None:
        """Send2Trash both jpg/raw paths of the given stem, then refresh."""
        from send2trash import send2trash
        cap = next((c for c in self._captures if c.stem == stem), None)
        if cap is None:
            return
        paths = [p for p in (cap.jpg_path, cap.raw_path) if p is not None]
        if not paths:
            return
        send2trash(paths)
        self.refresh()

    def import_files(self, side: str, spectrum: str, sources: list) -> list[Path]:
        """Queue async flat copies of `sources` into the output folder.
        With an override the copies are renamed `<override>_<infix>_NNN`;
        otherwise they keep their source filename. Returns the dest paths
        so the filmstrip can seed placeholders."""
        plan = self._plan_import(spectrum, sources)
        pool = QThreadPool.globalInstance()
        for src, dest in plan:
            runner = _CopyRunner(src, dest)
            runner.signals.failed.connect(self.import_failed)
            pool.start(runner)
        return [dest for _, dest in plan]

    def _plan_import(self, spectrum: str, sources: list) -> list[tuple[Path, Path]]:
        groups: dict[str, list[Path]] = {}
        for p in sources:
            p = Path(p)
            if not p.is_file():
                continue
            ext = p.suffix.lower()
            if ext not in JPG_EXTENSIONS and ext not in RAW_EXTENSIONS:
                continue
            groups.setdefault(p.stem, []).append(p)
        if not groups:
            return []
        out = Path(self.output_dir)
        os.makedirs(out, exist_ok=True)
        plan: list[tuple[Path, Path]] = []
        if not self._name_override:
            # Keep source names — collisions are the user's own doing.
            for paths in groups.values():
                for src in paths:
                    plan.append((src, out / src.name))
            return plan
        infix = self._SPECTRUM_INFIX.get(spectrum, "vis")
        prefix = f"{self._name_override}_{infix}"
        base = self._max_index_for(prefix)
        for i, paths in enumerate(groups.values()):
            stem = f"{prefix}_{base + i + 1:03d}"
            for src in paths:
                plan.append((src, out / f"{stem}{src.suffix}"))
        return plan

    # --- refresh / scan ------------------------------------------------

    def refresh(self) -> None:
        new_captures = self._scan()
        # "chosen" is only the take the viewer focuses when the folder
        # (re)opens. Pick the most recently written file rather than the
        # highest index — across two cameras' native naming schemes the
        # numeric index isn't chronological, but mtime is.
        new_chosen = self._latest_by_mtime(new_captures)
        old_sig = self._signature(self._captures, self._chosen)
        new_sig = self._signature(new_captures, new_chosen)
        self._captures = new_captures
        self._chosen = new_chosen
        if old_sig != new_sig:
            self.state_changed.emit()

    @staticmethod
    def _latest_by_mtime(captures: list[Capture]) -> Capture | None:
        """Most recently modified capture, or None. Files can vanish
        mid-scan (a delete racing the refresh) — treat a missing file as
        oldest so it never wins."""
        if not captures:
            return None
        def mtime(c: Capture) -> float:
            try:
                return os.path.getmtime(c.primary_path)
            except OSError:
                return float("-inf")
        return max(captures, key=mtime)

    @staticmethod
    def _signature(captures: list[Capture], chosen: Capture | None) -> tuple:
        return (
            tuple((c.stem, c.jpg_path, c.raw_path) for c in captures),
            chosen.stem if chosen else None,
        )

    def _scan(self) -> list[Capture]:
        """Walk the output folder; group JPG/RAW pairs by stem. Mirrors
        Object._scan_bucket but flat (one folder, no side/spectrum)."""
        if not os.path.isdir(self.output_dir):
            return []
        jpgs: dict[str, str] = {}
        raws: dict[str, str] = {}
        for entry in os.listdir(self.output_dir):
            full = os.path.join(self.output_dir, entry)
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

    def _max_index_for(self, prefix: str) -> int:
        """Highest NNN among `<prefix>_NNN` files in the folder; 0 if none."""
        if not os.path.isdir(self.output_dir):
            return 0
        max_idx = 0
        needle = prefix + "_"
        for f in os.listdir(self.output_dir):
            stem, ext = os.path.splitext(f)
            if ext.lower() not in JPG_EXTENSIONS and ext.lower() not in RAW_EXTENSIONS:
                continue
            if not stem.startswith(needle):
                continue
            idx = get_file_index(f)
            if idx is not None and idx > max_idx:
                max_idx = idx
        return max_idx
