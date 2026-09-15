#!/usr/bin/env python3
"""Object sharpness metric v5 — the sharpness of the papyrus plane, measured on the object.

Harvest the strongest intensity edges in the frame (ink strokes, fragment borders, fibre
contrast), sample a 1-D brightness profile across each along its gradient, fit an analytic
blurred step (erf) and report a LOW percentile of the fitted 10-90 % rise widths: defocus
puts a hard floor under how crisp the crispest edge can be, while content (soft torn
fibres, faded ink) can only add wide edges on top. The sharp end of the distribution tracks
focus, not content. No scale card needed; works on stitching segments and card-free frames.

What one measurement does:
  1. decode the raw with ONE recipe (DECODE) — the same one the capture software uses for
     its display, so a value measured at the capture table and one measured later on the
     server are the same measurement;
  2. gray per modality: VIS luminance, IR the green channel (the D90's red channel clips in
     the 8-bit render under the camera white balance);
  3. mask the reference cards (`cards`): they sit on their own plane. The chart is only
     looked for in VIS — it is never in an IR frame;
  4. edge sites: the strongest gradient per 96 px cell, above a floor estimated from the
     frame's own noise (defocus lowers ALL gradients, a fixed cutoff would starve exactly the
     blurry frames), a 350 px border skipped (sensor-margin bands of older LibRaw builds).
     When the edge material covers less than half the frame — a small loose fragment — the
     grid is re-laid over just that part, dense enough to sample it like a frame-filling
     object, against the SAME floor (recomputed over the object's cells it would rise
     exactly where the signal is);
  5. per site: profile ±24 px, rising segment located on a lightly smoothed copy, erf fit on
     the RAW values of that segment (no smoothing floor enters the width); fits whose
     residuals do not describe the segment (double edges, texture) are rejected, so are
     widths below WIDTH_FLOOR_PX (demosaic/noise artifacts, never optics);
  6. statistics: sharp_px = 20th percentile of the widths, plus per-orientation counts and
     p20 in four 45° bins. Linear motion during the exposure widens edges across the motion
     AND starves the along-motion bins, so `orientation_balance` (weakest / strongest bin)
     collapses under shake while defocus leaves it alone — a suspicion marker, not proof
     (near-parallel fibre strips lower it too).
  Honesty gate: fewer than MIN_EDGES_TOTAL real edges → the sharpness fields are None
  ("not measurable", never "ok"); the card geometry found in step 1 is reported anyway.

Values are calibrated per METRIC_VERSION against the corpus and never compared across
versions. v5 = the v4 measurement (its site selection, fit, floors and thresholds are
unchanged) with the ColorChecker mask from the patch-based detector and one decode recipe.

    measure(path, kind)          decode + measure
    measure_rgb(rgb, kind)       the same on a frame the caller already decoded
"""
from __future__ import annotations


import cv2
import numpy as np
import rawpy
from scipy.optimize import curve_fit
from scipy.special import erf

from . import cards

METRIC_VERSION = "v6"      # v6: the scale card by template + comb lattice (scalecard.py); the
                           # mask covers the full card face — values shift slightly on frames
                           # with a card, so v5 and v6 numbers are not compared
# rawpy recipe of every measurement — and of the capture software's display decode
DECODE = dict(half_size=False, use_camera_wb=True, output_bps=8, no_auto_bright=True)
KINDS = ("visible", "ir")

# ---- edge sites --------------------------------------------------------------------
N_EDGES = 4000             # strongest edge sites to sample
BORDER_PX = 350            # site-search border suppression
CELL_COARSE = 96           # one site per cell, first pass
CELL_MIN = 24              # = PROFILE_HALF: closer than that two sites read the same pixels
TARGET_CELLS = 1500        # cells to lay over a small object's extent
EXTENT_TRIM = 2.0          # percentile trim on the coarse extent (dust, plate marks)
MIN_COARSE_SITES = 8       # fewer is not an object, it is noise
EXTENT_MAX_FRAC = 0.5      # of the frame area: above this the coarse grid is all there is
# ---- profile fit -------------------------------------------------------------------
RISE_10_90_PER_SIGMA = 2.5631   # 10-90 % width of a step blurred with sigma
PROFILE_HALF = 24               # px sampled on each side of an edge site
MIN_CONTRAST = 30               # min plateau difference of a usable profile
WIDTH_FLOOR_PX = 0.8            # widths below are demosaic artifacts, not optics
# ---- statistics --------------------------------------------------------------------
MIN_EDGES_TOTAL = 40
ORIENTATION_BIN_DEG = 45
MIN_EDGES_PER_BIN = 25


