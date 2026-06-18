#!/usr/bin/env python
"""Flat-field correction for IR papyrus captures (Nikon D90 RAW/NEF).

Corrects uneven illumination (and lens vignetting) by dividing each capture by
a flat-field reference shot under the SAME lighting/geometry/aperture. This is
the standard fix when the lamps can't be positioned for even coverage.

    corrected = capture / flat x mean(flat)        (after RAW black subtraction)

You take the flats yourself (a uniform, matte, featureless surface filling the
frame — e.g. the white background or a grey card — under the same lamps, same
aperture, lamp not moved). This script only computes the correction.

By default the flat is heavily smoothed so only the low-frequency illumination
gradient + vignetting are corrected (not the flat's own noise or any marks on
the target). Use --no-smooth for full per-pixel correction (then average many
flats to keep noise down).

Usage:
    .venv/bin/python scripts/ir-flatfield.py \
        --flat flat1.nef flat2.nef --dark dark1.nef \
        capture1.nef capture2.nef ...
Outputs <name>_ff.tif (16-bit grey) + <name>_ff.png (8-bit preview) per capture.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import rawpy
from PIL import Image
from scipy.ndimage import gaussian_filter

GAMMA = 2.222  # sRGB-ish, for display-encoded output


def dev_linear(path):
    """Demosaic to linear RGB with neutral WB (raw-proportional channels)."""
    with rawpy.imread(path) as raw:
        return raw.postprocess(
            no_auto_bright=True, output_bps=16, gamma=(1, 1),
            use_camera_wb=False, use_auto_wb=False, user_wb=[1, 1, 1, 1],
        ).astype(np.float64)


def mean_stack(paths):
    if not paths:
        return None
    acc = None
    for p in paths:
        d = dev_linear(p)
        acc = d if acc is None else acc + d
    return acc / len(paths)


def uniformity(gray):
    """Spread of a (smoothed) luminance field as p95/p5 ratio, in %."""
    g = gaussian_filter(gray, 60)
    p5, p95 = np.percentile(g, [5, 95])
    return (p95 / max(p5, 1e-6) - 1) * 100


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("captures", nargs="+", help="capture RAW/NEF files to correct")
    ap.add_argument("--flat", nargs="+", required=True, help="flat-field RAW(s)")
    ap.add_argument("--dark", nargs="*", default=[], help="optional dark frame RAW(s)")
    ap.add_argument("--no-smooth", action="store_true",
                    help="full per-pixel correction (else smooth: gradient only)")
    ap.add_argument("--sigma", type=float, default=100.0,
                    help="smoothing sigma in px for the illumination map")
    ap.add_argument("--out", default=None, help="output directory")
    args = ap.parse_args()

    dark = mean_stack(args.dark)
    flat = mean_stack(args.flat)
    if dark is not None:
        flat = flat - dark
    flat = np.clip(flat, 1.0, None)

    if args.no_smooth:
        flat_map = flat
    else:
        flat_map = np.stack(
            [gaussian_filter(flat[..., c], args.sigma) for c in range(flat.shape[2])],
            axis=-1,
        )
    flat_map = np.clip(flat_map, 1.0, None)
    gain = flat_map.mean(axis=(0, 1), keepdims=True) / flat_map

    before = uniformity(flat.mean(axis=2))
    after = uniformity((flat * gain).mean(axis=2))
    print(f"Flat illumination spread: {before:.0f}% -> {after:.0f}% after correction "
          f"({'smoothed, ' if not args.no_smooth else ''}sigma={args.sigma:g})")

    for cap in args.captures:
        subj = dev_linear(cap)
        if dark is not None:
            subj = subj - dark
        corr = np.clip(subj * gain, 0, 65535)
        gray = corr.mean(axis=2)
        disp = np.clip(gray / 65535.0, 0, 1) ** (1 / GAMMA)
        out_dir = args.out or os.path.dirname(os.path.abspath(cap))
        os.makedirs(out_dir, exist_ok=True)
        base = os.path.splitext(os.path.basename(cap))[0] + "_ff"
        Image.fromarray((disp * 65535).astype(np.uint16)).save(
            os.path.join(out_dir, base + ".tif"))
        Image.fromarray((disp * 255).astype(np.uint8)).save(
            os.path.join(out_dir, base + ".png"))
        print(f"  {os.path.basename(cap)} -> {base}.tif / .png")
    print(f"Output: {args.out or 'alongside captures'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
