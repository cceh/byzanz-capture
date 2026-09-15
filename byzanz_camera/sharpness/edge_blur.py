#!/usr/bin/env python3
"""Measure how sharp the 1 cm card's tick edges are, in full-resolution pixels.

The ticks are the only high-contrast straight edges guaranteed to be in every
frame, at a known physical size — which makes them a usable focus probe. The
card is located on a half-size copy (fast, `scalecard.find` is scale-free), then
the edge profile is read off the full-resolution frame (accurate), along the comb
axis over the inner half of the ticks.

Reported: the 10–90 % rise distance of the tick edges, averaged. A perfectly
focused f/8 frame lands near the diffraction/sensor floor; defocus widens it
proportionally to the blur circle.

`measure_rgb(rgb, kind)` takes a frame the caller already decoded (the capture
software's display decode, object_blur.DECODE — decoded once, measured by every
check); `measure(path, kind)` decodes it first. Always a result: `edge_px` is None
when there is no card or no readable edge, the card's pitch is reported whenever
the card was found.
"""
from __future__ import annotations
import numpy as np
import cv2

from . import object_blur, scalecard

# Bumped when the measurement changes (as object_blur.METRIC_VERSION): v1 was the comb
# detector's un-rotated face profile, v2 reads the strip along the comb axis found by the
# template-free scale-card detector. Every result carries it.
METRIC_VERSION = "scalecard-v2"


def _locate(gray: np.ndarray, kind: str) -> dict | None:
    """The comb on a half-size copy, in full-resolution geometry."""
    half = cv2.resize(gray, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
    card = scalecard.find(half, kind)
    if card is None:
        return None
    return {"centre": (card["centre"][0] * 2, card["centre"][1] * 2), "angle": card["angle"],
            "px_per_mm": card["px_per_mm"] * 2, "ticks": card["ticks"]}


def _profile(gray: np.ndarray, card: dict) -> np.ndarray:
    """Mean intensity profile across the comb, at full resolution: along the comb axis,
    averaged over the inner half of the ticks (their ends are rounded)."""
    ppm = card["px_per_mm"]
    return scalecard.strip_profile(gray, card["centre"], card["angle"], 13 * ppm, 0.5 * scalecard.TICK_MM * ppm)


def _edge_widths(prof, min_contrast=25):
    """10–90 % rise distance of every edge in the profile, in pixels."""
    if prof is None or len(prof) < 8:
        return []
    widths = []
    d = np.diff(prof)
    # an edge is a run of same-signed gradient; measure between the local
    # plateau values on either side of it
    i = 1
    while i < len(prof) - 1:
        s = np.sign(d[i - 1])
        if s == 0:
            i += 1
            continue
        j = i
        while j < len(d) and np.sign(d[j]) == s:
            j += 1
        lo, hi = prof[i - 1], prof[j]
        if abs(hi - lo) >= min_contrast:
            t10, t90 = lo + 0.1 * (hi - lo), lo + 0.9 * (hi - lo)
            seg = prof[i - 1:j + 1]
            xs = np.arange(len(seg), dtype=float)
            if hi < lo:                      # falling edge: np.interp needs
                seg, xs = seg[::-1], xs[::-1]  # an increasing x-axis
            w = abs(np.interp(t90, seg, xs) - np.interp(t10, seg, xs))
            if 0 < w < 60:
                widths.append(w)
        i = max(j, i + 1)
    return widths


def measure_rgb(rgb: np.ndarray, kind: str) -> dict:
    """{edge_px: median 10-90 % rise of the tick edges in full-res px or None, edges: how
    many were read, px_per_cm: the card's pitch or None, ticks: comb ticks found,
    metric_version: METRIC_VERSION}."""
    gray = object_blur.gray_for(rgb, kind)
    card = _locate(gray, kind)
    if card is None:
        return {"edge_px": None, "edges": 0, "px_per_cm": None, "ticks": 0,
                "metric_version": METRIC_VERSION}
    w = _edge_widths(_profile(gray, card))
    return {"edge_px": float(np.median(w)) if w else None, "edges": len(w),
            "px_per_cm": card["px_per_mm"] * 10, "ticks": card["ticks"],
            "metric_version": METRIC_VERSION}


def measure(path: str, kind: str) -> dict:
    return measure_rgb(object_blur.decode(path), kind)