def decode(path: str) -> np.ndarray:
    """RGB uint8 frame of a raw file, DECODE recipe."""
    with rawpy.imread(path) as raw:
        return raw.postprocess(**DECODE)


def gray_for(rgb: np.ndarray, kind: str) -> np.ndarray:
    """The channel the metric measures on, float32: luminance for VIS, green for IR."""
    if kind == "visible":
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    if kind == "ir":
        return rgb[:, :, 1].astype(np.float32)
    raise ValueError(f"kind must be one of {KINDS}, not {kind!r}")


def measure(path: str, kind: str) -> dict:
    return measure_rgb(decode(path), kind)


_NOT_MEASURABLE = {"sharp_px": None, "median_px": None, "n_edges": 0, "n_rejected_subpx": 0,
                   "orientation_counts": {}, "orientation_p20": {}, "orientation_balance": None}


def measure_rgb(rgb: np.ndarray, kind: str) -> dict:
    """Measure one decoded frame. Always a result: the cards' geometry is found first and
    is reported even when the sharpness is not measurable (too few real edges — a
    stitching reference frame with nothing but the cards on the plate); `sharp_px` is
    None then, and so are the other sharpness numbers.

    sharp_px             20th percentile of edge widths — THE sharpness number, or None
    median_px            median width (content-dependent, for context)
    n_edges              successfully measured edges
    n_rejected_subpx     fits discarded below WIDTH_FLOOR_PX
    orientation_counts   edges per 45° orientation bin
    orientation_p20      p20 per bin (None: too few edges in that bin)
    orientation_balance  weakest bin count / strongest bin count
    excluded             {"cc": bool, "scale": bool} — which cards were masked
    regions              {"cc": [polygon], "scale": [polygon]} in frame px
    scale_card           {px_per_mm, ticks, residual, angle} of the scale card's comb, or None
    metric_version       METRIC_VERSION
    """
    gray = gray_for(rgb, kind)
    found = cards.find(rgb, gray, kind)
    polygons = {name: found[name] for name in ("cc", "scale")}
    mask = cards.exclusion_mask(gray.shape, polygons)
    result = _reduce(gray, _sites(gray, mask))
    if result is None:
        result = dict(_NOT_MEASURABLE)
    result["excluded"] = {name: bool(p) for name, p in polygons.items()}
    result["regions"] = {name: [np.round(p, 1).tolist() for p in plist]
                         for name, plist in polygons.items()}
    card = found["scale_card"]
    result["scale_card"] = None if card is None else {
        "px_per_mm": round(card["px_per_mm"], 4), "ticks": int(card["ticks"]),
        "residual": round(card["residual"], 3), "angle": round(card["angle"], 1)}
    result["metric_version"] = METRIC_VERSION
    return result


# ---- edge sites ------------------------------------------------------------------

def _grid_sites(gx, gy, m, y0, y1, x0, x1, cell, floor, n):
    """One site per `cell` box inside [y0:y1, x0:x1]: the strongest gradient in the cell,
    kept when it clears `floor`. `m` is the magnitude map with border and exclusions
    already zeroed."""
    h, w = y1 - y0, x1 - x0
    gh, gw = h // cell, w // cell
    if gh < 1 or gw < 1:
        return []
    sub = m[y0:y0 + gh * cell, x0:x0 + gw * cell]
    flat = sub.reshape(gh, cell, gw, cell).transpose(0, 2, 1, 3).reshape(gh, gw, -1)
    peak, pval = flat.argmax(axis=2), flat.max(axis=2)
    sites = []
    for k in np.argsort(pval, axis=None)[::-1][:n]:
        cy, cx = divmod(int(k), gw)
        if pval[cy, cx] < floor:
            break
        py, px = divmod(int(peak[cy, cx]), cell)
        y, x = y0 + cy * cell + py, x0 + cx * cell + px
        sites.append((y, x, gx[y, x], gy[y, x]))
    return sites


