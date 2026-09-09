"""Reference cards in a capture: the ColorChecker and the 1 cm scale card.

Both sit on their own plane above the papyrus, so their edges must never enter the
sharpness statistic, and their pixels must not pull the IR↔VIS registration. Two finders
and one mask builder:

  find_colorchecker(rgb)   OpenCV's `mcc` detector (Marrero-Fernández et al.; in the main
                           OpenCV repo since 5.0): adaptive-threshold sweep → quadrilateral
                           candidates → 6×4 grid fit → colour cost against the reference
                           chart. It keys on the PATCHES, so a card whose dark face merges
                           with dark papyrus or with the black glass-frame edge is still
                           found — the failure mode of the outline detector this replaces
                           (corpus 2026-09-03: 0 regressions in 1875 hits, 52 more cards
                           found, 8 outline false positives on dark fragments removed).
                           Needs the COLOUR frame; on gray the grid fit still finds the card
                           but mislocalises it in ~20 % of frames below 2400 px.
  scalecard.find(gray, kind)  the scale card, scale-free: barcode-style localiser, comb
                           period as the scale, the comb lattice as the proof (see
                           scalecard.py). Returns the mask box around the comb.
  exclusion_mask(shape, polygons)  one boolean mask at full resolution, every polygon
                           grown by MASK_MARGIN_PX (drop shadow + the metric's profile
                           half-window).

Frames may be smaller than the 9500 px repro-stand captures — the registration hands
over ~1600 px — so previews and margins scale with the frame (`_frame_scale`).
"""
from __future__ import annotations

import cv2
import numpy as np

try:
    from . import scalecard
except ImportError:                # script-style use
    import scalecard

# margin around every detected region, full-resolution px: the card's drop shadow and
# the metric's profile half-window
MASK_MARGIN_PX = 80
_TUNED_FRAME_W = 9500      # the repro-stand frame width the margin is defined on

# ---- ColorChecker (mcc) ----------------------------------------------------------
MCC_WIDTH = 2400           # working width; at 1600 the grid fit starts to mislocalise
MCC_CANDIDATES = 3         # mcc occasionally ranks a degenerate all-zero box first
MCC_MIN_AREA_FRAC = 0.0005 # of the working frame; the smallest real grid (H90) is ~1 %
# mcc's getBox() spans the patch grid; the card FACE is larger by these factors (measured
# against the outline detector's face polygon on 1873 corpus frames: 1.006 / 1.001 after)
FACE_LONG, FACE_SHORT = 1.0 / 0.909, 1.0 / 0.845

def _frame_scale(width: int) -> float:
    return min(1.0, width / _TUNED_FRAME_W)


def find_colorchecker(rgb: np.ndarray) -> dict | None:
    """The chart in an RGB uint8 frame, or None. Returns {"face": (4,2) float32 polygon of
    the card face in frame px, "grid": (4,2) patch-grid box, "cost": mcc's colour error
    (≤ 0.1 by construction), "patches": (24,3) mean patch RGB} — the face is what the
    exclusion mask needs, grid and patches are what a colour calibration needs."""
    h, w = rgb.shape[:2]
    width = min(MCC_WIDTH, w)
    scale = width / w
    small = cv2.cvtColor(cv2.resize(rgb, (width, round(h * scale)), interpolation=cv2.INTER_AREA),
                         cv2.COLOR_RGB2BGR)
    det = cv2.mcc.CCheckerDetector.create()
    det.setColorChartType(cv2.mcc.MCC24)
    if not det.process(small, nc=MCC_CANDIDATES):
        return None
    valid = [c for c in det.getListColorChecker() if _plausible_box(c.getBox(), small.shape)]
    if not valid:
        return None
    best = min(valid, key=lambda c: c.getCost())
    grid = np.asarray(best.getBox(), dtype=np.float32) / scale
    (cx, cy), (bw, bh), ang = cv2.minAreaRect(grid)
    if bw >= bh:
        bw, bh = bw * FACE_LONG, bh * FACE_SHORT
    else:
        bw, bh = bw * FACE_SHORT, bh * FACE_LONG
    face = cv2.boxPoints(((cx, cy), (bw, bh), ang)).astype(np.float32)
    # getChartsRGB(): one row per patch and channel (24 × R,G,B), columns
    # [pixel count, mean, std, max, min] — the mean column is the patch colour
    patches = np.asarray(best.getChartsRGB(), dtype=np.float32)[:, 1].reshape(24, 3)
    return {"face": face, "grid": grid, "cost": float(best.getCost()), "patches": patches}


def _plausible_box(box, shape) -> bool:
    """mcc sometimes reports a collapsed box at the origin (all zeros, cost ≈ 0.025) as
    its best checker; the real card is then the next candidate. A card touching the frame
    edge legitimately puts a corner a few pixels outside, so the test is the box's AREA
    and its centre, not its corners (5 edge cards of 1969 were lost to a corner test)."""
    pts = np.asarray(box, dtype=np.float32).reshape(-1, 2)
    h, w = shape[:2]
    if pts.shape[0] != 4 or cv2.contourArea(pts) < MCC_MIN_AREA_FRAC * h * w:
        return False
    cx, cy = pts.mean(axis=0)
    return bool(0 <= cx < w and 0 <= cy < h)


def find(rgb: np.ndarray | None, gray: np.ndarray, kind: str) -> dict:
    """Both cards of a frame: {"cc": [face] | [], "scale": [face] | [], "scale_card":
    scalecard.find's record | None}. The chart is only looked for in a VIS frame with its
    colour planes at hand — never in an IR frame; the scale card's localiser is tuned per
    modality."""
    cc = find_colorchecker(rgb) if kind == "visible" and rgb is not None else None
    card = scalecard.find(gray, kind)
    return {"cc": [cc["face"]] if cc else [], "scale": [card["face"]] if card else [],
            "scale_card": card}


def exclusion_mask(shape: tuple, polygons: dict) -> np.ndarray | None:
    """Boolean mask of `shape` (True = excluded) covering every polygon grown by the
    margin, or None when there is nothing to exclude. Every polygon here is a rotated
    rectangle, so growing it is a larger rectangle around the same centre."""
    quads = [p for name in ("cc", "scale") for p in polygons.get(name, [])]
    if not quads:
        return None
    h, w = shape[:2]
    margin = MASK_MARGIN_PX * _frame_scale(w)
    mask = np.zeros((h, w), np.uint8)
    for quad in quads:
        (cx, cy), (bw, bh), ang = cv2.minAreaRect(np.asarray(quad, dtype=np.float32))
        grown = cv2.boxPoints(((cx, cy), (bw + 2 * margin, bh + 2 * margin), ang))
        cv2.fillPoly(mask, [np.round(grown).astype(np.int32)], 1)
    return mask.astype(bool)
