#!/usr/bin/env python3
"""Object sharpness metric v3 - v2 (erf fit) hardened against demosaic
artifacts on near-black, low-contrast frames.

What changed vs. v2 (see docs/report-schaerfemass-v2-revalidierung.md
in croc-viewer, Nachtrag 2026-08-18, case P.Tebt. UC 2556):

1. WIDTH FLOOR: fitted widths below WIDTH_FLOOR_PX are discarded as
   non-optical. Real edges through the lens never measure below ~1.2 px
   10-90; sub-0.8 px "edges" are demosaic/noise artifacts. Rejecting
   them at edge level also decontaminates orientation_counts and
   orientation_balance (Bayer artifacts cluster in the 45/90 bins and
   fabricate a shake-like balance on partially contaminated frames).
   This is also what makes the environment-sensitive site floor in
   _edge_sites harmless: whether a degenerate frame yields 10 or 100
   artifact sites, their widths die here, and every environment
   converges to the same "not measurable" verdict - so the risky
   rewrite of the site-floor heuristic itself is intentionally NOT
   part of v3.
2. BORDER BUFFER: site search skips a 350 px border (was 200) -
   defense in depth against sensor-margin bands that older LibRaw
   versions include in the demosaic output (LibRaw 0.21 vs 0.22).
3. Honesty gate unchanged in spirit: fewer than MIN_EDGES_TOTAL
   surviving REAL edges -> None ("not measurable", never "ok"). The
   returned dict reports n_rejected_subpx for diagnostics.

Values are NOT comparable to v1 or v2 (metric_version "v3-erf").
IR frames: pass a green-channel `gray_loader` — the IR rig clips the red
channel, and clipped edges would measure as falsely sharp.
"""
from __future__ import annotations

import sys

import numpy as np

try:
    from . import exclusion_masks, object_blur, object_blur_v2
except ImportError:                # script-style use
    import exclusion_masks
    import object_blur
    import object_blur_v2

METRIC_VERSION = "v3-erf"
WIDTH_FLOOR_PX = 0.8      # widths below are demosaic artifacts, not optics
BORDER_PX = 350           # site-search border suppression (v2: 200)

MIN_EDGES_TOTAL = object_blur_v2.MIN_EDGES_TOTAL
ORIENTATION_BIN_DEG = object_blur_v2.ORIENTATION_BIN_DEG
MIN_EDGES_PER_BIN = object_blur_v2.MIN_EDGES_PER_BIN
MIN_CONTRAST = object_blur_v2.MIN_CONTRAST


def measure(path, gray_loader=None, exclude=("cc", "scale")):
    """Measure one capture. Same contract and return shape as
    object_blur_v2.measure, plus `n_rejected_subpx`; None when too few
    real edges remain ("not measurable", never "ok")."""
    gray = (gray_loader or object_blur._gray_full)(path)
    if exclude:
        mask, mask_found = exclusion_masks.build_mask(gray, exclude)
    else:
        mask, mask_found = None, {}
    sites = object_blur._edge_sites(gray, object_blur.N_EDGES,
                                    exclude_mask=mask, border=BORDER_PX)
    return reduce_sites(gray, mask_found, sites, METRIC_VERSION)


def reduce_sites(gray, mask_found, sites, version):
    """Profile, fit and summarise a set of edge sites — everything from the fit to the
    honesty gate, shared with v4, which differs ONLY in how the sites are chosen."""
    widths, orientations = [], []
    n_rejected_subpx = 0
    for site in sites:
        sampled = object_blur_v2._sample_profile(gray, site)
        if sampled is None:
            continue
        width = object_blur_v2._rise_width_erf_fit(*sampled)
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
        "n_rejected_subpx": n_rejected_subpx,
        "metric_version": version,
    }


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
                  f"balance={result['orientation_balance']:5.3f}   "
                  f"subpx-verworfen={result['n_rejected_subpx']}")
