"""Calibration due-tracking — the periodic reminder behind the bar's idle chip.

A camera's calibration is "done" when every *required* target for it
(from `papyri.calibration_spec`) has a recent enough shot — across all
timestamped runs under `_calibration/<run>/<spectrum>/<folder>/`. Capture +
review themselves live in `CalibrationTarget`; this module only answers
"is calibration due, per camera?" for the idle status chip.

`CalibrationController` is disk-derived and stateless — `summary()`
re-scans the calibration folders each call (cheap), so callers refresh
freely. A 60 s timer re-evaluates so age text ticks and the due threshold
flips without user action; `status_changed` fires only on a real change.

Trigger model (persisted setting `calibrationTrigger`):
    "off"     — never "due"; the chip stays neutral.
    "time"    — due when a required target's newest shot is older than the
                configured interval (`calibrationIntervalMinutes`).
    "session" — due when a required target's newest shot is not from today.
"""
from __future__ import annotations

import os
from datetime import datetime

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from papyri.calibration_spec import (
    CALIBRATION_DIRNAME, CALIBRATION_TARGETS, is_per_height, required_specs_for,
)
from papyri._layout import (
    CAPTURE_EXTENSIONS, SPECTRUM_INFRARED, SPECTRUM_VISIBLE,
)

# calibrationTrigger values.
TRIGGER_OFF = "off"
TRIGGER_TIME = "time"
TRIGGER_SESSION = "session"

_SPECTRUM_SHORT = {SPECTRUM_VISIBLE: "VIS", SPECTRUM_INFRARED: "IR"}
_LEVEL_RANK = {"ok": 0, "due": 1, "overdue": 2}     # worst wins


def _age_text(t: datetime | None, now: datetime) -> str:
    if t is None:
        return "never"
    secs = max(0, int((now - t).total_seconds()))
    if secs < 60:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return f"{mins} min ago"
    hours, rem = divmod(mins, 60)
    if hours < 24:
        return f"{hours} h {rem} min ago" if rem else f"{hours} h ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


