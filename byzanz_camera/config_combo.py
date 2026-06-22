"""ConfigComboBox — a QComboBox bound to one gphoto2 config property.

Both the RTI app and papyri drive a row of capture-setting combos
(ISO / aperture / shutter / format) from the camera's live config, which
is re-emitted ~every 0.5s by the worker's poll loop. The old approach
cleared and rebuilt each combo on every emit; with a dropdown open,
`clear()` + `addItem()` tears the popup down, so picking a value was
fiddly ("frickelig").

This widget instead diff-updates:
  - it rebuilds the item list ONLY when the choice set actually changed
    (rare — e.g. the camera's mode dial changes what's available);
  - it moves the selection ONLY when it differs AND the popup isn't open,
    so a poll never yanks the dropdown out from under the user.

There is no retained "is the popup open" flag (which could desync if an
event were missed): the guard reads Qt's live popup state
(`view().isVisible()`) at the moment of the update. A swallowed poll is
self-corrected by the next one.

`value_chosen(name, value)` fires only on a genuine USER selection
(programmatic updates are wrapped in `blockSignals`). The host wires it
to the worker's `set_single_config` — papyri to its active worker, the
RTI app to its single worker — so this widget stays worker-agnostic.
"""
from __future__ import annotations

from typing import Optional

import gphoto2 as gp
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QComboBox


class ConfigComboBox(QComboBox):
    value_chosen = pyqtSignal(str, str)          # (config property name, chosen value)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._name: Optional[str] = None         # property currently bound
        self._settable: bool = False
        self.currentIndexChanged.connect(self._on_user_change)

    # ---- public API ----------------------------------------------------

    def update_from_config(self, config, name: str, value_map: dict | None = None) -> bool:
        """Sync items + selection to property `name` in `config` (anything
        with `get_child_by_name` — a CameraWidget or PseudoConfig).

        Missing / empty / read-only widget → cleared and reported as not
        settable. Returns whether the property is settable (present,
        non-empty, writable)."""
        self._name = name
        try:
            cfg = config.get_child_by_name(name)
            choices = list(cfg.get_choices())
            current = cfg.get_value()
            readonly = bool(cfg.get_readonly())
        except (gp.GPhoto2Error, KeyError):
            self.clear_binding()                 # widget absent on this body
            return False
        if not choices:
            self.clear_binding()
            return False

        desired = [((value_map or {}).get(c, c), c) for c in choices]
        have = [(self.itemText(i), self.itemData(i)) for i in range(self.count())]
        blocked = self.blockSignals(True)
        try:
            if desired != have:                  # choices changed → rebuild (rare)
                self.clear()
                for label, data in desired:
                    self.addItem(label, data)
            # Don't fight an open dropdown: only re-select when the popup
            # is closed (the next poll re-syncs once the user is done).
            if not self.view().isVisible():
                idx = self.findData(current)
                if idx >= 0 and idx != self.currentIndex():
                    self.setCurrentIndex(idx)
        finally:
            self.blockSignals(blocked)

        self._settable = not readonly
        return self._settable

    def is_settable(self) -> bool:
        return self._settable

    def clear_binding(self) -> None:
        """Empty the combo and mark it non-settable (no property available)."""
        self._settable = False
        blocked = self.blockSignals(True)
        self.clear()
        self.blockSignals(blocked)

    # ---- internals -----------------------------------------------------

    def _on_user_change(self, _idx: int) -> None:
        # Programmatic updates above are blockSignals'd, so this only fires
        # on a genuine user pick. currentData() is the gphoto2 value.
        if self._name and self._settable:
            data = self.currentData()
            if data is not None:
                self.value_chosen.emit(self._name, data)
