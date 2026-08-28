#!/usr/bin/env python3
"""Object sharpness metric v4 - v3 with a thinning grid that scales with the OBJECT.

v3 takes at most one edge site per fixed 96 px cell, so how many sites a frame can yield is
decided by how many cells the object covers. That is invisible while the object fills the
frame, and it is why the metric quietly stopped working when the assistants began
photographing small loose fragments: at the fixed IR camera height such a fragment covers
roughly 1000x1000 px, about a hundred cells, and the usual yield of real edges leaves fewer
than twenty - below MIN_EDGES_TOTAL, so "not measurable". Across the archive the failure
rate tracks object size exactly: 1.9% of IR captures taken at 90 cm VIS height, 16% at 60,
79% at 40.

Both thresholds were suspected first and both were cleared by measurement on P.Tebt. Frag.
00058: dropping the site floor from 220 to 50 grew the site count from 98 to 932 and left
the valid edges at 18; relaxing MIN_CONTRAST from 30 to 3 took them to 24; both together
reached 29. The measured width sat at 2.0-2.4 px throughout. The metric was not short of
sensitivity - it was short of cells.

So v4 keeps every threshold, the erf fit, the width floor and the honesty gate of v3
unchanged, and changes ONE thing: when the coarse pass finds the edge material confined to
less than half the frame, the grid is re-laid over just that part, fine enough to sample it
as densely as a frame-filling object is sampled. Everywhere else — and that is 99.9% of VIS
and every frame-filling IR capture — v4 returns exactly v3's sites, bit for bit. Two guards keep that from manufacturing
edges out of nothing:

  - the acceptance bar stays the value the COARSE pass computed. That floor is an estimate
    of the frame's own noise level, and the coarse grid - most of whose cells are empty
    plate - is what can estimate it. Recomputing it over the object's own cells would raise
    the bar precisely where the signal is.
  - cells never go below CELL_MIN, the profile half-length. Closer than that and two sites
    read the same pixels: a larger population saying nothing new.

Values are NOT comparable to v3 (metric_version "v4") - a denser grid samples weaker edges
alongside the strong ones, so the distribution shifts even where v3 already worked.
"""
from __future__ import annotations

import sys

import cv2
import numpy as np

try:
    from . import exclusion_masks, object_blur, object_blur_v2, object_blur_v3
except ImportError:                # script-style use
    import exclusion_masks
    import object_blur
    import object_blur_v2
    import object_blur_v3

METRIC_VERSION = "v4"

CELL_COARSE = 96                          # v3's grid, and the first pass here
CELL_MIN = object_blur_v2.PROFILE_HALF    # 24 px: below this, two sites read the same pixels
TARGET_CELLS = 1500                       # cells to lay over the edge material
EXTENT_TRIM = 2.0                         # percentile trim on the coarse extent (dust, plate marks)
MIN_COARSE_SITES = 8                      # fewer than this is not an object, it is noise
# Refine only when the edge material is confined to a SMALL part of the frame. The first
# draft triggered on the coarse SITE count instead, and a 12 MP IR frame has only 1276
# coarse cells in total — fewer than TARGET_CELLS — so refinement ran on every IR frame,
# frame-filling fragments included, and shifted 99% of the corpus's IR values by ~+0.015 px
# for no gain. Keyed on extent, a frame-filling object stays bit-identical to v3 and only
# the small ones, the reason v4 exists, get new values.
EXTENT_MAX_FRAC = 0.5                     # of the frame area; above this v4 IS v3
BORDER_PX = object_blur_v3.BORDER_PX
WIDTH_FLOOR_PX = object_blur_v3.WIDTH_FLOOR_PX
MIN_EDGES_TOTAL = object_blur_v3.MIN_EDGES_TOTAL


def _grid_sites(gx, gy, m, y0, y1, x0, x1, cell, floor, n):
    """One site per `cell` box inside [y0:y1, x0:x1]: the strongest gradient in the cell,
    kept when it clears `floor`. `m` is the magnitude map with border and exclusions
    already zeroed, so this never has to know about either."""
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
    """Edge sites, with the grid matched to where the edges actually are."""
    gx = cv2.Scharr(gray, cv2.CV_32F, 1, 0)
    gy = cv2.Scharr(gray, cv2.CV_32F, 0, 1)
    mag = cv2.magnitude(gx, gy)
    m = np.zeros_like(mag)
    b = BORDER_PX
    m[b:-b, b:-b] = mag[b:-b, b:-b]
    if mask is not None:
        m[mask] = 0
    H, W = m.shape

    # coarse pass: v3's grid, and the bar every later pass is held to
    gh, gw = H // CELL_COARSE, W // CELL_COARSE
    flat = m[:gh * CELL_COARSE, :gw * CELL_COARSE].reshape(
        gh, CELL_COARSE, gw, CELL_COARSE).transpose(0, 2, 1, 3).reshape(gh, gw, -1)
    floor = max(220.0, 1.5 * float(np.median(flat.max(axis=2))))
    coarse = _grid_sites(gx, gy, m, 0, H, 0, W, CELL_COARSE, floor, object_blur.N_EDGES)

    # A frame with almost no edge material has nothing to refine.
    if len(coarse) < MIN_COARSE_SITES:
        return coarse

    ys = np.array([s[0] for s in coarse], dtype=float)
    xs = np.array([s[1] for s in coarse], dtype=float)
    y0, y1 = np.percentile(ys, [EXTENT_TRIM, 100 - EXTENT_TRIM])
    x0, x1 = np.percentile(xs, [EXTENT_TRIM, 100 - EXTENT_TRIM])
    y0, y1 = int(max(b, y0)), int(min(H - b, y1) + 1)
    x0, x1 = int(max(b, x0)), int(min(W - b, x1) + 1)
    area = max(1, (y1 - y0) * (x1 - x0))
    # A frame-filling object already has all the cells it needs — v4 IS v3 there.
    if area > EXTENT_MAX_FRAC * H * W:
        return coarse
    cell = int(min(CELL_COARSE, max(CELL_MIN, np.sqrt(area / TARGET_CELLS))))
    if cell >= CELL_COARSE:
        return coarse
    fine = _grid_sites(gx, gy, m, y0, y1, x0, x1, cell, floor, object_blur.N_EDGES)
    # never come back with less than the coarse pass already had
    return fine if len(fine) > len(coarse) else coarse


def measure(path, gray_loader=None, exclude=("cc", "scale")):
    """Measure one capture. Same contract and return shape as object_blur_v3.measure;
    None when too few real edges remain ("not measurable", never "ok")."""
    gray = (gray_loader or object_blur._gray_full)(path)
    if exclude:
        mask, mask_found = exclusion_masks.build_mask(gray, exclude)
    else:
        mask, mask_found = None, {}
    return object_blur_v3.reduce_sites(gray, mask_found, _sites(gray, mask), METRIC_VERSION)


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        result = measure(arg)
        name = arg.rsplit("/", 1)[-1]
        if result is None:
            print(f"{name[:46]:<46} nicht messbar")
        else:
            print(f"{name[:46]:<46} n={result['n_edges']:<5} "
                  f"p20={result['sharp_px']:5.2f} px   "
                  f"median={result['median_px']:5.2f} px   "
                  f"balance={result['orientation_balance']:5.3f}")