class CalibrationController(QObject):
    """Per-camera calibration due-tracking for the idle status chip."""

    status_changed = pyqtSignal()

    def __init__(self, working_dir: str, q_settings, parent=None):
        super().__init__(parent)
        self._working_dir = working_dir or ""
        self._q = q_settings
        self._sig: tuple | None = None         # last emitted signature
        self._timer = QTimer(self)
        self._timer.setInterval(60_000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

    # ---- config --------------------------------------------------------

    def set_working_dir(self, path: str) -> None:
        path = path or ""
        if path == self._working_dir:
            return
        self._working_dir = path
        self.refresh()

    @property
    def calibration_dir(self) -> str:
        if not self._working_dir:
            return ""
        return os.path.join(self._working_dir, CALIBRATION_DIRNAME)

    def _trigger(self) -> str:
        return self._q.value("calibrationTrigger", TRIGGER_TIME) or TRIGGER_TIME

    def _interval_seconds(self) -> int:
        return int(self._q.value("calibrationIntervalMinutes", 60)) * 60

    # ---- public status -------------------------------------------------

    def summary(self, spectra) -> tuple[str, str]:
        """Compact idle-chip status over the given (configured) cameras.
        Returns `(overall_level, text)`, e.g. `("due", "VIS due · IR ok")`
        or `("ok", "Calibration up to date · 8 min ago")`."""
        trigger = self._trigger()
        if trigger == TRIGGER_OFF:
            return ("off", "Calibration reminder off")
        now = datetime.now()
        interval = self._interval_seconds()
        overall = "ok"
        oldest: datetime | None = None
        parts: list[str] = []
        for sp in spectra:
            level, sp_oldest = self._spectrum_level(sp, trigger, now, interval)
            overall = overall if _LEVEL_RANK[overall] >= _LEVEL_RANK[level] else level
            parts.append(f"{_SPECTRUM_SHORT.get(sp, '?')} "
                         f"{'ok' if level == 'ok' else 'due'}")
            if sp_oldest is not None and (oldest is None or sp_oldest < oldest):
                oldest = sp_oldest
        if overall == "ok":
            return ("ok", f"Calibration up to date · {_age_text(oldest, now)}")
        return (overall, " · ".join(parts))

    def refresh(self) -> None:
        """Re-evaluate over every target; emit `status_changed` only on a
        real change (new file, due flip, or age-text tick)."""
        now = datetime.now()
        interval = self._interval_seconds()
        sig = (self._trigger(), tuple(
            (s.slot, s.spectrum,
             self._age_bucket(self._newest(s.spectrum, self._subpath(s, s.spectrum)),
                              now, interval))
            for s in CALIBRATION_TARGETS
        ))
        if sig != self._sig:
            self._sig = sig
            self.status_changed.emit()

    # ---- internals -----------------------------------------------------

    def _height_for(self, spectrum: str) -> str:
        """Current rig height for a camera (VIS = the sticky `currentHeight`;
        IR = the fixed `irCaptureHeight`). Mirrors what MainWindow gives the
        CalibrationTarget, so due-tracking scans the same folder it writes."""
        key = "currentHeight" if spectrum == SPECTRUM_VISIBLE else "irCaptureHeight"
        return str(self._q.value(key, "") or "")

    def _subpath(self, spec, spectrum: str) -> str:
        """Folder under `<run>/<spectrum>/` for a target — a height subfolder
        is appended for per-height targets (Flatfield)."""
        if is_per_height(spec.slot):
            height = self._height_for(spectrum)
            if height:
                return os.path.join(spec.folder, height)
        return spec.folder

    def _spectrum_level(self, spectrum: str, trigger: str, now: datetime,
                        interval: int) -> tuple[str, datetime | None]:
        """(level, oldest-shot) for one camera, over its required targets."""
        open_any = False
        overdue = False
        oldest: datetime | None = None
        for s in required_specs_for(spectrum):
            t = self._newest(spectrum, self._subpath(s, spectrum))
            if self._is_open(t, trigger, now, interval):
                open_any = True
            if (trigger == TRIGGER_TIME and t is not None
                    and (now - t).total_seconds() >= 2 * interval):
                overdue = True
            if t is not None and (oldest is None or t < oldest):
                oldest = t
        if not open_any:
            return ("ok", oldest)
        return ("overdue" if overdue else "due", oldest)

    @staticmethod
    def _is_open(t: datetime | None, trigger: str, now: datetime,
                 interval: int) -> bool:
        if t is None:
            return True
        if trigger == TRIGGER_SESSION:
            return t.date() < now.date()
        return (now - t).total_seconds() >= interval

    @staticmethod
    def _age_bucket(t: datetime | None, now: datetime, interval: int) -> int:
        """Coarse age bucket for change-detection: -1 never, 0 fresh,
        1 past-interval, 2 past-2×interval. Lets the 60 s tick flip the
        chip on a threshold crossing without emitting every second."""
        if t is None:
            return -1
        age = (now - t).total_seconds()
        if interval and age >= 2 * interval:
            return 2
        if interval and age >= interval:
            return 1
        return 0

    def _newest(self, spectrum: str, subpath: str) -> datetime | None:
        """Newest capture mtime for `<spectrum>/<subpath>` across all runs
        (`subpath` is the target folder, plus a height subfolder for
        per-height targets). Runs are timestamp-named, so we check
        newest-first and stop at the first run that actually has shots for
        this bucket — usually one folder. (A newer run may have the bucket
        but empty, e.g. only flatfield was re-shot; that's skipped so the
        real newest wins.)"""
        d = self.calibration_dir
        if not d or not os.path.isdir(d):
            return None
        for run in sorted(os.listdir(d), reverse=True):
            bucket = os.path.join(d, run, spectrum, subpath)
            if not os.path.isdir(bucket):
                continue
            newest: datetime | None = None
            for entry in os.listdir(bucket):
                full = os.path.join(bucket, entry)
                if not os.path.isfile(full):
                    continue
                if os.path.splitext(entry)[1].lower() not in CAPTURE_EXTENSIONS:
                    continue
                try:
                    dt = datetime.fromtimestamp(os.path.getmtime(full))
                except OSError:
                    continue
                if newest is None or dt > newest:
                    newest = dt
            if newest is not None:
                return newest
        return None
