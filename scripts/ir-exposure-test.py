#!/usr/bin/env python
"""IR exposure bracket for the Nikon D90 + CoastalOpt 60/4.

Fixes the aperture and ISO, brackets the shutter as RAW (NEF), then measures
each frame's RAW histogram and recommends the optimal exposure (ETTR: as
bright as possible without clipping). Same hot-light discipline as
ir-aperture-test.py: all setup with the light OFF, capture only between the
ON/OFF prompts.

Lens: ring locked at f/45 (else fEE), focus manually in visible light first.

Usage:
    .venv/bin/python scripts/ir-exposure-test.py
    .venv/bin/python scripts/ir-exposure-test.py --aperture f/8 --iso 100 \
        --shutters 1/30 1/60 1/125 1/250 1/500 1/1000 1/2000 1/4000
"""
from __future__ import annotations

import argparse
import datetime
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from byzanz_camera._gphoto2_paths import apply_paths  # noqa: E402

apply_paths(os.environ.get("CAMLIBS"), os.environ.get("IOLIBS"))
import gphoto2 as gp  # noqa: E402
import numpy as np  # noqa: E402
import rawpy  # noqa: E402

# Brackets around the fragment's likely exposure (it is darker than bare white
# paper, so we include slower times). The bright background may clip — that is
# fine, it is masked out of the measurement.
DEFAULT_SHUTTERS = ["1/15", "1/30", "1/40", "1/50", "1/60", "1/80", "1/125"]


def free_usb() -> None:
    for proc in ("PTPCamera", "ptpcamerad"):
        subprocess.run(["killall", proc], stderr=subprocess.DEVNULL)
    time.sleep(1)


def get(cam, name):
    return cam.get_config().get_child_by_name(name)


def set_confirm(cam, name, value, *, required=True):
    try:
        cfg = cam.get_config()
        w = cfg.get_child_by_name(name)
    except gp.GPhoto2Error:
        if required:
            raise SystemExit(f"No '{name}' widget on this body.")
        return
    w.set_value(value)
    cam.set_config(cfg)
    rb = get(cam, name).get_value()
    if str(rb) != str(value):
        raise SystemExit(f"Setting {name}={value} did not take (reads {rb!r}).")


def capture_to(cam, path):
    fp = cam.capture(gp.GP_CAPTURE_IMAGE)
    cf = cam.file_get(fp.folder, fp.name, gp.GP_FILE_TYPE_NORMAL)
    cf.save(path)
    try:
        cam.file_delete(fp.folder, fp.name)
    except gp.GPhoto2Error:
        pass


