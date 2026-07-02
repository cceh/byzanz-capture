"""Rotated-sample nudge — optional, self-contained operator reminder.

WHY: the ML team wants roughly every Nth piece (default 20) ALSO captured
once with the papyrus physically rotated 90° on the stage, so the fixed
dome lights hit it from a different relative angle. That extra capture is
filed as a *separate* object sharing the inventory number with a
``_rotiert`` suffix (e.g. ``P.Köln_8821`` → ``P.Köln_8821_rotiert``).

The rotation is a PHYSICAL act by the operator — this module never touches
the camera, the live-view rotation, or any capture. It only (a) shows a
slim banner under the title row when a rotated sample is due for the
current object, and (b) offers a one-click button that creates the twin
object and switches to it (reusing ``MainWindow.start_object``).

ISOLATION — this feature is deliberately quarantined so it can be removed
in one step:

  1. delete this file
  2. delete the lines in ``papyri/main.py`` marked "rotated-sample nudge"
     (the import, the ``install_…`` call, and the one ``elif`` in
     ``open_settings``)
  3. (optional) delete the blocks marked "rotated-sample nudge" in
     ``papyri/settings_dialog.py`` and ``papyri/ui/settings_dialog.ui``

Nothing else references it; it owns its own widget, its own due-logic, and
its own QSettings keys.

QSettings keys (read with defaults, so no migration needed):
  rotatedSampleNudge/enabled   bool, default True
  rotatedSampleNudge/interval  int,  default 20   (<=0 disables)
"""
from __future__ import annotations

import logging
import os

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from papyri.object_layout import list_managed_objects

_logger = logging.getLogger("RotatedSampleNudge")

# The suffix that marks a rotated-twin object. Twins are excluded from the
# "every Nth piece" count and never themselves trigger a nudge.
TWIN_SUFFIX = "_rotiert"

ENABLED_KEY = "rotatedSampleNudge/enabled"
INTERVAL_KEY = "rotatedSampleNudge/interval"
ENABLED_DEFAULT = True
INTERVAL_DEFAULT = 20


# ---- pure due-logic (no Qt) ---------------------------------------------

def is_twin_name(name: str) -> bool:
    return name.endswith(TWIN_SUFFIX)


def primary_object_count(working_dir: str | None) -> int:
    """Number of real pieces in the box — managed objects whose name is not
    a ``_rotiert`` twin. Twins don't count toward the cadence."""
    return sum(
        1 for n in list_managed_objects(working_dir) if not is_twin_name(n)
    )


def twin_exists(working_dir: str, base_name: str) -> bool:
    return os.path.isdir(os.path.join(working_dir, base_name + TWIN_SUFFIX))


# ---- banner widget ------------------------------------------------------

class RotatedSampleNudgeBar(QWidget):
    """Slim banner shown under the title row only while a rotated sample is
    due. Inline-styled (no global QSS edits) to stay self-contained."""

    create_twin_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("rotatedSampleNudgeBar")
        self.setVisible(False)
        # One line tall: a fixed vertical policy keeps the bar at its line-
        # height sizeHint instead of competing for the column's spare vertical
        # space (which made it expand to fill the gap above the viewer).
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(10)

        self._label = QLabel(self)
        # Single line — no wrap (wrapping + fixed height would clip).
        self._label.setWordWrap(False)
        self._button = QPushButton("Create rotated twin", self)
        self._button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._button.clicked.connect(self.create_twin_requested)

        layout.addWidget(self._label, 1)
        layout.addWidget(self._button, 0)

        # Amber "warning/attention" look, in the app's Tailwind-style palette.
        self.setStyleSheet(
            "#rotatedSampleNudgeBar {"
            "  background: #fef3c7;"               # amber-100
            "  border-top: 1px solid #fcd34d;"     # amber-300
            "  border-bottom: 1px solid #fcd34d;"
            "}"
            "#rotatedSampleNudgeBar QLabel {"
            "  color: #92400e;"                    # amber-800
            "  font-size: 12px;"
            "}"
            "#rotatedSampleNudgeBar QPushButton {"
            "  background: #f59e0b;"               # amber-500
            "  color: #ffffff; border: none; border-radius: 6px;"
            "  padding: 5px 12px; font-weight: 600;"
            "}"
            "#rotatedSampleNudgeBar QPushButton:hover { background: #d97706; }"
        )

    def show_due(self, interval: int) -> None:
        self._label.setText(
            f"⟳  Rotated sample due — the ML workflow asks for a 90°-rotated "
            f"capture of about every {interval}th piece. Rotate the papyrus "
            f"90° on the stage, then capture the twin."
        )
        self.setVisible(True)

    def hide_bar(self) -> None:
        self.setVisible(False)


