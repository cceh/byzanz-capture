"""Overlap coach (S4 MVP) — live segment-spacing feedback for stitch buckets.

While the assistant slides an oversized fragment to the next segment
position, the coach matches the live-view frame against the LAST captured
segment (the anchor) and reads the overlap off the affine translation —
"slide until green, then shoot". The preventive counterpart to the
connectivity check in `stitching.py`, built from the same primitives
(`detect_features` / `match_pair` / `load_segment_image`), so coach-green
means "the check will rate this seam green": both use the very same
`CONFIDENCE_THRESHOLD`.

Controller in the `stitching.py` / `calibration.py` pattern: owned and
wired by main.py, which feeds it every live frame from its
`_on_preview_image` funnel (the `focus_audio.push` precedent) and sets the
anchor from the bucket snapshot on every stitch-UI refresh. Matching
(30–100 ms) never runs on the GUI thread: `push()` gates by sample
interval and keeps at most ONE match in flight — while one runs, newer
frames are simply dropped, so a fresh frame is matched as soon as the
runner frees up (latest wins; a queue would show stale directions).

Concept + state definitions: docs/papyri-overlap-coach-concept.md.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace

import cv2
import numpy as np
from PIL import Image
from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal

from byzanz_camera.load_image_worker import compute_sharpness
from papyri.stitching import (
    CONFIDENCE_THRESHOLD, UNCERTAIN_THRESHOLD,
    detect_features, load_segment_image, match_pair,
)

_logger = logging.getLogger("overlap_coach")

# The "good" overlap band (percent of the anchor's dimension along the
# dominant slide axis). Proposal from the concept doc; CALIBRATE against
# real capture sessions during the material test.
OVERLAP_MIN_PCT = 25.0
OVERLAP_MAX_PCT = 45.0
# Live-frame sampling interval — 1–2 Hz is plenty for "slide until green".
SAMPLE_INTERVAL_S = 0.6
# Blur gate: skip matching while the frame's Laplacian variance (via
# compute_sharpness, on the LIVE-frame scale — not comparable to capture
# values) is below this. Deliberately low for the material test: a sharp
# but low-contrast frame must not be gated away; motion blur during a real
# slide scores far lower than a settled frame. CALIBRATE — every reading
# is logged with its sharpness value.
SHARPNESS_MIN = 5.0

# Canonical reading-state → color map — the ONE place pill border and ghost
# tint derive from (see docs/papyri-overlap-coach-concept.md). Colors match
# the calibration/status palette in styles.COLORS; kept literal because both
# consumers paint outside QSS.
STATE_COLORS = {
    "green": "#16a34a",         # = cal_ok
    "yellow": "#f59e0b",        # = cal_due
    "red_low": "#dc2626",       # = status_error
    "red_nomatch": "#dc2626",
    "uncertain": "#94a3b8",     # slate — the check's gray zone
    "dim": None,
}

# Ghost overlay (gold standard, see concept doc): the last segment warped
# into the live frame, tinted + translucent. Green when the spacing is
# right (same signal as the pill), cyan otherwise — cyan occurs nowhere on
# papyrus/light table, so the overlay can't be mistaken for the object.
GHOST_OPACITY = 0.35
GHOST_TINT_FALLBACK = "#06b6d4"     # cyan (= the viewer's live-dot color)
# With the ghost on, sample faster — it can only be re-placed per sample,
# and a fresher ghost tracks the sliding fragment more closely. Matching
# is 30–100 ms, so 0.35 s still keeps well under half duty.
GHOST_SAMPLE_INTERVAL_S = 0.35
# A stale ghost misleads more than no ghost: hide it when no fresh
# placement arrived for this long (live-view hiccup, failed matches).
GHOST_MAX_AGE_S = 2.0


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


@dataclass(frozen=True)
class _GhostOverlay:
    """One placed ghost, precomputed for cheap per-frame blending:
    `out = frame * inv_alpha + premultiplied`. Built per SAMPLE (warp +
    tint), consumed per FRAME (one multiply-add)."""
    premultiplied: object   # float32 H×W×3 — tinted ghost × alpha
    inv_alpha: object       # float32 H×W×1 — 1 − alpha
    size: tuple[int, int]   # (w, h) of the frame it was placed for


def _make_ghost(anchor_gray: np.ndarray, h: np.ndarray,
                frame_shape: tuple[int, ...], state: str) -> _GhostOverlay:
    """Warp the anchor into the live frame's coordinates (`h` maps
    frame→anchor, so the ghost uses its inverse) and colorize it: green
    when the spacing is right (the pill's own green), cyan otherwise."""
    fh, fw = frame_shape[:2]
    m = np.linalg.inv(h)[:2]
    warped = cv2.warpAffine(anchor_gray, m, (fw, fh),
                            flags=cv2.INTER_LINEAR,
                            borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    mask = cv2.warpAffine(np.ones_like(anchor_gray, dtype=np.float32),
                          m, (fw, fh), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=0.0)
    tint = _hex_to_rgb(STATE_COLORS["green"] if state == "green"
                       else GHOST_TINT_FALLBACK)
    colorized = (warped[..., None].astype(np.float32) / 255.0
                 * np.asarray(tint, dtype=np.float32))
    alpha = (mask * GHOST_OPACITY)[..., None]
    return _GhostOverlay(premultiplied=colorized * alpha,
                         inv_alpha=1.0 - alpha, size=(fw, fh))


@dataclass(frozen=True)
class CoachReading:
    """One coach sample. `state` drives the pill (see the concept doc):
    green (shoot now) / yellow (slide further) / red_low (too little
    measured) / red_nomatch (sharp frame, no reliable match) / uncertain
    (overlap in band but confidence in the check's gray zone — the check
    would say `uncertain` too) / dim (blurred, settling)."""
    state: str
    overlap_pct: float | None = None
    confidence: float | None = None
    scale: float | None = None
    sharpness: float | None = None
    anchor_stem: str | None = None
    ghost: _GhostOverlay | None = None    # only when the ghost overlay is on
                                          # and the reading has a usable H

    @property
    def text(self) -> str:
        """Pill line. Overlap rounded to 5 % steps — the rounding
        communicates "roughly"; color + direction is the product."""
        pct = (f"~{5 * round(self.overlap_pct / 5):.0f} %"
               if self.overlap_pct is not None else "")
        return {
            "green": f"{pct} — SHOOT NOW",
            "yellow": f"{pct} — SLIDE FURTHER",
            "red_low": f"{pct} — SLIDE BACK",
            "red_nomatch": "NO OVERLAP DETECTED",
            "uncertain": f"{pct} — LOW TEXTURE, SHOOT & CHECK",
            "dim": "—",
        }[self.state]


def evaluate(confidence: float, h: np.ndarray | None,
             anchor_shape: tuple[int, ...], frame_shape: tuple[int, ...],
             sharpness: float | None, anchor_stem: str,
             ) -> CoachReading:
    """Pure state evaluation of one match result (unit-testable).
    `h` maps frame coordinates into anchor coordinates; the overlap is
    read along the dominant translation axis, in anchor units (so the
    anchor↔live scale gap cancels out)."""
    if h is None or confidence <= UNCERTAIN_THRESHOLD:
        return CoachReading(state="red_nomatch", confidence=confidence,
                            sharpness=sharpness, anchor_stem=anchor_stem)
    scale = float(np.hypot(h[0, 0], h[0, 1]))
    # Where the live frame's centre lands in the anchor → translation.
    cx, cy = frame_shape[1] / 2, frame_shape[0] / 2
    p = h @ np.array([cx, cy, 1.0])
    dx = p[0] / p[2] - anchor_shape[1] / 2
    dy = p[1] / p[2] - anchor_shape[0] / 2
    if abs(dx) >= abs(dy):
        overlap = (1.0 - abs(dx) / anchor_shape[1]) * 100
    else:
        overlap = (1.0 - abs(dy) / anchor_shape[0]) * 100
    if overlap > OVERLAP_MAX_PCT:
        state = "yellow"
    elif overlap < OVERLAP_MIN_PCT:
        state = "red_low"
    elif confidence > CONFIDENCE_THRESHOLD:
        state = "green"
    else:
        state = "uncertain"
    return CoachReading(state=state, overlap_pct=overlap,
                        confidence=confidence, scale=scale,
                        sharpness=sharpness, anchor_stem=anchor_stem)


# ---- runners -----------------------------------------------------------

class _RunnerSignals(QObject):
    """QRunnable can't host signals — QObject sidecar (see stitching.py)."""
    # gen, stem, feats, gray image (None on failure)
    anchor_ready = pyqtSignal(int, str, object, object)
    reading = pyqtSignal(int, object)                    # gen, CoachReading | None


class _AnchorRunner(QRunnable):
    """Decodes the anchor segment's embedded JPEG near the check scale and
    detects its ORB features once — reused for every live-frame match
    until the anchor changes."""

    def __init__(self, gen: int, stem: str, path: str,
                 signals: _RunnerSignals):
        super().__init__()
        self._gen, self._stem, self._path = gen, stem, path
        self._signals = signals

    def run(self) -> None:
        cv2.ocl.setUseOpenCL(False)   # locale-fragile OpenCL kernel; see _CheckRunner
        try:
            img = load_segment_image(self._path)
            if img is None:
                _logger.warning("coach anchor %s: no readable JPEG preview",
                                self._stem)
                self._signals.anchor_ready.emit(self._gen, self._stem,
                                                None, None)
                return
            feats = detect_features(img)
            # Grayscale copy kept for the ghost overlay (colorized per
            # reading state at warp time; ~0.6 MP, negligible memory).
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            self._signals.anchor_ready.emit(self._gen, self._stem,
                                            feats, gray)
        except Exception:
            _logger.exception("coach anchor %s failed", self._stem)
            self._signals.anchor_ready.emit(self._gen, self._stem, None, None)


class _FrameRunner(QRunnable):
    """Matches one live frame against the cached anchor features. Emits a
    CoachReading (or None on an internal error — the pill just keeps its
    last state; the error is logged). With `make_ghost`, a usable match
    additionally carries the warped ghost overlay."""

    def __init__(self, gen: int, frame: Image.Image, anchor_feats,
                 anchor_gray, anchor_stem: str, make_ghost: bool,
                 signals: _RunnerSignals):
        super().__init__()
        self._gen, self._frame = gen, frame
        self._anchor_feats, self._anchor_gray = anchor_feats, anchor_gray
        self._anchor_stem = anchor_stem
        self._make_ghost = make_ghost
        self._signals = signals

    def run(self) -> None:
        cv2.ocl.setUseOpenCL(False)
        try:
            sharpness = compute_sharpness(self._frame)
            if sharpness is not None and sharpness < SHARPNESS_MIN:
                reading = CoachReading(state="dim", sharpness=sharpness,
                                       anchor_stem=self._anchor_stem)
            else:
                bgr = cv2.cvtColor(
                    np.asarray(self._frame.convert("RGB")),
                    cv2.COLOR_RGB2BGR)
                confidence, h = match_pair(
                    detect_features(bgr), self._anchor_feats)
                reading = evaluate(confidence, h, self._anchor_gray.shape,
                                   bgr.shape, sharpness, self._anchor_stem)
                if self._make_ghost and h is not None and reading.overlap_pct is not None:
                    reading = replace(reading, ghost=_make_ghost(
                        self._anchor_gray, h, bgr.shape, reading.state))
            self._signals.reading.emit(self._gen, reading)
        except Exception:
            _logger.exception("coach frame match failed")
            self._signals.reading.emit(self._gen, None)


# ---- controller ----------------------------------------------------------

class OverlapCoach(QObject):
    """Anchor management + live-frame sampling. Engaged iff an anchor is
    set (`set_anchor(None, ...)` disengages — no separate active flag).
    The generation counter drops results from a superseded anchor."""

    reading_changed = pyqtSignal(object)    # CoachReading

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._gen = 0
        self._anchor_stem: str | None = None
        self._anchor_feats = None
        self._anchor_gray = None
        self._busy = False
        self._last_sample = 0.0
        self._ghost_enabled = False
        self._ghost: _GhostOverlay | None = None
        self._ghost_placed_at = 0.0
        self._signals = _RunnerSignals()
        self._signals.anchor_ready.connect(self._on_anchor_ready)
        self._signals.reading.connect(self._on_reading)

    def set_anchor(self, stem: str | None, path: str | None) -> None:
        """Anchor = the bucket's newest segment (main.py derives it from
        `stitching.snapshot_bucket`, so the reference photo is already
        excluded). None disengages the coach. Feature detection runs off
        the GUI thread; `push` no-ops until the features are ready."""
        if stem == self._anchor_stem:
            return
        self._gen += 1
        self._anchor_stem = stem
        self._anchor_feats = self._anchor_gray = None
        self._ghost = None
        if stem is None or path is None:
            return
        _logger.info("coach anchor → %s", stem)
        QThreadPool.globalInstance().start(
            _AnchorRunner(self._gen, stem, path, self._signals))

    def set_ghost_enabled(self, enabled: bool) -> None:
        """Toggle the translucent previous-segment overlay (stitch bar's
        "Ghost" button). Also raises the sample rate while on — the ghost
        is only as fresh as the latest sample."""
        self._ghost_enabled = enabled
        if not enabled:
            self._ghost = None

    def blend_ghost(self, spectrum: str, frame: Image.Image) -> Image.Image:
        """Live-frame filter (registered via MainWindow's
        `add_live_frame_filter`): blend the cached ghost onto a live frame.
        Returns the frame untouched when the ghost is off, unplaced, stale
        (GHOST_MAX_AGE_S), or was placed for a different frame size."""
        ghost = self._ghost
        if ghost is None or not self._ghost_enabled:
            return frame
        if time.monotonic() - self._ghost_placed_at > GHOST_MAX_AGE_S:
            return frame
        if frame.size != ghost.size:
            return frame
        arr = np.asarray(frame.convert("RGB"), dtype=np.float32)
        out = arr * ghost.inv_alpha + ghost.premultiplied
        return Image.fromarray(out.astype(np.uint8))

    def push(self, frame: Image.Image) -> None:
        """Feed one live frame (GUI thread, every frame — the
        `focus_audio.push` pattern). Gates internally: engaged + anchor
        ready + sample interval + one match in flight."""
        if self._anchor_feats is None or self._busy:
            return
        now = time.monotonic()
        interval = (GHOST_SAMPLE_INTERVAL_S if self._ghost_enabled
                    else SAMPLE_INTERVAL_S)
        if now - self._last_sample < interval:
            return
        self._last_sample = now
        # The camera worker hands over a LAZY PIL JPEG (pixels not decoded
        # yet). Force the decode NOW, before sharing the frame with the
        # runner: PIL's lazy load closes the underlying file pointer when it
        # completes, so two threads loading the same image race on it and
        # BOTH crash (AssertionError in JpegImagePlugin.load_read — the
        # runner and the viewer's ImageQt). After load() the pixel data is
        # materialized and cross-thread reads are safe. No extra cost: the
        # viewer decodes this same frame right after anyway.
        frame.load()
        self._busy = True
        QThreadPool.globalInstance().start(
            _FrameRunner(self._gen, frame, self._anchor_feats,
                         self._anchor_gray, self._anchor_stem,
                         self._ghost_enabled, self._signals))

    # ---- internals -------------------------------------------------------

    def _on_anchor_ready(self, gen: int, stem: str, feats, gray) -> None:
        if gen != self._gen:
            return  # anchor changed while detecting — drop
        self._anchor_feats = feats
        self._anchor_gray = gray

    def _on_reading(self, gen: int, reading) -> None:
        self._busy = False
        if gen != self._gen or reading is None:
            return
        # Ghost cache: a usable placement replaces it; a no-transform
        # reading (nomatch/dim) clears it — a stale ghost would mislead.
        if reading.ghost is not None:
            self._ghost = reading.ghost
            self._ghost_placed_at = time.monotonic()
        elif reading.state in ("red_nomatch", "dim"):
            self._ghost = None
        _logger.info(
            "coach %s overlap=%s conf=%s scale=%s sharp=%s anchor=%s",
            reading.state,
            None if reading.overlap_pct is None else f"{reading.overlap_pct:.0f}%",
            None if reading.confidence is None else f"{reading.confidence:.2f}",
            None if reading.scale is None else f"{reading.scale:.3f}",
            None if reading.sharpness is None else f"{reading.sharpness:.0f}",
            reading.anchor_stem)
        self.reading_changed.emit(reading)
