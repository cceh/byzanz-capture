"""Auditory focus aid for manual focusing (e.g. IR, which can't autofocus).

Maps the live-view sharpness to a continuous pitch — sharper = higher — so
the focus peak can be found by ear. Absolute sharpness is meaningless across
cameras/subjects, so the pitch tracks a value's relative position in an
adaptive reference (a slow-decaying peak of the best-seen sharpness);
`reset()` it whenever the camera or live-view session changes.

QtMultimedia is optional: if it's missing, `AUDIO_AVAILABLE` is False and
every method is a no-op, so the feature just hides itself.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np
from PyQt6.QtCore import QIODevice, QObject, QTimer

try:
    from PyQt6.QtMultimedia import (
        QAudioFormat, QAudioSink, QMediaDevices,
    )
    AUDIO_AVAILABLE = True
except ImportError:
    AUDIO_AVAILABLE = False


_TWO_PI = 2.0 * math.pi


class _ToneGenerator(QIODevice):
    """Pull-mode QIODevice: a mono int16 sine whose frequency/amplitude glide
    toward set-points. Phase carries across buffers and both ramp within each
    buffer, so neither a frequency change nor start/stop clicks."""

    def __init__(self, sample_rate: int, parent=None):
        super().__init__(parent)
        self._sr = sample_rate
        self._phase = 0.0
        self._freq = 440.0
        self._target_freq = 440.0
        self._amp = 0.0
        self._target_amp = 0.0

    def set_frequency(self, freq: float) -> None:
        self._target_freq = float(freq)

    def set_amplitude(self, amp: float) -> None:
        self._target_amp = float(amp)

    # ---- QIODevice contract (pull mode) --------------------------------
    def isSequential(self) -> bool:
        return True

    def bytesAvailable(self) -> int:
        return 0x7FFFFFFF + super().bytesAvailable()  # endless: keep pulling

    def writeData(self, _data) -> int:  # read-only device
        return -1

    def readData(self, maxlen: int) -> bytes:
        n = int(maxlen) // 2  # 2 bytes per int16 sample
        if n <= 0:
            return b""
        # Glide freq+amp across the buffer; the next buffer continues from here.
        ramp = np.arange(1, n + 1, dtype=np.float64) / n
        freqs = self._freq + (self._target_freq - self._freq) * ramp
        amps = self._amp + (self._target_amp - self._amp) * ramp
        phases = self._phase + np.cumsum(_TWO_PI * freqs / self._sr)
        samples = (amps * np.sin(phases) * 32767.0).astype("<i2")
        self._phase = float(phases[-1] % _TWO_PI)
        self._freq = float(freqs[-1])
        self._amp = float(amps[-1])
        return samples.tobytes()


class FocusAudio(QObject):
    """Sharpness → pitch focus tone. Drive with `set_active(bool)` (start/stop),
    `push(value)` (feed each frame's sharpness) and `reset()` (forget the range
    on camera/live-view change)."""

    _F_LOW = 300.0
    _F_HIGH = 1100.0
    _VOLUME = 0.22
    # Smoothing / adaptation (per 20 Hz frame):
    _INPUT_ALPHA = 0.25    # low-pass on the noisy per-frame sharpness
    _PEAK_DECAY = 0.01     # slow decay of the best-seen sharpness (~5 s memory)
    _FREQ_ALPHA = 0.30     # low-pass on the output pitch
    _FLOOR_RATIO = 0.5     # sharpness <= this fraction of peak -> lowest pitch

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sr = 44100
        self._sink = None
        self._gen: Optional[_ToneGenerator] = None
        self._playing = False
        self._smooth: Optional[float] = None
        self._peak: Optional[float] = None
        self._freq_ema: Optional[float] = None

    def is_active(self) -> bool:
        return self._playing

    def set_active(self, active: bool) -> None:
        if not AUDIO_AVAILABLE:
            return
        if active and not self._playing:
            self._start()
        elif not active and self._playing:
            self._stop()

    def reset(self) -> None:
        """Forget the adaptive reference (call on camera/live-view change)."""
        self._smooth = None
        self._peak = None
        self._freq_ema = None

    def push(self, value: Optional[float]) -> None:
        if not self._playing or self._gen is None or value is None:
            return
        # Low-pass the noisy per-frame measurement so a held frame stays steady.
        self._smooth = (value if self._smooth is None
                        else self._smooth + (value - self._smooth) * self._INPUT_ALPHA)
        v = self._smooth
        # Best-seen sharpness: jump up instantly, decay slowly. A stable
        # reference keeps a held-in-focus frame at a steady pitch — unlike a
        # min/max range, which collapses onto the noise band once you stop.
        if self._peak is None or v > self._peak:
            self._peak = v
        else:
            self._peak += (v - self._peak) * self._PEAK_DECAY
        if self._peak <= 0:
            return
        # Pitch from the value's ratio to the peak; only the top band
        # [_FLOOR_RATIO, 1] maps to the sweep (defocus = low, in-focus = high).
        norm = (v / self._peak - self._FLOOR_RATIO) / (1.0 - self._FLOOR_RATIO)
        norm = min(1.0, max(0.0, norm))
        target = self._F_LOW + norm * (self._F_HIGH - self._F_LOW)
        self._freq_ema = (target if self._freq_ema is None
                          else self._freq_ema + (target - self._freq_ema) * self._FREQ_ALPHA)
        self._gen.set_frequency(self._freq_ema)

    # ---- internals -----------------------------------------------------
    def _start(self) -> None:
        device = QMediaDevices.defaultAudioOutput()
        if device is None or device.isNull():
            return  # no output device — stay idle

        fmt = QAudioFormat()
        fmt.setSampleRate(self._sr)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        if not device.isFormatSupported(fmt):
            return

        self._gen = _ToneGenerator(self._sr)
        self._gen.open(QIODevice.OpenModeFlag.ReadOnly)
        self._sink = QAudioSink(device, fmt)
        self._sink.setBufferSize(int(self._sr * 0.08) * 2)  # ~80 ms, responsive
        self._sink.start(self._gen)
        self._gen.set_amplitude(self._VOLUME)  # ramps from 0, no click
        self._playing = True
        self.reset()

    def _stop(self) -> None:
        self._playing = False
        if self._gen is not None:
            self._gen.set_amplitude(0.0)  # ramp down before stopping
        sink, gen = self._sink, self._gen
        self._sink = self._gen = None

        def _finish():
            # Keep sink+gen alive until the fade-out plays, then release.
            if sink is not None:
                sink.stop()
            if gen is not None:
                gen.close()

        QTimer.singleShot(80, _finish)
