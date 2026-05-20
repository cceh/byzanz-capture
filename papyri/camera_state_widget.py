"""Per-camera connection status widget.

Shows: side badge (VIS/IR), camera state icon, status text, busy spinner,
icon-only connect/disconnect buttons. Bound to a single CameraWorker via
`bind_worker(worker, side_label, profile)` — papyri instantiates one for
the visible camera and one for the IR camera.

State transitions render automatically via worker.state_changed; main.py's
orchestration (auto-connect, auto-reconnect, capture button gating) lives
elsewhere.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy

from byzanz_camera.camera_worker import CameraStates
from byzanz_camera.helpers import get_ui_path, set_state
from byzanz_camera.spinner import Spinner

if TYPE_CHECKING:
    from byzanz_camera.camera_worker import CameraWorker
    from byzanz_camera.profiles.base import Profile


class CameraStateWidget(QFrame):
    """One camera's connection status panel.

    Layout (horizontal):
        [VIS badge] [icon] [state text] [spinner] [connect btn] [disconnect btn]

    Optional `set_emphasized(True)` highlights the whole pill with a 2px
    spectrum-colored border — used by main.py to mark "this camera is the
    active one for capture / live view".
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("cameraStateWidget")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

        self._worker: "CameraWorker | None" = None
        self._profile: "Profile | None" = None
        self._side_label: str = ""
        self._emphasized: bool = False

        self._build_ui()
        self._refresh_chrome()

    # ---- public API -------------------------------------------------

    def bind_worker(
        self, worker: "CameraWorker", side_label: str, profile: "Profile"
    ) -> None:
        """Hook this widget to a worker.
        `side_label` is 'VIS' or 'IR' (drives the colored badge);
        `profile` is what gets emitted when the user clicks Connect."""
        self._worker = worker
        self._profile = profile
        self._side_label = side_label

        self._side_badge.setText(side_label)
        # Spectrum identity is property-driven against the host
        # stylesheet — `#cameraSideBadge[spectrum="VIS"]` etc.
        set_state(self._side_badge, "spectrum", side_label)
        set_state(self, "spectrum", side_label)

        worker.state_changed.connect(self._on_state)
        # Initial paint matches the worker's "just initialized" state.
        self._on_state(CameraStates.Waiting())
        self._refresh_chrome()

    def set_emphasized(self, emphasized: bool) -> None:
        """When True, draws a 2px spectrum-colored border around the pill so
        the user can tell at a glance which camera is the active one."""
        if self._emphasized == emphasized:
            return
        self._emphasized = emphasized
        self._refresh_chrome()

    def _refresh_chrome(self) -> None:
        """Apply the (possibly-emphasized) outer border via property
        selectors in the host stylesheet — see
        `#cameraStateWidget[emphasized="true"][spectrum="VIS"]` etc."""
        set_state(self, "emphasized", self._emphasized)

    # ---- ui construction -------------------------------------------

    def _build_ui(self) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(10, 4, 10, 4)
        outer.setSpacing(8)

        self._side_badge = QLabel("?")
        self._side_badge.setObjectName("cameraSideBadge")
        self._side_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self._side_badge, 0, Qt.AlignmentFlag.AlignVCenter)

        self._icon = QLabel()
        self._icon.setFixedSize(28, 28)
        self._icon.setScaledContents(True)
        self._icon.setPixmap(QPixmap(get_ui_path("ui/camera_waiting.png")))
        outer.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignVCenter)

        self._text = QLabel("Searching…")
        self._text.setWordWrap(True)
        self._text.setTextFormat(Qt.TextFormat.RichText)
        self._text.setSizePolicy(
            QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Preferred
        )
        outer.addWidget(self._text, 1)

        self._spinner = Spinner(self)
        self._spinner.setFixedSize(20, 20)
        self._spinner.isAnimated = False
        outer.addWidget(self._spinner, 0, Qt.AlignmentFlag.AlignVCenter)

        self._connect_btn = self._make_icon_button(
            "ui/camera-change.svg", "Connect"
        )
        self._connect_btn.clicked.connect(self._on_connect_clicked)
        outer.addWidget(self._connect_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self._disconnect_btn = self._make_icon_button(
            "ui/disconnect-camera.svg", "Disconnect"
        )
        self._disconnect_btn.clicked.connect(self._on_disconnect_clicked)
        outer.addWidget(self._disconnect_btn, 0, Qt.AlignmentFlag.AlignVCenter)

    @staticmethod
    def _make_icon_button(icon_path: str, tooltip: str) -> QPushButton:
        btn = QPushButton()
        btn.setIcon(QIcon(get_ui_path(icon_path)))
        btn.setIconSize(QSize(18, 18))
        btn.setFixedSize(30, 30)
        btn.setToolTip(tooltip)
        return btn

    # ---- state -> rendering ----------------------------------------

    def _on_state(self, state) -> None:
        if isinstance(state, CameraStates.Waiting):
            self._set_icon("camera_waiting.png")
            self._text.setText("Searching for camera…")
            self._spinner.isAnimated = True
            self._connect_btn.setVisible(False)
            self._disconnect_btn.setVisible(False)
        elif isinstance(state, CameraStates.Found):
            self._set_icon("camera_waiting.png")
            self._text.setText(f"Found: <b>{state.camera_name}</b>")
            self._spinner.isAnimated = True
            self._connect_btn.setVisible(False)
            self._disconnect_btn.setVisible(False)
        elif isinstance(state, CameraStates.Connecting):
            self._text.setText(f"Connecting…<br><b>{state.camera_name}</b>")
            self._spinner.isAnimated = True
            self._connect_btn.setVisible(False)
            self._disconnect_btn.setVisible(False)
        elif isinstance(state, CameraStates.Disconnecting):
            self._text.setText("Disconnecting…")
            self._spinner.isAnimated = True
            self._connect_btn.setVisible(False)
            self._disconnect_btn.setVisible(True)
            self._disconnect_btn.setEnabled(False)
        elif isinstance(state, CameraStates.Disconnected):
            self._set_icon("camera_not_ok.png")
            name = state.camera_name or ""
            self._text.setText(f"Disconnected: <b>{name.replace("Corporation ", "")}</b>")
            self._spinner.isAnimated = False
            self._connect_btn.setVisible(True)
            self._connect_btn.setEnabled(True)
            self._disconnect_btn.setVisible(False)
        elif isinstance(state, CameraStates.ConnectionError):
            self._set_icon("camera_not_ok.png")
            self._text.setText(f"Error: {state.error}")
            self._spinner.isAnimated = False
            self._connect_btn.setVisible(True)
            self._connect_btn.setEnabled(True)
            self._disconnect_btn.setVisible(False)
        else:
            # Ready / LiveView* / Capture* / Focus* — all "connected and idle/working" states.
            name = self._worker.camera_name if self._worker and self._worker.camera_name else ""
            self._set_icon("camera_ok.png")
            self._text.setText(f"Connected:<br><b>{name.replace("Corporation ", "")}</b>")
            self._spinner.isAnimated = False
            self._connect_btn.setVisible(False)
            self._disconnect_btn.setVisible(True)
            self._disconnect_btn.setEnabled(True)

    def _set_icon(self, name: str) -> None:
        self._icon.setPixmap(QPixmap(get_ui_path(f"ui/{name}")))

    # ---- button handlers -------------------------------------------

    def _on_connect_clicked(self) -> None:
        if self._worker and self._profile:
            self._worker.commands.connect_camera.emit(self._profile)

    def _on_disconnect_clicked(self) -> None:
        if self._worker:
            self._worker.commands.disconnect_camera.emit()
