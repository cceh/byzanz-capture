#!/usr/bin/env python3
"""Sharpness of the papyrus plane.

Approach: sample the strongest edges across the frame, measure each
edge's 10-90 % rise width, report the 20th percentile — the sharpest
edges track focus, not content (soft fibres and faded ink only ADD wide
edges; defocus caps how crisp the crispest edge can be).

1. Per-edge widths come from fitting an analytic blurred step (erf curve)
   to the raw profile. The fit averages sensor noise instead of smoothing
   it into the edge, so no measurement floor is added and widths below
   ~3 px stay comparable. Fits whose residuals show they do not describe
   the profile (double edges, texture) are rejected.
2. Edges are additionally grouped into four orientation bins. Linear
   motion during the exposure (knocked table, building vibration) widens
   edges across the motion direction AND starves the along-motion bins:
   those edges lose so much gradient that they drop out of the
   strongest-edge selection. `orientation_balance` (weakest bin count /
   strongest bin count) therefore collapses under motion while defocus
   leaves it roughly unchanged - a shake indicator the single overall
   number cannot provide. Caveat: strongly directional content (fragments
   of near-parallel fibre strips) also yields a low balance, so a low
   value marks a frame for visual inspection, it does not prove shake.

Thresholds are calibrated per metric version against the corpus; the
`metric_version` field is part of every result so values measured with
different versions are never compared.

IR frames: pass a green-channel `gray_loader` — the IR rig clips the red
channel, and clipped edges would measure as falsely sharp.
"""
from __future__ import annotations

import sys

import cv2
import numpy as np
from scipy.optimize import curve_fit
from scipy.special import erf

try:
    from . import exclusion_masks, object_blur
except ImportError:
    import exclusion_masks
    import object_blur

METRIC_VERSION = "v2-erf"
RISE_10_90_PER_SIGMA = 2.5631   # 10-90 % width of a step blurred with sigma
PROFILE_HALF = 24               # px sampled on each side of an edge site
MIN_CONTRAST = 30               # min plateau difference of a usable profile
MIN_EDGES_TOTAL = 40
ORIENTATION_BIN_DEG = 45
MIN_EDGES_PER_BIN = 25


def _sample_profile(gray, site):
    """Brightness profile across one edge site along its gradient
    direction, or None when the profile would leave the image."""
    y, x, gx_val, gy_val = site
    norm = np.hypot(gx_val, gy_val)
    if norm < 1e-3:
        return None
    ux, uy = gx_val / norm, gy_val / norm
    ts = np.arange(-PROFILE_HALF, PROFILE_HALF + 0.5, 1.0)
    xs, ys = x + ts * ux, y + ts * uy
    h, w = gray.shape
    if xs.min() < 1 or ys.min() < 1 or xs.max() > w - 2 or ys.max() > h - 2:
        return None
    profile = cv2.remap(gray, xs.astype(np.float32)[None, :],
                        ys.astype(np.float32)[None, :], cv2.INTER_LINEAR)[0]
    return ts, profile


def _blurred_step(x, base, amplitude, centre_x, sigma):
    return base + amplitude / 2 * (1 + erf((x - centre_x) / (sigma * np.sqrt(2))))


