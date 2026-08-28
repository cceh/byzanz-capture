"""Auditory focus aid for manual focusing (e.g. IR, which can't autofocus).

Maps the live-view sharpness to a pitch so the focus optimum can be found by
ear. The familiar top tone represents the best sustained sharpness confirmed
in this run; the tone sinks below it as focus moves away. A band above the top
is reserved for a statistically meaningful candidate improvement while the
fast value is ahead of the sustained reference. Once that improvement has
settled, it becomes the new confirmed best and maps to the familiar top.

After a short neutral acquisition phase, the confirmed best is monotonic
within a run: an unchanged or worse image can never improve merely because
the reference decayed. Noise only defines the minimum excess required to
enter the above-top band; it does not reshape the whole pitch scale. The
noise estimate uses raw second differences, which cancel a constant-rate
focus movement instead of classifying its slope as noise. `reset()` starts a
new run — call it whenever the camera, live-view session, magnification or
exposure changes.

QtMultimedia is optional: if it's missing, `AUDIO_AVAILABLE` is False and
every method is a no-op, so the feature just hides itself.
"""
from __future__ import annotations

import math
from collections import deque
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
    """Pull-mode QIODevice: a mono int16 tone whose frequency and amplitude
    glide toward set-points. Phase carries across buffers and both ramp
    within each buffer, so no parameter change clicks.

    Timbre is a sine plus a quieter 2nd harmonic: a pure sine is hard to
    track by ear (room modes make its loudness swing with head position);
    the harmonic anchors the pitch percept."""

    _HARMONIC_MIX = 0.15

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
        # Glide freq+amp across the buffer; the next buffer continues here.
        ramp = np.arange(1, n + 1, dtype=np.float64) / n
        freqs = self._freq + (self._target_freq - self._freq) * ramp
        amps = self._amp + (self._target_amp - self._amp) * ramp
        phases = self._phase + np.cumsum(_TWO_PI * freqs / self._sr)
        tone = ((np.sin(phases) + self._HARMONIC_MIX * np.sin(2.0 * phases))
                / (1.0 + self._HARMONIC_MIX))
        samples = (amps * tone * 32767.0).astype("<i2")
        self._phase = float(phases[-1] % _TWO_PI)
        self._freq = float(freqs[-1])
        self._amp = float(amps[-1])
        return samples.tobytes()


