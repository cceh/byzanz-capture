"""Empty-state widget shown in the viewer when no object is open.

Click on the CTA emits `new_object_requested`; main.py wires it to
the same handler the sidebar's `+ New object` uses.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)


class NoObjectOverlay(QWidget):
    """Card centered in the viewer when no object is loaded.

    Layout: a fixed-size card inside a stretches-on-all-sides outer
    layout — pure declarative centering, no geometry math.
    Styling: rules live in `papyri/ui/app.qss` against the
    `#noObjectPage`, `#noObjectCard`, etc. object names.
    """

    new_object_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("noObjectPage")
        # QWidget doesn't paint stylesheet backgrounds without this.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        card = QFrame()
        card.setObjectName("noObjectCard")
        # Card hugs its content; the outer page provides centering.
        card.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(40, 32, 40, 32)
        card_layout.setSpacing(10)

        title = QLabel("No object open")
        title.setObjectName("noObjectTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(title)

        subtitle = QLabel(
            "Start a new one or pick an existing one from the sidebar →"
        )
        subtitle.setObjectName("noObjectSubtitle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(subtitle)

        card_layout.addSpacing(8)

        button = QPushButton("Start new object")
        button.setObjectName("noObjectCta")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(self.new_object_requested)
        card_layout.addWidget(button, 0, Qt.AlignmentFlag.AlignCenter)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(card)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(1)
