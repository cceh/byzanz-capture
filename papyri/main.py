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
from dataclasses import dataclass

_camlibs_env = os.environ.get('CAMLIBS')
_iolibs_env = os.environ.get('IOLIBS')
import gphoto2 as gp  # noqa: E402
if _camlibs_env:
    os.environ['CAMLIBS'] = _camlibs_env
if _iolibs_env:
    os.environ['IOLIBS'] = _iolibs_env

# Configure logging BEFORE byzanz_camera imports so module-load-time INFO
# lines (e.g. from _autodetect reporting which path it picked) are visible.
logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')

from pathlib import Path

from PIL.ImageQt import ImageQt
from PyQt6.QtCore import QObject, QSettings, QThread, pyqtSignal
from PyQt6.QtGui import QAction, QCloseEvent, QIcon, QPixmap, QPixmapCache
from PyQt6.QtWidgets import (
    QApplication, QInputDialog, QLabel, QMainWindow, QMenu,
    QMessageBox, QPushButton, QSplitter,
)
from PyQt6.uic import loadUi

from byzanz_camera.camera_worker import (
    CameraStates, CameraWorker, CaptureImagesRequest, ConfigRequest,
)
from byzanz_camera.helpers import get_ui_path
from byzanz_camera.photo_browser import get_file_index
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
from papyri.capture_browser import PapyriCaptureBrowser
from papyri.metadata_pane import MetadataPane
from papyri.objects_sidebar import ObjectsSidebar
from papyri.session_state import SessionState
from byzanz_camera.profiles.base import Profile
from byzanz_camera.profiles.corodile_test_sony_ilce_7m3 import MoritzA7MIII
from byzanz_camera.profiles.paris_dome_sony_ilce_7rm5 import ParisDomeSonyIlce7RM5
from byzanz_camera.profiles.cceh_dome_nikon_d800e import CCeHDomeNikonD800E
from papyri.workflow_stepper import (
    WorkflowGroup, WorkflowStep, WorkflowStepper,
)

from send2trash import send2trash

from camera_config_dialog import CameraConfigDialog
from papyri.settings_dialog import PapyriSettingsDialog

PROFILES = {
    "MoritzA7III": MoritzA7MIII(),
    "ParisDomeSonyIlce7RM5": ParisDomeSonyIlce7RM5(),
    "CCeHDomeNikonD800E": CCeHDomeNikonD800E(),
}


# WorkflowStepper step ids — stable mapping to (side, spectrum) buckets.
# Workflow order matches the physical capture sequence: visible station
# (A → B) then infrared station (A → B).
_STEP_ID_BY_BUCKET = {
    (SIDE_A, SPECTRUM_VISIBLE):  "vis_a",
    (SIDE_B, SPECTRUM_VISIBLE):  "vis_b",
    (SIDE_A, SPECTRUM_INFRARED): "ir_a",
    (SIDE_B, SPECTRUM_INFRARED): "ir_b",
}
_BUCKET_BY_STEP_ID = {v: k for k, v in _STEP_ID_BY_BUCKET.items()}


def _build_workflow_groups() -> list[WorkflowGroup]:
    """Two groups (Visible, Infrared) × two sides (A, B). Tints overridden
    explicitly so they match the agreed papyri palette exactly (palette
    derivation from a base color doesn't quite hit the right values for
    warm hues)."""
    return [
        WorkflowGroup(
            label="Visible",
            short_label="VIS",
            base_color="#3b82f6",
            bg_active="#3b82f6",      # blue-500
            bg_done="#dbeafe",        # blue-200
            bg_pending="white",
            text_dark="#1e3a8a",      # blue-800
            steps=[
                WorkflowStep(_STEP_ID_BY_BUCKET[(SIDE_A, SPECTRUM_VISIBLE)],  "Side A"),
                WorkflowStep(_STEP_ID_BY_BUCKET[(SIDE_B, SPECTRUM_VISIBLE)],  "Side B"),
            ],
        ),
        WorkflowGroup(
            label="Infrared",
            short_label="IR",
            base_color="#ea580c",
            bg_active="#ea580c",      # orange-600
            bg_done="#ffedd5",        # orange-100 (pastel)
            bg_pending="white",
            text_dark="#9a3412",      # orange-800
            steps=[
                WorkflowStep(_STEP_ID_BY_BUCKET[(SIDE_A, SPECTRUM_INFRARED)], "Side A"),
                WorkflowStep(_STEP_ID_BY_BUCKET[(SIDE_B, SPECTRUM_INFRARED)], "Side B"),
            ],
        ),
    ]