def _sites(gray, mask):
    """Edge sites (y, x, gx, gy), the grid matched to where the edges actually are."""
    gx = cv2.Scharr(gray, cv2.CV_32F, 1, 0)
    gy = cv2.Scharr(gray, cv2.CV_32F, 0, 1)
    mag = cv2.magnitude(gx, gy)
    m = np.zeros_like(mag)
    b = BORDER_PX
    m[b:-b, b:-b] = mag[b:-b, b:-b]
    if mask is not None:
        m[mask] = 0
    H, W = m.shape

    # coarse pass: the bar every later pass is held to — an estimate of the frame's own
    # noise level, which the coarse grid (most of its cells empty plate) is what can give
    gh, gw = H // CELL_COARSE, W // CELL_COARSE
    flat = m[:gh * CELL_COARSE, :gw * CELL_COARSE].reshape(
        gh, CELL_COARSE, gw, CELL_COARSE).transpose(0, 2, 1, 3).reshape(gh, gw, -1)
    floor = max(220.0, 1.5 * float(np.median(flat.max(axis=2))))
    coarse = _grid_sites(gx, gy, m, 0, H, 0, W, CELL_COARSE, floor, N_EDGES)
    if len(coarse) < MIN_COARSE_SITES:
        return coarse

    ys = np.array([s[0] for s in coarse], dtype=float)
    xs = np.array([s[1] for s in coarse], dtype=float)
    y0, y1 = np.percentile(ys, [EXTENT_TRIM, 100 - EXTENT_TRIM])
    x0, x1 = np.percentile(xs, [EXTENT_TRIM, 100 - EXTENT_TRIM])
    y0, y1 = int(max(b, y0)), int(min(H - b, y1) + 1)
    x0, x1 = int(max(b, x0)), int(min(W - b, x1) + 1)
    area = max(1, (y1 - y0) * (x1 - x0))
    if area > EXTENT_MAX_FRAC * H * W:          # frame-filling object: the coarse grid is all it needs
        return coarse
    cell = int(min(CELL_COARSE, max(CELL_MIN, np.sqrt(area / TARGET_CELLS))))
    if cell >= CELL_COARSE:
        return coarse
    fine = _grid_sites(gx, gy, m, y0, y1, x0, x1, cell, floor, N_EDGES)
    return fine if len(fine) > len(coarse) else coarse   # never less than the coarse pass had


# ---- profile fit ------------------------------------------------------------------

def _sample_profile(gray, site):
    """Brightness profile across one edge site along its gradient direction, or None
    when the profile would leave the image."""
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


def _rise_width(ts, profile):
    """10-90 % width of the erf curve fitted to the raw values of the rising segment
    around the profile centre, or None for unusable profiles. The segment is LOCATED on a
    lightly smoothed copy (noise would break the monotonic walk); the fit runs on the
    unsmoothed values. Fitting only the segment (plus a small margin) keeps thin ink
    strokes measurable, whose full profile is a valley rather than one step."""
    raw = profile.astype(np.float64)
    smoothed = cv2.GaussianBlur(raw.reshape(1, -1).astype(np.float32), (0, 0), 1.2).ravel()
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


# ---- statistics ----------------------------------------------------------------------

def _reduce(gray, sites):
    widths, orientations = [], []
    n_rejected_subpx = 0
    for site in sites:
        sampled = _sample_profile(gray, site)
        if sampled is None:
            continue
        width = _rise_width(*sampled)
        if width is None:
            continue
        if width < WIDTH_FLOOR_PX:
            n_rejected_subpx += 1
            continue
        widths.append(width)
        orientations.append(np.degrees(np.arctan2(site[3], site[2])) % 180.0)
    if len(widths) < MIN_EDGES_TOTAL:
        return None
    widths = np.array(widths)
    orientations = np.array(orientations)
    # bin keys as strings ("0", "45", "90", "135"): the result is persisted as JSON on
    # both sides, and a result must equal its own round trip
    orientation_counts, orientation_p20 = {}, {}
    for start in range(0, 180, ORIENTATION_BIN_DEG):
        in_bin = widths[(orientations >= start) & (orientations < start + ORIENTATION_BIN_DEG)]
        orientation_counts[str(start)] = int(len(in_bin))
        orientation_p20[str(start)] = (float(np.percentile(in_bin, 20))
                                       if len(in_bin) >= MIN_EDGES_PER_BIN else None)
    return {
        "n_edges": len(widths),
        "sharp_px": float(np.percentile(widths, 20)),
        "median_px": float(np.median(widths)),
        "orientation_counts": orientation_counts,
        "orientation_p20": orientation_p20,
        "orientation_balance": min(orientation_counts.values()) / max(orientation_counts.values()),
        "n_rejected_subpx": n_rejected_subpx,
    }