# ---- controller ---------------------------------------------------------

class _NudgeController:
    """Recomputes the due-state from disk on every current-object change and
    drives the banner. Stateless beyond the two refs it holds."""

    def __init__(self, main_window, bar: RotatedSampleNudgeBar, object_cls):
        self._mw = main_window
        self._bar = bar
        # The real `Object` class, passed in from main.py. We must NOT
        # `from papyri.main import Object` here: the app runs as
        # `python -m papyri.main`, so papyri.main is `__main__` and a fresh
        # import would yield a SECOND, distinct Object class — isinstance
        # against it is always False. See install_rotated_sample_nudge.
        self._object_cls = object_cls

    def _settings(self) -> tuple[bool, int]:
        qs = self._mw.q_settings
        enabled = qs.value(ENABLED_KEY, ENABLED_DEFAULT, type=bool)
        try:
            interval = int(qs.value(INTERVAL_KEY, INTERVAL_DEFAULT))
        except (TypeError, ValueError):
            interval = INTERVAL_DEFAULT
        return enabled, interval

    def refresh(self, *_) -> None:
        enabled, interval = self._settings()
        if enabled and interval > 0 and self._is_due(interval):
            self._bar.show_due(interval)
        else:
            self._bar.hide_bar()

    def _is_due(self, interval: int) -> bool:
        obj = self._mw.session.current_object
        if not isinstance(obj, self._object_cls) or is_twin_name(obj.name):
            return False
        if twin_exists(obj.working_dir, obj.name):
            return False
        n = primary_object_count(obj.working_dir)
        # Heuristic kept deliberately simple: the bar shows whenever the box
        # holds a multiple-of-interval real pieces and the current piece has
        # no twin yet. In the normal front-to-back capture flow this lands on
        # the piece that just crossed the threshold; creating the twin (or
        # moving to the next piece) clears it. See module docstring.
        return n > 0 and n % interval == 0

    def create_twin(self) -> None:
        obj = self._mw.session.current_object
        if not isinstance(obj, self._object_cls) or is_twin_name(obj.name):
            return
        # Reuse the normal object-creation path: it makes the dirs, refuses
        # duplicates, and switches the session to the new object — which fires
        # current_object_changed → refresh() → the bar hides (a twin is not a
        # primary). No live-view rotation: the operator rotates physically.
        self._mw.start_object(obj.name + TWIN_SUFFIX)


def install_rotated_sample_nudge(main_window, object_cls) -> None:
    """Wire the entire feature. Call once from ``MainWindow.__init__`` after
    the UI is loaded and the session exists. This is the ONLY entry point —
    deleting the call (and this file) removes the feature cleanly.

    ``object_cls`` MUST be the ``Object`` class as seen by the caller
    (``papyri/main.py``). The app runs as ``python -m papyri.main`` → that
    module is ``__main__`` and its ``Object`` is ``__main__.Object``; a
    ``from papyri.main import Object`` here would load a second module copy
    with a *different* ``Object`` class, so isinstance would never match.
    Passing it in sidesteps the dual-module trap entirely."""
    bar = RotatedSampleNudgeBar(main_window)
    right_layout = main_window.findChild(QVBoxLayout, "rightLayout")
    if right_layout is None:
        _logger.warning("rightLayout not found — rotated-sample nudge disabled")
        return
    # Insert directly under the title row (rightLayout item 0 == topRow).
    right_layout.insertWidget(1, bar)

    controller = _NudgeController(main_window, bar, object_cls)
    bar.create_twin_requested.connect(controller.create_twin)
    main_window.session.current_object_changed.connect(controller.refresh)
    # Strong ref so the plain-object controller isn't garbage-collected.
    main_window._rotated_sample_nudge = controller
    controller.refresh()
