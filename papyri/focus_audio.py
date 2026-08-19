"""Auditory focus aid for manual focusing (e.g. IR, which can't autofocus).

Maps the live-view sharpness to a continuous pitch — sharper = higher — so
the focus peak can be found by ear. Absolute sharpness is meaningless across
cameras/subjects, so the pitch tracks a value's relative position in an
adaptive reference (a slow-decaying peak of the best-seen sharpness);
`reset()` it whenever the camera, live-view session, magnification or
exposure changes.

Intended use (the reference starts empty, so the first sweep calibrates):
sweep once through the focus range until the tone clearly falls, then turn
back toward the highest pitch — a fluttering tone (tremolo) confirms the
peak. Until that first fall-off the pitch sits near the top whenever the
current frame is the best seen so far; the tremolo is suppressed until the
range has actually been explored, so "flutter" never fires on a cold start.

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
    """Pull-mode QIODevice: a mono int16 tone whose frequency, amplitude and
    tremolo depth glide toward set-points. Phase carries across buffers and
    everything ramps within each buffer, so no parameter change clicks.

    Timbre is a sine plus a quieter 2nd harmonic: a pure sine is hard to
    track by ear (room modes make its loudness swing with head position);
    the harmonic anchors the pitch percept. Tremolo is a ~6 Hz amplitude
    flutter used as the categorical "at the peak" confirmation."""

    _HARMONIC_MIX = 0.3
    _TREMOLO_HZ = 6.0

    def __init__(self, sample_rate: int, parent=None):
        super().__init__(parent)
        self._sr = sample_rate
        self._phase = 0.0
        self._trem_phase = 0.0
        self._freq = 440.0
        self._target_freq = 440.0
        self._amp = 0.0
        self._target_amp = 0.0
        self._trem_depth = 0.0
        self._target_trem_depth = 0.0

    def set_frequency(self, freq: float) -> None:
        self._target_freq = float(freq)

    def set_amplitude(self, amp: float) -> None:
        self._target_amp = float(amp)

    def set_tremolo(self, depth: float) -> None:
        """0 = steady tone, 1 = full flutter."""
        self._target_trem_depth = float(depth)

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
        # Glide freq/amp/tremolo across the buffer; the next buffer
        # continues from here.
        ramp = np.arange(1, n + 1, dtype=np.float64) / n
        freqs = self._freq + (self._target_freq - self._freq) * ramp
        amps = self._amp + (self._target_amp - self._amp) * ramp
        depths = (self._trem_depth
                  + (self._target_trem_depth - self._trem_depth) * ramp)
        phases = self._phase + np.cumsum(_TWO_PI * freqs / self._sr)
        trem_phases = (self._trem_phase
                       + _TWO_PI * self._TREMOLO_HZ / self._sr
                       * np.arange(1, n + 1, dtype=np.float64))
        tone = ((np.sin(phases) + self._HARMONIC_MIX * np.sin(2.0 * phases))
                / (1.0 + self._HARMONIC_MIX))
        gain = 1.0 - depths * 0.5 * (1.0 - np.cos(trem_phases))
        samples = (amps * gain * tone * 32767.0).astype("<i2")
        self._phase = float(phases[-1] % _TWO_PI)
        self._trem_phase = float(trem_phases[-1] % _TWO_PI)
        self._freq = float(freqs[-1])
        self._amp = float(amps[-1])
        self._trem_depth = float(depths[-1])
        return samples.tobytes()


class FocusAudio(QObject):
    """Sharpness → pitch focus tone. Drive with `set_active(bool)` (start/stop),
    `push(value)` (feed each frame's sharpness) and `reset()` (forget the range
    on camera/live-view/magnification/exposure change)."""

    # 400–1600 Hz: two octaves inside the ear's discrimination sweet spot,
    # and above the roll-off of small laptop speakers (300 Hz sine is nearly
    # gone on those, and equal-loudness makes it sound quieter still — the
    # tone would fade out exactly when defocused).
    _F_LOW = 400.0
    _F_HIGH = 1600.0
    _VOLUME = 0.22
    # Pitch is perceived logarithmically, so map norm to log-frequency
    # (equal musical intervals per sharpness step); the >1 exponent spreads
    # the top of the band, where the hunt actually happens, across a larger
    # interval (2.0: the last 4% below the reference spans ~3 semitones —
    # a "nearly there vs. there" difference must be obvious, per field
    # feedback; the bottom keeps ~0.5 semitone per 5%, coarse but audible).
    _CURVE_GAMMA = 2.0
    # Sigma refinement near the top (see the mapping in push). Deliberately
    # permissive (allowance 1, ~1 semitone per sqrt-sigma): field testing
    # showed differences barely above the noise must be audible, at the
    # cost of some resting-pitch warble — the tremolo still marks "at the
    # top" unambiguously.
    _SIGMA_FRAC = 0.4      # smooth's residual sigma ≈ this x diff-median (measured)
    _ST_PER_SIGMA = 1.0    # semitones below the ceiling per sqrt(sigma deficit)
    _SIGMA_ALLOWANCE = 1.0 # deficit (in sigmas) that still counts as "at best"
    # Input smoothing (per 20 Hz frame): a short median kills single-frame
    # spikes, then one fixed light EMA. Deliberately simple — an adaptive
    # move-vs-noise smoother was tried and made small adjustments near the
    # focus peak feel seconds-late (they hide below any statistical
    # deadband); a predictable ~0.4 s total latency won in field testing.
    _MEDIAN_WINDOW = 5     # 250 ms @ 20 Hz, ~2 frames of added lag
    _SMOOTH_ALPHA = 0.2    # fixed EMA (~0.25 s)
    # Noise scale = median of the raw frame-to-frame differences over a
    # sliding window: move-insensitive BY CONSTRUCTION (even a fast focus
    # pull adds little per-frame change compared to the sensor wobble it
    # rides on), so it needs no gating and no tuning for sensor-noise
    # levels we can't test (IR live view).
    _DIFF_WINDOW = 40      # 2 s of raw diffs
    # The peak reference settles above the level the smoothed value wanders
    # around (it ratchets on the wander's upward excursions), which would
    # park the held-in-focus ratio just below the tremolo band. Compare
    # against the peak minus a margin scaled to the diff-median noise
    # scale, which grows exactly when the wander does. Larger factors
    # plateau the top of the band and confirm focus well below the real
    # peak; smaller ones leave the tremolo flapping. Capped so a
    # pathological estimate can't hollow out the reference.
    _PEAK_NOISE_MARGIN = 0.4  # in units of the diff-median noise scale
    _PEAK_MARGIN_CAP = 0.3    # fraction of peak
    _LEVEL_ALPHA = 0.05    # the sustained-level probe feeding the peak
    # Peak decay ~30 s: bucket changes already reset() the reference, so the
    # decay only tracks slow drift (light, gain). Anything faster erodes the
    # reference while the user hunts in defocus and the pitch rises without
    # the image getting sharper — indistinguishable, by ear, from focus.
    _PEAK_DECAY = 0.0015
    _FLOOR_RATIO = 0.5     # sharpness <= this fraction of peak -> lowest pitch
    # "At the peak" confirmation: tremolo with hysteresis, gated on the
    # range having been explored once (see _CALIBRATED_BELOW) — otherwise a
    # cold start (peak == first frame, ratio == 1) would flutter immediately.
    _CALIBRATED_BELOW = 0.8
    _TREMOLO_ON = 0.97
    _TREMOLO_OFF = 0.93
    _TREMOLO_DEPTH = 0.6

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sr = 44100
        self._sink = None
        self._gen: Optional[_ToneGenerator] = None
        self._playing = False
        self._window: deque[float] = deque(maxlen=self._MEDIAN_WINDOW)
        self._smooth: Optional[float] = None
        self._level: Optional[float] = None
        self._diffs: deque[float] = deque(maxlen=self._DIFF_WINDOW)
        self._prev_raw: Optional[float] = None
        self._peak: Optional[float] = None
        self._calibrated = False
        self._tremolo = False

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
        """Forget the adaptive reference (call on camera/live-view/
        magnification/exposure change)."""
        self._window.clear()
        self._smooth = None
        self._level = None
        self._diffs.clear()
        self._prev_raw = None
        self._peak = None
        self._calibrated = False
        self._tremolo = False
        if self._gen is not None:
            self._gen.set_tremolo(0.0)

    def push(self, value: Optional[float]) -> Optional[float]:
        """Feed one frame's sharpness. Returns the smoothed value — the
        readout label shows this instead of the raw wobble, so ear and eye
        agree. The filtering runs whenever frames are fed (so the readout is
        steady even with the tone off); only the tone output is gated."""
        if value is None:
            return None
        # Median (spike killer), then the noise-adaptive EMA — see the
        # constants block. This plus the generator's ~80 ms per-buffer glide
        # is the whole smoothing chain; a fixed heavy low-pass instead would
        # add lag on genuine moves, and lag means overshooting the peak.
        self._window.append(float(value))
        med = float(np.median(self._window))
        if self._prev_raw is not None:
            self._diffs.append(abs(float(value) - self._prev_raw))
        self._prev_raw = float(value)
        noise = float(np.median(self._diffs)) if self._diffs else 0.0
        bootstrap = len(self._diffs) < 10  # noise scale not settled yet
        if self._smooth is None:
            self._smooth = med
        else:
            self._smooth += (med - self._smooth) * self._SMOOTH_ALPHA
        v = self._smooth
        if not self._playing or self._gen is None:
            return v
        # Best-seen sharpness = running max of the SUSTAINED level, decaying
        # slowly. The level is a second, symmetric EMA over quasi-stationary
        # frames only (re-seeded after each detected move): unlike ratcheting
        # on v directly — whose upward excursions bias any rise-rate high or
        # learn too slowly — the level is unbiased and so smooth that its
        # maximum sits within ~1 sigma of the true sustained best, which the
        # sigma scale below depends on. A stable reference also keeps a
        # held-in-focus frame at a steady pitch — unlike a min/max range,
        # which collapses onto the noise band once you stop.
        sigma = self._SIGMA_FRAC * noise
        if bootstrap:
            self._level = None  # noise scale (and thus the seed) not settled
        else:
            # Seed BELOW the current value: a seed is a near-single-frame
            # estimate, and the running max would keep its upward error for
            # good — approaching the true level from below costs ~1 s and
            # no accuracy.
            self._level = (v - 2.0 * sigma if self._level is None
                           else self._level + (v - self._level) * self._LEVEL_ALPHA)
            if self._peak is None or self._level > self._peak:
                self._peak = self._level
            elif self._peak - v < max(10.0 * sigma, 0.02 * self._peak):
                # Near the peak: relax onto the sustained level.
                self._peak += (self._level - self._peak) * self._PEAK_DECAY
            else:
                # Far below (hunting in defocus): a big gap makes even the
                # slow decay erode absolute units fast — near-freeze it; a
                # parked reference says more about the best achievable than
                # anything measured way down here.
                self._peak += (self._level - self._peak) * (self._PEAK_DECAY * 0.05)
        # No reference yet (bootstrap / moving since reset): map against the
        # current value itself, WITHOUT storing it — a stored single-frame
        # peak would anchor the reference on a noise excursion for good.
        peak = self._peak if self._peak is not None else v
        if peak <= 0:
            return v
        margin = min(self._PEAK_NOISE_MARGIN * noise,
                     self._PEAK_MARGIN_CAP * peak)
        ratio = v / (peak - margin)
        # The range counts as explored once the tone has clearly fallen off
        # the peak — only then can "back at the top" mean anything.
        if ratio < self._CALIBRATED_BELOW and not bootstrap:
            self._calibrated = True
        # Pitch, two regimes (seamed monotonically via min/max):
        # - Global ratio sweep: the top band [_FLOOR_RATIO, 1] maps to
        #   [_F_LOW, _F_HIGH] — coarse far-field orientation (defocus = low).
        # - Sigma refinement near the top: the deficit below the learned
        #   best, in units of the smoothed value's own residual sigma, drops
        #   the pitch by _ST_PER_SIGMA x sqrt(deficit) semitones — sensitive
        #   for the first sigmas, compressive further out, so it grades the
        #   whole near-field without ever plateauing. This makes exactly the
        #   differences audible that are statistically real: on a clean feed
        #   a fraction of a percent below the best is a clear step down, on
        #   a noisy one only a genuine deficit is — the ratio scale alone
        #   compressed all of this into one plateau at the ceiling. The
        #   allowance keeps the resting tone latched at _F_HIGH; min() hands
        #   over to the ratio sweep once it is the lower of the two.
        norm = (ratio - self._FLOOR_RATIO) / (1.0 - self._FLOOR_RATIO)
        norm = min(1.0, max(0.0, norm))
        freq = self._F_LOW * (self._F_HIGH / self._F_LOW) ** (norm ** self._CURVE_GAMMA)
        if sigma > 0:
            over = max(0.0, (peak - v) / sigma - self._SIGMA_ALLOWANCE)
            st_below = self._ST_PER_SIGMA * math.sqrt(over)
            freq = min(freq, self._F_HIGH * 2.0 ** (-st_below / 12.0))
        self._gen.set_frequency(freq)
        if not self._calibrated:
            self._tremolo = False
        elif not self._tremolo and ratio >= self._TREMOLO_ON:
            self._tremolo = True
        elif self._tremolo and ratio < self._TREMOLO_OFF:
            self._tremolo = False
        self._gen.set_tremolo(self._TREMOLO_DEPTH if self._tremolo else 0.0)
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