@dataclass(frozen=True)
class Capture:
    """One capture take in an object's step.

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


class Object(QObject):
    """A single papyri capture object: its directory, four (side, spectrum)
    capture buckets, metadata.

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

    def next_template(self, side: str, spectrum: str) -> str:
        """Compute the file-path template for the next capture in `(side, spectrum)`.
        Filename format: `<name>_<side_letter>_<spectrum_infix>_NNN.${extension}`.

        Uses `max_index + 1` (not `count + 1`) so it survives gaps caused by
        deletes or moves between buckets — otherwise after deleting take 002
        of [001, 002, 003] the next take would collide with the existing 003."""
        n = self._max_index_on_disk(side, spectrum) + 1
        s_inf = self._SIDE_INFIX[side]
        sp_inf = self._SPECTRUM_INFIX[spectrum]
        filename = f"{self.name}_{s_inf}_{sp_inf}_{n:03d}${{extension}}"
        return os.path.join(self.dir_for(side, spectrum), filename)

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
        """Read `_chosen_<spectrum>.txt` (a stem); fall back to first capture
        if absent/stale."""
        preferred = self._read_chosen_pref(side, spectrum)
        if preferred:
            for c in captures:
                if c.stem == preferred:
                    return c
        return captures[0] if captures else None

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

        self.q_settings = QSettings()
        self._init_default_settings()
        # Qt's default QPixmapCache limit is 10 MB — too small for one decoded
        # JPEG (~72 MB) let alone a RAW (~180 MB). Apply the user setting.
        QPixmapCache.setCacheLimit(int(self.q_settings.value("maxPixmapCache")) * 1024)
        self.profile = PROFILES[self.q_settings.value("profile", "MoritzA7III")]
        self.current_object: Object | None = None
        # Per-spectrum camera states — VIS and IR each have their own worker
        # and lifecycle. update_ui / capture_button gating reads the active
        # spectrum's state via the `camera_state` property below.
        self.camera_states: dict[str, CameraStates.StateType | None] = {
            SPECTRUM_VISIBLE: None,
            SPECTRUM_INFRARED: None,
        }
        self.cam_config_dialog: CameraConfigDialog | None = None
        self._live_view_paused = False
        # Workflow state — two orthogonal axes:
        #   active_spectrum  : VISIBLE | INFRARED — drives which camera worker
        #                      fires + which spectrum chip is highlighted.
        #   active_side      : A | B — drives which side dir captures land in.
        # D.1 introduces the side dimension at the data layer; the side
        # toggle UI lands in D.2, so for now active_side stays pinned to A.
        self.active_spectrum: str = SPECTRUM_VISIBLE
        self.active_side: str = SIDE_A

        self._bind_widgets()
        self._wire_actions()
        self._wire_camera()
        self._wire_session()
        # Initial paint of the side cards so Side A reads as active before
        # the first object is loaded.
        self._refresh_workflow_stepper()
        self.update_ui()

    # ------------------------------------------------------------------ setup

    def _init_default_settings(self):
        defaults = {
            "profile": "MoritzA7III",
            "irProfile": None,                              # set when IR camera is configured
            "workingDirectory": os.path.expanduser("~"),
            "maxPixmapCache": 256,
            "enableSecondScreenMirror": False,
        }
        for key, value in defaults.items():
            if self.q_settings.value(key) is None:
                self.q_settings.setValue(key, value)

    def _bind_widgets(self):
        self.visible_camera_state: CameraStateWidget = self.findChild(
            CameraStateWidget, "visibleCameraState"
        )
        self.ir_camera_state: CameraStateWidget = self.findChild(
            CameraStateWidget, "irCameraState"
        )

        self.settings_button: QPushButton = self.findChild(QPushButton, "settingsButton")

        self.objects_sidebar: ObjectsSidebar = self.findChild(ObjectsSidebar, "objectsSidebar")
        self.metadata_splitter: QSplitter = self.findChild(QSplitter, "metadataSplitter")
        # Make the workspace absorb extra space; the metadata pane stays at
        # its sizeHint width (200px) by default and can be dragged in either
        # direction (down to 150px min, up to whatever the user wants).
        self.metadata_splitter.setStretchFactor(0, 0)
        self.metadata_splitter.setStretchFactor(1, 1)
        self.metadata_pane: MetadataPane = self.findChild(MetadataPane, "metadataPane")

        # WorkflowStepper replaces the old step-indicator chips and the
        # short-lived SideCard widgets. One stepper renders all four
        # (side, spectrum) buckets as a chevron flow with a per-spectrum
        # bracket above each pair.
        self.workflow_stepper: WorkflowStepper = self.findChild(
            WorkflowStepper, "workflowStepper"
        )
        self.workflow_stepper.set_groups(_build_workflow_groups())
        self.workflow_stepper.step_clicked.connect(self._on_workflow_step_clicked)
        self.photo_browser: PapyriCaptureBrowser = self.findChild(PapyriCaptureBrowser, "captureBrowser")

        self.pause_live_view_button: QPushButton = self.findChild(QPushButton, "pauseLiveViewButton")
        self.autofocus_button: QPushButton = self.findChild(QPushButton, "autofocusButton")
        self.capture_status_label: QLabel = self.findChild(QLabel, "captureStatusLabel")
        self.capture_button: QPushButton = self.findChild(QPushButton, "captureButton")

        # Settings menu (popup off the "Settings" button)
        self.open_program_settings_action = self._action(
            "General settings", self.open_settings, icon="ui/general_settings.svg")
        self.open_advanced_cam_config_action = self._action(
            "Advanced camera config", self.open_advanced_camera_config, icon="ui/cam_settings.svg")
        self.settings_menu = QMenu(self)
        self.settings_menu.addActions([
            self.open_program_settings_action, self.open_advanced_cam_config_action,
        ])

    def _action(self, label: str, slot, icon: str | None = None) -> QAction:
        action = QAction(label, self)
        action.triggered.connect(slot)
        if icon:
            action.setIcon(QIcon(get_ui_path(icon)))
        return action

    def _popup_below(self, button, menu: QMenu):
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))

    def _wire_actions(self):
        self.settings_button.clicked.connect(
            lambda: self._popup_below(self.settings_button, self.settings_menu))

        # Metadata pane owns the object-name affordance + close + rename
        # buttons (they live in its title row).
        self.metadata_pane.start_object_requested.connect(self.start_object)
        self.metadata_pane.rename_requested.connect(self.rename_current_object)
        self.metadata_pane.close_requested.connect(self.close_object)

        # Connect/disconnect buttons live inside CameraStateWidget —
        # widget-internal wiring, no main.py plumbing needed.

        self.pause_live_view_button.toggled.connect(self._on_pause_toggled)
        self.photo_browser.image_selected.connect(self._on_image_selected)
        self.photo_browser.directory_loaded.connect(self._on_directory_loaded)
        self.autofocus_button.clicked.connect(self._trigger_autofocus)
        self.capture_button.clicked.connect(self.capture_image)

        # Objects sidebar
        self.objects_sidebar.set_working_directory(
            self.q_settings.value("workingDirectory", "")
        )
        self.objects_sidebar.object_selected.connect(self._on_sidebar_object_selected)
        self.objects_sidebar.new_object_requested.connect(self._on_sidebar_new_object)

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

    def _wire_session(self) -> None:
        """All `session.*_changed.connect(...)` calls live here. Single grep
        target for "what reacts to what" — one line per receiver per axis,
        sorted by axis. Receivers are added as each axis migrates onto
        SessionState (currently empty: skeleton only)."""
        return

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
        Tracks `self.active_spectrum`; falls back to visible if IR isn't configured."""
        if self.active_spectrum == SPECTRUM_INFRARED and self.ir_worker is not None:
            return self.ir_worker
        return self.visible_worker

    def _set_active_bucket(self, side: str, spectrum: str) -> None:
        """Switch the active (side, spectrum). Updates side card visuals,
        swaps active_worker for capture/live-view routing, re-binds the
        photo browser, transitions live view to the new spectrum's worker,
        and re-renders dependent UI."""
        if spectrum == SPECTRUM_INFRARED and self.ir_worker is None:
            # IR not configured at this station — silently keep visible.
            spectrum = SPECTRUM_VISIBLE
        if side == self.active_side and spectrum == self.active_spectrum:
            return

        old_spectrum = self.active_spectrum
        self.active_side = side
        self.active_spectrum = spectrum

        # Spectrum changed → hand live view from old worker to new worker.
        # Stops the old camera streaming USB frames we don't show anymore,
        # and gives the user immediate frames on the new spectrum (instead
        # of waiting for them to manually toggle Pause Live View off).
        if old_spectrum != spectrum:
            self._transition_live_view(old_spectrum, spectrum)

        self._refresh_workflow_stepper()
        if self.current_object is not None:
            self.photo_browser.bind_object(
                self.current_object, side, spectrum
            )
        self.update_ui()

    def _transition_live_view(self, old_spectrum: str, new_spectrum: str) -> None:
        """When the active spectrum flips, stop live view on the old worker
        (if it was running) and start it on the new worker (if its camera
        is in a state where we can, and the user hasn't paused live view)."""
        old_worker = (
            self.visible_worker if old_spectrum == SPECTRUM_VISIBLE else self.ir_worker
        )
        new_worker = (
            self.visible_worker if new_spectrum == SPECTRUM_VISIBLE else self.ir_worker
        )

        old_state = self.camera_states[old_spectrum]
        if old_worker is not None and isinstance(old_state, (
            CameraStates.LiveViewStarted,
            CameraStates.LiveViewActive,
        )):
            old_worker.commands.live_view.emit(False)

        if self._live_view_paused or new_worker is None:
            return
        new_state = self.camera_states[new_spectrum]
        # Only start if the new camera is actually ready. If it's mid-
        # connect / reconnecting, the Ready→auto-start path in
        # _on_camera_state_changed will pick up live view when it gets there.
        if isinstance(new_state, (
            CameraStates.Ready,
            CameraStates.LiveViewStopped,
            CameraStates.CaptureFinished,
        )):
            new_worker.commands.live_view.emit(True)

    def _refresh_workflow_stepper(self) -> None:
        """Push current (active_side, active_spectrum) + per-bucket counts to
        the workflow stepper. Idempotent; called after any state change."""
        for (side, spectrum), step_id in _STEP_ID_BY_BUCKET.items():
            count = (
                self.current_object.count(side, spectrum)
                if self.current_object is not None else 0
            )
            self.workflow_stepper.set_count(step_id, count)
        self.workflow_stepper.set_active(
            _STEP_ID_BY_BUCKET[(self.active_side, self.active_spectrum)]
        )
        # Capture button label always reflects the active bucket so the
        # operator never has to guess where the next shot lands.
        self._update_capture_button_label()
        # Top-bar camera-state pill borders match the active spectrum so the
        # right station is visually obvious.
        self.visible_camera_state.set_emphasized(
            self.active_spectrum == SPECTRUM_VISIBLE
        )
        self.ir_camera_state.set_emphasized(
            self.active_spectrum == SPECTRUM_INFRARED
        )

    def _update_capture_button_label(self) -> None:
        side_label = "Side A" if self.active_side == SIDE_A else "Side B"
        spectrum_label = (
            "Visible" if self.active_spectrum == SPECTRUM_VISIBLE else "Infrared"
        )
        self.capture_button.setText(f"Capture · {side_label} · {spectrum_label}")

    def _on_workflow_step_clicked(self, step_id: str) -> None:
        """Stepper click → translate id back to (side, spectrum) and route
        through the unified bucket switcher."""
        bucket = _BUCKET_BY_STEP_ID.get(step_id)
        if bucket is None:
            return
        side, spectrum = bucket
        self._set_active_bucket(side, spectrum)

    # ---------------------------------------------------------- camera state

    @property
    def camera_state(self) -> CameraStates.StateType | None:
        """The active spectrum's camera state. Backwards-compatible name —
        all the existing isinstance() callsites just keep working."""
        return self.camera_states[self.active_spectrum]

    def _on_camera_state_changed(self, spectrum: str, state: CameraStates.StateType) -> None:
        """Unified handler for both worker threads.

        Side-effects split into two groups:
        - **per-spectrum**: auto-connect on Found, auto-reconnect on
          Disconnected — fire for the spectrum that emitted, regardless
          of which is active. Each camera should come up and recover
          independently.
        - **active-only**: live view start, dialog rejection on disconnect,
          error logging — fire only when the emitting spectrum is the
          currently active one. Otherwise we'd e.g. start live view on
          IR while the user is working with VIS.
        """
        short = "VIS" if spectrum == SPECTRUM_VISIBLE else "IR"
        self.logger.info("[%s] %s", short, state.__class__.__name__)

        self.camera_states[spectrum] = state

        worker = (
            self.visible_worker if spectrum == SPECTRUM_VISIBLE else self.ir_worker
        )
        profile = (
            self.profile if spectrum == SPECTRUM_VISIBLE else self.ir_profile
        )

        # ---- per-spectrum side effects (always) ----
        if isinstance(state, CameraStates.Found):
            if profile is not None:
                worker.commands.connect_camera.emit(profile)
        elif isinstance(state, CameraStates.Disconnected):
            if state.auto_reconnect:
                worker.commands.find_camera.emit()

        # ---- active-only side effects ----
        if spectrum == self.active_spectrum:
            match state:
                case CameraStates.Disconnecting():
                    if self.cam_config_dialog:
                        self.cam_config_dialog.reject()

                case CameraStates.ConnectionError(error=err):
                    self.logger.error("Connection error: %s", err)

                case CameraStates.Ready():
                    # Live view is the papyri default; auto-resume on every
                    # Ready (initial connect, post-capture, etc.) unless
                    # user paused. Safe with capture_one — it never
                    # transitions through Ready between live-view and
                    # CaptureInProgress.
                    if not self._live_view_paused:
                        worker.commands.live_view.emit(True)

                case CameraStates.CaptureError(error=err):
                    self.logger.error("Capture error: %s", err)

        self.update_ui()

    def _on_preview_image(self, spectrum: str, image):
        # Drop frames from the inactive spectrum — keeps the photo viewer
        # from flickering between two feeds when both workers are streaming.
        if spectrum != self.active_spectrum:
            return
        self.photo_browser.show_preview(ImageQt(image.image))
        # Each arriving live frame asserts "live" — handles transitions
        # away from preview/paused without needing extra plumbing.
        if not self._live_view_paused:
            self.photo_browser.set_view_state("live")

    # ------------------------------------------------------------- update_ui

    def update_ui(self):
        """Single source of truth for every widget's enable/visibility/text.

        Always derives from (camera_state, session, _live_view_paused). Called
        from _on_camera_state_changed and from anywhere that changes context.
        """
        camera_state = self.camera_state
        has_object = self.current_object is not None
        object_loaded = has_object and self.current_object.dir_loaded

        # ---- object loading state → metadata pane spinner
        # The pane itself owns name field + rename + close button visibility
        # (driven by metadata_pane.bind_object). Loading busy is the only bit
        # that depends on transient state, so we drive it from here.
        self.metadata_pane.set_loading_busy(has_object and not object_loaded)

        # ---- live view + autofocus + capture (bottom row)
        camera_ready = isinstance(camera_state, (
            CameraStates.Ready,
            CameraStates.LiveViewStarted,
            CameraStates.LiveViewActive,
            CameraStates.FocusStarted,
            CameraStates.FocusFinished,
            CameraStates.CaptureFinished,
        ))
        self.pause_live_view_button.setEnabled(camera_ready)
        self.capture_button.setEnabled(camera_ready and object_loaded)

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
                self.autofocus_button.setEnabled(True)

            case CameraStates.FocusStarted():
                self.autofocus_button.setEnabled(False)

            case CameraStates.FocusFinished(success=success):
                self.autofocus_button.setEnabled(True)
                if not success:
                    self.capture_status_label.setText("Could not focus.")
                    self.capture_status_label.setStyleSheet("color: red;")

            case CameraStates.LiveViewStopped():
                self.autofocus_button.setEnabled(False)
                # Don't clear the viewer — selected capture or last live frame
                # should keep showing. The next live frame will overwrite, or
                # the user-selected capture stays.

            case CameraStates.CaptureInProgress():
                self.autofocus_button.setEnabled(False)
                self.capture_status_label.setText("Capturing…")
                self.capture_status_label.setStyleSheet("")

            case CameraStates.CaptureFinished(file_paths=paths):
                if paths:
                    names = ", ".join(os.path.basename(p) for p in paths)
                    self.capture_status_label.setText(f"Captured: {names}")
                else:
                    self.capture_status_label.setText("Captured.")
                self.capture_status_label.setStyleSheet("color: palette(mid);")

            case CameraStates.CaptureCanceled():
                self.capture_status_label.setText("Capture canceled.")
                self.capture_status_label.setStyleSheet("color: red;")

            case CameraStates.CaptureError(error=err):
                self.capture_status_label.setText(f"Error: {err}")
                self.capture_status_label.setStyleSheet("color: red;")

    # ------------------------------------------------------------- handlers

    def _on_directory_loaded(self, _path: str):
        if self.current_object is not None:
            self.current_object.dir_loaded = True
            self.current_object.refresh()  # capture count may have changed
            # Existing objects: PhotoBrowser auto-selects the last-loaded
            # thumb and shows it briefly, but its currentItemChanged signal
            # is suppressed during auto-selection so live view doesn't get
            # paused — and the next live frame overwrites the displayed
            # thumb. Manually pause + set the preview indicator so the
            # auto-selected take stays visible until the user resumes.
            current_name = self.photo_browser.current_file_name()
            if current_name is not None:
                if not self.pause_live_view_button.isChecked():
                    self.pause_live_view_button.setChecked(True)  # fires _on_pause_toggled
                stem = os.path.splitext(current_name)[0]
                self.photo_browser.set_view_state("preview", stem)
            self.update_ui()

    def _on_image_selected(self, _path: str):
        # Selecting a previous capture pauses live view so the chosen image
        # stays on screen. Resume is explicit via the Pause/Resume button.
        if not self.pause_live_view_button.isChecked():
            self.pause_live_view_button.setChecked(True)  # fires _on_pause_toggled
        # Indicator → preview, with the file stem in the pill.
        stem = os.path.splitext(os.path.basename(_path))[0]
        self.photo_browser.set_view_state("preview", stem)

    def _on_pause_toggled(self, paused: bool):
        self._live_view_paused = paused
        self.pause_live_view_button.setText(
            "Resume Live View" if paused else "Pause Live View"
        )
        if paused:
            self.active_worker.commands.live_view.emit(False)
            # Only flip to "paused" if we're not already showing a preview;
            # _on_image_selected sets "preview" first and we don't want to
            # immediately overwrite it (the auto-pause from selecting a
            # thumb fires this handler right after).
            if self.photo_browser._view_state != "preview":
                self.photo_browser.set_view_state("paused")
        elif isinstance(self.camera_state, (
            CameraStates.Ready, CameraStates.LiveViewStopped, CameraStates.CaptureFinished,
        )):
            self.active_worker.commands.live_view.emit(True)

    def _trigger_autofocus(self):
        self.active_worker.commands.trigger_autofocus.emit()

    # --------------------------------------------------- object lifecycle

    def rename_current_object(self):
        if not self.current_object:
            return
        new_name, ok = QInputDialog.getText(
            self, "Rename object", "New name:", text=self.current_object.name,
        )
        if not ok:
            return
        new_name = new_name.strip().replace(" ", "_")
        if not new_name or new_name == self.current_object.name:
            return

        new_dir = os.path.join(self.current_object.working_dir, new_name)
        if Path(new_dir).exists():
            QMessageBox.critical(self, "Error", f"Object {new_name!r} already exists.")
            return

        old_name = self.current_object.name
        old_dir = self.current_object.dir

        # Commit any pending metadata edits to the OLD `_meta.json` so they
        # travel with the directory when we rename it. (Without this the
        # debounced save fires after the rename and either crashes on the
        # missing path or writes to a stale location.)
        self.metadata_pane.flush_pending_save()

        # Stop watching before moving the directory; reopen at the new path.
        self.photo_browser.bind_object(None)

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
        self._set_current_object(Object(self.current_object.working_dir, new_name))

    def start_object(self, name: str):
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
        self._set_current_object(obj)

    def close_object(self):
        self._set_current_object(None)

    def _set_current_object(self, obj: Object | None):
        # Disconnect the previous object's signal so a stale instance can't
        # keep firing into our handlers (and to release the connection ref).
        if self.current_object is not None:
            try:
                self.current_object.state_changed.disconnect(self._on_object_state_changed)
            except TypeError:
                pass

        self.current_object = obj

        if obj is not None:
            obj.state_changed.connect(self._on_object_state_changed)
            obj.refresh()

        # bind_object handles both the open + close + chosen-state plumbing.
        # Pass the current (side, spectrum) so the browser shows that bucket.
        self.photo_browser.bind_object(obj, self.active_side, self.active_spectrum)
        self.metadata_pane.bind_object(obj)

        # Reset the viewer indicator on object close so the stale preview
        # caption / live border doesn't persist with no captures listed.
        # (Open path: the next live frame or thumb-selection drives state.)
        if obj is None:
            self.photo_browser.set_view_state("empty")
        self._on_object_state_changed()  # paint chip even when obj is None
        self.objects_sidebar.set_active_object_name(obj.name if obj else None)
        self.objects_sidebar.refresh()
        self.update_ui()

    # ---- objects sidebar handlers ----

    def _on_sidebar_object_selected(self, name: str) -> None:
        """Sidebar row clicked: switch focus to that object."""
        if self.current_object is not None and self.current_object.name == name:
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
        self._set_current_object(obj)

    def _on_sidebar_new_object(self) -> None:
        """Sidebar '+ New object' clicked: close any current object and focus
        the metadata pane's name input so the user can type + Enter to create."""
        if self.current_object is not None:
            self.close_object()
        self.metadata_pane.focus_name_input()

    def _on_object_state_changed(self):
        """Single sink for any change in the current object's derived state.
        Components that mirror that state (side cards, objects sidebar badge,
        metadata pane subtitle) re-read from `self.current_object` here."""
        self._refresh_workflow_stepper()
        # The active object's `· → ?? → ✓` badge in the sidebar can flip
        # when captures land. Cheap re-scan; no FS watcher needed.
        self.objects_sidebar.refresh()

    # ---------------------------------------------------------- capture

    def capture_image(self):
        if not self.current_object:
            return
        # Re-ensure the on-disk skeleton — covers the case where someone
        # deleted a side or spectrum dir in Finder between captures.
        self.current_object.ensure_dir()
        req = CaptureImagesRequest(
            file_path_template=self.current_object.next_template(
                self.active_side, self.active_spectrum
            ),
            num_images=1,
            image_quality=CaptureImagesRequest.CaptureFormat.JPEG_AND_RAW,
            manual_trigger=False,
        )
        self.active_worker.commands.capture_images.emit(req)

    # ---------------------------------------------------------- dialogs

    def open_settings(self):
        dialog = PapyriSettingsDialog(self.q_settings, PROFILES, self)
        if dialog.exec():
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
                    self.update_ui()
                    self.objects_sidebar.set_working_directory(value)
                elif name == "maxPixmapCache":
                    QPixmapCache.setCacheLimit(int(value) * 1024)

    def open_advanced_camera_config(self):
        if not isinstance(self.camera_state, (
            CameraStates.Ready,
            CameraStates.LiveViewStarted,
            CameraStates.LiveViewActive,
            CameraStates.CaptureFinished,
        )):
            QMessageBox.information(
                self, "Camera not ready",
                "Connect the camera before opening the advanced config dialog.",
            )
            return

        def open_dialog(cfg):
            self.cam_config_dialog = CameraConfigDialog(cfg, self.active_worker, self)
            self.cam_config_dialog.setModal(False)
            self.cam_config_dialog.show()
            self.cam_config_dialog.finished.connect(
                lambda *_: setattr(self, "cam_config_dialog", None)
            )

        req = ConfigRequest()
        req.signal.got_config.connect(open_dialog)
        self.active_worker.commands.get_config.emit(req)

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
    app.setApplicationName("Papyri Capture")
    win = PapyriMainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
