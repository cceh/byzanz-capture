"""The 1 cm scale card: where it lies in a frame, and what its ruler says.

The card is a 20 × 10 mm white tile with an eleven-tick 1 mm comb and a "1 cm" label. It
is found the way a barcode reader finds a barcode — without knowing the capture height,
without a picture of the card:

  1. localise   OpenCV's 1D-barcode localiser (`cv2.barcode.BarcodeDetector.detect`, a
                gradient-coherence search for dense parallel edges; nothing is decoded) on
                every level of an image pyramid, with its box-filter scales and edge
                threshold set for a comb of a few hundred pixels. One to three candidates
                per frame, next to none on frames without a card. Each candidate: a quad,
                its long axis is the comb's axis.
  2. period     the comb rhythm from the autocorrelation of a brightness profile along the
                candidate's axis — the scale in px/mm comes from the pattern itself.
  3. lattice    the tick minima on that profile, located to sub-pixel precision and fitted
                to a constant pitch. LATTICE_MIN ticks on one lattice, sitting on it to
                within LATTICE_RESIDUAL of the pitch, IS the card; the fit's slope is the
                pitch (px/mm, better than 0.1 %). On the corpus this test admitted every
                card and no fibre, text or plate pattern.
  4. mask       the card face for the exclusion mask: MASK_MM along the comb axis and
                across it, around the comb's centre — the label sits on one side of the
                comb and the box covers it either way.

Corpus 2026-09-08 (8948 frames, benchmark experiments/scale_bench/): 4767 of 4777 cards
confirmed present, zero false alarms on 3751 card-less frames (the residual gate removed
the last one, parallel fibres), the pitch within 0.1 % (median) of the previous comb
measurement. Misses: cards at the black glass-frame edge,
one under the papyrus, one of the older abraded-print card in IR. The height tag is no
longer an input; a pitch that contradicts the tag is a capture-side finding (77 frames in
the corpus carried a wrong one).
"""
from __future__ import annotations

import cv2
import numpy as np

# The localiser looks for dense parallel edges in box filters whose size is a fraction of
# the image's shorter side (its defaults: 1/3/6/8 % of an image it first shrinks to 512 px,
# so boxes of 5-41 px). We run it on an image PYRAMID instead: each level is the frame
# reduced to a width of the ladder, the levels shrink geometrically, so the whole pyramid
# costs ~1.6× its largest level (the detector costs ~0.008 µs per pixel — only the pixel
# sum matters). Per modality the widths of the levels: VIS from a third of the 9566 px
# demosaic, IR (13 px/mm, the comb is small already) from three quarters of its 4310 px.
LOCALISER = {"visible": (3200, 2100, 1400, 950), "ir": (3200, 2400, 1800, 1350, 1000)}
# The detector's own knobs, per pyramid level: no internal downsampling (the pyramid does
# that), box filters of BOX_PX pixels on the shorter side (2-36 px: the smaller ones catch
# a short, thin comb), and an edge threshold of 32 of 255 instead of 64 — after reduction
# and lens blur a tick is a ramp over two or three pixels, and in IR (dark ticks on a grey
# face) its gradient falls below the default. Corpus (8948 frames): this finds 4767 of
# 4777 cards, 3 more than 450 px windows tiled over each level, at 0.52 vs 0.41 s/frame.
BOX_PX = (2.25, 4.5, 9.0, 13.5, 27.0, 36.0)
GRADIENT_THRESHOLD = 32.0
MAX_CANDIDATES = 4            # read per frame, in the localiser's order
LATTICE_MIN = 8               # ticks on a constant pitch a card must show (a card has 11)
LATTICE_RESIDUAL = 0.05       # the lattice fit's rms, as a fraction of the pitch, above which the
                              # ticks are not printed ones: corpus cards 1.4 % median, 2.2 % max;
                              # parallel fibres 17 %, a ribbed frame edge 8 %
MASK_MM = (22.0, 16.0)        # the mask box around the comb centre, along the comb axis and
                              # across it: the comb sits 9.4 mm from one end and 8 mm from
                              # one edge of the 20 x 10 mm tile, and the label side is unknown
TICK_MM = 3.1                 # a short tick; the profile is read over the inner half of it


# ---- 1. localise --------------------------------------------------------------------

def _reduce(g8: np.ndarray, width: int) -> tuple[np.ndarray, float]:
    """The frame at `width`: by an integer factor when one is within 10 % (INTER_AREA is a
    fast box filter then — a 0.5018 factor took 90 ms, 0.5 takes 1 ms)."""
    k = max(1, round(g8.shape[1] / width))
    if abs(g8.shape[1] / k - width) < 0.1 * width:
        down = 1.0 / k
    else:
        down = min(1.0, width / g8.shape[1])
    if down >= 1:
        return g8, 1.0
    return cv2.resize(g8, None, fx=down, fy=down, interpolation=cv2.INTER_AREA), down


def _candidates(g8: np.ndarray, kind: str) -> list[np.ndarray]:
    """Candidate quads (frame px) from the barcode localiser on every pyramid level,
    duplicates across levels collapsed."""
    det = cv2.barcode.BarcodeDetector()
    det.setDownsamplingThreshold(1e9)
    det.setGradientThreshold(GRADIENT_THRESHOLD)
    quads: list[np.ndarray] = []
    for width in LOCALISER[kind]:
        small, down = _reduce(g8, width)
        det.setDetectorScales([b / min(small.shape) for b in BOX_PX])
        ok, pts = det.detect(small)
        if not ok or pts is None:
            continue
        for q in pts:
            q = np.asarray(q, np.float64) / down
            if all(np.hypot(*(q.mean(axis=0) - u.mean(axis=0))) > 40 / down for u in quads):
                quads.append(q)
    return quads