def _rise_width_erf_fit(ts, profile):
    """10-90 % width of the erf curve fitted to the raw values of the
    rising segment around the profile centre, or None for unusable
    profiles. The segment is LOCATED on a lightly smoothed copy (noise
    would break the monotonic walk); the fit itself runs on unsmoothed
    values so no smoothing floor enters the width. Fitting only the segment (plus a small margin)
    keeps thin ink strokes measurable, whose full profile is a valley
    rather than one step."""
    raw = profile.astype(np.float64)
    smoothed = cv2.GaussianBlur(raw.reshape(1, -1).astype(np.float32),
                                (0, 0), 1.2).ravel()
    centre = len(smoothed) // 2
    rising = smoothed[centre + 3] >= smoothed[centre - 3]
    segment_src = smoothed if rising else smoothed[::-1]
    raw_oriented = raw if rising else raw[::-1]
    i = j = centre
    while i > 0 and segment_src[i - 1] < segment_src[i]:
        i -= 1
    while j < len(segment_src) - 1 and segment_src[j + 1] > segment_src[j]:
        j += 1
    if j - i < 2 or segment_src[j] - segment_src[i] < MIN_CONTRAST:
        return None
    lo_idx, hi_idx = max(i - 4, 0), min(j + 4, len(raw_oriented) - 1)
    xs = ts[lo_idx:hi_idx + 1]
    ys = raw_oriented[lo_idx:hi_idx + 1]
    base_0, amplitude_0 = float(ys.min()), float(ys.max() - ys.min())
    try:
        popt, _ = curve_fit(
            _blurred_step, xs, ys,
            p0=(base_0, amplitude_0, (ts[i] + ts[j]) / 2,
                max((j - i) / RISE_10_90_PER_SIGMA, 0.3)),
            bounds=((base_0 - 40, MIN_CONTRAST * 0.5, xs[0], 0.02),
                    (base_0 + amplitude_0 + 40, 2 * amplitude_0 + 40, xs[-1], 20.0)),
            maxfev=300)
    except (RuntimeError, ValueError):
        return None
    width = RISE_10_90_PER_SIGMA * popt[3]
    if not 0 < width < ts[-1] - ts[0]:
        return None
    residuals = ys - _blurred_step(xs, *popt)
    if residuals.std() > 0.2 * popt[1]:   # fit does not describe the segment
        return None
    return float(width)


def measure(path, gray_loader=None, exclude=("cc", "scale")):
    """Measure one capture. Returns None when the frame has too little
    contrast for a statement ("not measurable", never "ok"), else a dict:

    sharp_px             20th percentile of edge widths - THE sharpness number
    median_px            median width (content-dependent, for context)
    n_edges              number of successfully measured edges
    orientation_counts   edges per 45-degree orientation bin
    orientation_p20      p20 per bin (None: too few edges in that bin)
    orientation_balance  weakest bin count / strongest bin count; collapses
                         towards 0 under motion blur but is also lowered by
                         strongly directional content - a suspicion marker
    excluded             which exclusion masks matched, e.g.
                         {"cc": True, "scale": False}
    metric_version       METRIC_VERSION - never compare values across versions

    `exclude` names the exclusion_masks detectors to apply; pass () to
    measure the raw frame.
    """
    gray = (gray_loader or object_blur._gray_full)(path)
    if exclude:
        mask, mask_found = exclusion_masks.build_mask(gray, exclude)
    else:
        mask, mask_found = None, {}
    widths, orientations = [], []
    for site in object_blur._edge_sites(gray, object_blur.N_EDGES,
                                        exclude_mask=mask):
        sampled = _sample_profile(gray, site)
        if sampled is None:
            continue
        width = _rise_width_erf_fit(*sampled)
        if width is None:
            continue
        widths.append(width)
        orientations.append(np.degrees(np.arctan2(site[3], site[2])) % 180.0)
    if len(widths) < MIN_EDGES_TOTAL:
        return None
    widths = np.array(widths)
    orientations = np.array(orientations)

    orientation_counts, orientation_p20 = {}, {}
    for start in range(0, 180, ORIENTATION_BIN_DEG):
        in_bin = widths[(orientations >= start)
                        & (orientations < start + ORIENTATION_BIN_DEG)]
        orientation_counts[start] = int(len(in_bin))
        orientation_p20[start] = (float(np.percentile(in_bin, 20))
                                  if len(in_bin) >= MIN_EDGES_PER_BIN else None)

    return {
        "n_edges": len(widths),
        "sharp_px": float(np.percentile(widths, 20)),
        "median_px": float(np.median(widths)),
        "orientation_counts": orientation_counts,
        "orientation_p20": orientation_p20,
        "orientation_balance": (min(orientation_counts.values())
                                / max(orientation_counts.values())),
        "excluded": mask_found,
        "metric_version": METRIC_VERSION,
    }


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        result = measure(arg)
        name = arg.rsplit("/", 1)[-1]
        if result is None:
            print(f"{name[:46]:<46} zu wenig Kontrast")
        else:
            print(f"{name[:46]:<46} n={result['n_edges']:<5} "
                  f"p20={result['sharp_px']:5.2f} px   "
                  f"median={result['median_px']:5.2f} px   "
                  f"balance={result['orientation_balance']:5.3f}")
