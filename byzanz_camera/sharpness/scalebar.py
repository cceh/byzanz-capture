#!/usr/bin/env python3
"""Measure the 1 cm scale card in a VIS capture and return px/mm.

The card carries a comb of ~11 parallel tick lines 1 mm apart. Finding the
card by appearance is brittle (it sits anywhere, sometimes rotated, and the
whole background is white), so we look for the *pattern* instead: dark,
thin, elongated blobs that are mutually parallel and evenly spaced. That
signature is unique in these images — papyrus fibres and ink are neither
straight nor periodic.

Scale bookkeeping: everything is measured on a half_size demosaic that is
then downscaled by DOWNSCALE, so the reported px/mm is converted back to
full-resolution pixels at the end.
"""
from __future__ import annotations
import math
import numpy as np
import cv2
import rawpy

DOWNSCALE = 1          # on top of rawpy half_size -> 1/2 of full res
MIN_TICKS = 6          # a comb needs at least this many lines to count
SPACING_CV_MAX = 0.18  # allowed spread of the tick spacings within a comb


def _load_gray(path):
    """Demosaic to 1/4 resolution grayscale. Returns (gray, px_factor) where
    px_factor converts a distance here into full-resolution pixels."""
    with rawpy.imread(path) as raw:
        rgb = raw.postprocess(half_size=True, use_camera_wb=True,
                              no_auto_bright=False, output_bps=8)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    if DOWNSCALE > 1:
        gray = cv2.resize(gray, (gray.shape[1] // DOWNSCALE,
                                 gray.shape[0] // DOWNSCALE),
                          interpolation=cv2.INTER_AREA)
    return gray, 2 * DOWNSCALE


def _tick_candidates(gray):
    """Dark elongated blobs, as (cx, cy, angle, length, width)."""
    # Local threshold: the card is white, the ticks are near-black, but
    # global thresholding would also grab every dark papyrus fragment.
    th = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                               cv2.THRESH_BINARY_INV, 51, 25)
    n, _, stats, cents = cv2.connectedComponentsWithStats(th, 8)
    out = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < 8 or area > 4000:
            continue
        long_side, short_side = max(w, h), min(w, h)
        if long_side < 8 or long_side > 220:
            continue
        if short_side > 0.45 * long_side:      # must be a line, not a blob
            continue
        if area < 0.25 * w * h:                # must fill its bbox like a bar
            continue
        angle = 0.0 if w >= h else 90.0
        out.append((cents[i][0], cents[i][1], angle, long_side, short_side))
    return out


def _cluster(along, across, lens):
    """Union-find over ticks that could belong to the same comb: roughly
    aligned on their long axis and stacked no further apart than a few tick
    lengths. Clustering spatially FIRST matters — sorting all ticks of the
    image by position and cutting on gaps lets any stray ink blob that
    happens to fall between two ticks split the comb in half."""
    n = len(along)
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i in range(n):
        for j in range(i + 1, n):
            ref = max(lens[i], lens[j])
            if min(lens[i], lens[j]) < ref / 3:      # wildly different bars
                continue
            if abs(along[i] - along[j]) < 0.8 * ref and \
               abs(across[i] - across[j]) < 2.0 * ref:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def _longest_lattice(idx, across):
    """From a cluster (already sorted by position), return the longest run of
    ticks at a constant pitch. A cluster may pick up a stray blob or lose a
    faint tick; requiring the *whole* cluster to be evenly spaced would throw
    away an otherwise perfect comb over one bad member."""
    a = across[idx]
    if len(a) < 2:
        return None
    pitch = float(np.median(np.diff(a)))
    if pitch <= 1:
        return None
    best = None
    run = [0]
    for i in range(1, len(a)):
        if abs((a[i] - a[run[-1]]) - pitch) <= 0.3 * pitch:
            run.append(i)
        else:
            if best is None or len(run) > len(best):
                best = run
            run = [i]
    if best is None or len(run) > len(best):
        best = run
    return [idx[i] for i in best]


def _combs(ticks):
    """Group ticks into evenly spaced parallel families. Yields dicts."""
    if len(ticks) < MIN_TICKS:
        return []
    pts = np.array([(t[0], t[1]) for t in ticks], float)
    lens = np.array([t[3] for t in ticks], float)
    horiz = np.array([t[2] == 0.0 for t in ticks])

    results = []
    for is_h in (True, False):
        sel = np.where(horiz == is_h)[0]
        if len(sel) < MIN_TICKS:
            continue
        p, L = pts[sel], lens[sel]
        # ticks of a comb are stacked across their short axis
        across = p[:, 1] if is_h else p[:, 0]
        along = p[:, 0] if is_h else p[:, 1]

        for grp in _cluster(along, across, L):
            if len(grp) < MIN_TICKS:
                continue
            idx = _longest_lattice(sorted(grp, key=lambda k: across[k]), across)
            if idx is None or len(idx) < MIN_TICKS:
                continue
            a = across[idx]
            d = np.diff(a)
            if d.mean() <= 1 or d.std() / d.mean() > SPACING_CV_MAX:
                continue
            results.append({
                "n": len(idx),
                "spacing": float(np.median(d)),
                "span": float(a[-1] - a[0]),
                "cv": float(d.std() / d.mean()),
                "cx": float(along[idx].mean()),
                "cy": float(a.mean()),
                "horizontal": bool(is_h),
                "tick_len": float(np.median(L[idx])),
            })
    return results


def measure(path):
    """Return the best comb found, with px/mm in FULL-resolution pixels,
    or None if no scale card is visible."""
    gray, factor = _load_gray(path)
    combs = _combs(_tick_candidates(gray))
    if not combs:
        return None
    # the real comb is the one with the most ticks; ties -> tightest spacing
    best = max(combs, key=lambda c: (c["n"], -c["cv"]))
    best["px_per_mm"] = best["spacing"] * factor
    best["px_per_cm"] = best["px_per_mm"] * 10
    best["candidates"] = len(combs)
    # Scale-invariant sanity check: on this card a tick is ~3.3x as long as
    # the 1 mm pitch. A comb that skipped every other tick would still look
    # evenly spaced, but its ratio would halve — this catches that.
    best["len_ratio"] = best["tick_len"] / best["spacing"]
    best["ok"] = 2.3 <= best["len_ratio"] <= 4.6
    return best


if __name__ == "__main__":
    import sys, json
    for p in sys.argv[1:]:
        r = measure(p)
        print(json.dumps({"file": p.rsplit("/", 1)[-1], **(r or {})},
                         ensure_ascii=False))
