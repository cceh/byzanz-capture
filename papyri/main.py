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

import json
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
    QObject, QSettings, QSize, QThread, QThreadPool, pyqtSignal,
)
from PyQt6.QtGui import QAction, QCloseEvent, QIcon, QPixmap, QPixmapCache
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QInputDialog, QLabel, QMainWindow,
    QMenu, QMessageBox, QPushButton, QSplitter, QToolButton,
)
from PyQt6.uic import loadUi

from byzanz_camera.camera_worker import (
    CameraStates, CameraWorker, CaptureImagesRequest, ConfigRequest,
)
from byzanz_camera.filmstrip_widget import get_file_index
from byzanz_camera.load_image_worker import ImageMode, LoadImageWorker
from byzanz_camera.orientation import read_orientation, write_orientation
from byzanz_camera.helpers import (
    get_app_icon, get_ui_path, set_state, set_themed_icon, set_themed_pixmap,
)
from byzanz_camera.viewer_widget import ViewerWidget
from byzanz_camera.zoom_control_bar import ZoomControlBar
from byzanz_camera.config_combo import ConfigComboBox
from papyri.capture_model import Capture, _CopyRunner
from papyri._layout import (
    BUCKETS,
    JPG_EXTENSIONS,
    RAW_EXTENSIONS,
    SIDE_A,
    SIDE_B,
    SPECTRUM_INFRARED,
    SPECTRUM_VISIBLE,
    chosen_path_for,
    dir_for_bucket,
    is_managed_object_dir,
    meta_path_for,
    side_dir_for,
)
from papyri.camera_state_widget import CameraStateWidget
from papyri.papyri_filmstrip import PapyriFilmstrip
from papyri.metadata_pane import MetadataPane
from papyri.no_object_overlay import NoObjectOverlay
from papyri.object_title_bar import ObjectTitleBar
from papyri.objects_sidebar import ObjectsSidebar
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
from papyri.calibration_spec import first_slot_for, label_for_slot
from papyri.calibration_target import CalibrationTarget
from papyri.capture_mode import CALIBRATION_MODE, get_mode
from papyri._metadata import parse_height_choices
from papyri.simple_target import SimpleTarget

from send2trash import send2trash

from camera_config_dialog import CameraConfigDialog
from papyri.settings_dialog import PapyriSettingsDialog

