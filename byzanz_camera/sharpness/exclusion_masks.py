#!/usr/bin/env python3
"""Exclusion masks for the sharpness metrics.

The ColorChecker and the 1 cm scale card are a few millimetres tall, so
their surfaces lie on a different focal plane than the papyrus. Their
edges measure the card plane, not the object plane, and must therefore
never contribute to the object sharpness statistic.

Detector registry: `register_detector(name, fn)` adds a detector. A
detector takes the full-resolution grayscale frame (float32, 0-255) and
returns a list of polygons (Nx2 arrays, full-resolution x/y coordinates)
around its targets - an empty list when the target is not in the frame.
Built-ins:

  "cc"     ColorChecker, found by its dark chart face (large near-black
           rectangle of chart-like aspect ratio)
  "scale"  1 cm scale card, found by its periodic tick comb (the pattern
           search from `scalebar`; text or fibres are never that regular)

A future setup with a different colour chart only needs one new
registered detector - the metric itself stays untouched.
"""
from __future__ import annotations

from typing import Callable, Optional

import cv2
import numpy as np

try:
    from . import scalebar
except ImportError:
    import scalebar

# margin added around every detected region (full-resolution pixels):
# covers the card's drop shadow and the metric's profile half-window
MASK_MARGIN_PX = 80

_DETECT_DOWNSCALE = 8      # mask rasterisation runs on a 1/8 preview

_DETECTORS: dict[str, Callable[[np.ndarray], list]] = {}

# Every reduction and margin below is expressed for a full-resolution capture. Callers
# that hand over a reduced rendition — the registration works on ~1600 px — would
# otherwise have their cards shrink under the detectors' size gates. `_frame_scale`
# lets each detector adapt to the frame it was actually given, so no caller has to know
# the internals and no module constant has to be mutated (the sharpness audit imports
# this module in the same process, and mutating them would corrupt its measurements).
_TUNED_FRAME_W = 9500      # the repro-stand captures these gates were tuned on


def _frame_scale(gray: np.ndarray) -> float:
    return min(1.0, gray.shape[1] / _TUNED_FRAME_W)


def _reduction(gray: np.ndarray, tuned: int) -> float:
    """Reduction that puts the preview at the SIZE these gates were tuned for, not at a
    tuned fraction of an arbitrary frame. Deliberately fractional: rounding to whole
    numbers on a 1600 px frame lands on 1, and at that preview size the chart's patches
    no longer merge under the fixed closing kernel — the detector then latches onto a
    dark stretch of papyrus instead."""
    return max(1.0, tuned * _frame_scale(gray))


def register_detector(name: str, fn: Callable[[np.ndarray], list]) -> None:
    _DETECTORS[name] = fn


def available() -> tuple[str, ...]:
    return tuple(_DETECTORS)


def detect_polygons(gray: np.ndarray, names=None) -> dict[str, list]:
    """Run the requested detectors; {name: [polygon, ...]} (may be empty)."""
    names = tuple(_DETECTORS) if names is None else tuple(names)
    return {name: _DETECTORS[name](gray) for name in names}


def build_mask(gray: np.ndarray, names=None):
    """Combined exclusion mask at full resolution.

    Returns (mask, found): `mask` is a boolean array of gray's shape with
    True inside every detected region plus MASK_MARGIN_PX, or None when
    nothing was detected; `found` maps each requested detector name to
    whether it found its target.
    """
    polygons_by_name = detect_polygons(gray, names)
    found = {name: len(polys) > 0 for name, polys in polygons_by_name.items()}
    all_polygons = [p for polys in polygons_by_name.values() for p in polys]
    if not all_polygons:
        return None, found

    scale = _reduction(gray, _DETECT_DOWNSCALE)
    small_shape = (int(gray.shape[0] / scale) + 1, int(gray.shape[1] / scale) + 1)
    small = np.zeros(small_shape, np.uint8)
    for polygon in all_polygons:
        cv2.fillPoly(small, [np.int32(np.asarray(polygon) / scale)], 1)
    margin = max(1, int(MASK_MARGIN_PX * _frame_scale(gray) / scale))
    small = cv2.dilate(small, np.ones((2 * margin + 1, 2 * margin + 1), np.uint8))
    mask = cv2.resize(small, (gray.shape[1], gray.shape[0]),
                      interpolation=cv2.INTER_NEAREST)
    return mask.astype(bool), found


# ---- built-in detector: ColorChecker --------------------------------------

