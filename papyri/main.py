"""Papyri Capture — minimal single-shot JPEG+RAW workflow.

Sibling to the RTI main.py at the repo root. Imports byzanz_camera/* as the
shared camera-plumbing library. UI is derived from ui/main_window.ui (reduced
to remove dome / RTI / LP-file controls) and lives at papyri/ui/main_window.ui.

State handling mirrors the RTI app's pattern: `_on_camera_state_changed(spectrum,
state)` runs side-effects (per-spectrum + active-only), then `update_ui()`
is the single source of truth for every widget's enable/visibility/text.
update_ui() is also invoked from any other place that changes context
(session start/close, side/spectrum toggle, etc.).

Run from the repo root via:
    python -m papyri.main
or  python papyri/main.py
"""

import logging
import os
import sys

# Logging + crash reporting BEFORE the gphoto2-path resolver so its
# INFO line (and the autodetect logs from byzanz_camera) are captured.
# Installs the rotating log file, faulthandler and the excepthook that
# stops PyQt6 from aborting on unhandled slot exceptions.
from papyri._logging_setup import install as _install_logging
_install_logging()

# `gphoto2/__init__.py` rewrites CAMLIBS/IOLIBS on import. Capture the
# env-provided values before that happens, then let the resolver
# decide which source wins (frozen / env / vendor / bundled). See
# byzanz_camera/_gphoto2_paths.py for the full precedence.
_pre_camlibs = os.environ.get('CAMLIBS')
_pre_iolibs = os.environ.get('IOLIBS')
import gphoto2 as gp  # noqa: E402
from byzanz_camera._gphoto2_paths import apply_paths as _apply_gphoto2_paths  # noqa: E402
_apply_gphoto2_paths(_pre_camlibs, _pre_iolibs)

from datetime import datetime
from pathlib import Path

from PIL import Image
from PIL.ImageQt import ImageQt
from PyQt6.QtCore import (
    QObject, QSettings, QSize, Qt, QThread, QThreadPool, pyqtSignal,
)
from PyQt6.QtGui import QAction, QCloseEvent, QIcon, QPixmap, QPixmapCache
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QInputDialog, QLabel, QMainWindow,
    QMenu, QMessageBox, QPushButton, QSplitter, QToolButton, QWidget,
)
from PyQt6.uic import loadUi

from byzanz_camera.camera_worker import (
    CameraStates, CameraWorker, CaptureImagesRequest, ConfigRequest,
)
from byzanz_camera.filmstrip_widget import get_file_index
from byzanz_camera.load_image_worker import (
    ImageMode, LoadImageWorker, compute_sharpness,
)
from byzanz_camera.orientation import read_orientation, write_orientation
from byzanz_camera.helpers import (
    get_app_icon, get_ui_path, set_state, set_themed_icon, set_themed_pixmap,
)
from byzanz_camera.viewer_widget import ViewerWidget
from byzanz_camera.zoom_control_bar import ZoomControlBar
from byzanz_camera.config_combo import ConfigComboBox
from papyri.capture_model import Capture, _CopyRunner
from papyri.focus_audio import AUDIO_AVAILABLE, FocusAudio
from papyri.capture_vocab import (
    JPG_EXTENSIONS,
    RAW_EXTENSIONS,
    SIDE_A,
    SIDE_B,
    SPECTRUM_INFIX,
    SPECTRUM_INFRARED,
    SPECTRUM_VISIBLE,
    is_hidden_file,
    sanitize_name,
)
from papyri.object_layout import (
    BUCKETS,
    CURRENT_LAYOUT_VERSION,
    MarkerRole,
    MetaKey,
    bucket_key,
    dir_for_bucket,
    is_managed_object_dir,
    meta_path_for,
    migrate_working_dir,
    read_meta,
    update_meta,
    write_meta,
)
from papyri.camera_state_widget import CameraStateWidget
from papyri.papyri_filmstrip import PapyriFilmstrip
from papyri.metadata_pane import MetadataPane
from papyri.no_object_overlay import NoObjectOverlay
from papyri.object_title_bar import ObjectTitleBar
from papyri.objects_sidebar import ObjectsSidebar
from papyri.rotated_sample_nudge import install_rotated_sample_nudge  # rotated-sample nudge
from papyri.session_state import SessionState
from byzanz_camera.profiles.base import Profile
from byzanz_camera.profiles.corodile_test_sony_ilce_7m3 import MoritzA7MIII
from byzanz_camera.profiles.paris_dome_sony_ilce_7rm5 import ParisDomeSonyIlce7RM5
from byzanz_camera.profiles.cceh_dome_nikon_d800e import CCeHDomeNikonD800E
from byzanz_camera.profiles.nikon_d90 import NikonD90
from byzanz_camera.profiles.virtual_camera_vusb import VirtualCameraVusb
from papyri.bucket_selector import BucketSelector, FusingPanel
from papyri.calibration import CalibrationController
from papyri.calibration_bar import CalibrationBar
from papyri.calibration_layout import first_slot_for, label_for_slot
from papyri.calibration_target import CalibrationTarget
from papyri.capture_mode import CALIBRATION_MODE, get_mode
from papyri._metadata import (
    current_height_for,
    height_choices_for,
    set_current_height,
)
from papyri.simple_target import SimpleTarget
from papyri.stitch_bar import StitchBar
from papyri.stitching import StitchController

from send2trash import send2trash

from camera_config_dialog import CameraConfigDialog
from papyri.settings_dialog import PapyriSettingsDialog

PROFILES = {
    "MoritzA7III": MoritzA7MIII(),
    "ParisDomeSonyIlce7RM5": ParisDomeSonyIlce7RM5(),
    "CCeHDomeNikonD800E": CCeHDomeNikonD800E(),
    "NikonD90": NikonD90(),
    "VirtualCameraVusb": VirtualCameraVusb(),
    # Second emulator on the "vusb:2" port (patched vendor build), so the
    # visible AND infrared slots can both run without hardware: assign this
    # to the IR profile in Settings while the visible slot uses the first.
    "VirtualCameraVusb2": VirtualCameraVusb(
        port="vusb:2", name="Virtual Camera 2 (vusb)"
    ),
}


# Step-id ↔ (side, spectrum) maps and the workflow-group definitions now
# live on the active CaptureMode (papyri.capture_mode) so the two modes
# don't fork MainWindow. `self.mode.step_id_by_bucket` /
# `self.mode.bucket_by_step_id` / `self.mode.groups` replace the former
# module constants + `_build_workflow_groups()`.

# Live-view display rotation → PIL transpose op. We rotate the PIL frame
# (exact, lossless for multiples of 90) rather than the QPixmap, which shears
# the image — ImageQt's buffer stride doesn't survive a QTransform reliably.
# Angles are clockwise (PIL ROTATE_n is counter-clockwise, hence the swap).
_ROTATE_TRANSPOSE = {
    90:  Image.Transpose.ROTATE_270,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90,
}


# Distinguishes an absent marker key (→ role default) from a present-but-
# null one (reference explicitly cleared). `None` is a real marker value,
# so it can't double as "missing".
_MISSING = object()


class Object(QObject):
    """A single papyri capture object: its directory, four (side, spectrum)
    capture buckets, metadata. Implements the CaptureTarget contract
    (papyri/capture_target.py).

    Layout (full tree: papyri/object_layout.py):
      <object>/_meta.json                          (object state incl. take markers)
      <object>/<side>/<spectrum>/<name>_<side_letter>_<spectrum_infix>_NNN.{jpg,arw}

    Public API is (side, spectrum)-parametric throughout: `captures(side, spectrum)`,
    `chosen(side, spectrum)`, `set_chosen(side, spectrum, stem)`,
    `delete(side, spectrum, stem)`, `next_template(side, spectrum)`,
    `count(side, spectrum)`. Sides are SIDE_A | SIDE_B; spectra are
    SPECTRUM_VISIBLE | SPECTRUM_INFRARED.

    Take markers (chosen take, stitch reference) are persisted per bucket in
    `_meta.json` under `markers` — one object-state file, no sidecar files.

    Single source of truth: `refresh()` re-reads disk for all four buckets
    and emits `state_changed` only when something actually changed.
    """

    state_changed = pyqtSignal()
    # Emitted when a `_CopyRunner` queued by `import_files` raises —
    # the dest path was never materialised, so any placeholder seeded
    # for it is orphaned and the caller (filmstrip) should drop it.
    import_failed = pyqtSignal(Path)

    # File-name infixes used in `next_template`.
    _SIDE_INFIX = {SIDE_A: "a", SIDE_B: "b"}

    def __init__(self, working_dir: str, name: str, parent=None):
        super().__init__(parent)
        self.working_dir = working_dir
        self.name = name
        self.dir = os.path.join(working_dir, name)
        self.dir_loaded = False
        # Per-bucket cached state, keyed by (side, spectrum).
        self._captures: dict[tuple[str, str], list[Capture]] = {b: [] for b in BUCKETS}
        self._chosen: dict[tuple[str, str], Capture | None] = {b: None for b in BUCKETS}
        self._reference: dict[tuple[str, str], Capture | None] = {b: None for b in BUCKETS}
        self._stitching = False

    # --- paths ----------------------------------------------------------

    @property
    def meta_path(self) -> str:
        return meta_path_for(self.dir)

    def dir_for(self, side: str, spectrum: str) -> str:
        """`<obj>/<side>/<spectrum>/`."""
        return dir_for_bucket(self.dir, side, spectrum)

    # --- (side, spectrum)-parametric read-side -------------------------

    def captures(self, side: str, spectrum: str) -> list[Capture]:
        return list(self._captures[(side, spectrum)])  # defensive copy

    def chosen(self, side: str, spectrum: str) -> Capture | None:
        """The currently-chosen take for `(side, spectrum)`, or None on an
        empty bucket. Defaults to the latest capture when unmarked (see
        `_resolve_chosen`)."""
        return self._chosen[(side, spectrum)]

    def reference(self, side: str, spectrum: str) -> Capture | None:
        """The stitch reference photo (ColorChecker + scale) of a bucket.
        Defaults to the first capture when unmarked, is None on an empty
        bucket, and is None when the user explicitly cleared it (see
        `_resolve_reference`). Only meaningful for stitching objects;
        resolved regardless (cheap)."""
        return self._reference[(side, spectrum)]

    def is_stitching(self) -> bool:
        """Object-wide flag from `_meta.json`: this object is larger than
        the field of view and is captured as overlapping segments. Set via
        the capture-row Stitch toggle (`set_stitching`)."""
        return self._stitching

    def count(self, side: str, spectrum: str) -> int:
        return len(self._captures[(side, spectrum)])

    def total_count(self) -> int:
        """Total captures across all 4 buckets."""
        return sum(len(v) for v in self._captures.values())

    def filled_buckets(self) -> int:
        """How many of the 4 buckets contain ≥ 1 capture."""
        return sum(1 for v in self._captures.values() if v)

    # --- mutation -------------------------------------------------------

    def ensure_dir(self):
        for side, spectrum in BUCKETS:
            os.makedirs(self.dir_for(side, spectrum), exist_ok=True)
        if not os.path.exists(self.meta_path):
            # Born at the current layout version so migration skips it.
            write_meta(self.meta_path,
                       {MetaKey.LAYOUT_VERSION: CURRENT_LAYOUT_VERSION})

    def mark_dir_loaded(self) -> None:
        """Set dir_loaded=True and run a fresh refresh so per-bucket
        capture lists are populated from disk. F-XLAYER fix: callers
        no longer reach in to mutate dir_loaded directly."""
        self.dir_loaded = True
        self.refresh()

    def next_stem(self, side: str, spectrum: str) -> tuple[str, str]:
        """Reserve the next `(stem, bucket_dir)` for `(side, spectrum)`.

        Single source of truth for capture naming — used both by the
        camera-driven `next_template` (which suffixes `${extension}`
        for the worker to substitute) and by the filmstrip's
        drag-and-drop import path (which already knows each file's
        extension and just needs the stem to copy under).

        Filename stem format: `<name>_<side_letter>_<spectrum_infix>_NNN`.

        Uses `max_index + 1` (not `count + 1`) so it survives gaps
        caused by deletes or moves between buckets — otherwise after
        deleting take 002 of [001, 002, 003] the next take would
        collide with the existing 003."""
        n = self._max_index_on_disk(side, spectrum) + 1
        s_inf = self._SIDE_INFIX[side]
        sp_inf = SPECTRUM_INFIX[spectrum]
        stem = f"{self.name}_{s_inf}_{sp_inf}_{n:03d}"
        return stem, self.dir_for(side, spectrum)

    def next_template(self, side: str, spectrum: str) -> str:
        """File-path template for the camera worker. `${extension}`
        is substituted per file as it's written."""
        stem, bucket_dir = self.next_stem(side, spectrum)
        return os.path.join(bucket_dir, f"{stem}${{extension}}")

    def import_files(
        self, side: str, spectrum: str, sources: list,
    ) -> list[Path]:
        """Plan + queue async file copies for drop-import. Returns the
        list of dest paths so the caller can seed placeholders for
        immediate visual feedback while the copies are still in flight.

        Owns naming, grouping, sequencing, and threadpool dispatch —
        the filmstrip just renders the returned dests as placeholders.
        Returns `[]` (and is a no-op) when no source is a supported
        image file."""
        plan = self._plan_import(side, spectrum, sources)
        pool = QThreadPool.globalInstance()
        for src, dest in plan:
            runner = _CopyRunner(src, dest)
            # Re-emit per-runner failures as `import_failed` so a
            # single Object-level signal covers all queued copies.
            runner.signals.failed.connect(self.import_failed)
            pool.start(runner)
        return [dest for _, dest in plan]

    def _plan_import(
        self, side: str, spectrum: str, sources: list,
    ) -> list[tuple[Path, Path]]:
        """Group sources by stem, reserve a fresh sequence number per
        group, return `[(src, dest), …]` without touching the
        filesystem. Single-pass index reservation (`_max_index_on_disk`
        + offsets) avoids re-reading the directory between groups —
        necessary because writes haven't happened yet so a per-group
        `next_stem` would collide."""
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
        base = self._max_index_on_disk(side, spectrum)
        s_inf = self._SIDE_INFIX[side]
        sp_inf = SPECTRUM_INFIX[spectrum]
        bucket_dir = Path(self.dir_for(side, spectrum))
        os.makedirs(bucket_dir, exist_ok=True)
        plan: list[tuple[Path, Path]] = []
        for i, paths in enumerate(groups.values()):
            stem = f"{self.name}_{s_inf}_{sp_inf}_{base + i + 1:03d}"
            for src in paths:
                plan.append((src, bucket_dir / f"{stem}{src.suffix}"))
        return plan

    def move(self, src_side: str, src_spectrum: str, stem: str,
             dest_side: str) -> None:
        """Move all files for `stem` from `(src_side, src_spectrum)` to
        `(dest_side, src_spectrum)` (same spectrum — moving across spectra
        doesn't make physical sense). Renumbers in the destination bucket
        so the moved capture lands as the next take there.

        Used by the "Move to side B / A" right-click action for fixing
        captures that were taken on the wrong side."""
        if src_side == dest_side:
            return
        cap = next((c for c in self._captures[(src_side, src_spectrum)]
                    if c.stem == stem), None)
        if cap is None:
            return

        dest_dir = self.dir_for(dest_side, src_spectrum)
        os.makedirs(dest_dir, exist_ok=True)
        next_idx = self._max_index_on_disk(dest_side, src_spectrum) + 1
        s_inf = self._SIDE_INFIX[dest_side]
        sp_inf = SPECTRUM_INFIX[src_spectrum]
        new_stem = f"{self.name}_{s_inf}_{sp_inf}_{next_idx:03d}"

        # Move every file for this stem (jpg + raw if present).
        for src_path in (cap.jpg_path, cap.raw_path):
            if src_path is None:
                continue
            ext = os.path.splitext(src_path)[1]
            dest_path = os.path.join(dest_dir, new_stem + ext)
            os.replace(src_path, dest_path)

        self._drop_stale_stem_markers(src_side, src_spectrum, stem)
        self.refresh()

    def set_chosen(self, side: str, spectrum: str, stem: str) -> None:
        """Pin `stem` as the chosen (displayed) take for the bucket."""
        self._set_marker(side, spectrum, MarkerRole.CHOSEN, stem)

    def set_reference(self, side: str, spectrum: str, stem: str) -> None:
        """Pin `stem` as the stitch reference photo for the bucket."""
        self._set_marker(side, spectrum, MarkerRole.REFERENCE, stem)

    def clear_reference(self, side: str, spectrum: str) -> None:
        """Explicitly leave the bucket with NO reference photo — every
        capture becomes a checked segment. Persisted as `reference: null`,
        which overrides the auto-pick-first default (an absent key)."""
        self._set_marker(side, spectrum, MarkerRole.REFERENCE, None)

    def _set_marker(self, side: str, spectrum: str, role: MarkerRole,
                    value: str | None) -> None:
        """Write one per-bucket take marker into `_meta.json` under
        `markers[<bucket>][<role>]`, then refresh. `value` is a stem, or
        None (reference-cleared)."""
        data = read_meta(self.meta_path)
        bucket = data.setdefault(MetaKey.MARKERS, {}).setdefault(
            bucket_key(side, spectrum), {})
        bucket[role] = value
        write_meta(self.meta_path, data)
        self.refresh()

    def set_stitching(self, enabled: bool) -> None:
        """Flip the object-wide stitching flag in `_meta.json` — the Stitch
        toggle's single state location — then refresh so dependents react
        (the flag is part of the change signature)."""
        update_meta(self.meta_path, {MetaKey.STITCHING: bool(enabled)})
        self.refresh()

    def delete(self, side: str, spectrum: str, stem: str) -> None:
        """Send2Trash both jpg/raw paths of the given stem in `(side, spectrum)`,
        then refresh."""
        cap = next((c for c in self._captures[(side, spectrum)] if c.stem == stem), None)
        if cap is None:
            return
        paths = [p for p in (cap.jpg_path, cap.raw_path) if p is not None]
        if not paths:
            return
        send2trash(paths)
        self._drop_stale_stem_markers(side, spectrum, stem)
        self.refresh()

    def _drop_stale_stem_markers(self, side: str, spectrum: str, stem: str) -> None:
        """When `stem` leaves a bucket (move/delete), drop any chosen/
        reference marker pointing at it so the resolvers fall back to their
        defaults instead of a dangling stem. Leaves `reference: null`
        (cleared) untouched — that's an intent, not a dangling stem."""
        data = read_meta(self.meta_path)
        bucket = data.get(MetaKey.MARKERS, {}).get(bucket_key(side, spectrum), {})
        removed = [role for role in MarkerRole if bucket.get(role) == stem]
        for role in removed:
            del bucket[role]
        if removed:
            write_meta(self.meta_path, data)

    def refresh(self):
        """Re-read state from disk for all four buckets (plus the take
        markers + stitching flag, both from `_meta.json`); emit
        `state_changed` if anything changed. Meta is read once here and
        the resolvers work off it — no per-bucket file reads."""
        meta = read_meta(self.meta_path)
        markers = meta.get(MetaKey.MARKERS, {})
        new_captures = {b: self._scan_bucket(*b) for b in BUCKETS}
        new_chosen = {b: self._resolve_chosen(b, new_captures[b], markers)
                      for b in BUCKETS}
        new_reference = {b: self._resolve_reference(b, new_captures[b], markers)
                         for b in BUCKETS}
        new_stitching = meta.get(MetaKey.STITCHING) is True

        old_signature = self._signature(
            self._captures, self._chosen, self._reference, self._stitching)
        new_signature = self._signature(
            new_captures, new_chosen, new_reference, new_stitching)

        self._captures = new_captures
        self._chosen = new_chosen
        self._reference = new_reference
        self._stitching = new_stitching

        if old_signature != new_signature:
            self.state_changed.emit()

    # --- internals ------------------------------------------------------

    @staticmethod
    def _signature(
        captures: dict[tuple[str, str], list[Capture]],
        chosen: dict[tuple[str, str], Capture | None],
        reference: dict[tuple[str, str], Capture | None],
        stitching: bool,
    ) -> tuple:
        """A hashable summary across all buckets used to detect 'anything changed'."""
        per_bucket = tuple(
            (
                bucket,
                tuple((c.stem, c.jpg_path, c.raw_path) for c in captures[bucket]),
                chosen[bucket].stem if chosen[bucket] else None,
                reference[bucket].stem if reference[bucket] else None,
            )
            for bucket in BUCKETS
        )
        return (per_bucket, stitching)

    def _count_stems_on_disk(self, side: str, spectrum: str) -> int:
        """Count unique capture takes (one per stem) in `(side, spectrum)`."""
        bucket_dir = self.dir_for(side, spectrum)
        if not os.path.isdir(bucket_dir):
            return 0
        stems: set[str] = set()
        for f in os.listdir(bucket_dir):
            if is_hidden_file(f):
                continue
            stem, ext = os.path.splitext(f)
            if ext.lower() in JPG_EXTENSIONS or ext.lower() in RAW_EXTENSIONS:
                stems.add(stem)
        return len(stems)

    def _max_index_on_disk(self, side: str, spectrum: str) -> int:
        """Highest NNN index among stems in `(side, spectrum)`. Returns 0
        when the bucket is empty (so `+1` gives 001 for the first capture)."""
        bucket_dir = self.dir_for(side, spectrum)
        if not os.path.isdir(bucket_dir):
            return 0
        max_idx = 0
        for f in os.listdir(bucket_dir):
            if is_hidden_file(f):
                continue
            stem, ext = os.path.splitext(f)
            if ext.lower() not in JPG_EXTENSIONS and ext.lower() not in RAW_EXTENSIONS:
                continue
            idx = get_file_index(f)
            if idx is not None and idx > max_idx:
                max_idx = idx
        return max_idx

    def _scan_bucket(self, side: str, spectrum: str) -> list[Capture]:
        """Walk one bucket directory; group JPG/RAW pairs by stem."""
        bucket_dir = self.dir_for(side, spectrum)
        if not os.path.isdir(bucket_dir):
            return []

        jpgs: dict[str, str] = {}
        raws: dict[str, str] = {}
        for entry in os.listdir(bucket_dir):
            if is_hidden_file(entry):
                continue
            full = os.path.join(bucket_dir, entry)
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
            # get_file_index expects a name with extension (its splitext call
            # would misfire on a bare stem containing a dot, e.g. "P.Köln_…").
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
    def _marker_stem(markers: dict, bucket: tuple[str, str], role: MarkerRole):
        """Raw marker value for a bucket/role from the meta `markers` dict:
        a stem, None (present-but-null), or _MISSING (key absent)."""
        return markers.get(bucket_key(*bucket), {}).get(role, _MISSING)

    def _resolve_chosen(
        self, bucket: tuple[str, str], captures: list[Capture], markers: dict
    ) -> Capture | None:
        """The chosen (displayed) take. A pinned stem wins; otherwise fall
        back to the LATEST capture — the newest take is presumably the
        keeper until the user marks another (which pins across new arrivals).
        Chosen has no cleared state; a null/absent marker both mean latest."""
        stem = self._marker_stem(markers, bucket, MarkerRole.CHOSEN)
        if stem and stem is not _MISSING:
            for c in captures:
                if c.stem == stem:
                    return c
        return captures[-1] if captures else None

    def _resolve_reference(
        self, bucket: tuple[str, str], captures: list[Capture], markers: dict
    ) -> Capture | None:
        """The stitch reference photo. A pinned stem wins; an explicit null
        (user cleared it) means none; an absent marker falls back to the
        FIRST capture — the ColorChecker+scale shot is taken before the
        segments, so it is auto-marked without persisting anything."""
        stem = self._marker_stem(markers, bucket, MarkerRole.REFERENCE)
        if stem is None:
            return None                       # present-but-null: cleared
        if stem is not _MISSING:
            for c in captures:
                if c.stem == stem:
                    return c                  # pinned (or stale → fall through)
        return captures[0] if captures else None


class PapyriMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.logger = logging.getLogger(self.__class__.__name__)

        loadUi(get_ui_path('papyri/ui/main_window.ui'), self)

        # Centralized orchestrator state. Per-axis migration is in flight;
        # until each axis lands on `self.session`, the field stays on
        # MainWindow and the session is the no-op default.
        self.session = SessionState(self)

        # Bumped on every _refresh_bucket_chosen_thumbs call; async results
        # from the worker pool check it on arrival and discard themselves
        # if a newer refresh has already started (object swap, mark-chosen).
        self._chosen_thumb_gen = 0

        self.q_settings = QSettings()
        self._init_default_settings()
        # Capture mode (full papyri vs simple flat-folder). Chosen at
        # startup from the persisted setting; `self.mode` carries the
        # bucket layout / workflow groups / chrome flags so the two modes
        # don't fork MainWindow. See papyri.capture_mode.
        self.mode = get_mode(self.q_settings.value("captureMode", "papyri"))
        # Qt's default QPixmapCache limit is 10 MB — too small for one decoded
        # JPEG (~72 MB) let alone a RAW (~180 MB). Apply the user setting.
        QPixmapCache.setCacheLimit(int(self.q_settings.value("maxPixmapCache")) * 1024)
        self.profile = PROFILES[self.q_settings.value("profile", "MoritzA7III")]
        # Live-view display rotation in degrees (0/90/180/270), per spectrum
        # — VIS and IR are the two physical cameras, so this is per-camera.
        # Client-side display only (we rotate the preview pixmap), persisted
        # so it survives restarts. Applied in _on_preview_image.
        self._lv_rotation = {
            sp: int(self.q_settings.value(f"liveViewRotation/{sp}", 0))
            for sp in (SPECTRUM_VISIBLE, SPECTRUM_INFRARED)
        }
        # Path of the capture currently shown in the viewer, so the rotate
        # button can target it. Set by _on_filmstrip_image_decoded, cleared
        # when the viewer is cleared.
        self._shown_image_path: str | None = None
        # Connection bookkeeping for current_object's state_changed signal —
        # tracks what we're currently subscribed to so we can disconnect
        # before re-subscribing. Maintained by _handle_current_object_subscription.
        self._subscribed_object: "Object | None" = None
        # Periodic-calibration controller (papyri mode only). Created in
        # _wire_calibration after the widgets exist; stays None in simple
        # mode and is guarded everywhere it's used.
        self.calibration: "CalibrationController | None" = None
        # Whether the workspace is currently in the calibration sub-mode
        # (a transient context entered from papyri mode). Drives
        # `effective_mode`, which the chrome receivers read instead of the
        # fixed startup `self.mode`.
        self._calibration_active: bool = False
        # Calibration sub-mode state: the open per-camera target and the
        # object+bucket stashed for "← Back".
        self._cal_target: "CalibrationTarget | None" = None
        self._object_before_calibration = None
        self._bucket_before_calibration: tuple[str, str] | None = None

        self._bind_widgets()
        self._wire_actions()
        self._wire_camera()
        # _wire_session installs all session-axis receivers and invokes
        # each once for initial paint, so widgets reflect the session
        # defaults (Side A · Visible, no current object, no camera yet)
        # before the user does anything.
        self._wire_session()
        # Calibration bar + controller (papyri mode only) — after
        # _wire_session so the camera-dependent UI receivers exist.
        self._wire_calibration()

        # Simple mode has no "open an object" step — the chosen output
        # folder IS the (always-open) capture target. It lives in its own
        # setting (separate from papyri's workingDirectory) so we never
        # scan the home default; empty until the user picks one.
        if self.mode.key == "simple":
            self._open_simple_target(
                self.q_settings.value("simpleOutputDirectory", ""))

        # rotated-sample nudge: optional, fully self-contained reminder to
        # capture a 90°-rotated twin of every Nth piece. Remove this one call
        # (and the import + the module) to drop the feature entirely. Pass our
        # own Object class — this module runs as __main__, so the nudge must
        # not re-import it (would be a different class → isinstance fails).
        install_rotated_sample_nudge(self, Object)

        # Restore the last window geometry, overriding the .ui's baked-in
        # default size (saved in closeEvent). No-op on first launch.
        geometry = self.q_settings.value("windowGeometry")
        if geometry is not None:
            self.restoreGeometry(geometry)

    # ------------------------------------------------------------------ setup

    def _init_default_settings(self):
        defaults = {
            "profile": "MoritzA7III",
            "irProfile": None,                              # set when IR camera is configured
            "captureMode": "papyri",                        # "papyri" | "simple"
            "simpleOutputDirectory": "",                    # simple mode output folder (chosen via picker)
            "calibrationTrigger": "time",                   # "off" | "time" | "session" (papyri calibration reminder)
            "calibrationIntervalMinutes": 60,               # "time" trigger: minutes before a calibration is due
            "captureHeightChoices": "30,45,60,75,90",       # VIS height presets (cm), comma-separated; shared by capture row + flatfield
            "currentHeight": "45",                          # sticky current VIS rig height (stamped onto objects, tags flatfield)
            "irCaptureHeight": "45",                        # IR fixed camera height (single)
            "workingDirectory": "",   # no box open until the user picks one
            "maxPixmapCache": 256,
            "enableSecondScreenMirror": False,
            "sharpnessCheckEnabled": True,
            "liveViewSharpnessEnabled": True,
            "enableAuditiveFocusAssist": False,
        }
        for key, value in defaults.items():
            if self.q_settings.value(key) is None:
                self.q_settings.setValue(key, value)
        # Apply the sharpness-check toggle to the worker module-global
        # so workers spawned anywhere in the app see it.
        from byzanz_camera.load_image_worker import set_sharpness_enabled
        set_sharpness_enabled(self.q_settings.value(
            "sharpnessCheckEnabled", True, type=bool,
        ))
        # Gates the live-view sharpness compute and its label.
        self._live_sharpness_enabled = self.q_settings.value(
            "liveViewSharpnessEnabled", True, type=bool,
        )
        # Master gate for the focus tone; off if QtMultimedia is absent.
        self._focus_audio_enabled = AUDIO_AVAILABLE and self.q_settings.value(
            "enableAuditiveFocusAssist", False, type=bool,
        )

    def _bind_widgets(self):
        self.visible_camera_state: CameraStateWidget = self.findChild(
            CameraStateWidget, "visibleCameraState"
        )
        self.ir_camera_state: CameraStateWidget = self.findChild(
            CameraStateWidget, "irCameraState"
        )

        self.settings_button: QToolButton = self.findChild(QToolButton, "settingsButton")

        self.objects_sidebar: ObjectsSidebar = self.findChild(ObjectsSidebar, "objectsSidebar")
        # Outer splitter: ObjectsSidebar (index 0, fixed-ish) | rightColumn
        # (index 1, grows). Sidebar's own min/max width clamps the drag range.
        self.outer_splitter: QSplitter = self.findChild(QSplitter, "outerSplitter")
        self.outer_splitter.setStretchFactor(0, 0)
        self.outer_splitter.setStretchFactor(1, 1)
        self.metadata_splitter: QSplitter = self.findChild(QSplitter, "metadataSplitter")
        # Inner splitter: workspace (index 0, grows) | metadata pane (index 1,
        # stays at sizeHint by default, draggable down to 150px min).
        self.metadata_splitter.setStretchFactor(0, 1)
        self.metadata_splitter.setStretchFactor(1, 0)
        self.metadata_pane: MetadataPane = self.findChild(MetadataPane, "metadataPane")
        self.title_bar: ObjectTitleBar = self.findChild(ObjectTitleBar, "objectTitleBar")

        # BucketSelector — grouped tabbed boxes (Visible | Infrared)
        # paired with a FusingPanel below that contains the viewer +
        # capture controls + filmstrip. The active tab visually fuses
        # with the panel.
        self.bucket_selector: BucketSelector = self.findChild(
            BucketSelector, "bucketSelector"
        )
        self.fusing_panel: FusingPanel = self.findChild(
            FusingPanel, "fusingPanel"
        )
        # Mode-dependent chrome (bucket groups, sidebar/metadata/filmstrip,
        # calibration bar, title-bar visibility) is applied in one place by
        # `_apply_mode_chrome`, called at the end of _bind_widgets and again
        # whenever the effective mode flips (entering/leaving calibration).
        self.bucket_selector.set_fusing_panel(self.fusing_panel)
        self.fusing_panel.set_bucket_selector(self.bucket_selector)
        self.bucket_selector.step_clicked.connect(self._on_workflow_step_clicked)

        # Simple mode turns the title bar into a filename-override field + an
        # output-folder picker; the per-mode toggle is applied in
        # _apply_mode_state (re-runnable). Wire the picker once here.
        self.title_bar.output_folder_requested.connect(
            self._choose_simple_output_folder)
        self.viewer: ViewerWidget = self.findChild(ViewerWidget, "viewer")
        # The zoom bar lives in the panel's top toolbar (declared in the
        # .ui), not inside the viewer. Wire it to the viewer's
        # photo_viewer here.
        self.zoom_control_bar = self.findChild(ZoomControlBar, "zoomControlBar")
        self.viewer.attach_zoom_bar(self.zoom_control_bar)
        # Inject the papyri-specific "no object open" CTA into the
        # generic viewer's overlay slot. Drives via show_overlay /
        # show_photo from `_refresh_no_object_lockout`.
        self._no_object_overlay = NoObjectOverlay()
        self._no_object_overlay.new_object_requested.connect(
            self._on_sidebar_new_object
        )
        self.viewer.set_overlay_widget(self._no_object_overlay)
        self.filmstrip: PapyriFilmstrip = self.findChild(PapyriFilmstrip, "filmstrip")

        # Stitch connectivity check + its status strip. Built here (before
        # _wire_session's first filmstrip binding) so _refresh_stitch_ui can
        # run from the first bind. The bar hides itself for non-stitch buckets.
        self.stitch_bar: StitchBar = self.findChild(StitchBar, "stitchBar")
        self.stitch = StitchController(self)
        self.stitch.check_finished.connect(self._on_stitch_check_finished)
        self.stitch.preview_finished.connect(self._on_stitch_preview_finished)
        self.stitch_bar.preview_requested.connect(self._on_stitch_preview_requested)

        self.calibration_bar: CalibrationBar = self.findChild(
            CalibrationBar, "calibrationBar")

        self.pause_live_view_button: QPushButton = self.findChild(QPushButton, "pauseLiveViewButton")
        self.autofocus_button: QPushButton = self.findChild(QPushButton, "autofocusButton")
        self.magnify_button: QPushButton = self.findChild(QPushButton, "magnifyButton")
        self.rotate_live_view_button: QPushButton = self.findChild(QPushButton, "rotateLiveViewButton")
        self.rotation_label: QLabel = self.findChild(QLabel, "rotationLabel")
        self.focus_sharpness_label: QLabel = self.findChild(QLabel, "focusSharpnessLabel")
        self.focus_assist_button: QPushButton = self.findChild(QPushButton, "focusAssistButton")
        self.focus_audio = FocusAudio(self)
        self.capture_status_label: QLabel = self.findChild(QLabel, "captureStatusLabel")
        self.capture_button: QPushButton = self.findChild(QPushButton, "captureButton")

        # Capture-setting combos (ISO / aperture / shutter) in the capture
        # row. Populated from the active camera's live config; see
        # _on_config_update / config_hookup_select. Cache the last config
        # per spectrum so switching VIS<->IR can repopulate without waiting
        # for a fresh emit.
        self.iso_select: ConfigComboBox = self.findChild(ConfigComboBox, "isoSelect")
        self.f_number_select: ConfigComboBox = self.findChild(ConfigComboBox, "fNumberSelect")
        self.shutter_speed_select: ConfigComboBox = self.findChild(ConfigComboBox, "shutterSpeedSelect")
        self._capture_setting_combos = (
            self.iso_select, self.f_number_select, self.shutter_speed_select,
        )
        # A user pick routes to the ACTIVE worker (read at emit time so a
        # VIS<->IR switch targets the right camera). Connected once; the
        # widget only emits on genuine user changes, never on the 0.5s poll.
        for combo in self._capture_setting_combos:
            combo.value_chosen.connect(
                lambda name, value:
                    self.active_worker.commands.set_single_config.emit(name, value)
            )

        # Current VIS rig height — a sticky setting shared by object capture
        # (stamped per object) and per-height Flatfield calibration. Presets
        # come from Settings (captureHeightChoices). Lives in a prominent top-
        # bar cluster (rig height = a rig-wide state, not a per-shot setting)
        # alongside a read-only chip showing the open object's captured height.
        # The whole cluster is hidden in simple mode.
        self.rig_height_cluster: QWidget = self.findChild(QWidget, "rigHeightCluster")
        # A container QFrame only paints its QSS background/border with this
        # attribute set (same as the filmstrip / no-object overlay).
        self.rig_height_cluster.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.height_select: QComboBox = self.findChild(QComboBox, "heightSelect")
        self._populate_height_select()
        # `textActivated` fires ONLY on a user gesture (picking an item), never
        # on programmatic changes. That's the single seam that lets the combo
        # follow the open object (repopulate) without ever counting as an edit.
        self.height_select.textActivated.connect(self._on_height_changed)
        # Stitch toggle — a two-way binding to the current object's
        # `stitching` flag in `_meta.json`, no state of its own. `clicked`
        # (user interaction only, unlike `toggled`) writes the flag;
        # _refresh_stitch_toggle reflects it on object switch.
        self.stitch_toggle: QPushButton = self.findChild(
            QPushButton, "stitchToggleButton")
        self.stitch_toggle.clicked.connect(self._on_stitch_toggled)
        # Height-selector visibility is per-mode (papyri only) — set in
        # _apply_mode_state so a live switch updates it too.
        self._last_config: dict[str, object] = {}

        # Override raw .ui-set icons with themed versions so they
        # follow light/dark. capture_button gets its themed icon via
        # `_refresh_capture_button_label` (state-dependent); the others
        # are set once here.
        set_themed_icon(self.settings_button.setIcon, get_ui_path("ui/general_settings.svg"))
        set_themed_icon(self.pause_live_view_button.setIcon, get_ui_path("ui/live_preview.svg"))
        set_themed_icon(self.autofocus_button.setIcon, get_ui_path("ui/focus.svg"))
        set_themed_icon(self.magnify_button.setIcon, get_ui_path("ui/magnify.svg"))
        set_themed_icon(self.rotate_live_view_button.setIcon, get_ui_path("ui/rotate.svg"))
        # Capture-setting labels: same icons as the RTI (byzanz) app, themed
        # for light/dark (the SVGs use currentColor). The .ui leaves them
        # empty (28x28, scaledContents); we render the glyphs here.
        set_themed_pixmap(self.findChild(QLabel, "isoLabel").setPixmap,
                          get_ui_path("ui/iso-svgrepo-com.svg"))
        set_themed_pixmap(self.findChild(QLabel, "fNumberLabel").setPixmap,
                          get_ui_path("ui/aperture.svg"))
        set_themed_pixmap(self.findChild(QLabel, "shutterSpeedLabel").setPixmap,
                          get_ui_path("ui/shutter_speed.svg"))

        # Settings menu (popup off the "Settings" button)
        self.open_program_settings_action = self._action(
            "General settings", self.open_settings, icon="ui/general_settings.svg")
        # Per-camera advanced-config entries. IR entry's visibility is
        # toggled in _wire_camera based on whether IR was configured.
        # Enabled state is refreshed via aboutToShow so the menu reflects
        # current per-camera readiness without needing live signal wiring
        # (which lands in Stage 5 when camera_states migrate).
        self.open_vis_cam_config_action = self._action(
            "Configure VIS camera…",
            lambda: self.open_advanced_camera_config(SPECTRUM_VISIBLE),
            icon="ui/cam_settings.svg",
        )
        self.open_ir_cam_config_action = self._action(
            "Configure IR camera…",
            lambda: self.open_advanced_camera_config(SPECTRUM_INFRARED),
            icon="ui/cam_settings.svg",
        )
        # Mode toggle — switches between full papyri and simple capture mode
        # live (no restart; see _switch_capture_mode). Label reflects the
        # target mode and is kept current by _apply_mode_state.
        self.toggle_mode_action = self._action(
            self._mode_toggle_label(self.mode),
            self._toggle_capture_mode,
        )
        self.settings_menu = QMenu(self)
        self.settings_menu.addAction(self.open_program_settings_action)
        self.settings_menu.addSeparator()
        self.settings_menu.addAction(self.open_vis_cam_config_action)
        self.settings_menu.addAction(self.open_ir_cam_config_action)
        self.settings_menu.addSeparator()
        self.settings_menu.addAction(self.toggle_mode_action)
        self.settings_menu.aboutToShow.connect(self._refresh_settings_menu_state)

        # Apply the startup mode's chrome + state once, now that every widget
        # exists. Both are re-run by _switch_capture_mode on a live switch.
        self._apply_mode_chrome(self.mode)
        self._apply_mode_state(self.mode)

    @property
    def effective_mode(self):
        """The mode whose chrome/layout is currently in force. Equals the
        fixed startup `self.mode`, except while the transient calibration
        sub-mode is active (entered from papyri mode). Chrome receivers read
        THIS, not `self.mode`, so calibration borrows the simple-mode look
        without forking the window."""
        return CALIBRATION_MODE if self._calibration_active else self.mode

    def _apply_mode_chrome(self, mode) -> None:
        """Apply every mode-dependent widget setting in one place. Called
        at startup and whenever the effective mode flips (enter/leave
        calibration). `set_groups` rebuilds the bucket bars — callers that
        flip the mode at runtime must re-assert the active bucket after."""
        self.bucket_selector.set_show_thumbs(mode.show_thumbs)
        self.bucket_selector.set_groups(mode.groups)
        self.objects_sidebar.setVisible(mode.show_sidebar)
        self.metadata_pane.setVisible(mode.show_metadata)
        self.filmstrip.set_simple_mode(mode.whole_folder_filmstrip)
        self.calibration_bar.setVisible(mode.show_calibration)
        # The calibration bar carries the kind toggle + back button, so the
        # object title bar is hidden in the calibration sub-mode.
        self.title_bar.setVisible(mode.key != "calibration")

    def _apply_mode_state(self, mode) -> None:
        """Apply the non-chrome, mode-dependent widget state — the bits that
        were one-time __init__ setup but must re-run on a live mode switch.
        Idempotent; startup and _switch_capture_mode share this (no
        duplication). Chrome (visibility/groups/filmstrip) is _apply_mode_chrome.
        Does NOT open a target — that's the caller's concern (startup vs switch)."""
        simple = mode.key == "simple"
        # Title bar: filename-override + folder picker (simple) vs Inv-No.
        # display (papyri). set_simple_mode is reversible.
        self.title_bar.set_simple_mode(
            simple, self.q_settings.value("simpleOutputDirectory", ""))
        # Rig-height cluster and Stitch toggle are papyri-only.
        self.rig_height_cluster.setVisible(not simple)
        self.stitch_toggle.setVisible(not simple)
        # The mode-toggle menu action names the OTHER mode.
        self.toggle_mode_action.setText(self._mode_toggle_label(mode))

    @staticmethod
    def _mode_toggle_label(mode) -> str:
        """Label for the mode-toggle action — names the mode it switches TO."""
        return ("Switch to full (papyri) mode" if mode.key == "simple"
                else "Switch to simple capture mode")

    def _action(self, label: str, slot, icon: str | None = None) -> QAction:
        action = QAction(label, self)
        action.triggered.connect(slot)
        if icon:
            set_themed_icon(action.setIcon, get_ui_path(icon))
        return action

    def _popup_below(self, button, menu: QMenu):
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))

    def _wire_actions(self):
        self.settings_button.clicked.connect(
            lambda: self._popup_below(self.settings_button, self.settings_menu))

        # Title bar owns the object-name affordance + close + rename
        # buttons (its .ui forwards the QToolButton clicks to these signals).
        self.title_bar.start_object_requested.connect(self.start_object)
        self.title_bar.rename_requested.connect(self.rename_current_object)
        self.title_bar.close_requested.connect(self.close_object)
        # "+ New" in the title bar reuses the sidebar's new-object flow
        # (close current, then focus the name field for typing + Enter).
        self.title_bar.new_object_requested.connect(self._on_sidebar_new_object)

        # Connect/disconnect buttons live inside CameraStateWidget —
        # widget-internal wiring, no main.py plumbing needed.

        self.pause_live_view_button.toggled.connect(self._on_live_view_toggled)
        self.autofocus_button.clicked.connect(self._trigger_autofocus)
        self.magnify_button.toggled.connect(self._on_magnify_toggled)
        self.focus_assist_button.toggled.connect(self._on_focus_assist_toggled)
        self.rotate_live_view_button.clicked.connect(self._rotate_view)
        self.capture_button.clicked.connect(self.capture_image)

        # Filmstrip → main.py (user actions, lifecycle)
        self.filmstrip.image_selected.connect(self._on_image_selected)
        self.filmstrip.directory_loaded.connect(self._on_directory_loaded)
        # Filmstrip → viewer (decoded images, cleared selection, closed dir).
        # _on_filmstrip_image_decoded is a small wrapper that drops the path
        # arg (signal carries it for future logging; viewer only needs the
        # pixmap).
        self.filmstrip.image_decoded.connect(self._on_filmstrip_image_decoded)
        self.filmstrip.image_cleared.connect(self.viewer.clear)
        self.filmstrip.image_cleared.connect(self._clear_shown_image)
        # directory_closed signal carries path; viewer.clear takes no args
        # — PyQt silently drops extra signal args, so this connects fine.
        self.filmstrip.directory_closed.connect(self.viewer.clear)
        self.filmstrip.directory_closed.connect(self._clear_shown_image)
        # Spinner during cache-miss full-decode of a clicked thumb.
        # show_busy takes no args; PyQt drops the path arg.
        self.filmstrip.image_decode_started.connect(self.viewer.show_busy)

        # Objects sidebar. The working directory IS the open box (its folder
        # name is the box no.); the last box auto-reopens via this setting.
        self.objects_sidebar.set_recent_boxes(self._recent_boxes())
        self._activate_box(self.q_settings.value("workingDirectory", ""))
        self.objects_sidebar.object_selected.connect(self._on_sidebar_object_selected)
        self.objects_sidebar.new_object_requested.connect(self._on_sidebar_new_object)
        self.objects_sidebar.open_box_requested.connect(self._on_open_box)
        self.objects_sidebar.new_box_requested.connect(self._on_new_box)
        self.objects_sidebar.recent_box_chosen.connect(self._open_box)
        self.objects_sidebar.delete_object_requested.connect(self._on_sidebar_delete_object)

        # Metadata changes flip the sidebar badge between `??` and `✓` —
        # keep them in sync without manual refresh.
        self.metadata_pane.metadata_changed.connect(self.objects_sidebar.refresh)

        # Cmd+/ toggles the objects sidebar; Cmd+\ toggles the metadata pane.
        # (Qt maps Ctrl → Cmd on macOS; on Windows/Linux these stay Ctrl.)
        toggle_sidebar = QAction("Toggle objects sidebar", self)
        toggle_sidebar.setShortcut("Ctrl+/")
        toggle_sidebar.triggered.connect(
            lambda: self.objects_sidebar.setVisible(not self.objects_sidebar.isVisible())
        )
        self.addAction(toggle_sidebar)

        toggle_metadata = QAction("Toggle metadata pane", self)
        toggle_metadata.setShortcut("Ctrl+\\")
        toggle_metadata.triggered.connect(
            lambda: self.metadata_pane.setVisible(not self.metadata_pane.isVisible())
        )
        self.addAction(toggle_metadata)

    def _wire_camera(self):
        # Visible worker — always present.
        self.visible_worker, self.visible_thread = self._spawn_worker(self.profile)
        self.visible_worker.state_changed.connect(
            lambda s: self._on_camera_state_changed(SPECTRUM_VISIBLE, s)
        )
        # Both workers' preview frames go through one handler that drops
        # frames from the inactive spectrum — otherwise IR frames couldn't
        # display when IR is active (only VIS was wired) and switching
        # spectrum would briefly show the wrong feed.
        self.visible_worker.preview_image.connect(
            lambda img: self._on_preview_image(SPECTRUM_VISIBLE, img)
        )
        self.visible_worker.initialized.connect(
            lambda: self.visible_worker.commands.find_camera.emit()
        )
        # Capture-setting combos: each worker emits config_updated on connect
        # and after every set_single_config. Route through one handler that
        # only repaints when the event is from the active spectrum (mirrors
        # the preview-frame drop pattern).
        self.visible_worker.events.config_updated.connect(
            lambda cfg: self._on_config_update(SPECTRUM_VISIBLE, cfg)
        )
        self.visible_worker.usb_offenders_detected.connect(
            self._on_usb_offenders_detected
        )
        self.visible_camera_state.bind_worker(self.visible_worker, "VIS", self.profile)
        self.visible_thread.start()

        # IR worker — only if the user configured an IR profile in Settings.
        self.ir_worker: CameraWorker | None = None
        self.ir_thread: QThread | None = None
        self.ir_profile = None
        ir_profile_id = self.q_settings.value("irProfile")
        if ir_profile_id and ir_profile_id in PROFILES:
            self.ir_profile = PROFILES[ir_profile_id]
            self.ir_worker, self.ir_thread = self._spawn_worker(self.ir_profile)
            self.ir_worker.state_changed.connect(
                lambda s: self._on_camera_state_changed(SPECTRUM_INFRARED, s)
            )
            self.ir_worker.preview_image.connect(
                lambda img: self._on_preview_image(SPECTRUM_INFRARED, img)
            )
            self.ir_worker.initialized.connect(
                lambda: self.ir_worker.commands.find_camera.emit()
            )
            self.ir_worker.events.config_updated.connect(
                lambda cfg: self._on_config_update(SPECTRUM_INFRARED, cfg)
            )
            self.ir_worker.usb_offenders_detected.connect(
                self._on_usb_offenders_detected
            )
            self.ir_camera_state.bind_worker(self.ir_worker, "IR", self.ir_profile)
            self.ir_thread.start()
            self.logger.info(
                "IR worker started for profile %r (model pattern: %r)",
                self.ir_profile.name(), self.ir_profile.gphoto2_model_pattern(),
            )
        else:
            # No IR profile — hide the IR camera-state widget. The IR cells
            # in the side cards stay visible but are silently no-op'd (clicks
            # fall back to visible in _set_active_bucket).
            self.ir_camera_state.setVisible(False)
            # Same logic for the per-camera advanced-config menu entry.
            self.open_ir_cam_config_action.setVisible(False)

    def _wire_session(self) -> None:
        """All `session.*_changed.connect(...)` calls live here. Single grep
        target for "what reacts to what" — one line per receiver per axis,
        sorted by axis. After each connect block, invoke each receiver once
        for initial paint (receivers are idempotent — rule #1 — so this is
        safe and gives widgets the right state before the user does anything).

        Receiver naming convention — name after the *property* set, not
        an abstract concept. Anything vaguer (`_state`, `_status`,
        `_appearance`) is a red flag that the receiver is doing too much.

            _enable     setEnabled(...)
            _visible    setVisible(...)
            _text       text content (+ associated indicators like
                        checked / icon when they always change together)
            _color      foreground / background tint
            _binding    a model / object binding (bind_object, set_count, …)
            _emphasis   a non-content visual highlight (border, glow)

        A receiver that needs to depend on multiple axes subscribes to
        each one — multi-subscribe is normal; the receiver re-reads from
        session each call so over-running is harmless."""
        s = self.session

        # active_bucket
        s.active_bucket_changed.connect(self._refresh_workflow_stepper_active)
        s.active_bucket_changed.connect(self._refresh_capture_button_label)
        s.active_bucket_changed.connect(self._refresh_camera_state_emphasis)
        s.active_bucket_changed.connect(self._refresh_filmstrip_binding)
        # Spectrum switch → repaint capture-setting combos from the newly
        # active camera's cached config, and re-evaluate camera-dependent
        # controls (autofocus button etc.) against the now-active camera.
        s.active_bucket_changed.connect(self._populate_capture_setting_combos)
        s.active_bucket_changed.connect(self._refresh_camera_dependent_ui)
        # Spectrum switch → repopulate the height combo from the now-active
        # camera's choices (VIS preset list vs IR's single fixed value).
        s.active_bucket_changed.connect(self._populate_height_select)
        # While calibrating, the "for height X" banner follows the active camera.
        s.active_bucket_changed.connect(self._refresh_calibration_banner)
        s.active_bucket_changed.connect(self._on_active_bucket_changed_live_view)
        self._refresh_workflow_stepper_active()
        self._refresh_capture_button_label()
        self._refresh_camera_state_emphasis()
        self._refresh_filmstrip_binding()

        # current_object
        s.current_object_changed.connect(self._refresh_metadata_pane_binding)
        s.current_object_changed.connect(self._refresh_title_bar_binding)
        s.current_object_changed.connect(self._refresh_objects_sidebar_active)
        s.current_object_changed.connect(self._refresh_objects_sidebar_entries)
        s.current_object_changed.connect(self._refresh_workflow_stepper_active)
        s.current_object_changed.connect(self._refresh_bucket_chosen_thumbs)
        s.current_object_changed.connect(self._refresh_filmstrip_binding)
        s.current_object_changed.connect(self._handle_current_object_subscription)
        s.current_object_changed.connect(self._handle_current_object_view_mode_reset)
        s.current_object_changed.connect(self._refresh_no_object_lockout)
        s.current_object_changed.connect(self._refresh_capture_button_label)
        # Enable "Calibrate ▸" only with an object open (calibration is for its
        # height).
        s.current_object_changed.connect(self._refresh_calibration_bar)
        s.current_object_changed.connect(self._refresh_stitch_toggle)
        # Object switch → the combo follows the now-open object's height.
        s.current_object_changed.connect(self._populate_height_select)
        self._refresh_metadata_pane_binding()
        self._refresh_title_bar_binding()
        self._refresh_objects_sidebar_active()
        self._refresh_objects_sidebar_entries()
        self._refresh_bucket_chosen_thumbs()
        # _refresh_filmstrip_binding already called above (active_bucket init)
        self._handle_current_object_subscription()
        self._handle_current_object_view_mode_reset()
        self._refresh_no_object_lockout()
        self._refresh_stitch_toggle()
        self._populate_height_select()

        # live_view_paused
        s.live_view_paused_changed.connect(self._refresh_live_view_button)
        s.live_view_paused_changed.connect(self._sync_live_view)
        self._refresh_live_view_button()

        # view_mode
        s.view_mode_changed.connect(self._refresh_view_mode_indicator)
        self._refresh_view_mode_indicator()
        # Rotate button is independent of live view: usable whenever the
        # viewer shows something (live, preview or paused), disabled when empty.
        s.view_mode_changed.connect(self._refresh_rotate_button)
        self._refresh_rotate_button()

        # camera_state (per-spectrum). Several receivers, each with its own
        # match block — see the design note above _refresh_camera_dependent_ui
        # for the per-state-vs-per-purpose split rationale.
        s.camera_state_changed.connect(self._handle_camera_lifecycle)
        s.camera_state_changed.connect(self._handle_active_camera_state)
        s.camera_state_changed.connect(self._sync_live_view)
        s.camera_state_changed.connect(self._refresh_camera_dependent_ui)
        # Camera-state also drives the capture-button caption (flips
        # to "<X> camera not connected" when the active camera isn't
        # ready). Filtered to the active spectrum inside the slot.
        s.camera_state_changed.connect(
            lambda *_: self._refresh_capture_button_label()
        )
        # Also depends on object loaded state for capture-button enable.
        s.current_object_changed.connect(self._refresh_camera_dependent_ui)
        # Don't init-paint _handle_camera_lifecycle / _handle_active_camera_state
        # — they emit worker commands which would spuriously fire on init
        # before the camera is connected. UI receiver is safe to invoke.
        self._refresh_camera_dependent_ui()

    def _spawn_worker(self, profile: Profile) -> tuple[CameraWorker, QThread]:
        """Build a worker pre-configured to find the right camera by model
        pattern. Caller wires the signals and starts the thread."""
        worker = CameraWorker()
        worker.target_model_pattern = profile.gphoto2_model_pattern()
        worker.pinned_port = profile.gphoto2_port()
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.initialize)
        return worker, thread

    def _hot_switch_profile(self, spectrum: str, new_profile: Profile) -> str | None:
        """Rebind a camera slot to a new profile at runtime (settings change
        without restart). Returns None on success, or a user-facing reason
        string — having changed nothing — when switching is refused:
        mid-capture (would tear the camera down around the in-flight
        capture) or the profile is the other slot's (our two workers would
        fight over one physical camera with USB claim errors).

        The detection filter (model pattern / pinned port) lives as plain
        worker attributes set at spawn time, so it must be rebound here;
        everything downstream of "camera found" follows the profile object
        emitted with the next connect_camera."""
        if spectrum == SPECTRUM_VISIBLE:
            worker, state_widget = self.visible_worker, self.visible_camera_state
            other_profile, other_label = self.ir_profile, "IR"
        else:
            worker, state_widget = self.ir_worker, self.ir_camera_state
            other_profile, other_label = self.profile, "visible"

        if new_profile is other_profile:
            return (f"This profile is already used by the {other_label} "
                    "camera. Two slots cannot drive the same camera.")

        state = self.session.camera_state(spectrum)
        if isinstance(state, (CameraStates.CaptureInProgress,
                              CameraStates.CaptureCancelling)):
            return ("A capture is running on this camera. Change the "
                    "profile again once the capture has finished.")

        if spectrum == SPECTRUM_VISIBLE:
            self.profile = new_profile
        else:
            self.ir_profile = new_profile
        worker.target_model_pattern = new_profile.gphoto2_model_pattern()
        worker.pinned_port = new_profile.gphoto2_port()
        # The old target may have burned the USB recovery budget (e.g. a
        # claim fight); the new target gets a fresh allowance.
        worker.reset_usb_recovery_budget()
        state_widget.set_profile(new_profile)

        self.logger.info("Hot-switched %s profile to %r",
                         spectrum, new_profile.name())
        if isinstance(state, CameraStates.Waiting):
            # The worker is inside its find loop and can't process a queued
            # reconnect until a camera is found; it re-reads the filter each
            # iteration, so the rebind alone takes effect. A queued reconnect
            # would fire only after the next connect and cause a spurious
            # disconnect cycle.
            return None
        worker.commands.reconnect_camera.emit()
        return None

    @property
    def active_worker(self) -> CameraWorker:
        """The worker currently driving the UI (live view, capture, etc.).
        Tracks `session.active_spectrum`; falls back to visible if IR isn't
        configured (defense-in-depth — the IR-fallback in
        _on_workflow_step_clicked should already prevent active_spectrum
        from being IR without an IR worker)."""
        if (self.session.active_spectrum == SPECTRUM_INFRARED
                and self.ir_worker is not None):
            return self.ir_worker
        return self.visible_worker

    def _active_profile(self) -> Profile | None:
        """Profile of the active spectrum's camera (None if IR active but
        unconfigured — shouldn't happen, defensive)."""
        if (self.session.active_spectrum == SPECTRUM_INFRARED
                and self.ir_worker is not None):
            return self.ir_profile
        return self.profile

    def _autofocus_supported(self) -> bool:
        """Whether the active camera's profile allows autofocus. Gates the
        autofocus button on top of the state-driven enable, so a
        manual-focus body (e.g. IR D90 + CoastalOpt 60/4) never enables it."""
        profile = self._active_profile()
        return bool(profile and profile.supports_autofocus())

    def _focus_magnify_supported(self) -> bool:
        """Whether the active camera can magnify the live view for focus
        checking. Gates the magnify button's visibility (together with
        live-view-active state)."""
        profile = self._active_profile()
        return bool(profile and profile.focus_magnify_property_name())

    def _live_view_supported(self) -> bool:
        """Whether the active camera can stream live view. Gates the pause
        button (a no-live-view camera has no stream to pause). Sibling of
        _autofocus_supported / _focus_magnify_supported; _sync_live_view
        checks the per-spectrum profile directly in its own loop."""
        profile = self._active_profile()
        return bool(profile and profile.supports_live_view())

    # ---- capture-setting combos (ISO / aperture / shutter) -------------

    def _on_config_update(self, spectrum: str, config) -> None:
        """A worker pushed fresh config (on connect, or after a
        set_single_config). Cache it per spectrum and, if it's the active
        spectrum, repaint the capture-setting combos."""
        self._last_config[spectrum] = config
        if spectrum == self.session.active_spectrum:
            self._populate_capture_setting_combos()

    def _populate_capture_setting_combos(self) -> None:
        """(Re)bind ISO / aperture / shutter combos to the active camera's
        cached config. Called on config_update for the active spectrum and
        after an active-spectrum switch."""
        config = self._last_config.get(self.session.active_spectrum)
        profile = self._active_profile()
        if config is None or profile is None:
            for combo in self._capture_setting_combos:
                combo.clear_binding()
        else:
            self.iso_select.update_from_config(config, profile.iso_property_name())
            if profile.has_settable_aperture():
                self.f_number_select.setToolTip("Aperture (f-number)")
                self.f_number_select.update_from_config(
                    config, profile.f_number_property_name())
            else:
                # Manual aperture-ring lens (e.g. IR D90 + CoastalOpt 60/4):
                # leave the combo cleared → _refresh_capture_combo_enabled
                # keeps it disabled.
                self.f_number_select.clear_binding()
                self.f_number_select.setToolTip(
                    "Aperture is set on the lens ring (manual lens)")
            self.shutter_speed_select.update_from_config(
                config, profile.shutterspeed_property_name())
        self._refresh_capture_combo_enabled()

    def _refresh_capture_combo_enabled(self) -> None:
        """Final enable state for the capture-setting combos: intrinsically
        settable (per ConfigComboBox.is_settable) AND the active camera is
        ready and not mid-capture."""
        live = self._active_camera_ready() and not isinstance(
            self.session.active_camera_state, CameraStates.CaptureInProgress)
        for combo in self._capture_setting_combos:
            combo.setEnabled(live and combo.is_settable())

    # ---- capture height (per-object, capture-row cluster) --------------

    def _object_height_seed(self, spectrum: str) -> str:
        """The height a not-yet-captured object shows / inherits: the last
        height set by the user (persisted). New objects inherit it; capture
        stamps it as the object's own `capture_height_vis`."""
        return current_height_for(self.q_settings, spectrum)

    def _populate_height_select(self) -> None:
        """(Re)fill the height combo from the ACTIVE camera's choices and
        select the OPEN OBJECT's height. Height is a per-object value: VIS
        objects carry their own `capture_height_vis`; an object with none yet
        (uncaptured) shows the inherited seed. IR is a single fixed value — a
        one-element list can't be changed, so editability falls out of the
        count (no per-camera branch). Called at startup, on spectrum switch,
        on object switch, and when presets change in Settings."""
        spectrum = self.session.active_spectrum
        choices = height_choices_for(self.q_settings, spectrum)
        if self._calibration_active:
            # Calibration is FOR the stashed object's height and can't be
            # changed here — show it locked, mirroring the "for height X" banner.
            current = self._object_height(self._object_before_calibration, spectrum)
            enabled = False
        else:
            current = self._current_object_height(spectrum)
            # A single choice is not adjustable — fixed height, no special case.
            enabled = len(choices) > 1
        self.height_select.blockSignals(True)
        self.height_select.clear()
        self.height_select.addItems(choices)
        idx = self.height_select.findText(current)
        self.height_select.setCurrentIndex(idx if idx >= 0 else 0)
        self.height_select.blockSignals(False)
        self.height_select.setEnabled(enabled)

    def _object_height(self, obj, spectrum: str) -> str:
        """Height of a specific object in `spectrum`: its stamped
        `capture_height_vis` if it has one, else the inherited seed. IR always
        resolves to its fixed value (objects don't carry a per-object IR
        height)."""
        if spectrum == SPECTRUM_VISIBLE and isinstance(obj, Object):
            stored = read_meta(obj.meta_path).get(MetaKey.HEIGHT_VIS)
            if stored is not None:
                return str(stored)
        return self._object_height_seed(spectrum)

    def _current_object_height(self, spectrum: str) -> str:
        """`_object_height` for the currently open object."""
        return self._object_height(self.session.current_object, spectrum)

    def _on_height_changed(self, text: str) -> None:
        """User picked a height (via `textActivated` — a deliberate edit, not a
        programmatic combo sync). Height belongs to the OPEN OBJECT and spans
        all its VIS captures (both sides). Changing it on an already-captured
        object re-labels those shots, so confirm first. Either way the value
        becomes the seed new objects inherit."""
        if not text:
            return
        spectrum = self.session.active_spectrum
        obj = self.session.current_object
        # No object (or a non-papyri target): the pick is just the seed.
        if not isinstance(obj, Object):
            set_current_height(self.q_settings, spectrum, text)
            self._refresh_after_height_change()
            return
        old = self._current_object_height(spectrum)
        if text == old:
            return
        captured = obj.count(SIDE_A, spectrum) + obj.count(SIDE_B, spectrum) > 0
        if captured and not self._confirm_height_change(obj, old, text):
            self.height_select.blockSignals(True)      # user kept the old height
            self.height_select.setCurrentText(old)
            self.height_select.blockSignals(False)
            return
        update_meta(obj.meta_path, {MetaKey.HEIGHT_VIS: text})
        set_current_height(self.q_settings, spectrum, text)   # seed follows the last edit
        self._refresh_after_height_change()

    def _confirm_height_change(self, obj: "Object", old: str, new: str) -> bool:
        """Ask before re-labelling an already-captured object's height. Returns
        True to proceed, False to keep the old height."""
        box = QMessageBox(self)
        box.setWindowTitle("Change object height?")
        box.setText(
            f"The height of {obj.name} will be changed for all VIS images "
            f"(Side A and B) from {old} cm to {new} cm.\n\n"
            f"The images themselves will remain unchanged — only the "
            f"documented height will be updated."
        )
        box.addButton(f"Keep {old} cm", QMessageBox.ButtonRole.RejectRole)
        change = box.addButton(f"Change to {new} cm", QMessageBox.ButtonRole.AcceptRole)
        box.exec()
        return box.clickedButton() is change

    def _refresh_after_height_change(self) -> None:
        """A height edit changes which Flatfield set is in play. While
        calibrating, the active folder is per height, so rebind the filmstrip
        to the new height's shots and re-evaluate due-status."""
        if self._calibration_active and self._cal_target is not None:
            self._cal_target.refresh()
            self._refresh_filmstrip_binding()
        if self.calibration is not None:
            self.calibration.refresh()

    def _on_stitch_toggled(self, checked: bool) -> None:
        """User clicked the Stitch toggle → write the flag to the current
        object's `_meta.json`. `set_stitching` refreshes the object, so the
        filmstrip and stitch check react via `state_changed`."""
        obj = self.session.current_object
        if isinstance(obj, Object):
            obj.set_stitching(checked)

    def _refresh_stitch_toggle(self) -> None:
        """Reflect the current object's stitching flag; disabled without an
        object. setChecked does not emit `clicked`, so no write-back loop."""
        obj = self.session.current_object
        is_obj = isinstance(obj, Object)
        self.stitch_toggle.setEnabled(is_obj)
        self.stitch_toggle.setChecked(is_obj and obj.is_stitching())

    def _stamp_capture_metadata(self, obj: "Object") -> None:
        """Merge derived metadata into the object's `_meta.json` on capture:
        the heights it was shot at, and the box number (= the basename of
        the box/working directory the object lives in — box no. is the folder,
        not a typed field). Merge (not overwrite) so metadata-pane fields
        survive; the pane likewise merges. The VIS height is the object's own:
        set from the inherited seed on the first VIS capture, then FROZEN —
        later captures keep it, so only an explicit (confirmed) edit changes it."""
        meta = read_meta(obj.meta_path)
        try:
            update_meta(obj.meta_path, {
                MetaKey.HEIGHT_VIS: meta.get(MetaKey.HEIGHT_VIS)
                    or current_height_for(self.q_settings, SPECTRUM_VISIBLE),
                MetaKey.HEIGHT_IR: current_height_for(
                    self.q_settings, SPECTRUM_INFRARED),
                MetaKey.BOX_NR: os.path.basename(os.path.normpath(obj.working_dir)),
            })
        except OSError:
            # A failed stamp must not block the capture itself.
            self.logger.warning(
                "could not stamp %s", obj.meta_path, exc_info=True)

    # ---- active-bucket receivers ----------------------------------------

    def _refresh_bucket_chosen_thumbs(self) -> None:
        """Update each bucket card's thumb to its chosen-take preview.
        For chosen JPEGs we load the file directly; for chosen RAWs we
        use the camera's embedded JPEG preview via rawpy (~50–200 ms).
        Both paths flow through LoadImageWorker (same code the filmstrip
        uses), running on the global thread pool so the UI doesn't block.

        Buckets without a chosen take get a synchronous `None` —
        BucketSelector renders the dashed-placeholder empty state.

        Subscribed to current_object_changed (object swap). Also called
        from _on_object_state_changed so that marking-as-chosen and new
        captures update the thumb without needing a separate signal.

        A generation token is bumped on every call; async results check
        it on arrival and silently drop themselves if a newer refresh
        has overtaken them (avoids the "previous object's thumbs flash
        in the new object's cards" race)."""
        # Simple / calibration modes have no chosen-take concept — the cards
        # are a plain camera switch (show_thumbs=False), so there's nothing
        # to load.
        if not self.effective_mode.show_thumbs:
            return
        self._chosen_thumb_gen += 1
        gen = self._chosen_thumb_gen
        obj = self.session.current_object
        pool = QThreadPool.globalInstance()
        for (side, spectrum), step_id in self.effective_mode.step_id_by_bucket.items():
            cap = obj.chosen(side, spectrum) if obj is not None else None
            path = (cap.jpg_path or cap.raw_path) if cap is not None else None
            if path is None:
                self.bucket_selector.set_chosen_thumb(step_id, None)
                continue
            worker = LoadImageWorker(
                path,
                mode=ImageMode.THUMB,
                thumb_max_size=128,
            )
            worker.signals.finished.connect(
                lambda result, sid=step_id, g=gen:
                    self._on_chosen_thumb_loaded(sid, g, result)
            )
            pool.start(worker)

    def _on_chosen_thumb_loaded(self, step_id: str, gen: int, result) -> None:
        if gen != self._chosen_thumb_gen:
            return
        if result.thumbnail is None or result.thumbnail.isNull():
            return
        self.bucket_selector.set_chosen_thumb(
            step_id, QPixmap.fromImage(result.thumbnail)
        )

    def _refresh_workflow_stepper_active(self) -> None:
        """Active step highlight. Reads active bucket + current object —
        when no object is
        loaded there's no "where the next capture goes" to point at, so
        clear the highlight rather than leave it pointing at a stale
        bucket from the closed object."""
        if self.session.current_object is None:
            self.bucket_selector.set_active(None)
            return
        self.bucket_selector.set_active(
            self.effective_mode.step_id_by_bucket[(
                self.session.active_side, self.session.active_spectrum
            )]
        )

    def _refresh_capture_button_label(self) -> None:
        """Capture button caption + icon. Priority order:
        (1) no object loaded → instruct the user to open one;
        (2) active spectrum's camera not ready → name the missing camera
            AND swap to the camera-not-ok icon so the disabled button
            reads as a warning hint, not a static label;
        (3) normal → "Capture · Side X · Visible/Infrared" with the
            capture icon."""
        key = self.effective_mode.key
        if self.session.current_object is None:
            self.capture_button.setText(
                "Set an output folder" if key == "simple"
                else "Open an object to capture")
            set_themed_icon(self.capture_button.setIcon, get_ui_path("ui/capture.svg"))
            return
        spectrum_label = (
            "Visible" if self.session.active_spectrum == SPECTRUM_VISIBLE
            else "Infrared"
        )
        if not self._active_camera_ready():
            self.capture_button.setText(f"{spectrum_label} camera not connected")
            set_themed_icon(self.capture_button.setIcon, get_ui_path("ui/camera_not_ok.svg"))
            return
        if key == "calibration":
            # No side axis — caption names the calibration target (from the
            # active slot) instead of "Side X".
            kind_label = label_for_slot(self.session.active_side)
            self.capture_button.setText(f"Capture · {kind_label} · {spectrum_label}")
        elif key == "simple":
            # Simple mode has no side axis — drop "Side X" from the caption.
            self.capture_button.setText(f"Capture · {spectrum_label}")
        else:
            side_label = "Side A" if self.session.active_side == SIDE_A else "Side B"
            self.capture_button.setText(f"Capture · {side_label} · {spectrum_label}")
        self.capture_button.setIcon(QIcon(get_ui_path("ui/capture.svg")))

    def _active_camera_ready(self) -> bool:
        """True iff the active spectrum's camera is in a state where
        capture / focus / live view are actually possible (vs Waiting,
        Connecting, Disconnected, ConnectionError, etc.)."""
        return isinstance(self.session.active_camera_state, (
            CameraStates.Ready,
            CameraStates.LiveViewStarted,
            CameraStates.LiveViewActive,
            CameraStates.FocusStarted,
            CameraStates.FocusFinished,
            CameraStates.CaptureFinished,
        ))

    def _refresh_camera_state_emphasis(self) -> None:
        """Top-bar camera-state pill emphasis: the active spectrum's
        widget gets a 2px colored border. Reads the active spectrum."""
        self.visible_camera_state.set_emphasized(
            self.session.active_spectrum == SPECTRUM_VISIBLE
        )
        self.ir_camera_state.set_emphasized(
            self.session.active_spectrum == SPECTRUM_INFRARED
        )

    def _refresh_filmstrip_binding(self) -> None:
        """Re-bind PapyriFilmstrip to (current_object, active_side,
        active_spectrum). Safe with current_object=None (filmstrip closes
        its directory; the directory_closed signal also clears the viewer).
        Reads active bucket + current object."""
        self.filmstrip.bind_object(
            self.session.current_object,
            self.session.active_side,
            self.session.active_spectrum,
        )
        self._refresh_stitch_ui()

    # ---- stitch connectivity check --------------------------------------

    def _active_stitch_bucket(self) -> tuple["Object", str, str] | None:
        """The (object, side, spectrum) the stitch UI applies to, or None
        when the active bucket is not a stitch bucket (no stitching object,
        simple mode, or the calibration sub-mode)."""
        obj = self.session.current_object
        if (isinstance(obj, Object) and obj.is_stitching()
                and not self._calibration_active):
            return obj, self.session.active_side, self.session.active_spectrum
        return None

    def _refresh_stitch_ui(self, *, schedule_recheck: bool = False) -> None:
        """Reconcile the stitch bar + filmstrip dots for the active bucket.
        Shows a persisted report when it still matches the bucket's files,
        otherwise runs a check (debounced on capture settle via
        `schedule_recheck`, immediate on a bucket switch)."""
        bucket = self._active_stitch_bucket()
        if bucket is None:
            self.stitch_bar.setVisible(False)
            self.filmstrip.set_connectivity(None)
            return
        self.stitch_bar.setVisible(True)
        report = self.stitch.fresh_report(*bucket)
        if report is not None:
            self._apply_stitch_report(report)
            return
        # Stale or never checked — dots go grey, bar spins, run a check.
        self.stitch_bar.show_checking()
        self.filmstrip.set_connectivity(None)
        if schedule_recheck:
            self.stitch.schedule_check(*bucket)
        else:
            self.stitch.run_check_now(*bucket)

    def _apply_stitch_report(self, report) -> None:
        self.stitch_bar.show_message(report.message, report.level)
        self.stitch_bar.set_preview_enabled(report.allows_preview())
        self.filmstrip.set_connectivity(report.status_by_stem())

    def _on_stitch_check_finished(
        self, obj_dir: str, side: str, spectrum: str, report,
    ) -> None:
        """A check completed and wrote report.json — this object's stitch
        completeness may have changed, so refresh the sidebar regardless of
        the active bucket. Update the bar/dots only if this bucket is still
        the active one (the user may have switched away since it started)."""
        self.objects_sidebar.refresh()
        bucket = self._active_stitch_bucket()
        if bucket is None:
            return
        obj, active_side, active_spectrum = bucket
        if (obj.dir == obj_dir and side == active_side
                and spectrum == active_spectrum):
            self._apply_stitch_report(report)

    def _on_stitch_preview_requested(self) -> None:
        """User clicked "Stitch preview" (only enabled on a green set) —
        composite the active bucket's segments in the background. Pause live
        view now (like selecting a thumbnail does), so no segment streams
        into the viewer during or after the composite."""
        bucket = self._active_stitch_bucket()
        if bucket is None:
            return
        self.session.set_live_view_paused(True)
        self.stitch_bar.show_previewing()
        self.stitch.run_preview(*bucket)

    def _on_stitch_preview_finished(
        self, obj_dir: str, side: str, spectrum: str, result,
    ) -> None:
        """The composite finished. Show the panorama in the viewer (still
        the active bucket only); restore the verdict bar afterwards. On
        failure, surface the message and keep the button live for a retry."""
        bucket = self._active_stitch_bucket()
        if bucket is None:
            return
        obj, active_side, active_spectrum = bucket
        if not (obj.dir == obj_dir and side == active_side
                and spectrum == active_spectrum):
            return
        if result.ok:
            # Live view was paused at request time. Show the composite
            # exactly like a selected capture (same choke point), then stamp
            # the stitch-specific pill label.
            self._show_still_image(
                result.preview_path, QPixmap.fromImage(result.image))
            self.session.set_view_mode(
                "preview", f"STITCH PREVIEW · {result.n_segments} segments")
            # Restore the verdict line + re-enable the button (fresh report).
            self._refresh_stitch_ui()
        else:
            self.stitch_bar.show_message(result.message, "error")
            self.stitch_bar.set_preview_enabled(True)

    def _on_filmstrip_image_decoded(self, path: str, pixmap) -> None:
        """Display the filmstrip's decoded image in the viewer."""
        self._show_still_image(path, pixmap)

    def _show_still_image(self, path: str, pixmap) -> None:
        """THE single way to show a decoded image as the current still in the
        viewer — used by filmstrip selection and the stitch preview. Tracks
        the path (rotate button target), refreshes the rotation indicator.
        The decode already honours EXIF orientation, so no rotation here;
        `show_image` (no fit) lets setPhoto auto-fit on a size change and
        preserve zoom otherwise — exactly the capture-browsing behaviour."""
        self._shown_image_path = path
        self.viewer.show_image(pixmap)
        self._refresh_rotation_indicator()

    # ---- live-view reconciliation ---------------------------------------

    def _sync_live_view(self, *_) -> None:
        """Single owner of the live-view rule: stream on the active camera
        iff not paused, the profile supports it, and the camera is in a
        startable state. Connected to every input that can change the
        outcome (active bucket, camera state, pause intent); stale or
        duplicate commands are refused worker-side, so this stays stateless."""
        for spectrum, worker, profile in (
            (SPECTRUM_VISIBLE, self.visible_worker, self.profile),
            (SPECTRUM_INFRARED, self.ir_worker, self.ir_profile),
        ):
            if worker is None:
                continue
            desired = (
                spectrum == self.session.active_spectrum
                and not self.session.live_view_paused
                and profile.supports_live_view()
            )
            state = self.session.camera_state(spectrum)
            if isinstance(state, CameraStates.LIVE_VIEW_STREAMING) and not desired:
                worker.commands.live_view.emit(False)
            elif isinstance(state, CameraStates.LIVE_VIEW_STARTABLE) and desired:
                worker.commands.live_view.emit(True)

    def _on_active_bucket_changed_live_view(self, side: str, spectrum: str) -> None:
        """A bucket with captures opens in review (paused), an empty one
        opens live. Decided here, synchronously, so _sync_live_view acts on
        it immediately; the async directory load re-asserts the same
        decision once the filmstrip catches up. Simple mode is exempt: its
        one shared folder says nothing about the entered bucket."""
        self.focus_audio.reset()  # new target = new sharpness scale
        obj = self.session.current_object
        if self.effective_mode.key != "simple" and obj is not None:
            self.session.set_live_view_paused(obj.count(side, spectrum) > 0)
        self._sync_live_view()

    def _on_workflow_step_clicked(self, step_id: str) -> None:
        """Stepper click → translate id back to (side, spectrum). The
        IR→VIS fallback lives here because SessionState doesn't know
        worker availability."""
        bucket = self.effective_mode.bucket_by_step_id.get(step_id)
        if bucket is None:
            return
        side, spectrum = bucket
        if spectrum == SPECTRUM_INFRARED and self.ir_worker is None:
            spectrum = SPECTRUM_VISIBLE
        self.session.set_active_bucket(side, spectrum)
        self._refresh_camera_dependent_ui()

    # ---------------------------------------------------------- camera state

    def _on_camera_state_changed(
        self, spectrum: str, state: CameraStates.StateType
    ) -> None:
        """Worker-signal slot. Thin: just write through to session.
        All side effects (per-spectrum lifecycle, active-only policy,
        UI rendering) live in receivers wired in _wire_session() —
        each contains its own match block on state so the per-state
        scan stays intact within each concern."""
        self.session.set_camera_state(spectrum, state)

    def _on_usb_offenders_detected(
        self, offenders: list[tuple[str, str]]
    ) -> None:
        """macOS USB-claim recovery couldn't free the camera. Tell the
        user which apps to quit. While the dialog is open, the auto-
        reconnect path in _handle_camera_lifecycle is suppressed via
        `_usb_offender_dialog_open`. Dismissing the dialog ("user has
        presumably acted") is the trigger to resume — we re-emit
        find_camera on all workers since ptpcamerad claims block ALL
        attached cameras, not just the one whose worker happened to
        raise first."""
        if getattr(self, "_usb_offender_dialog_open", False):
            return
        labels = sorted({label for _, label in offenders})
        message = (
            "The camera is being held by another application:\n\n  · "
            + "\n  · ".join(labels)
            + "\n\nQuit the listed application(s) and click OK to retry."
        )
        self.logger.warning("USB-claim recovery failed; offenders: %s",
                            ", ".join(labels))
        self._usb_offender_dialog_open = True
        try:
            QMessageBox.warning(self, "Camera is busy", message)
        finally:
            self._usb_offender_dialog_open = False
        # Resume: re-emit find_camera on every worker. Auto-reconnect
        # was suppressed while the dialog was on screen, so workers are
        # sitting in Disconnected — this is the kick they need.
        for w in (self.visible_worker, self.ir_worker):
            if w is not None:
                w.commands.find_camera.emit()

    # ---- camera-state receivers ------------------------------------------

    def _handle_camera_lifecycle(
        self, spectrum: str, state: CameraStates.StateType
    ) -> None:
        """Per-spectrum side effects — fire regardless of active spectrum.
        Each camera comes up and recovers independently. Match block on
        state so 'what does Found cause?' is a single read."""
        worker = (
            self.visible_worker if spectrum == SPECTRUM_VISIBLE
            else self.ir_worker
        )
        profile = (
            self.profile if spectrum == SPECTRUM_VISIBLE else self.ir_profile
        )
        match state:
            case CameraStates.Found():
                if profile is not None:
                    worker.commands.connect_camera.emit(profile)
            case CameraStates.Disconnected(auto_reconnect=True):
                # Suppress auto-reconnect while the "camera is busy"
                # dialog is on screen. The user needs to act (close the
                # offending app) before another retry makes sense; the
                # dialog's OK button is the trigger to resume — see
                # _on_usb_offenders_detected.
                if getattr(self, "_usb_offender_dialog_open", False):
                    return
                worker.commands.find_camera.emit()
            case CameraStates.Disconnecting():
                # Per-camera advanced-config dialog auto-rejects — only
                # if the dialog is for THIS spectrum. The other camera's
                # dialog (if any) stays open.
                if self.session.cam_config_dialog_spectrum == spectrum:
                    self.session.cam_config_dialog.reject()

    def _handle_active_camera_state(
        self, spectrum: str, state: CameraStates.StateType
    ) -> None:
        """Active-only side effects — fire only when the emitting spectrum
        is the active one, so the inactive camera's errors don't clear the
        viewer the user is looking at."""
        if spectrum != self.session.active_spectrum:
            return
        match state:
            case CameraStates.CaptureFinished():
                # A capture just landed in the active bucket → enter review of
                # the shot. Declaring the pause intent here, as the capture
                # settles, is what keeps _sync_live_view from bouncing live
                # view back on for the ~1.5 s until the async filmstrip reload
                # (_on_directory_loaded) sets the same intent — on a real body
                # that restart is a physical mirror actuation for nothing.
                # Worker emits CaptureFinished before Ready, so this lands
                # before the reconciler ever sees the post-capture Ready.
                # CaptureError is deliberately NOT paused: a failed shot
                # resumes live view on its Ready so the user can retry.
                self.session.set_live_view_paused(True)
            case CameraStates.ConnectionError(error=err):
                self.logger.error("Connection error: %s", err)
                # No live frames possible — clear stale "live" pill.
                self.session.set_view_mode("empty")
            case CameraStates.CaptureError(error=err):
                self.logger.error("Capture error: %s", err)
            case CameraStates.Disconnected():
                # Active camera is gone — clear the live indicator so the
                # viewer doesn't show stale "live" pill / border with no
                # frames possible. On reconnect, _sync_live_view resumes
                # live view at the next Ready, and the first preview frame
                # sets view_mode back to "live" in _on_preview_image.
                self.session.set_view_mode("empty")

    def _on_preview_image(self, spectrum: str, image):
        # Drop frames from the inactive spectrum — keeps the photo viewer
        # from flickering between two feeds when both workers are streaming.
        if spectrum != self.session.active_spectrum:
            return
        # Drop frames during the pause window too. live_view(False) is
        # async — frames already in flight (or about to be emitted by the
        # worker before it processes the stop) would otherwise overwrite
        # whatever's displayed (preview thumbnail, last-paused frame).
        if self.session.live_view_paused:
            return
        # One sharpness compute feeds both the readout and the focus tone.
        if self._live_sharpness_enabled or self.focus_audio.is_active():
            sharp = compute_sharpness(image.image)
            if self._live_sharpness_enabled:
                self.focus_sharpness_label.setText(
                    f"◎ {sharp:.0f}" if sharp is not None else "◎ –")
            self.focus_audio.push(sharp)
        # Fit-to-viewport only on the first frame of a live-view session
        # — the transition from any non-"live" view_mode (paused / preview
        # / empty) into live is the natural trigger. After that, subsequent
        # frames preserve whatever transform the user set via scroll-wheel
        # zoom; otherwise every ~50ms a fresh fitInView would clobber it.
        fit = self.session.view_mode != "live"
        # Client-side live-view rotation (display only — captures are
        # untouched). The angle is per-camera and persisted; see _lv_rotation.
        # Rotate the PIL frame, not the QPixmap: the latter shears the image.
        pil_image = image.image
        angle = self._lv_rotation[spectrum]
        if angle:
            pil_image = pil_image.transpose(_ROTATE_TRANSPOSE[angle])
        # .copy() detaches from the PIL-owned bytes buffer. ImageQt wraps it
        # without owning it, and for RGB32 frames QPixmap.fromImage takes a
        # shallow share instead of converting — once the ImageQt temporary is
        # collected, the pixmap would point into freed memory and any later
        # repaint segfaults (observed when the last live frame lingers after
        # a disconnect stops live view).
        frame = ImageQt(pil_image).copy()
        self.viewer.show_image(QPixmap.fromImage(frame), fit=fit)
        # Each arriving live frame asserts "live" — handles transitions
        # away from preview/paused without needing extra plumbing.
        self.session.set_view_mode("live")

    def _on_focus_assist_toggled(self, _checked: bool) -> None:
        self._update_focus_audio()

    def _update_focus_audio(self) -> None:
        """Play the focus tone only when enabled, the button is on, and live
        view is streaming. Idempotent."""
        live = isinstance(
            self.session.active_camera_state,
            (CameraStates.LiveViewStarted, CameraStates.LiveViewActive),
        )
        self.focus_audio.set_active(
            self._focus_audio_enabled
            and self.focus_assist_button.isChecked()
            and live
        )

    # ---- camera-state-driven UI receiver ---------------------------------

    def _refresh_camera_dependent_ui(self):
        """Autofocus button enable, capture status label, plus the
        capture/pause-button enables that gate on camera readiness AND
        object-loaded state. Reads the active spectrum's camera state +
        current object. Wired to camera_state_changed AND current_object_changed.

        The match block is the single per-state scan target — use it
        to answer "what UI changes when camera reaches state X?"."""
        camera_state = self.session.active_camera_state
        has_object = self.session.current_object is not None
        object_loaded = has_object and self.session.current_object.dir_loaded

        # ---- live view + autofocus + capture (bottom row)
        camera_ready = self._active_camera_ready()
        # Gate on live-view support too: a no-live-view camera (the vusb
        # virtual one) has no stream to pause, and toggling it would leave
        # the button (pause intent) and badge (frame arrival) permanently
        # out of sync — no frame ever comes to flip view_mode back to live.
        self.pause_live_view_button.setEnabled(
            camera_ready and self._live_view_supported())
        self.capture_button.setEnabled(camera_ready and object_loaded)
        # (Calibration shoots via this same Capture button — the
        # CalibrationTarget is the current object while the sub-mode is
        # active — so no separate gating is needed here.)

        # Magnify is a live-view focusing aid: show it only while the live
        # view is actually streaming AND the camera supports it. (Rotate is
        # independent of live view — handled in _refresh_rotate_button, driven
        # by view_mode.) One place, so we don't scatter setVisible across the
        # match arms below.
        live = isinstance(camera_state, (CameraStates.LiveViewStarted,
                                         CameraStates.LiveViewActive))
        # Live angle is per active camera — refresh the readout on spectrum /
        # state changes (this method is wired to both).
        self._refresh_rotation_indicator()
        show_magnify = live and self._focus_magnify_supported()
        self.magnify_button.setVisible(show_magnify)
        if not show_magnify and self.magnify_button.isChecked():
            # Leaving live view resets the toggle (the camera drops the zoom
            # itself) — block signals so we don't emit a stray config write.
            self.magnify_button.blockSignals(True)
            self.magnify_button.setChecked(False)
            self.magnify_button.blockSignals(False)

        # Focus aids: show each only while streaming AND its feature is on.
        self.focus_sharpness_label.setVisible(
            self._live_sharpness_enabled and live)
        self.focus_assist_button.setVisible(self._focus_audio_enabled and live)
        self._update_focus_audio()
        # Zoom controls (fit / 1:1 / −/slider/+) apply to a still capture, not
        # the live feed — hide them while streaming.
        self.zoom_control_bar.setVisible(not live)

        # ISO/aperture/shutter combos: gated on readiness + not-capturing,
        # intersected with each widget's intrinsic settable-ness.
        self._refresh_capture_combo_enabled()

        # ---- camera-state-driven UI
        # Camera-state widgets (icon/label/spinner/connect/disconnect buttons)
        # update themselves via worker.state_changed — no main.py plumbing
        # needed here. The handlers below only touch widgets *outside* the
        # camera-state widget: autofocus button enable + capture status label.
        match camera_state:
            case CameraStates.Waiting():
                self.autofocus_button.setEnabled(False)
                self.capture_status_label.setText("")

            case CameraStates.Connecting() | CameraStates.Disconnecting() \
                    | CameraStates.Disconnected() | CameraStates.ConnectionError():
                self.autofocus_button.setEnabled(False)

            case CameraStates.Ready():
                self.autofocus_button.setEnabled(False)

            case CameraStates.LiveViewStarted() | CameraStates.LiveViewActive():
                self.autofocus_button.setEnabled(self._autofocus_supported())

            case CameraStates.FocusStarted():
                self.autofocus_button.setEnabled(False)

            case CameraStates.FocusFinished(success=success):
                self.autofocus_button.setEnabled(self._autofocus_supported())
                if not success:
                    self.capture_status_label.setText("Could not focus.")
                    set_state(self.capture_status_label, "state", "error")

            case CameraStates.LiveViewStopped():
                self.autofocus_button.setEnabled(False)
                # Don't clear the viewer — selected capture or last live frame
                # should keep showing. The next live frame will overwrite, or
                # the user-selected capture stays.

            case CameraStates.CaptureInProgress():
                self.autofocus_button.setEnabled(False)
                self.capture_status_label.setText("Capturing…")
                set_state(self.capture_status_label, "state", None)

            case CameraStates.CaptureFinished(file_paths=paths):
                # The camera's fixed mount rotation is baked into each file's
                # EXIF Orientation in the worker, before the file is made
                # visible (see CaptureImagesRequest.orientation) — so there is
                # nothing to stamp here, and the filmstrip/viewer decode never
                # races an un-rotated file.
                if paths:
                    names = ", ".join(os.path.basename(p) for p in paths)
                    self.capture_status_label.setText(f"Captured: {names}")
                else:
                    self.capture_status_label.setText("Captured.")
                set_state(self.capture_status_label, "state", "done")
                # A settled capture may have been a calibration shot — keep
                # the per-camera due chip current (no-op for object captures).
                self._note_calibration_capture_settled()

            case CameraStates.CaptureCanceled():
                self.capture_status_label.setText("Capture canceled.")
                set_state(self.capture_status_label, "state", "error")

            case CameraStates.CaptureError(error=err):
                self.capture_status_label.setText(f"Error: {err}")
                set_state(self.capture_status_label, "state", "error")
                self._note_calibration_capture_settled(rescan=False)

    # ------------------------------------------------------------- handlers

    def _on_directory_loaded(self, _path: str):
        """A bucket's strip finished loading → set its view state.

        The has-captures decision uses the object's AUTHORITATIVE count, NOT
        the filmstrip's current selection: mid-load the selection can be
        transiently empty, which would wrongly flip an occupied bucket into
        live view (the "jumps back" bug when switching between two filled
        buckets). Bucket with captures → review the auto-selected take
        (preview, paused); empty bucket → frame it live to compose the next
        shot. Setting the pause intent is enough — _sync_live_view starts or
        stops the camera to match. Usually this re-asserts what
        _on_active_bucket_changed_live_view already decided at switch time;
        it corrects the cases only the load can see (disk changed, object
        freshly opened).
        """
        obj = self.session.current_object
        if obj is None:
            return
        obj.mark_dir_loaded()
        has_captures = obj.count(
            self.session.active_side, self.session.active_spectrum) > 0
        if has_captures:
            # current_file_name is only the pill label here — the decision
            # above doesn't depend on it, so a transient None can't misroute
            # us into live view. The take itself is shown by the filmstrip's
            # auto-selection.
            self.session.set_live_view_paused(True)
            name = self.filmstrip.current_file_name()
            self.session.set_view_mode(
                "preview", os.path.splitext(name)[0] if name else "")
        else:
            # Empty bucket — nothing to review, so frame it live. Clearing
            # the pause intent lets _sync_live_view start the feed once the
            # camera is ready. Until the first live frame asserts "live",
            # the pill shows "empty".
            self.session.set_live_view_paused(False)
            self.session.set_view_mode("empty")
        self._refresh_camera_dependent_ui()

    def _on_image_selected(self, _path: str):
        """Action handler for PhotoBrowser.image_selected — only fires on
        USER click (auto-selection during load doesn't emit it).
        Pauses live view so the chosen image persists; flips view_mode to
        preview with the file stem."""
        self.session.set_live_view_paused(True)
        stem = os.path.splitext(os.path.basename(_path))[0]
        self.session.set_view_mode("preview", stem)

    def _on_live_view_toggled(self, on: bool):
        """Action handler for the Live View toggle. `on` = live view should
        stream; off = freeze the preview. Inverse of the pause intent — the
        live_view command emit and button-state update are receiver concerns
        (see _wire_session).

        When turned off, the viewer would otherwise keep showing the (now
        stale) last live frame. Two branches replace it:
          - filmstrip has a current selection ⇒ re-display that take,
            flip view_mode to "preview" with its stem (same end state
            as the user having clicked the thumb directly).
          - empty filmstrip ⇒ blank the viewer, flip to "paused".
        Already in "preview" (user clicked a thumb earlier) means the
        selected take is already showing — leave it."""
        paused = not on
        self.session.set_live_view_paused(paused)
        if not paused or self.session.view_mode == "preview":
            return
        file_name = self.filmstrip.show_current()
        if file_name is not None:
            stem = os.path.splitext(file_name)[0]
            self.session.set_view_mode("preview", stem)
        else:
            self.viewer.show_image(None)
            self.session.set_view_mode("paused")

    def _trigger_autofocus(self):
        self.active_worker.commands.trigger_autofocus.emit()

    def _on_magnify_toggled(self, on: bool):
        """Toggle the active camera's live-view focus magnifier. The profile
        maps on/off to the camera-specific PTP property + value; we reuse the
        existing set_single_config command (no worker changes). Only reachable
        while live view is active — the button is hidden otherwise."""
        profile = self._active_profile()
        prop = profile and profile.focus_magnify_property_name()
        if prop:
            self.active_worker.commands.set_single_config.emit(
                prop, profile.focus_magnify_value(on))

    def _rotate_view(self):
        """Rotate the current view +90° clockwise.

        - Live view: rotates the live preview for the active camera (the fixed
          mount), persisted, and used as the orientation written into that
          camera's new captures. Display-only; no file involved.
        - A capture shown: rotates *that file* — read its current EXIF
          orientation, +90, write it back (NEF/ARW/JPEG), and reload the item
          so its thumbnail and the viewer reflect the file. Each capture
          carries its own orientation, independent of the live-view angle."""
        if self.session.view_mode == "live":
            spectrum = self.session.active_spectrum
            angle = (self._lv_rotation[spectrum] + 90) % 360
            self._lv_rotation[spectrum] = angle
            self.q_settings.setValue(f"liveViewRotation/{spectrum}", angle)
            self._refresh_rotation_indicator()
            return  # next live frame picks it up
        if self._shown_image_path:
            angle = (read_orientation(self._shown_image_path) + 90) % 360
            write_orientation(self._shown_image_path, angle)
            self.filmstrip.reload_current()
            self._refresh_rotation_indicator()

    def _refresh_rotate_button(self, *_) -> None:
        """Enable the rotate button whenever the viewer has content, and
        refresh the rotation readout next to it."""
        self.rotate_live_view_button.setEnabled(self.session.view_mode != "empty")
        self._refresh_rotation_indicator()

    def _refresh_rotation_indicator(self, *_) -> None:
        """Show the rotation of whatever the viewer displays — the live view's
        per-camera angle, or a shown capture's own EXIF orientation. Mirrors
        the branching in _rotate_view so the readout matches what a click does."""
        if self.session.view_mode == "live":
            angle = self._lv_rotation[self.session.active_spectrum]
        elif self._shown_image_path:
            angle = read_orientation(self._shown_image_path)
        else:
            self.rotation_label.setText("")
            return
        self.rotation_label.setText(f"{angle}°")

    def _clear_shown_image(self, *_) -> None:
        """Forget the currently-shown capture (viewer cleared / dir closed),
        so the rotate button can't write into a stale path."""
        self._shown_image_path = None

    # --------------------------------------------------- object lifecycle

    def rename_current_object(self):
        if not self.session.current_object:
            return
        new_name, ok = QInputDialog.getText(
            self, "Rename object", "New name:", text=self.session.current_object.name,
        )
        if not ok:
            return
        new_name = sanitize_name(new_name)
        if not new_name or new_name == self.session.current_object.name:
            return

        new_dir = os.path.join(self.session.current_object.working_dir, new_name)
        if Path(new_dir).exists():
            QMessageBox.critical(self, "Error", f"Object {new_name!r} already exists.")
            return

        old_name = self.session.current_object.name
        old_dir = self.session.current_object.dir

        # Commit any pending metadata edits to the OLD `_meta.json` so they
        # travel with the directory when we rename it. (Without this the
        # debounced save fires after the rename and either crashes on the
        # missing path or writes to a stale location.)
        self.metadata_pane.flush_pending_save()

        # Stop watching before moving the directory; reopen at the new path.
        self.filmstrip.bind_object(None)

        # Rename capture files in every (side, spectrum) bucket whose basename
        # starts with the old object name. `_meta.json` travels with the dir.
        for side, spectrum in BUCKETS:
            bucket_dir = Path(dir_for_bucket(old_dir, side, spectrum))
            if not bucket_dir.is_dir():
                continue
            for entry in os.listdir(bucket_dir):
                entry_path = bucket_dir / entry
                if not entry_path.is_file():
                    continue
                basename, ext = os.path.splitext(entry)
                if basename.startswith(old_name):
                    renamed = basename.replace(old_name, new_name, 1) + ext
                    os.rename(entry_path, bucket_dir / renamed)

        # Take markers in `_meta.json` store stems that include the old
        # object name (`<oldname>_a_vis_001`) — reprefix them so they still
        # point at the renamed captures (covers both chosen and reference).
        meta = read_meta(meta_path_for(old_dir))
        for bucket in meta.get(MetaKey.MARKERS, {}).values():
            for role, stem in bucket.items():
                if isinstance(stem, str) and stem.startswith(old_name):
                    bucket[role] = stem.replace(old_name, new_name, 1)
        write_meta(meta_path_for(old_dir), meta)

        os.rename(old_dir, new_dir)
        self.session.set_current_object(Object(self.session.current_object.working_dir, new_name))

    def start_object(self, name: str):
        # Simple mode reuses the title-bar name field as a filename-override
        # for the always-open folder target — typing a name doesn't create
        # a new object, it just renames subsequent captures.
        if self.mode.key == "simple":
            target = self.session.current_object
            if isinstance(target, SimpleTarget):
                target.set_name_override(name)
            return

        wd = self.q_settings.value("workingDirectory", "")
        name = sanitize_name(name)
        if not wd or not name:
            return

        obj = Object(wd, name)
        # Refuse to silently take over an existing folder. Forces the user to
        # either pick a unique name or open the existing object via the
        # sidebar (no surprise filesystem mutation, no accidental merging
        # of captures into the wrong record).
        if os.path.exists(obj.dir):
            QMessageBox.warning(
                self,
                "Object already exists",
                f"An object named {name!r} already exists in this box "
                f"folder.\n\nPick it from the sidebar to open it, or "
                f"choose a different name.",
            )
            return

        obj.ensure_dir()
        # Reset active bucket on object open so the user always starts a
        # new object at the canonical first step (Side A · Visible) rather
        # than inheriting whatever bucket the previous object left behind.
        self.session.set_active_bucket(SIDE_A, SPECTRUM_VISIBLE)
        self.session.set_current_object(obj)

    def close_object(self):
        self.stitch.invalidate()
        self.session.set_current_object(None)

    def _open_simple_target(self, output_dir: str) -> None:
        """Bind the flat-folder target for simple mode. The output folder
        is the (always-open) capture target; called at startup and when
        the user picks a new folder. A blank folder leaves no target
        (capture stays disabled until one is chosen)."""
        if not output_dir:
            self.session.set_current_object(None)
            return
        target = SimpleTarget(output_dir)
        target.ensure_dir()
        # Carry over whatever filename override is currently typed so a
        # folder change doesn't silently reset naming back to camera-default.
        target.set_name_override(self.title_bar.current_name())
        self.session.set_active_bucket(SIDE_A, SPECTRUM_VISIBLE)
        self.session.set_current_object(target)

    def _choose_simple_output_folder(self) -> None:
        """Simple mode: pick the output folder via a native dialog, persist
        it, and re-bind the flat target. The folder is the capture target."""
        start = self.q_settings.value("simpleOutputDirectory", "") or \
            os.path.expanduser("~")
        path = QFileDialog.getExistingDirectory(
            self, "Choose output folder", start)
        if not path:
            return
        self.q_settings.setValue("simpleOutputDirectory", path)
        self.title_bar.set_output_folder(path)
        self._open_simple_target(path)

    # ---- B5 receivers (current_object_changed) -------------------------

    def _refresh_metadata_pane_binding(self) -> None:
        """Re-bind metadata pane to current object. Reads B5.

        Simple/calibration modes have no metadata (the pane is hidden and
        their flat targets have no `_meta.json`), so never bind the target
        to it. Only the papyri object workspace binds a real Object."""
        if self.effective_mode.key != "papyri":
            self.metadata_pane.bind_object(None)
            return
        self.metadata_pane.bind_object(self.session.current_object)

    def _refresh_title_bar_binding(self) -> None:
        """Re-bind title bar to current object. Reads B5.

        Simple mode never binds the target: the title-bar name field stays
        a free-text filename-override (editable, no rename/close), wired to
        start_object → set_name_override. Calibration hides the title bar
        entirely (the calibration bar carries its controls), so it must not
        bind the flat target either."""
        if self.effective_mode.key != "papyri":
            self.title_bar.bind_object(None)
            return
        self.title_bar.bind_object(self.session.current_object)

    def _refresh_objects_sidebar_active(self) -> None:
        """Active row highlight in the sidebar. Reads B5."""
        obj = self.session.current_object
        self.objects_sidebar.set_active_object_name(obj.name if obj else None)

    def _refresh_objects_sidebar_entries(self) -> None:
        """Re-scan disk for the sidebar entries. Wrapped as a receiver so
        the connect() line shows up in the wiring grep; the underlying
        widget call is also invoked imperatively from non-session paths
        (object's state_changed handler, metadata_changed signal)."""
        self.objects_sidebar.refresh()

    # ---- B5 imperative handlers ----------------------------------------

    def _handle_current_object_subscription(self) -> None:
        """Manage the Object.state_changed connection — disconnect from
        the previous object, connect to the new one, kick off an initial
        refresh so captures populate. Tracks the previously-subscribed
        instance via self._subscribed_object since session only exposes
        the new value."""
        prev = self._subscribed_object
        new = self.session.current_object
        if prev is new:
            return  # defensive — set_current_object's identity guard
                    # already prevents this, but keep idempotent
        if prev is not None:
            try:
                prev.state_changed.disconnect(self._on_object_state_changed)
            except TypeError:
                pass
        self._subscribed_object = new
        if new is not None:
            new.state_changed.connect(self._on_object_state_changed)
            new.refresh()

    def _handle_current_object_view_mode_reset(self) -> None:
        """When the object closes (B5 → None), reset view_mode to "empty"
        so the stale preview / live indicator doesn't persist with no
        listing. The no-object CTA overlay is handled separately via
        `_refresh_no_object_lockout`."""
        if self.session.current_object is None:
            self.session.set_view_mode("empty")

    def _refresh_no_object_lockout(self) -> None:
        """No object → bucket selector disabled (Qt blocks input +
        BucketSelector.paintEvent dims the cards) + viewer shows the
        host-installed overlay page. Object loaded → re-enable + show
        photo. The overlay was installed once at startup via
        `viewer.set_overlay_widget(NoObjectOverlay(...))`."""
        has_object = self.session.current_object is not None
        self.bucket_selector.setEnabled(has_object)
        if has_object:
            self.viewer.show_photo()
        elif self.effective_mode.key != "papyri":
            # No papyri "new object" CTA in simple/calibration modes — just
            # blank the viewer (calibration always has a target open, so it
            # rarely reaches here anyway).
            self.viewer.show_image(None)
        else:
            self.viewer.show_image(None)
            self.viewer.show_overlay()

    # ---- live-view-pause receivers ----------------------------------------

    def _refresh_live_view_button(self) -> None:
        """Live View toggle's checked state mirrors the pause intent
        (checked = live view on = not paused). Label + eye icon are static (set in the .ui /
        _bind_widgets); the button is not the source of truth.

        setChecked may fire `toggled` if the value differs; the handler then
        calls set_live_view_paused which is a no-op (already the current
        value), breaking the cycle."""
        self.pause_live_view_button.setChecked(not self.session.live_view_paused)

    # ---- view_mode receivers --------------------------------------------

    def _refresh_view_mode_indicator(self) -> None:
        """Pill text/border + viewer border tint. ViewerWidget owns the
        rendering; this receiver just hands the (mode, label) over."""
        self.viewer.set_view_state(
            self.session.view_mode, self.session.view_mode_label
        )

    # ---- objects sidebar handlers ----

    def _on_sidebar_object_selected(self, name: str) -> None:
        """Sidebar row clicked: switch focus to that object."""
        if self.session.current_object is not None and self.session.current_object.name == name:
            return
        wd = self.q_settings.value("workingDirectory", "")
        if not wd:
            return
        obj = Object(wd, name)
        # Sidebar only lists managed objects, so meta exists; defensive check
        # to avoid crashing if it's been deleted between scan and click.
        if not is_managed_object_dir(obj.dir):
            self.objects_sidebar.refresh()
            return
        # Reset active bucket — see start_object for rationale.
        self.session.set_active_bucket(SIDE_A, SPECTRUM_VISIBLE)
        self.session.set_current_object(obj)

    def _on_sidebar_delete_object(self, name: str) -> None:
        """Sidebar 'Move to Trash' on a row: confirm, then send the object's
        whole folder (all sides/spectra + metadata) to the Trash. If it's the
        object currently open, close it first so no model points at a vanished
        directory."""
        wd = self.q_settings.value("workingDirectory", "")
        if not wd or not name:
            return
        obj_dir = os.path.join(wd, name)
        if not os.path.isdir(obj_dir):
            self.objects_sidebar.refresh()
            return
        if QMessageBox.question(
            self,
            "Move object to Trash",
            f"Move object {name!r} and all its captures to the Trash?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        current = self.session.current_object
        if isinstance(current, Object) and current.name == name:
            self.close_object()
        try:
            send2trash(obj_dir)
        except Exception:
            self.logger.exception("Move to Trash failed for %s", obj_dir)
            QMessageBox.critical(
                self, "Error", f"Could not move {name!r} to the Trash.")
        self.objects_sidebar.refresh()

    def _on_sidebar_new_object(self) -> None:
        """'+ New object' (sidebar or title bar): prompt for the inventory
        number in a dialog — mirrors rename_current_object — then create.
        start_object handles sanitising / duplicate / empty and switches to
        the new object (flushing the previous object's metadata on rebind).
        With no box open there's nowhere to put an object, so funnel the user
        to pick/create a box first."""
        if not self.q_settings.value("workingDirectory", ""):
            self._on_new_box()
            return
        name, ok = QInputDialog.getText(self, "New object", "Inv-No.:")
        if ok and name.strip():
            self.start_object(name)

    # ---- box (= working directory) switching ----

    def _recent_boxes(self) -> list[str]:
        """Recently-opened box directories (most-recent first), pruned to those
        that still exist. QSettings may hand back a single str or a list."""
        raw = self.q_settings.value("recentBoxes", []) or []
        if isinstance(raw, str):
            raw = [raw]
        return [p for p in raw if p and os.path.isdir(p)]

    def _push_recent_box(self, path: str) -> None:
        norm = os.path.normpath(path)
        recents = [p for p in self._recent_boxes()
                   if os.path.normpath(p) != norm]
        recents.insert(0, path)
        self.q_settings.setValue("recentBoxes", recents[:8])

    def _activate_box(self, path: str) -> None:
        """The single choke point for "this box is now the active box":
        migrate its objects to the current on-disk layout, then point the
        sidebar at it. Every path that opens / restores / changes the box
        funnels here, so migration can never be skipped. An empty path
        clears the sidebar (no box)."""
        if path and os.path.isdir(path):
            migrate_working_dir(path)
        self.objects_sidebar.set_working_directory(path)

    def _open_box(self, path: str) -> None:
        """Make `path` the open box: persist it (auto-reopens next launch),
        record it in recents, and repoint the sidebar + calibration. Closes any
        open object first so we never show one box's object against another."""
        if not path or not os.path.isdir(path):
            return
        self.close_object()
        self.q_settings.setValue("workingDirectory", path)
        self._push_recent_box(path)
        self.objects_sidebar.set_recent_boxes(self._recent_boxes())
        self._activate_box(path)
        # Decision C: calibration lives under the box (same external volume as
        # the captures), so it follows the box.
        if self.calibration is not None:
            self.calibration.set_working_dir(path)
            self._refresh_calibration_bar()

    def _box_dialog_start(self) -> str:
        """Where the box folder dialog opens: the parent of the current box
        (so sibling boxes are in view), else home."""
        wd = self.q_settings.value("workingDirectory", "")
        if wd and os.path.isdir(wd):
            return os.path.dirname(os.path.normpath(wd))
        return os.path.expanduser("~")

    def _on_open_box(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Open box folder", self._box_dialog_start())
        if path:
            self._open_box(path)

    def _on_new_box(self) -> None:
        # Pure OS dialog (the user creates the box folder via the dialog's
        # New-Folder affordance); the title makes the intent explicit.
        path = QFileDialog.getExistingDirectory(
            self, "Create or choose a box folder", self._box_dialog_start())
        if path:
            self._open_box(path)

    def _handle_missing_box(self) -> None:
        """The open box folder is gone (moved / renamed / deleted). Drop to the
        no-box empty state rather than recreating a ghost folder on capture."""
        QMessageBox.warning(
            self, "Box folder missing",
            "The box folder no longer exists (it was moved, renamed, or "
            "deleted).\n\nOpen or create a box to continue.",
        )
        self.close_object()
        self.q_settings.setValue("workingDirectory", "")
        self._activate_box("")

    def _on_object_state_changed(self):
        """Single sink for any change in the current object's derived state.
        Components that mirror that state (side cards, objects sidebar badge,
        metadata pane subtitle) re-read from `self.session.current_object` here."""
        self._refresh_bucket_chosen_thumbs()
        # The active object's `· → ?? → ✓` badge in the sidebar can flip
        # when captures land. Cheap re-scan; no FS watcher needed.
        self.objects_sidebar.refresh()
        # The stitching flag is part of the object's signature, so a flag
        # flip (toggle click or external meta edit) lands here too.
        self._refresh_stitch_toggle()
        # A new capture / reference change / toggle flip invalidates any
        # prior report — re-check (debounced to coalesce the JPG+RAW pair).
        self._refresh_stitch_ui(schedule_recheck=True)

    # ---------------------------------------------------------- capture

    def capture_image(self):
        obj = self.session.current_object
        if not obj:
            return
        # Box folder vanished (moved / renamed / deleted in Finder while open)?
        # Don't let ensure_dir() silently recreate the old path — that splits
        # captures across a ghost box. Bail to the "open a box" empty state.
        if isinstance(obj, Object) and not os.path.isdir(obj.working_dir):
            self._handle_missing_box()
            return
        # Re-ensure the on-disk skeleton — covers the case where someone
        # deleted a side or spectrum dir in Finder between captures.
        obj.ensure_dir()
        # Object captures record the height they were shot at (papyri only;
        # calibration/simple targets aren't papyri Objects).
        if isinstance(obj, Object):
            self._stamp_capture_metadata(obj)
        req = CaptureImagesRequest(
            file_path_template=obj.next_template(
                self.session.active_side, self.session.active_spectrum
            ),
            num_images=1,
            image_quality=CaptureImagesRequest.CaptureFormat.RAW,
            manual_trigger=False,
            # Bake this camera's fixed mount rotation into the captured file's
            # EXIF Orientation in the worker, before the file is made visible —
            # so the filmstrip/viewer decode never races an un-rotated file.
            orientation=self._lv_rotation[self.session.active_spectrum],
        )
        self.active_worker.commands.capture_images.emit(req)

    # ---------------------------------------------------------- calibration

    def _wire_calibration(self) -> None:
        """Build the calibration controller and wire the bar, once at startup
        regardless of mode. The bar's visibility is driven by
        _apply_mode_chrome (hidden in simple mode), and _refresh_calibration_bar
        no-ops while hidden — so a live simple↔papyri switch needs no
        create/teardown here. The controller's working dir tracks the open box
        (set on box change / mode switch); it's harmless with an empty dir."""
        self.calibration = CalibrationController(
            self.q_settings.value("workingDirectory", ""), self.q_settings, self
        )
        self.calibration.status_changed.connect(self._refresh_calibration_bar)
        self.calibration_bar.enter_requested.connect(self._enter_calibration)
        self.calibration_bar.exit_requested.connect(self._exit_calibration)
        self.calibration.refresh()
        self._refresh_calibration_bar()

    def _refresh_calibration_bar(self) -> None:
        """Repaint the bar's IDLE status (per-camera due) from the
        controller. No-op while the calibration sub-mode is active (the bar
        then shows "← Back", set by _enter_calibration)."""
        if self.calibration is None or self._calibration_active:
            return
        spectra = [SPECTRUM_VISIBLE]
        if self.ir_worker is not None:
            spectra.append(SPECTRUM_INFRARED)
        level, text = self.calibration.summary(spectra)
        self.calibration_bar.set_idle(text, level)
        # Calibration is for the open object's height, so it's only offered
        # with an object open.
        self.calibration_bar.set_can_enter(
            isinstance(self.session.current_object, Object))

    # ---- calibration sub-mode enter / exit ----------------------------

    def _enter_calibration(self) -> None:
        """Flip the workspace into the calibration sub-mode: stash the open
        object + bucket, apply the calibration chrome, and open the
        CalibrationTarget so the normal capture/review surface now points at
        the calibration buckets."""
        if self.calibration is None or self._calibration_active:
            return
        wd = self.q_settings.value("workingDirectory", "")
        if not wd:
            return
        # Calibration is always FOR an object's height (shown big, not editable
        # in this sub-mode). No object → nothing to calibrate for.
        if not isinstance(self.session.current_object, Object):
            return
        # Pick a valid opening bucket: a target slot for the active camera,
        # falling back to VIS if the active camera has none configured.
        spectrum = self.session.active_spectrum
        slot = first_slot_for(spectrum)
        if slot is None:
            spectrum = SPECTRUM_VISIBLE
            slot = first_slot_for(spectrum)
        if slot is None:
            return                        # no calibration targets at all

        # Remember where we were so "← Back" restores it exactly.
        self._object_before_calibration = self.session.current_object
        self._bucket_before_calibration = (
            self.session.active_side, self.session.active_spectrum)

        self._calibration_active = True
        self._apply_mode_chrome(CALIBRATION_MODE)
        # Each Calibrate click is its own timestamped run, so a mid-day
        # setup change just starts fresh. ensure_dir so the filmstrip's
        # watcher catches tethered captures; an unused run is pruned on exit.
        run_id = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        # The height this run calibrates for: the object's own VIS height (IR
        # stays the fixed global). Captured once at enter — fixed for the run.
        cal_vis_height = self._object_height(
            self._object_before_calibration, SPECTRUM_VISIBLE)
        self._cal_target = CalibrationTarget(
            wd, run_id,
            height_for=lambda spectrum: cal_vis_height
                if spectrum == SPECTRUM_VISIBLE
                else current_height_for(self.q_settings, spectrum))
        self._cal_target.ensure_dir()
        # Swap target+bucket so no receiver ever sees a mismatched pair:
        # clear the object first, then set the calibration bucket, then the
        # target. (Otherwise a receiver fires with the old papyri Object +
        # a 'cal_*' slot, and Object.dir_for rightly rejects that slot.)
        self.session.set_current_object(None)
        self.session.set_active_bucket(slot, spectrum)
        self.session.set_current_object(self._cal_target)
        self._refresh_workflow_stepper_active()
        self.calibration_bar.set_active(self._back_label())
        self._refresh_calibration_banner()
        self._refresh_capture_button_label()

    def _calibration_height_text(self) -> str:
        """The prominent "for height X" caption: the height the current
        calibration run is filed under, for the active camera."""
        spectrum = self.session.active_spectrum
        if spectrum == SPECTRUM_VISIBLE:
            height = self._object_height(
                self._object_before_calibration, SPECTRUM_VISIBLE)
        else:
            height = current_height_for(self.q_settings, SPECTRUM_INFRARED)
        return f"for height {height} cm"

    def _refresh_calibration_banner(self) -> None:
        """Keep the "for height X" caption in step with the active camera
        while calibrating (VIS uses the object's height, IR its fixed value)."""
        if self._calibration_active:
            self.calibration_bar.set_active_height(self._calibration_height_text())

    def _exit_calibration(self) -> None:
        """Leave the calibration sub-mode: restore the object + bucket that
        were open before entering, and repaint the idle status chip."""
        if not self._calibration_active:
            return
        self._calibration_active = False
        self._apply_mode_chrome(self.mode)
        side, spectrum = self._bucket_before_calibration or (SIDE_A, SPECTRUM_VISIBLE)
        # Same atomic swap as on enter, reversed (clear first so no receiver
        # sees the calibration target with a papyri side). Unbinding the
        # filmstrip first also makes it safe to prune an unused run folder.
        self.session.set_current_object(None)
        if self._cal_target is not None:
            self._cal_target.discard_if_empty()
        self.session.set_active_bucket(side, spectrum)
        self.session.set_current_object(self._object_before_calibration)
        self._refresh_workflow_stepper_active()
        self._cal_target = None
        self._object_before_calibration = None
        self._bucket_before_calibration = None
        self.calibration.refresh()
        self._refresh_calibration_bar()

    def _back_label(self) -> str:
        obj = self._object_before_calibration
        name = getattr(obj, "name", None) if obj is not None else None
        return f"← Back to {name}" if name else "← Back to objects"

    def _note_calibration_capture_settled(self, rescan: bool = True) -> None:
        """A capture settled — optionally re-scan `_calibration/` so per-camera
        due is current (matters for the idle chip once we leave calibration).
        Harmless for normal object captures (the re-scan finds nothing new).

        `rescan=False` is passed on the CaptureError path: a failed capture
        wrote no file, so there is nothing new on disk to pick up — skip the
        scan and just repaint the bar."""
        if self.calibration is None:
            return
        if rescan:
            self.calibration.refresh()
        self._refresh_calibration_bar()      # no-op while the sub-mode is active

    # ---------------------------------------------------------- mode toggle

    def _toggle_capture_mode(self) -> None:
        """Flip between papyri and simple, live (no restart)."""
        self._switch_capture_mode(
            "papyri" if self.mode.key == "simple" else "simple")

    def _switch_capture_mode(self, target: str) -> None:
        """Switch papyri↔simple at runtime, no restart. Mirrors the
        calibration sub-mode swap and reuses the same idempotent setup the
        startup path runs (_apply_mode_chrome / _apply_mode_state) — no code
        duplication. Cameras are mode-agnostic, so workers are untouched.

        State-safety: drop the current target FIRST (bind_object(None) flushes
        any pending metadata to the old object), then flip the mode + chrome +
        state, then open the new mode's last-used target. A final
        current_object_changed re-emit repaints the object-dependent UI under
        the new mode (covers the papyri "no object open" case, where opening
        the box fires no object transition)."""
        if target == self.mode.key:
            return
        if isinstance(self.session.active_camera_state,
                      CameraStates.CaptureInProgress):
            QMessageBox.warning(self, "Camera is busy",
                                "Finish the capture before switching mode.")
            return
        if self._calibration_active:
            self._exit_calibration()

        self.session.set_current_object(None)
        self.mode = get_mode(target)
        self.q_settings.setValue("captureMode", target)
        self.q_settings.sync()
        self._apply_mode_chrome(self.mode)
        self._apply_mode_state(self.mode)
        # set_groups rebuilt the bucket bars — re-assert a canonical bucket.
        self.session.set_active_bucket(SIDE_A, SPECTRUM_VISIBLE)

        if self.mode.key == "simple":
            self._open_simple_target(
                self.q_settings.value("simpleOutputDirectory", ""))
        else:
            wd = self.q_settings.value("workingDirectory", "")
            if wd:
                self._open_box(wd)          # repoints sidebar + calibration
            self.objects_sidebar.refresh()  # force re-scan (same-path is a no-op)

        # Repaint object-dependent UI under the new mode.
        self.session.current_object_changed.emit(self.session.current_object)
        self._refresh_calibration_bar()
        self.logger.info("Switched capture mode → %s (no restart)", target)

    # ---------------------------------------------------------- dialogs

    def open_settings(self):
        # Snapshot irProfile BEFORE the dialog runs so we can detect what
        # kind of change happened: profile→profile hot-switches at runtime,
        # while enabling/disabling IR (None↔profile) still needs a restart —
        # IR worker spawn/teardown happens only in _wire_camera.
        previous_ir_profile = self.q_settings.value("irProfile")
        ir_hot_switched = False

        dialog = PapyriSettingsDialog(self.q_settings, PROFILES, self)
        if not dialog.exec():
            return

        for name, value in dialog.settings.items():
            if name == "profile":
                # The "profile" setting is the VIS-camera profile — always
                # target the VIS worker, never active_worker (that would
                # wrongly switch IR if IR were the active spectrum). On a
                # mid-capture guard refusal the setting is NOT persisted, so
                # QSettings and runtime state stay consistent.
                new_profile = PROFILES[value]
                if new_profile is not self.profile:
                    refusal = self._hot_switch_profile(SPECTRUM_VISIBLE,
                                                       new_profile)
                    if refusal is not None:
                        QMessageBox.information(
                            self, "Profile not changed", refusal)
                        continue
                self.q_settings.setValue(name, value)
                continue
            if name == "irProfile":
                if (value and previous_ir_profile
                        and value != previous_ir_profile
                        and value in PROFILES
                        and self.ir_worker is not None):
                    refusal = self._hot_switch_profile(SPECTRUM_INFRARED,
                                                       PROFILES[value])
                    if refusal is not None:
                        QMessageBox.information(
                            self, "IR profile not changed", refusal)
                        continue
                    ir_hot_switched = True
                self.q_settings.setValue(name, value)
                continue
            self.q_settings.setValue(name, value)
            if name == "workingDirectory":
                self._refresh_camera_dependent_ui()
                self._activate_box(value)
                # Calibration files live under <workingDirectory>/_calibration/
                # — re-point the controller so the bar reflects the new root.
                if self.calibration is not None:
                    self.calibration.set_working_dir(value)
                    self._refresh_calibration_bar()
                # Simple mode's output folder is independent (its own
                # setting, chosen via the title-bar picker), so changing
                # papyri's workingDirectory here doesn't touch it.
            elif name == "maxPixmapCache":
                QPixmapCache.setCacheLimit(int(value) * 1024)
            elif name == "sharpnessCheckEnabled":
                from byzanz_camera.load_image_worker import set_sharpness_enabled
                set_sharpness_enabled(bool(value))
            elif name == "liveViewSharpnessEnabled":
                self._live_sharpness_enabled = bool(value)
                self._refresh_camera_dependent_ui()
            elif name == "enableAuditiveFocusAssist":
                self._focus_audio_enabled = AUDIO_AVAILABLE and bool(value)
                self._refresh_camera_dependent_ui()
            elif name in ("calibrationTrigger", "calibrationIntervalMinutes"):
                # Controller reads these from QSettings live; refresh so the
                # bar reflects the new trigger/interval immediately.
                if self.calibration is not None:
                    self.calibration.refresh()
                    self._refresh_calibration_bar()
            elif name in ("captureHeightChoices", "irCaptureHeight"):
                # VIS presets or the fixed IR height changed → rebuild the
                # combo for whichever camera is active.
                self._populate_height_select()
            elif name.startswith("rotatedSampleNudge/"):  # rotated-sample nudge
                if getattr(self, "_rotated_sample_nudge", None) is not None:
                    self._rotated_sample_nudge.refresh()

        # F-PERS-1 (narrowed): profile→profile changes hot-switch above; only
        # enabling/disabling IR (None↔profile) — or a change while no IR
        # worker exists — still requires a restart, because worker
        # spawn/teardown happens only in _wire_camera at startup.
        new_ir_profile = self.q_settings.value("irProfile")
        if new_ir_profile != previous_ir_profile and not ir_hot_switched:
            QMessageBox.information(
                self,
                "Restart required",
                "The IR camera profile change will take effect after you "
                "restart the application.",
            )

    _CAM_CONFIG_READY_STATES = (
        CameraStates.Ready,
        CameraStates.LiveViewStarted,
        CameraStates.LiveViewActive,
        CameraStates.CaptureFinished,
    )

    def _refresh_settings_menu_state(self) -> None:
        """aboutToShow handler for the Settings menu. Per-camera advanced-
        config entries are enabled only when that camera is in a state
        where reading its config is sensible. Disabled-vs-hidden gives
        the user feedback ("camera not ready") without an info dialog."""
        self.open_vis_cam_config_action.setEnabled(
            isinstance(self.session.camera_state(SPECTRUM_VISIBLE),
                       self._CAM_CONFIG_READY_STATES)
        )
        # IR action is hidden when IR isn't configured (set in _wire_camera);
        # when visible, gate on its own state.
        if self.open_ir_cam_config_action.isVisible():
            self.open_ir_cam_config_action.setEnabled(
                isinstance(self.session.camera_state(SPECTRUM_INFRARED),
                           self._CAM_CONFIG_READY_STATES)
            )

    def open_advanced_camera_config(self, spectrum: str) -> None:
        """Open the advanced-config dialog for `spectrum`'s camera.

        Per-camera (option A): each call targets a specific worker, not
        active_worker. Closes any existing dialog first — only one open at
        a time. The menu's aboutToShow gates this on camera readiness, so
        we don't repeat the check here (defense-in-depth would be fine
        but the menu is the only entry point)."""
        worker = (
            self.visible_worker if spectrum == SPECTRUM_VISIBLE else self.ir_worker
        )
        if worker is None:
            return  # IR not configured; menu entry is hidden but be safe

        def open_dialog(cfg):
            existing = self.session.cam_config_dialog
            if existing is not None:
                # Synchronously fires `finished` → clears the session slot,
                # so when we set the new dialog below we're not stomped.
                existing.reject()
            dialog = CameraConfigDialog(cfg, worker, self)
            dialog.setModal(False)
            dialog.finished.connect(
                lambda *_: self.session.set_cam_config_dialog(None, None)
            )
            self.session.set_cam_config_dialog(dialog, spectrum)
            dialog.show()

        req = ConfigRequest()
        req.signal.got_config.connect(open_dialog)
        worker.commands.get_config.emit(req)

    # ---------------------------------------------------------- lifecycle

    def closeEvent(self, event: QCloseEvent):
        # Remember the window geometry (size + position + maximized state)
        # for the next launch; restored in __init__.
        self.q_settings.setValue("windowGeometry", self.saveGeometry())
        # Release the audio sink.
        self.focus_audio.set_active(False)
        # Worker shutdown:
        #   1. requestInterruption() so polling loops in __find_camera /
        #      captureImages break out of their while-conditions.
        #   2. exit() asks the worker's event loop to terminate after the
        #      current slot returns.
        #   3. wait(2000) blocks until the worker has finished — bounded so
        #      a worker stuck inside C-level libgphoto2 (camera.init() etc.,
        #      which doesn't honor requestInterruption) can't hang the close.
        #      Fall back to terminate() so the app actually quits.
        _SHUTDOWN_GRACE_MS = 5000
        for thread in (self.visible_thread, self.ir_thread):
            if thread is None:
                continue
            thread.requestInterruption()
            thread.exit()
            if not thread.wait(_SHUTDOWN_GRACE_MS):
                self.logger.warning(
                    "Worker thread didn't quit cleanly within %dms "
                    "(likely stuck in libgphoto2 C call) — terminating.",
                    _SHUTDOWN_GRACE_MS,
                )
                thread.terminate()
                thread.wait(500)
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setOrganizationName("CCeH")
    app.setApplicationName("Crocodile Capture")
    # Multi-resolution icon for dock / taskbar / alt-tab / window
    # decorations. Set on the QApplication so every window inherits
    # it, on every platform.
    app.setWindowIcon(get_app_icon())
    # Centralised stylesheet + hot-reload on file change.
    # See papyri/styles.py + papyri/ui/app.qss.
    from papyri.styles import install_app_stylesheet
    install_app_stylesheet(app)
    win = PapyriMainWindow()
    win.show()
    # PAPYRI_AUTO_OPEN=<object_name> auto-opens that object 500ms after
    # the window appears — used for unattended UI debugging where the
    # filmstrip needs real captures to render.
    auto_open = os.environ.get("PAPYRI_AUTO_OPEN")
    if auto_open:
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(500, lambda: win._on_sidebar_object_selected(auto_open))
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