def _quad_axis(q: np.ndarray) -> tuple[float, float, float]:
    """(angle of the long side in degrees, long side, short side) of a candidate quad —
    the long side runs along the comb, across the ticks."""
    (_, _), (w, h), ang = cv2.minAreaRect(q.astype(np.float32))
    return (ang, w, h) if w >= h else (ang + 90.0, h, w)


# ---- 2. + 3. the profile across the ticks: period and lattice -----------------------

def strip_profile(gray: np.ndarray, centre, deg: float, length: float, width: float) -> np.ndarray:
    """Mean brightness along a strip of `length` × `width` px centred on `centre`, its long
    side at `deg` (minAreaRect's convention, as `_quad_axis` reports it) — one value per
    pixel along the comb, averaged across the tick length. A strip point at +d from the
    centre lies at centre + d · (cos deg, sin deg) in the frame."""
    L, W = max(8, int(length)), max(4, int(width))
    m = cv2.getRotationMatrix2D((float(centre[0]), float(centre[1])), deg, 1.0)
    m[0, 2] += L / 2 - centre[0]
    m[1, 2] += W / 2 - centre[1]
    strip = cv2.warpAffine(gray, m, (L, W), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return strip.mean(axis=0).astype(np.float64)


def period(profile: np.ndarray) -> float | None:
    """The comb period (px) from the first local maximum of the profile's autocorrelation,
    sub-pixel by a parabola; None when the profile has no rhythm."""
    prof = profile - profile.mean()
    if prof.std() < 1:
        return None
    ac = np.correlate(prof, prof, "full")[len(prof) - 1:]
    ac /= ac[0]
    i = next((k for k in range(3, len(ac) - 1) if ac[k - 1] < ac[k] >= ac[k + 1] and ac[k] > 0.2), None)
    if i is None:
        return None
    a, b, c = ac[i - 1], ac[i], ac[i + 1]
    d = a - 2 * b + c
    return i + (0.5 * (a - c) / d if d != 0 else 0.0)


def lattice(profile: np.ndarray, expected: float) -> dict | None:
    """Tick minima of the profile (dark ticks on a bright face) fitted to a lattice:
    {pitch, n, residual, centre} in profile px, or None below LATTICE_MIN ticks or when
    they scatter around the fit by more than LATTICE_RESIDUAL of the pitch."""
    from scipy.signal import find_peaks
    inv = profile.max() - profile
    inv = np.clip(inv - np.percentile(inv, 30), 0, None)
    if inv.max() <= 0:
        return None
    peaks, _ = find_peaks(inv, distance=max(2, int(0.6 * expected)), prominence=0.3 * inv.max())
    if len(peaks) < LATTICE_MIN:
        return None
    pos = []
    for k in peaks:
        if 0 < k < len(inv) - 1:
            a, b, c = inv[k - 1], inv[k], inv[k + 1]
            d = a - 2 * b + c
            pos.append(k + (0.5 * (a - c) / d if d != 0 else 0.0))
        else:
            pos.append(float(k))
    pos = np.array(pos)
    ok = np.abs(np.diff(pos) - expected) < 0.3 * expected
    best, run = [], [0]
    for i, good in enumerate(ok):
        if good:
            run.append(i + 1)
        else:
            best = run if len(run) > len(best) else best
            run = [i + 1]
    best = run if len(run) > len(best) else best
    if len(best) < LATTICE_MIN:
        return None
    idx = np.arange(len(best))
    slope, intercept = np.polyfit(idx, pos[best], 1)
    resid = pos[best] - (slope * idx + intercept)
    rms = float(np.sqrt(np.mean(resid ** 2)))
    if rms > LATTICE_RESIDUAL * slope:
        return None
    return {"pitch": float(slope), "n": len(best), "residual": rms, "centre": float(pos[best].mean())}


# ---- the card -----------------------------------------------------------------------

def find(gray: np.ndarray, kind: str) -> dict | None:
    """The scale card in a gray frame of any resolution, or None.

    {face: (4,2) float32 polygon of the mask box (MASK_MM around the comb centre, long side
     along the comb axis), centre: the comb's centre, angle: deg of the comb axis,
     px_per_mm: the lattice pitch, ticks, residual: lattice rms in px}"""
    g8 = np.clip(gray, 0, 255).astype(np.uint8) if gray.dtype != np.uint8 else gray
    best = None
    read = 0
    for q in _candidates(g8, kind):
        deg, length, width = _quad_axis(q)
        c = q.mean(axis=0)
        p = period(strip_profile(g8, c, deg, length * 1.2, width))
        if p is None:                          # no comb rhythm along the candidate
            continue
        read += 1
        if read > MAX_CANDIDATES:
            break
        # the whole comb with room to spare, read over the inner half of the ticks
        L = max(length * 1.2, 14 * p)
        lat = lattice(strip_profile(g8, c, deg, L, 0.5 * TICK_MM * p), p)
        if lat is None:
            continue
        if best is None or lat["n"] > best[1]["n"] or (lat["n"] == best[1]["n"] and lat["residual"] < best[1]["residual"]):
            best = (c, lat, deg, L)
    if best is None:
        return None
    c, lat, deg, L = best
    off = lat["centre"] - L / 2                # the lattice's centre along the strip
    rad = np.deg2rad(deg)
    cx, cy = c[0] + off * np.cos(rad), c[1] + off * np.sin(rad)
    ppm = lat["pitch"]
    face = cv2.boxPoints(((float(cx), float(cy)), (MASK_MM[0] * ppm, MASK_MM[1] * ppm), float(deg)))
    return {"face": face.astype(np.float32), "centre": (float(cx), float(cy)), "angle": float(deg),
            "px_per_mm": float(ppm), "ticks": lat["n"], "residual": lat["residual"]}