class FocusAudio(QObject):
    """Sharpness → pitch focus tone. Drive with `set_active(bool)` (start/stop),
    `push(value)` (feed each frame's sharpness) and `reset()` (start a new run
    on camera/live-view/magnification/exposure change)."""

    # Below the confirmed best: freq = _F_TOP * (v / best)^_G.
    # _F_TOP is deliberately NOT the top of the range: the band above it is
    # reserved for a significant candidate improvement. Keep the range
    # compact enough for sustained listening on small computer speakers.
    _F_TOP = 1200.0
    _F_MAX = 1500.0
    _F_MIN = 400.0
    _VOLUME = 0.14
    # Fixed pitch scale: noise controls confidence, never the overall mapping.
    # A fixed G means the same sharpness ratio sounds the same after every
    # reset, independent of whether the operator moved during startup.
    _G = 2.0
    # Input smoothing (per 20 Hz frame): median-3 rejects an isolated spike
    # with only ~1 frame of lag. The following EMA reacts fast to movement but
    # falls back to stronger smoothing inside the measured noise band, so a
    # held focus does not pay for the lower latency with constant pitch warble.
    _MEDIAN_WINDOW = 3
    _SMOOTH_ALPHA_IDLE = 0.2
    _SMOOTH_ALPHA_MOVING = 0.5
    _SMOOTH_MOTION_SIGMA = 2.0
    _SMOOTH_MOTION_REL = 0.002  # minimum motion threshold, fraction of level
    # Noise scale = median absolute raw SECOND difference over a sliding
    # window. x[t] - 2*x[t-1] + x[t-2] cancels a constant slope, so turning
    # the focus ring at a steady rate does not inflate the estimate merely
    # because the sharpness value is moving. The factor converts its larger
    # raw-noise scale back to the smoothed value's approximate residual sigma.
    _NOISE_WINDOW = 40     # 2 s @ 20 Hz
    _NOISE_BOOTSTRAP = 6   # suppress above-top claims until minimally settled
    _SIGMA_FRAC = 0.23
    _HEADROOM_SIGMA = 2.0  # excess required before "new best" is claimed
    # Do not let focus-curve acceleration or a direction reversal inflate the
    # noise estimate. Once a baseline exists, reject a strong curvature and a
    # few following samples while the movement settles.
    _NOISE_GUARD_MIN = 5
    _NOISE_CURVATURE_FACTOR = 4.0
    _NOISE_DIRECTION_FACTOR = 2.0
    _NOISE_FREEZE_FRAMES = 3
    # Sustained candidate feeding the monotonic confirmed best.
    _LEVEL_ALPHA = 0.05
    # A genuine improvement may initially use the headroom band, but should
    # not remain referenced to a badly undersampled fast-sweep best. After a
    # short sustained excess, let the candidate catch up quickly.
    _HEADROOM_CONFIRM_FRAMES = 3
    _LEVEL_ALPHA_ADOPT = 0.3
    # Asymptotic headroom compression: approaches _F_MAX but never creates a
    # hard plateau where several better values become exactly the same pitch.
    _HEADROOM_MAX_ST = 12.0 * math.log2(_F_MAX / _F_TOP)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sr = 44100
        self._sink = None
        self._gen: Optional[_ToneGenerator] = None
        self._playing = False
        self._window: deque[float] = deque(maxlen=self._MEDIAN_WINDOW)
        self._smooth: Optional[float] = None
        self._level: Optional[float] = None
        self._second_diffs: deque[float] = deque(maxlen=self._NOISE_WINDOW)
        self._prev_raw: Optional[float] = None
        self._prev_delta: Optional[float] = None
        self._noise_freeze = 0
        self._peak: Optional[float] = None
        self._headroom_frames = 0
        self._adopting_best = False

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
        """Start a new run (call on camera/live-view/magnification/exposure
        change): forget the confirmed reference and the noise state."""
        self._window.clear()
        self._smooth = None
        self._level = None
        self._second_diffs.clear()
        self._prev_raw = None
        self._prev_delta = None
        self._noise_freeze = 0
        self._peak = None
        self._headroom_frames = 0
        self._adopting_best = False

    def push(self, value: Optional[float]) -> Optional[float]:
        """Feed one frame's sharpness. Returns the smoothed value — the
        readout label shows this instead of the raw wobble, so ear and eye
        agree. The filtering runs whenever frames are fed (so the readout is
        steady even with the tone off); only the tone output is gated."""
        if value is None:
            return None
        self._window.append(float(value))
        med = float(np.median(self._window))
        raw = float(value)
        if self._prev_raw is not None:
            delta = raw - self._prev_raw
            if self._prev_delta is not None:
                curvature = abs(delta - self._prev_delta)
                baseline = (float(np.median(self._second_diffs))
                            if len(self._second_diffs) >= self._NOISE_GUARD_MIN
                            else None)
                if baseline is not None:
                    scale = max(baseline, abs(raw) * 1e-6)
                    direction_change = (
                        delta * self._prev_delta < 0.0
                        and min(abs(delta), abs(self._prev_delta))
                        > self._NOISE_DIRECTION_FACTOR * scale
                    )
                    strong_curvature = (
                        curvature > self._NOISE_CURVATURE_FACTOR * scale)
                    if direction_change or strong_curvature:
                        self._noise_freeze = self._NOISE_FREEZE_FRAMES
                if self._noise_freeze > 0:
                    self._noise_freeze -= 1
                else:
                    self._second_diffs.append(curvature)
            self._prev_delta = delta
        self._prev_raw = raw
        noise = (float(np.median(self._second_diffs))
                 if self._second_diffs else 0.0)
        noise_ready = len(self._second_diffs) >= self._NOISE_BOOTSTRAP
        if self._smooth is None:
            self._smooth = med
        else:
            sigma = self._SIGMA_FRAC * noise
            motion_threshold = max(
                self._SMOOTH_MOTION_SIGMA * sigma,
                self._SMOOTH_MOTION_REL * max(abs(self._smooth), 1.0),
            )
            moving = (not noise_ready
                      or abs(med - self._smooth) > motion_threshold)
            alpha = (self._SMOOTH_ALPHA_MOVING if moving
                     else self._SMOOTH_ALPHA_IDLE)
            self._smooth += (med - self._smooth) * alpha
        v = self._smooth
        if not self._playing or self._gen is None:
            return v
        # The sustained candidate rejects short upward excursions. During the
        # brief noise acquisition it is also the neutral reference, so a
        # single first-frame excursion cannot anchor the whole run. Once the
        # noise is minimally settled, its running maximum is the confirmed
        # best. Crucially, that confirmed best never decays.
        sigma = self._SIGMA_FRAC * noise
        allowance = self._HEADROOM_SIGMA * sigma

        # Start fast adoption only after the improvement has remained above
        # the noise allowance for several frames. Keep adopting until the
        # sustained reference has caught up or the current value falls back.
        prior_peak = self._peak
        if (noise_ready and prior_peak is not None
                and v > prior_peak + allowance):
            self._headroom_frames += 1
            if self._headroom_frames >= self._HEADROOM_CONFIRM_FRAMES:
                self._adopting_best = True
        elif not self._adopting_best:
            self._headroom_frames = 0
        if (self._adopting_best and prior_peak is not None
                and v <= prior_peak + allowance):
            self._adopting_best = False
            self._headroom_frames = 0

        level_alpha = (self._LEVEL_ALPHA_ADOPT if self._adopting_best
                       else self._LEVEL_ALPHA)
        self._level = (v if self._level is None else
                       self._level + (v - self._level) * level_alpha)
        if not noise_ready:
            self._peak = self._level
        elif self._peak is None or self._level > self._peak:
            self._peak = self._level
        peak = self._peak
        if peak <= 0 or v <= 0:
            self._gen.set_frequency(self._F_MIN)
            return v

        # Below the best, pitch follows the fixed ratio scale. Above it, enter
        # the headroom band only after clearing the noise allowance. Removing
        # that allowance from the mapped excess keeps the transition at
        # _F_TOP continuous instead of producing a jump.
        if not noise_ready:
            ratio = 1.0
        elif v <= peak:
            ratio = v / peak
        else:
            significant_excess = max(
                0.0, v - peak - allowance)
            raw_headroom_st = (
                12.0 * self._G
                * math.log2(1.0 + significant_excess / peak))
            compressed_st = self._HEADROOM_MAX_ST * (
                1.0 - math.exp(-raw_headroom_st / self._HEADROOM_MAX_ST))
            freq = self._F_TOP * 2.0 ** (compressed_st / 12.0)
            self._gen.set_frequency(freq)
            return v
        freq = self._F_TOP * ratio ** self._G
        self._gen.set_frequency(min(self._F_MAX, max(self._F_MIN, freq)))
        return v

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
        self._sink.setBufferSize(int(self._sr * 0.04) * 2)  # request ~40 ms
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