PROFILES = {
    "MoritzA7III": MoritzA7MIII(),
    "ParisDomeSonyIlce7RM5": ParisDomeSonyIlce7RM5(),
    "CCeHDomeNikonD800E": CCeHDomeNikonD800E(),
    "NikonD90": NikonD90(),
    "VirtualCameraVusb": VirtualCameraVusb(),
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


class Object(QObject):
    """A single papyri capture object: its directory, four (side, spectrum)
    capture buckets, metadata. Implements the CaptureTarget contract
    (papyri/capture_target.py).

    Layout:
      <object>/_meta.json
      <object>/<side>/_chosen_<spectrum>.txt   (optional; falls back to first)
      <object>/<side>/<spectrum>/<name>_<side_letter>_<spectrum_infix>_NNN.{jpg,arw}

    Public API is (side, spectrum)-parametric throughout: `captures(side, spectrum)`,
    `chosen(side, spectrum)`, `set_chosen(side, spectrum, stem)`,
    `delete(side, spectrum, stem)`, `next_template(side, spectrum)`,
    `count(side, spectrum)`. Sides are SIDE_A | SIDE_B; spectra are
    SPECTRUM_VISIBLE | SPECTRUM_INFRARED.

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
    _SPECTRUM_INFIX = {SPECTRUM_VISIBLE: "vis", SPECTRUM_INFRARED: "ir"}

    def __init__(self, working_dir: str, name: str, parent=None):
        super().__init__(parent)
        self.working_dir = working_dir
        self.name = name
        self.dir = os.path.join(working_dir, name)
        self.dir_loaded = False
        # Per-bucket cached state, keyed by (side, spectrum).
        self._captures: dict[tuple[str, str], list[Capture]] = {b: [] for b in BUCKETS}
        self._chosen: dict[tuple[str, str], Capture | None] = {b: None for b in BUCKETS}

    # --- paths ----------------------------------------------------------

    @property
    def meta_path(self) -> str:
        return meta_path_for(self.dir)

    def side_dir(self, side: str) -> str:
        """`<obj>/<side>/` — the parent of both spectrum dirs for that side."""
        return side_dir_for(self.dir, side)

    def dir_for(self, side: str, spectrum: str) -> str:
        """`<obj>/<side>/<spectrum>/`."""
        return dir_for_bucket(self.dir, side, spectrum)

    def chosen_path_for(self, side: str, spectrum: str) -> str:
        """`<obj>/<side>/_chosen_<spectrum>.txt`."""
        return chosen_path_for(self.dir, side, spectrum)

    # --- (side, spectrum)-parametric read-side -------------------------

    def captures(self, side: str, spectrum: str) -> list[Capture]:
        return list(self._captures[(side, spectrum)])  # defensive copy

    def chosen(self, side: str, spectrum: str) -> Capture | None:
        """The currently-chosen take for `(side, spectrum)`, or None.
        Defaults to the first capture if `_chosen_<spectrum>.txt` is absent
        or stale."""
        return self._chosen[(side, spectrum)]

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
            with open(self.meta_path, "w") as f:
                json.dump({}, f)

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
        sp_inf = self._SPECTRUM_INFIX[spectrum]
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
        sp_inf = self._SPECTRUM_INFIX[spectrum]
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
        sp_inf = self._SPECTRUM_INFIX[src_spectrum]
        new_stem = f"{self.name}_{s_inf}_{sp_inf}_{next_idx:03d}"

        # Move every file for this stem (jpg + raw if present).
        for src_path in (cap.jpg_path, cap.raw_path):
            if src_path is None:
                continue
            ext = os.path.splitext(src_path)[1]
            dest_path = os.path.join(dest_dir, new_stem + ext)
            os.replace(src_path, dest_path)

        # If this stem was the chosen-take in the source bucket, drop the
        # stale chosen file so the resolver falls back to the first capture.
        chosen_path = self.chosen_path_for(src_side, src_spectrum)
        if os.path.exists(chosen_path):
            try:
                with open(chosen_path) as f:
                    if f.read().strip() == stem:
                        os.remove(chosen_path)
            except OSError:
                pass

        self.refresh()

    def set_chosen(self, side: str, spectrum: str, stem: str) -> None:
        """Mark the capture with the given stem as chosen for `(side, spectrum)`.
        Persists to `<obj>/<side>/_chosen_<spectrum>.txt`, then refresh."""
        # The chosen file lives in the side dir, which must exist before write.
        os.makedirs(self.side_dir(side), exist_ok=True)
        with open(self.chosen_path_for(side, spectrum), "w") as f:
            f.write(stem)
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
        self.refresh()

    def refresh(self):
        """Re-read state from disk for all four buckets; emit `state_changed`
        if anything changed across any bucket."""
        new_captures = {b: self._scan_bucket(*b) for b in BUCKETS}
        new_chosen = {b: self._resolve_chosen(*b, new_captures[b]) for b in BUCKETS}

        old_signature = self._signature(self._captures, self._chosen)
        new_signature = self._signature(new_captures, new_chosen)

        self._captures = new_captures
        self._chosen = new_chosen

        if old_signature != new_signature:
            self.state_changed.emit()

    # --- internals ------------------------------------------------------

    @staticmethod
    def _signature(
        captures: dict[tuple[str, str], list[Capture]],
        chosen: dict[tuple[str, str], Capture | None],
    ) -> tuple:
        """A hashable summary across all buckets used to detect 'anything changed'."""
        return tuple(
            (
                bucket,
                tuple((c.stem, c.jpg_path, c.raw_path) for c in captures[bucket]),
                chosen[bucket].stem if chosen[bucket] else None,
            )
            for bucket in BUCKETS
        )

    def _count_stems_on_disk(self, side: str, spectrum: str) -> int:
        """Count unique capture takes (one per stem) in `(side, spectrum)`."""
        bucket_dir = self.dir_for(side, spectrum)
        if not os.path.isdir(bucket_dir):
            return 0
        stems: set[str] = set()
        for f in os.listdir(bucket_dir):
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

    def _resolve_chosen(
        self, side: str, spectrum: str, captures: list[Capture]
    ) -> Capture | None:
        """Read `_chosen_<spectrum>.txt` (a stem); fall back to the LATEST
        capture if absent/stale. Latest-as-default mirrors the user's
        likely intent: the newest take is presumably the keeper until
        they decide otherwise (via right-click → mark chosen, which
        persists to `_chosen_*.txt` and pins their choice across new
        arrivals)."""
        preferred = self._read_chosen_pref(side, spectrum)
        if preferred:
            for c in captures:
                if c.stem == preferred:
                    return c
        return captures[-1] if captures else None

    def _read_chosen_pref(self, side: str, spectrum: str) -> str | None:
        path = self.chosen_path_for(side, spectrum)
        if not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                return f.read().strip() or None
        except OSError:
            return None


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

        # Simple mode: the title bar becomes a filename-override field +
        # an output-folder picker. Wire the picker to the folder chooser.
        if self.mode.key == "simple":
            self.title_bar.set_simple_mode(
                True, self.q_settings.value("simpleOutputDirectory", ""))
            self.title_bar.output_folder_requested.connect(
                self._choose_simple_output_folder)
        self.viewer: ViewerWidget = self.findChild(ViewerWidget, "viewer")
        # The zoom bar lives in the panel's top toolbar (declared in the
        # .ui), not inside the viewer. Wire it to the viewer's
        # photo_viewer here.
        self.viewer.attach_zoom_bar(self.findChild(ZoomControlBar, "zoomControlBar"))
        # Inject the papyri-specific "no object open" CTA into the
        # generic viewer's overlay slot. Drives via show_overlay /
        # show_photo from `_refresh_no_object_lockout`.
        self._no_object_overlay = NoObjectOverlay()
        self._no_object_overlay.new_object_requested.connect(
            self._on_sidebar_new_object
        )
        self.viewer.set_overlay_widget(self._no_object_overlay)
        self.filmstrip: PapyriFilmstrip = self.findChild(PapyriFilmstrip, "filmstrip")

        self.calibration_bar: CalibrationBar = self.findChild(
            CalibrationBar, "calibrationBar")

        self.pause_live_view_button: QPushButton = self.findChild(QPushButton, "pauseLiveViewButton")
        self.autofocus_button: QPushButton = self.findChild(QPushButton, "autofocusButton")
        self.magnify_button: QPushButton = self.findChild(QPushButton, "magnifyButton")
        self.rotate_live_view_button: QPushButton = self.findChild(QPushButton, "rotateLiveViewButton")
        self.rotation_label: QLabel = self.findChild(QLabel, "rotationLabel")
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
        # come from Settings (captureHeightChoices). Hidden in simple mode.
        self.height_label: QLabel = self.findChild(QLabel, "heightLabel")
        self.height_select: QComboBox = self.findChild(QComboBox, "heightSelect")
        self._populate_height_select()
        self.height_select.currentTextChanged.connect(self._on_height_changed)
        height_visible = self.mode.key != "simple"
        self.height_label.setVisible(height_visible)
        self.height_select.setVisible(height_visible)
        self._last_config: dict[str, object] = {}

        # Override raw .ui-set icons with themed versions so they
        # follow light/dark. capture_button gets its themed icon via
        # `_refresh_capture_button_label` (state-dependent); the others
        # are set once here.
        set_themed_icon(self.settings_button.setIcon, get_ui_path("ui/general_settings.svg"))
        set_themed_icon(self.pause_live_view_button.setIcon, get_ui_path("ui/preview_closed.svg"))
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
        # Mode toggle — switches between full papyri and simple capture
        # mode by persisting the setting and relaunching (no live UI
        # rebuild, so no state to reset). Label reflects the target mode.
        self.toggle_mode_action = self._action(
            "Switch to full (papyri) mode" if self.mode.key == "simple"
            else "Switch to simple capture mode",
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

        # Apply the startup mode's chrome once, now that every widget exists.
        self._apply_mode_chrome(self.mode)

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

        # Connect/disconnect buttons live inside CameraStateWidget —
        # widget-internal wiring, no main.py plumbing needed.

        self.pause_live_view_button.toggled.connect(self._on_pause_toggled)
        self.autofocus_button.clicked.connect(self._trigger_autofocus)
        self.magnify_button.toggled.connect(self._on_magnify_toggled)
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
        self.objects_sidebar.set_working_directory(
            self.q_settings.value("workingDirectory", "")
        )
        self.objects_sidebar.object_selected.connect(self._on_sidebar_object_selected)
        self.objects_sidebar.new_object_requested.connect(self._on_sidebar_new_object)
        self.objects_sidebar.open_box_requested.connect(self._on_open_box)
        self.objects_sidebar.new_box_requested.connect(self._on_new_box)
        self.objects_sidebar.recent_box_chosen.connect(self._open_box)

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

        # B1+B2 active_bucket
        s.active_bucket_changed.connect(self._refresh_workflow_stepper_active)
        s.active_bucket_changed.connect(self._refresh_capture_button_label)
        s.active_bucket_changed.connect(self._refresh_camera_state_emphasis)
        s.active_bucket_changed.connect(self._refresh_filmstrip_binding)
        # Spectrum switch → repaint capture-setting combos from the newly
        # active camera's cached config, and re-evaluate camera-dependent
        # controls (autofocus button etc.) against the now-active camera.
        s.active_bucket_changed.connect(self._populate_capture_setting_combos)
        s.active_bucket_changed.connect(self._refresh_camera_dependent_ui)
        s.active_bucket_changed.connect(self._refresh_height_select_enabled)
        self._refresh_workflow_stepper_active()
        self._refresh_capture_button_label()
        self._refresh_camera_state_emphasis()
        self._refresh_filmstrip_binding()
        self._refresh_height_select_enabled()

        # B5 current_object
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
        self._refresh_metadata_pane_binding()
        self._refresh_title_bar_binding()
        self._refresh_objects_sidebar_active()
        self._refresh_objects_sidebar_entries()
        self._refresh_bucket_chosen_thumbs()
        # _refresh_filmstrip_binding already called above (B1+B2 init)
        self._handle_current_object_subscription()
        self._handle_current_object_view_mode_reset()
        self._refresh_no_object_lockout()

        # B6 live_view_paused
        s.live_view_paused_changed.connect(self._refresh_pause_button_text)
        s.live_view_paused_changed.connect(self._handle_live_view_paused)
        self._refresh_pause_button_text()
        # _handle_live_view_paused NOT invoked at init: it would emit a
        # live_view command to the active worker before the camera is
        # connected — pointless and noisy.

        # B7 view_mode
        s.view_mode_changed.connect(self._refresh_view_mode_indicator)
        self._refresh_view_mode_indicator()
        # Rotate button is independent of live view: usable whenever the
        # viewer shows something (live, preview or paused), disabled when empty.
        s.view_mode_changed.connect(self._refresh_rotate_button)
        self._refresh_rotate_button()

        # B3+B4 camera_state (per-spectrum). Three receivers, each with
        # its own match block — see the design note above
        # _refresh_camera_dependent_ui for the per-state-vs-per-purpose
        # split rationale.
        s.camera_state_changed.connect(self._handle_camera_lifecycle)
        s.camera_state_changed.connect(self._handle_active_camera_state)
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
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.initialize)
        return worker, thread

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

    def _live_view_supported(self) -> bool:
        """Whether the active camera can stream a live preview. Gates the
        auto-resume below so a no-live-view body (e.g. the vusb virtual
        camera) isn't asked to start a preview it can only reject. The
        worker also no-ops the request defensively."""
        profile = self._active_profile()
        return bool(profile and profile.supports_live_view())

    def _focus_magnify_supported(self) -> bool:
        """Whether the active camera can magnify the live view for focus
        checking. Gates the magnify button's visibility (together with
        live-view-active state)."""
        profile = self._active_profile()
        return bool(profile and profile.focus_magnify_property_name())

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

    # ---- capture-row height (sticky VIS rig height) --------------------

    def _populate_height_select(self) -> None:
        """(Re)fill the height combo from the configured presets and select
        the persisted current height. Called at startup and when the presets
        change in Settings."""
        choices = parse_height_choices(self.q_settings.value("captureHeightChoices", ""))
        current = str(self.q_settings.value("currentHeight", choices[0]))
        self.height_select.blockSignals(True)
        self.height_select.clear()
        self.height_select.addItems(choices)
        idx = self.height_select.findText(current)
        self.height_select.setCurrentIndex(idx if idx >= 0 else 0)
        self.height_select.blockSignals(False)
        # Persist the resolved value (a current height no longer in the preset
        # list falls back to the first).
        self.q_settings.setValue("currentHeight", self.height_select.currentText())

    def _on_height_changed(self, text: str) -> None:
        """Sticky current height changed → persist it. Object captures stamp
        it; the Flatfield calibration tags by it. While calibrating, the
        active Flatfield folder is per height, so rebind the filmstrip to the
        new height's shots and re-evaluate due-status."""
        if text:
            self.q_settings.setValue("currentHeight", text)
        if self._calibration_active and self._cal_target is not None:
            self._cal_target.refresh()
            self._refresh_filmstrip_binding()
        if self.calibration is not None:
            self.calibration.refresh()

    def _calibration_height_for(self, spectrum: str) -> str:
        """Current rig height (str) per camera for the CalibrationTarget:
        VIS = the sticky `currentHeight`, IR = the fixed `irCaptureHeight`."""
        key = "currentHeight" if spectrum == SPECTRUM_VISIBLE else "irCaptureHeight"
        return str(self.q_settings.value(key, "") or "")

    def _refresh_height_select_enabled(self) -> None:
        """Height applies to VIS only (IR is a single fixed height) — disable
        the control when the IR camera is active."""
        self.height_select.setEnabled(
            self.session.active_spectrum == SPECTRUM_VISIBLE)

    def _stamp_capture_metadata(self, obj: "Object") -> None:
        """Merge derived metadata into the object's `_meta.json` on capture:
        the rig heights it was shot at, and the box number (= the basename of
        the box/working directory the object lives in — box no. is the folder,
        not a typed field). Merge (not overwrite) so metadata-pane fields
        survive; the pane likewise merges."""
        try:
            with open(obj.meta_path) as f:
                data = json.load(f) or {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            data = {}
        data["capture_height_vis"] = str(self.q_settings.value("currentHeight", ""))
        data["capture_height_ir"] = str(self.q_settings.value("irCaptureHeight", ""))
        data["box_nr"] = os.path.basename(os.path.normpath(obj.working_dir))
        try:
            with open(obj.meta_path, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except OSError:
            pass

    # ---- B1+B2 receivers (active_bucket_changed) -----------------------

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
        """Active step highlight. Reads B1+B2 + B5 — when no object is
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
        widget gets a 2px colored border. Reads B2."""
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
        Reads B1+B2+B5."""
        self.filmstrip.bind_object(
            self.session.current_object,
            self.session.active_side,
            self.session.active_spectrum,
        )

    def _on_filmstrip_image_decoded(self, path: str, pixmap) -> None:
        """Display the filmstrip's decoded image in the viewer. The decode
        already honours the file's EXIF orientation, so no rotation here.
        We keep the path so the rotate button can target this file."""
        self._shown_image_path = path
        self.viewer.show_image(pixmap)
        self._refresh_rotation_indicator()

    # ---- B1+B2 imperative handlers (not signal-driven) ------------------

    def _handle_live_view_handoff(
        self, old_spectrum: str, new_spectrum: str
    ) -> None:
        """When the active spectrum flips, stop live view on the old worker
        (if running) and start it on the new (if its camera is in a state
        we can, and the user hasn't paused). Takes (old, new) explicitly
        because the OLD value is by definition gone from session state by
        the time we'd want to use it; this isn't a reactive refresh, it's
        a one-shot business action triggered by the workflow-step click."""
        old_worker = (
            self.visible_worker if old_spectrum == SPECTRUM_VISIBLE
            else self.ir_worker
        )
        new_worker = (
            self.visible_worker if new_spectrum == SPECTRUM_VISIBLE
            else self.ir_worker
        )

        old_state = self.session.camera_state(old_spectrum)
        if old_worker is not None and isinstance(old_state, (
            CameraStates.LiveViewStarted,
            CameraStates.LiveViewActive,
        )):
            old_worker.commands.live_view.emit(False)

        if self.session.live_view_paused or new_worker is None:
            return
        # Don't auto-start live view if the new bucket already has captures
        # — _on_directory_loaded will pause + show preview as soon as the
        # async load completes. Starting here would actuate the shutter
        # for nothing AND open a race window where late live frames
        # overwrite the preview thumbnail.
        obj = self.session.current_object
        if obj is not None and obj.count(
            self.session.active_side, self.session.active_spectrum
        ) > 0:
            return
        new_state = self.session.camera_state(new_spectrum)
        # Only start if the new camera is actually ready. If it's mid-
        # connect / reconnecting, the Ready→auto-start path in
        # _handle_active_camera_state will pick up live view when it gets there.
        if isinstance(new_state, (
            CameraStates.Ready,
            CameraStates.LiveViewStopped,
            CameraStates.CaptureFinished,
        )):
            new_worker.commands.live_view.emit(True)

    def _on_workflow_step_clicked(self, step_id: str) -> None:
        """Stepper click → translate id back to (side, spectrum), apply
        IR-fallback (H1, caller-side because SessionState is ignorant of
        worker availability), update session, then run live-view handoff
        if the spectrum actually changed."""
        bucket = self.effective_mode.bucket_by_step_id.get(step_id)
        if bucket is None:
            return
        side, spectrum = bucket
        # H1 IR-fallback — silently downgrade to VIS if no IR worker.
        if spectrum == SPECTRUM_INFRARED and self.ir_worker is None:
            spectrum = SPECTRUM_VISIBLE

        old_spectrum = self.session.active_spectrum
        self.session.set_active_bucket(side, spectrum)
        if old_spectrum != self.session.active_spectrum:
            self._handle_live_view_handoff(
                old_spectrum, self.session.active_spectrum
            )
        # update_ui handles capture-button enable/disable etc. that depend
        # on derived state across multiple axes; will dissolve into
        # receivers in Stage 5.
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

    # ---- B3+B4 receivers (camera_state_changed) ------------------------

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
        is the active one. Otherwise we'd e.g. start live view on IR while
        the user is working with VIS."""
        if spectrum != self.session.active_spectrum:
            return
        match state:
            case CameraStates.Ready():
                # Live view is the papyri default; auto-resume on every
                # Ready (initial connect, post-capture, etc.) unless
                # the user paused — or the camera has no live view (e.g.
                # the vusb virtual camera), which stays in Ready and is
                # captured from directly. Safe with capture_one — it never
                # transitions through Ready between live-view and
                # CaptureInProgress.
                if not self.session.live_view_paused and self._live_view_supported():
                    self.active_worker.commands.live_view.emit(True)
            case CameraStates.ConnectionError(error=err):
                self.logger.error("Connection error: %s", err)
                # No live frames possible — clear stale "live" pill.
                self.session.set_view_mode("empty")
            case CameraStates.CaptureError(error=err):
                self.logger.error("Capture error: %s", err)
            case CameraStates.Disconnected():
                # Active camera is gone — clear the live indicator so the
                # viewer doesn't show stale "live" pill / border with no
                # frames possible. Reconnect path: H17 auto-resumes live
                # view on the next Ready, which sets view_mode back to
                # "live" via the next preview frame in _on_preview_image.
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
        self.viewer.show_image(QPixmap.fromImage(ImageQt(pil_image)), fit=fit)
        # Each arriving live frame asserts "live" — handles transitions
        # away from preview/paused without needing extra plumbing.
        self.session.set_view_mode("live")

    # ---- B3+B4 + B5 receiver (camera-state-driven UI) -----------------

    def _refresh_camera_dependent_ui(self):
        """Autofocus button enable, capture status label, plus the
        capture/pause-button enables that gate on camera readiness AND
        object-loaded state. Reads B3/B4 (active spectrum's state) + B5.
        Wired to camera_state_changed AND current_object_changed.

        The match block is the single per-state scan target — use it
        to answer "what UI changes when camera reaches state X?"."""
        camera_state = self.session.active_camera_state
        has_object = self.session.current_object is not None
        object_loaded = has_object and self.session.current_object.dir_loaded

        # ---- live view + autofocus + capture (bottom row)
        camera_ready = self._active_camera_ready()
        self.pause_live_view_button.setEnabled(camera_ready)
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
                # Stamp the camera's fixed mount rotation into each captured
                # file's EXIF Orientation, so downstream processing (and any
                # orientation-aware viewer) gets it right. Idempotent.
                angle = self._lv_rotation[self.session.active_spectrum]
                if angle:
                    for p in paths:
                        write_orientation(p, angle)
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
        """Action handler for PhotoBrowser.directory_loaded. Folds in the
        F-VIEW-1 fix: explicit view_mode for the empty-bucket case so the
        previous bucket's "live" / "preview" pill doesn't leak in (option
        (a) — show "empty" until live frames or selection assert otherwise).
        """
        if self.session.current_object is None:
            return
        self.session.current_object.mark_dir_loaded()
        # Existing objects: PhotoBrowser auto-selects the last-loaded
        # thumb and shows it briefly, but its currentItemChanged signal
        # is suppressed during auto-selection (H22) so image_selected
        # never fires. Manually pause + assert the preview indicator so
        # the auto-selected take stays visible until the user resumes.
        current_name = self.filmstrip.current_file_name()
        if current_name is not None:
            self.session.set_live_view_paused(True)
            stem = os.path.splitext(current_name)[0]
            self.session.set_view_mode("preview", stem)
        else:
            # F-VIEW-1: empty bucket — no captures, no thumb to show.
            # If active camera is streaming, the next preview_image will
            # assert "live"; otherwise the pill stays "empty".
            self.session.set_view_mode("empty")
        self._refresh_camera_dependent_ui()

    def _on_image_selected(self, _path: str):
        """Action handler for PhotoBrowser.image_selected — only fires on
        USER click (auto-selection during load is suppressed per H22).
        Pauses live view so the chosen image persists; flips view_mode to
        preview with the file stem."""
        self.session.set_live_view_paused(True)
        stem = os.path.splitext(os.path.basename(_path))[0]
        self.session.set_view_mode("preview", stem)

    def _on_pause_toggled(self, paused: bool):
        """Action handler for the pause button's toggled signal. Updates
        session — the live_view command emit and pause-button-text update
        are receiver concerns (see _wire_session).

        On pause, the viewer would otherwise keep showing the (now
        stale) last live frame. Two branches replace it:
          - filmstrip has a current selection ⇒ re-display that take,
            flip view_mode to "preview" with its stem (same end state
            as the user having clicked the thumb directly).
          - empty filmstrip ⇒ blank the viewer, flip to "paused".
        Already in "preview" (user clicked a thumb earlier) means the
        selected take is already showing — leave it."""
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
        new_name = new_name.strip().replace(" ", "_")
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
        # starts with the old object name. Metadata file (_meta.json) and
        # `_chosen_*.txt` stay put — they're not name-prefixed and travel
        # with the parent dir.
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

        # Update `_chosen_<spectrum>.txt` references too — they store stems
        # that include the old object name (`<oldname>_a_vis_001`).
        for side, spectrum in BUCKETS:
            chosen_path = Path(chosen_path_for(old_dir, side, spectrum))
            if not chosen_path.is_file():
                continue
            stem = chosen_path.read_text().strip()
            if stem.startswith(old_name):
                chosen_path.write_text(stem.replace(old_name, new_name, 1))

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
        name = name.strip().replace(" ", "_")
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
                f"An object named {name!r} already exists in this working "
                f"directory.\n\nPick it from the sidebar to open it, or "
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

    # ---- B6 receivers (live_view_paused_changed) -----------------------

    def _refresh_pause_button_text(self) -> None:
        """Pause button text + checked state mirror B6 (the F-DUP fix —
        button is no longer the source of truth)."""
        paused = self.session.live_view_paused
        self.pause_live_view_button.setText(
            "Resume Live View" if paused else "Pause Live View"
        )
        # setChecked may fire `toggled` if the value differs; the action
        # handler then calls set_live_view_paused which is a no-op
        # (already current value), breaking the cycle.
        self.pause_live_view_button.setChecked(paused)

    def _handle_live_view_paused(self, paused: bool) -> None:
        """When paused intent flips, send live_view command to active
        worker. Started/stopped policy mirrors the prior in-handler
        logic — only auto-resume when the camera is in a state ready
        for it."""
        if paused:
            self.active_worker.commands.live_view.emit(False)
        elif isinstance(self.session.active_camera_state, (
            CameraStates.Ready,
            CameraStates.LiveViewStopped,
            CameraStates.CaptureFinished,
        )):
            self.active_worker.commands.live_view.emit(True)

    # ---- B7 receivers (view_mode_changed) ------------------------------

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

    def _on_sidebar_new_object(self) -> None:
        """Sidebar '+ New object' clicked: close any current object and focus
        the title bar's name input so the user can type + Enter to create.
        With no box open there's nowhere to put an object, so funnel the user
        to pick/create a box first."""
        if not self.q_settings.value("workingDirectory", ""):
            self._on_new_box()
            return
        if self.session.current_object is not None:
            self.close_object()
        self.title_bar.focus_name_input()

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
        self.objects_sidebar.set_working_directory(path)
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
            self, "Open box directory", self._box_dialog_start())
        if path:
            self._open_box(path)

    def _on_new_box(self) -> None:
        # Pure OS dialog (the user creates the box folder via the dialog's
        # New-Folder affordance); the title makes the intent explicit.
        path = QFileDialog.getExistingDirectory(
            self, "Create or choose a box directory", self._box_dialog_start())
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
        self.objects_sidebar.set_working_directory("")

    def _on_object_state_changed(self):
        """Single sink for any change in the current object's derived state.
        Components that mirror that state (side cards, objects sidebar badge,
        metadata pane subtitle) re-read from `self.session.current_object` here."""
        self._refresh_bucket_chosen_thumbs()
        # The active object's `· → ?? → ✓` badge in the sidebar can flip
        # when captures land. Cheap re-scan; no FS watcher needed.
        self.objects_sidebar.refresh()

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
        )
        self.active_worker.commands.capture_images.emit(req)

    # ---------------------------------------------------------- calibration

    def _wire_calibration(self) -> None:
        """Build the calibration controller and wire the bar (papyri mode
        only — in simple mode the bar is hidden and the controller stays
        None, guarded everywhere). The bar enters/leaves the calibration
        sub-mode; the target is picked via the tabs and capture goes
        through the normal Capture button."""
        if not self.mode.show_calibration:
            return
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
        self._cal_target = CalibrationTarget(
            wd, run_id, height_for=self._calibration_height_for)
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
        self._refresh_capture_button_label()

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

    def _note_calibration_capture_settled(self) -> None:
        """A capture finished — re-scan `_calibration/` so per-camera due
        is current (matters for the idle chip once we leave calibration).
        Harmless for normal object captures (the re-scan finds nothing new)."""
        if self.calibration is None:
            return
        self.calibration.refresh()
        self._refresh_calibration_bar()      # no-op while the sub-mode is active

    # ---------------------------------------------------------- mode toggle

    def _toggle_capture_mode(self) -> None:
        """Flip captureMode and relaunch. A confirm dialog guards the
        restart; the new mode is read at startup (see __init__)."""
        target = "papyri" if self.mode.key == "simple" else "simple"
        target_label = "full papyri" if target == "papyri" else "simple capture"
        if QMessageBox.question(
            self,
            "Switch capture mode",
            f"Switch to {target_label} mode?\n\nThe application will "
            f"restart to apply the change.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        self.q_settings.setValue("captureMode", target)
        self.q_settings.sync()
        self._restart_app()

    def _restart_app(self) -> None:
        """Relaunch this process, then quit. Worker threads are shut down
        first (self.close → closeEvent) so the camera is released before
        the new instance tries to claim it."""
        from PyQt6.QtCore import QProcess
        if getattr(sys, "frozen", False):
            program, args = sys.executable, sys.argv[1:]
        else:
            program, args = sys.executable, sys.argv
        self.logger.info("Restarting for mode switch: %s %s", program, args)
        self.close()  # triggers closeEvent → graceful worker shutdown
        QProcess.startDetached(program, args)
        QApplication.quit()

    # ---------------------------------------------------------- dialogs

    def open_settings(self):
        # Snapshot irProfile BEFORE the dialog runs so we can detect a change
        # and warn the user — IR worker spawn happens once at startup, so
        # changing irProfile at runtime has no effect until restart.
        previous_ir_profile = self.q_settings.value("irProfile")

        dialog = PapyriSettingsDialog(self.q_settings, PROFILES, self)
        if not dialog.exec():
            return

        for name, value in dialog.settings.items():
            self.q_settings.setValue(name, value)
            if name == "profile":
                new_profile = PROFILES[value]
                if new_profile is not self.profile:
                    self.profile = new_profile
                    # Always target the VIS worker — the "profile"
                    # setting is the VIS-camera profile. Routing via
                    # active_worker would wrongly reconnect IR if IR
                    # were the active spectrum.
                    self.visible_worker.commands.reconnect_camera.emit()
            elif name == "workingDirectory":
                self._refresh_camera_dependent_ui()
                self.objects_sidebar.set_working_directory(value)
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
            elif name in ("calibrationTrigger", "calibrationIntervalMinutes"):
                # Controller reads these from QSettings live; refresh so the
                # bar reflects the new trigger/interval immediately.
                if self.calibration is not None:
                    self.calibration.refresh()
                    self._refresh_calibration_bar()
            elif name == "captureHeightChoices":
                self._populate_height_select()

        # F-PERS-1: irProfile change has no runtime effect — the IR worker
        # is constructed once in _wire_camera at startup based on this
        # setting. Warn the user so they know to restart.
        new_ir_profile = self.q_settings.value("irProfile")
        if new_ir_profile != previous_ir_profile:
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