# geometry gates for the chart face, relative to the frame
_CC_MIN_AREA_FRACTION = 0.001
_CC_MAX_AREA_FRACTION = 0.06
_CC_ASPECT_RANGE = (1.25, 1.8)
_CC_MIN_FILL = 0.6
_CC_DARK_THRESHOLD = 60


def _detect_colorchecker(gray: np.ndarray) -> list:
    """The chart is the only LARGE near-black rectangle in these frames
    (background is white, papyrus is mid-gray). Search the dark blobs of
    a small preview for one with chart-like aspect ratio and solidity;
    keep the largest match."""
    scale = _reduction(gray, _DETECT_DOWNSCALE)
    preview = cv2.resize(gray, None, fx=1 / scale, fy=1 / scale,
                         interpolation=cv2.INTER_AREA)
    preview = np.clip(preview, 0, 255).astype(np.uint8)
    dark = (preview < _CC_DARK_THRESHOLD).astype(np.uint8) * 255
    # close the gaps between the dark patches so the chart face becomes
    # one blob even when its light patches split the dark threshold mask
    dark = cv2.morphologyEx(dark, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    contours, _ = cv2.findContours(dark, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    frame_area = preview.size
    best = None
    for contour in contours:
        area = cv2.contourArea(contour)
        if not (_CC_MIN_AREA_FRACTION * frame_area <= area
                <= _CC_MAX_AREA_FRACTION * frame_area):
            continue
        (_, _), (w, h), _ = cv2.minAreaRect(contour)
        if min(w, h) < 15:
            continue
        aspect = max(w, h) / min(w, h)
        if not (_CC_ASPECT_RANGE[0] < aspect < _CC_ASPECT_RANGE[1]):
            continue
        if area / (w * h) < _CC_MIN_FILL:
            continue
        if best is None or area > best[0]:
            best = (area, contour)
    if best is None:
        return []
    corners = cv2.boxPoints(cv2.minAreaRect(best[1])) * scale
    return [corners.astype(np.float32)]


# ---- built-in detector: scale card ----------------------------------------

# the comb's bounding box is padded by this many tick lengths so the whole
# card (including the "1 cm" label under the comb) lands inside the mask
_SCALE_PAD_TICK_LENGTHS = 1.5
_SCALE_MAX_COMBS = 3
# the card's signature: a tick is ~3.3x as long as the 1 mm pitch. Text
# lines can form comb-like blobs, but never at this length/pitch ratio
_SCALE_LEN_RATIO_RANGE = (2.3, 4.6)
_SCALE_MIN_SPACING_PX = 3


def _detect_scalebar(gray: np.ndarray) -> list:
    """Reuse the tick-comb pattern search from `scalebar` on a half-size
    preview (its size gates are tuned for half resolution). Every comb
    matching the card's length/pitch signature becomes a padded rectangle
    around the card. On a reduced frame the ticks are already only a few
    pixels apart, so the halving is dropped rather than pushing them under
    `_SCALE_MIN_SPACING_PX`."""
    scale = _reduction(gray, 2)
    preview = np.clip(cv2.resize(gray, None, fx=1 / scale, fy=1 / scale,
                                 interpolation=cv2.INTER_AREA),
                      0, 255).astype(np.uint8)
    combs = scalebar._combs(scalebar._tick_candidates(preview))
    combs = [c for c in combs
             if c["spacing"] >= _SCALE_MIN_SPACING_PX
             and _SCALE_LEN_RATIO_RANGE[0]
                 <= c["tick_len"] / c["spacing"]
                 <= _SCALE_LEN_RATIO_RANGE[1]]
    combs = sorted(combs, key=lambda c: -c["n"])[:_SCALE_MAX_COMBS]
    polygons = []
    for comb in combs:
        pad = _SCALE_PAD_TICK_LENGTHS * comb["tick_len"]
        half_along = comb["tick_len"] / 2 + pad
        half_across = comb["span"] / 2 + pad
        if comb["horizontal"]:
            x0, x1 = comb["cx"] - half_along, comb["cx"] + half_along
            y0, y1 = comb["cy"] - half_across, comb["cy"] + half_across
        else:
            x0, x1 = comb["cy"] - half_across, comb["cy"] + half_across
            y0, y1 = comb["cx"] - half_along, comb["cx"] + half_along
        polygons.append(np.float32(
            [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]) * scale)
    return polygons


register_detector("cc", _detect_colorchecker)
register_detector("scale", _detect_scalebar)