def otsu_threshold(x: np.ndarray) -> float:
    """Otsu split of values in [0,1] into a dark and a bright class; returns
    the threshold. Used to separate the (bright, irrelevant) background from
    the darker subject (papyrus fragment + ink)."""
    hist, edges = np.histogram(x, bins=256, range=(0.0, 1.0))
    hist = hist.astype(np.float64)
    centers = (edges[:-1] + edges[1:]) / 2
    w = hist.cumsum()
    mu = (hist * centers).cumsum()
    total_w, total_mu = w[-1], mu[-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        between = (total_mu * w - mu) ** 2 / (w * (total_w - w))
    return float(centers[np.nanargmax(between)])


def measure_raw(path):
    """Measure exposure on the SUBJECT only (the darker region = papyrus
    fragment), masking out the bright background, which is allowed to clip.

    Returns dict with subject p99.9 / clip / mean (% of white level), the
    background clip %, and the subject's share of the frame."""
    with rawpy.imread(path) as raw:
        v = raw.raw_image_visible.astype(np.float64)
        black = float(np.mean(raw.black_level_per_channel))
        white = float(raw.white_level)
    span = max(white - black, 1.0)
    frac = np.clip(v - black, 0, None) / span

    # 2x2 Bayer-quad mean -> per-location brightness map for segmentation.
    h, w = frac.shape
    H, W = h // 2 * 2, w // 2 * 2
    bm = frac[:H, :W].reshape(H // 2, 2, W // 2, 2).mean(axis=(1, 3))
    thresh = otsu_threshold(bm)
    sub = bm[bm < thresh]
    if sub.size == 0:
        sub = bm.ravel()
    return {
        "sub_p999": float(np.percentile(sub, 99.9)) * 100,
        "sub_mean": float(sub.mean()) * 100,
        "sub_clip": float(np.mean(sub >= 0.999)) * 100,
        "bg_clip": float(np.mean(frac >= 0.999)) * 100,
        "sub_frac": float((bm < thresh).mean()) * 100,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--aperture", default="f/8")
    ap.add_argument("--iso", default="100")
    ap.add_argument("--shutters", nargs="+", default=DEFAULT_SHUTTERS)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_dir = args.out or os.path.join(
        os.path.expanduser("~"), "CaptureTests", f"ir-exposure-test-{stamp}")
    os.makedirs(out_dir, exist_ok=True)

    free_usb()
    if not list(gp.Camera.autodetect()):
        print("No camera detected.", file=sys.stderr)
        return 1
    cam = gp.Camera()
    cam.init()
    try:
        print(f"Connected: {get(cam, 'cameramodel').get_value()}")
        if get(cam, "f-number").get_value() == "f/655.35":
            print("Aperture not coupled — set ring to f/45 + lock, re-run.",
                  file=sys.stderr)
            return 2
        set_confirm(cam, "expprogram", "M")
        set_confirm(cam, "capturetarget", "Internal RAM", required=False)
        set_confirm(cam, "recordingmedia", "SDRAM", required=False)
        set_confirm(cam, "viewfinder", 0, required=False)
        set_confirm(cam, "imagequality", "NEF (Raw)")
        set_confirm(cam, "iso", args.iso)
        set_confirm(cam, "f-number", args.aperture)

        print(f"\nBracket @ {args.aperture}, ISO {args.iso}, RAW: "
              f"{', '.join(args.shutters)}")
        print(f"Output: {out_dir}")
        print("\n" + "=" * 56)
        input(">>> Focus set? Switch IR LIGHT *ON*, then ENTER ")
        print("=" * 56)
        t0 = time.time()
        shots = []
        for sh in args.shutters:
            set_confirm(cam, "shutterspeed2", sh)
            dest = os.path.join(out_dir, f"ir_{args.aperture.replace('/', '')}"
                                         f"_{sh.replace('/', '-')}s.nef")
            capture_to(cam, dest)
            shots.append((sh, dest))
            print(f"  captured {sh:>6}s")
        print("\n" + "#" * 56)
        print(f"###  DONE — switch IR LIGHT *OFF* NOW.  (on for "
              f"{time.time() - t0:.1f}s)")
        print("#" * 56)
    finally:
        cam.exit()

    print("\nRAW exposure analysis — measured on the SUBJECT (fragment),")
    print("background masked out (its clipping is shown but ignored):")
    print(f"  {'shutter':>8}  {'subj_mean':>9}  {'subj_p99.9':>10}  "
          f"{'subj_clip':>9}  {'bg_clip':>7}  {'subj%img':>8}  verdict")
    rows = []
    for sh, dest in shots:
        m = measure_raw(dest)
        m["sh"] = sh
        rows.append(m)
        if m["sub_clip"] > 0.5:
            v = "CLIPPED (subject!)"
        elif m["sub_p999"] > 90:
            v = "optimal (ETTR)"
        elif m["sub_p999"] > 70:
            v = "good"
        else:
            v = "dark"
        print(f"  {sh:>8}  {m['sub_mean']:8.1f}%  {m['sub_p999']:9.1f}%  "
              f"{m['sub_clip']:8.2f}%  {m['bg_clip']:6.1f}%  {m['sub_frac']:7.1f}%  {v}")

    # Brightest exposure that does NOT clip the subject (ETTR on the fragment).
    ok = [r for r in rows if r["sub_clip"] <= 0.5]
    best = max(ok, key=lambda r: r["sub_p999"]) if ok else None
    print()
    if best:
        print(f"--> Recommended: {args.aperture}, ISO {args.iso}, "
              f"{best['sh']}s  (fragment p99.9 = {best['sub_p999']:.1f}% of white, "
              f"no subject clipping; background may clip — irrelevant)")
    else:
        print("Subject clipped in every frame — re-run with faster shutters.")
    print(f"RAWs: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
