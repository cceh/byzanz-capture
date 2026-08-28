#!/usr/bin/env python3
"""Shared metric primitives: full-res decode + strongest-edge selection.

VENDORED SUBSET of the canonical object_blur.py (croc-viewer): only the
pieces the metric chain (object_blur_v2/_v3/_v4) consumes — `_gray_full`,
`_edge_sites`, `N_EDGES`. The canonical file additionally carries viewer-side tooling
(its own width estimator, `measure()`, a CLI) that this app never runs.
When syncing metric changes, port only these shared pieces.
"""
from __future__ import annotations
import numpy as np
import cv2
import rawpy

N_EDGES = 4000          # strongest edge sites to sample


def _gray_full(path):
    with rawpy.imread(path) as raw:
        rgb = raw.postprocess(half_size=False, use_camera_wb=True, output_bps=8)
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)


def _edge_sites(gray, n, exclude_mask=None, border=200):
    """Strong, non-clustered edge sites as (y, x, gx, gy). `exclude_mask`
    (optional bool array, True = excluded) suppresses regions that must
    not contribute edges - e.g. the ColorChecker / scale card, which sit
    on a different focal plane (see exclusion_masks)."""
    # work on a blurred copy only for SELECTING sites, measure on the original
    gx = cv2.Scharr(gray, cv2.CV_32F, 1, 0)
    gy = cv2.Scharr(gray, cv2.CV_32F, 0, 1)
    mag = cv2.magnitude(gx, gy)
    # suppress the outer border (vignetting, frame edges)
    m = np.zeros_like(mag)
    b = border
    m[b:-b, b:-b] = mag[b:-b, b:-b]
    if exclude_mask is not None:
        m[exclude_mask] = 0
    # greedy pick with spatial thinning: take top sites on a coarse grid so
    # one razor-sharp label doesn't dominate the whole sample
    cell = 96
    H, W = m.shape
    sites = []
    gh, gw = H // cell, W // cell
    flat = m[:gh * cell, :gw * cell].reshape(gh, cell, gw, cell).transpose(0, 2, 1, 3)
    peak = flat.reshape(gh, gw, -1).argmax(axis=2)
    pval = flat.reshape(gh, gw, -1).max(axis=2)
    order = np.argsort(pval, axis=None)[::-1]
    # Adaptive floor: defocus lowers ALL gradients, so a fixed cutoff starves
    # exactly the blurry frames. Anchor on the image's own noise level (the
    # median cell peak is background texture) but keep an absolute minimum so
    # a contrast-free margin shot still returns "nothing" rather than noise.
    floor = max(220.0, 1.5 * float(np.median(pval)))
    for k in order[:n]:
        cy, cx = divmod(int(k), gw)
        if pval[cy, cx] < floor:
            break
        py, px = divmod(int(peak[cy, cx]), cell)
        y, x = cy * cell + py, cx * cell + px
        sites.append((y, x, gx[y, x], gy[y, x]))
    return sites
