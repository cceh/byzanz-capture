#!/usr/bin/env python
"""IR aperture x exposure MATRIX for the Nikon D90 + CoastalOpt 60/4.

Shoots a 2D matrix (rows = aperture, columns = exposure offset in EV around the
predicted ETTR) as RAW, then auto-detects the papyrus fragment (no grid, no
manual ROI) and reports, per cell, the substrate ETTR and the sharpness — so we
can pick the optimal (aperture, shutter) in one hot-light session.

Fragment auto-detection: the white background is flat (low local texture) and
touches the image border; the papyrus has a textured edge that walls off its
(possibly smooth) substrate. So: background = flat region connected to the
border, fragment = the rest, hole-filled. Works even when papyrus and
background are equally bright in IR (where brightness thresholding fails).

Columns are aligned in EV across rows, so the "0 EV" column is matched exposure
across apertures (fair sharpness comparison); the brightest non-clipping column
per row is that aperture's ETTR.

Lens: ring locked at f/45 (else fEE); focus manually in visible light first;
do NOT move the fragment afterwards.

Usage:
    .venv/bin/python scripts/ir-matrix-test.py
    .venv/bin/python scripts/ir-matrix-test.py \
        --apertures f/4 f/5.6 f/8 f/11 --anchor f/8 --shutter 1/50 \
        --offsets -0.67 -0.33 0 0.33 --iso 100
"""
from __future__ import annotations

import argparse
import datetime
import math
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
from PIL import Image  # noqa: E402
from scipy.ndimage import (  # noqa: E402
    uniform_filter, binary_fill_holes, binary_closing, binary_opening, label,
)


# ----------------------------------------------------------------- value parsing
def fval(label: str) -> float:
    return float(label.lower().replace("f/", "").strip())


def shutter_seconds(label: str) -> float | None:
    label = label.strip()
    if label.lower() == "bulb":
        return None
    try:
        if "/" in label:
            a, b = label.split("/")
            return float(a) / float(b)
        return float(label)
    except ValueError:
        return None


def snap_shutter(target_s: float, choices: list[str]) -> str:
    scored = []
    for c in choices:
        s = shutter_seconds(c)
        if s and s > 0:
            scored.append((abs(math.log(s) - math.log(target_s)), c))
    return min(scored)[1]


# ---------------------------------------------------------------- camera helpers
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


# ---------------------------------------------------------------- analysis
def gray(path):
    with rawpy.imread(path) as raw:
        rgb = raw.postprocess(no_auto_bright=True, output_bps=8, gamma=(2.222, 4.5))
    return np.asarray(Image.fromarray(rgb).convert("L")).astype(np.float64)


def fragment_mask(g):
    """Auto-detect the papyrus fragment (see module docstring)."""
    m = uniform_filter(g, 25)
    m2 = uniform_filter(g * g, 25)
    tex = np.sqrt(np.clip(m2 - m * m, 0, None))
    flat = tex < 1.5
    lab, _ = label(flat)
    border = np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))
    bg = np.isin(lab, border[border > 0])
    frag = ~bg
    frag = binary_closing(frag, iterations=5)
    frag = binary_fill_holes(frag)
    frag = binary_opening(frag, iterations=3)
    lab2, n2 = label(frag)
    if n2 >= 1:
        sz = np.bincount(lab2.ravel())
        sz[0] = 0
        frag = lab2 == sz.argmax()
    return frag


def raw_frac(path):
    with rawpy.imread(path) as raw:
        v = raw.raw_image_visible.astype(np.float64)
        black = float(np.mean(raw.black_level_per_channel))
        white = float(raw.white_level)
    return np.clip(v - black, 0, None) / max(white - black, 1.0)


def hf_ratio(a):
    a = a - a.mean()
    P = np.abs(np.fft.fftshift(np.fft.fft2(a))) ** 2
    h, w = a.shape
    yy, xx = np.ogrid[:h, :w]
    r = np.sqrt(((yy - h / 2) / h) ** 2 + ((xx - w / 2) / w) ** 2)
    hi = P[(r >= 0.20) & (r < 0.45)].sum()
    mid = P[(r >= 0.03) & (r < 0.20)].sum()
    return hi / mid if mid else 0.0


# ------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apertures", nargs="+", default=["f/4", "f/5.6", "f/8", "f/11"])
    ap.add_argument("--anchor", default="f/8")
    ap.add_argument("--shutter", default="1/50", help="predicted ETTR at --anchor")
    ap.add_argument("--offsets", nargs="+", type=float,
                    default=[-0.67, -0.33, 0.0, 0.33], help="exposure EV offsets")
    ap.add_argument("--iso", default="100")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_dir = args.out or os.path.join(
        os.path.expanduser("~"), "CaptureTests", f"ir-matrix-{stamp}")
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
        sswidget = get(cam, "shutterspeed2")
        sschoices = [sswidget.get_choice(i) for i in range(sswidget.count_choices())]

        set_confirm(cam, "expprogram", "M")
        set_confirm(cam, "capturetarget", "Internal RAM", required=False)
        set_confirm(cam, "recordingmedia", "SDRAM", required=False)
        set_confirm(cam, "viewfinder", 0, required=False)
        set_confirm(cam, "imagequality", "NEF (Raw)")
        set_confirm(cam, "iso", args.iso)

        anchor_f = fval(args.anchor)
        anchor_t = shutter_seconds(args.shutter)
        # Build the plan: per aperture, EV offsets around its compensated ETTR.
        plan = []  # (ap_label, ev, shutter_label)
        for a in args.apertures:
            center = anchor_t * (fval(a) / anchor_f) ** 2
            for ev in args.offsets:
                plan.append((a, ev, snap_shutter(center * (2 ** ev), sschoices)))

        print(f"\nMatrix: {len(args.apertures)} apertures x {len(args.offsets)} "
              f"EV offsets = {len(plan)} RAWs, ISO {args.iso}")
        print(f"Output: {out_dir}")
        print("\n" + "=" * 56)
        input(">>> Focus set? Switch IR LIGHT *ON*, then ENTER ")
        print("=" * 56)
        t0 = time.time()
        cells = []
        for a, ev, sh in plan:
            set_confirm(cam, "f-number", a)
            set_confirm(cam, "shutterspeed2", sh)
            fn = f"{a.replace('/', '')}_ev{ev:+.2f}_{sh.replace('/', '-')}s.nef"
            dest = os.path.join(out_dir, fn)
            capture_to(cam, dest)
            cells.append({"ap": a, "ev": ev, "sh": sh, "path": dest})
            print(f"  {a:6} {ev:+.2f}EV {sh:>6}s")
        print("\n" + "#" * 56)
        print(f"###  DONE — switch IR LIGHT *OFF* NOW.  (on for {time.time()-t0:.1f}s)")
        print("#" * 56)
    finally:
        cam.exit()

    # ---- light OFF: auto-mask + analyse -------------------------------------
    # Mask from the anchor aperture's 0-EV frame (well-exposed, fragment clear).
    ref = min(cells, key=lambda c: (abs(c["ev"]), c["ap"] != args.anchor))
    frag = fragment_mask(gray(ref["path"]))
    print(f"\nFragment auto-detected: {frag.mean()*100:.1f}% of frame "
          f"(ref {os.path.basename(ref['path'])})")

    for c in cells:
        fr = raw_frac(c["path"])
        fm = np.asarray(Image.fromarray((frag * 255).astype("uint8"))
                        .resize((fr.shape[1], fr.shape[0]))) > 127
        sub = fr[fm]
        c["p999"] = float(np.percentile(sub, 99.9)) * 100
        c["clip"] = float(np.mean(sub >= 0.999)) * 100
        g = gray(c["path"])
        gm = np.asarray(Image.fromarray((frag * 255).astype("uint8"))
                        .resize((g.shape[1], g.shape[0]))) > 127
        ys, xs = np.where(gm)
        c["hf"] = hf_ratio(g[ys.min():ys.max()+1, xs.min():xs.max()+1])

    aps = list(dict.fromkeys(c["ap"] for c in cells))
    evs = sorted({c["ev"] for c in cells})
    print("\n== Substrate p99.9 % (ETTR) / clip% — rows=aperture, cols=EV ==")
    head = "  ".join(f"{e:+.2f}EV" for e in evs)
    print(f"  {'ap':6} {head}")
    for a in aps:
        row = []
        for e in evs:
            c = next(x for x in cells if x["ap"] == a and x["ev"] == e)
            mark = "!" if c["clip"] > 0.5 else " "
            row.append(f"{c['p999']:4.0f}{mark}")
        print(f"  {a:6} " + "    ".join(row))

    print("\n== Sharpness (HF-ratio) — rows=aperture, cols=EV ==")
    print(f"  {'ap':6} {head}")
    for a in aps:
        row = [f"{next(x for x in cells if x['ap']==a and x['ev']==e)['hf']:.3f}"
               for e in evs]
        print(f"  {a:6} " + "  ".join(row))

    # Per aperture: ETTR = brightest non-clipping cell. Sharpness at that cell.
    print("\n== Per-aperture optimum (ETTR exposure + its sharpness) ==")
    best = []
    for a in aps:
        ettr = [c for c in cells if c["ap"] == a and c["clip"] <= 0.5]
        if not ettr:
            print(f"  {a:6}  all cells clip — widen --offsets downward")
            continue
        pick = max(ettr, key=lambda c: c["p999"])
        best.append(pick)
        print(f"  {a:6}  {pick['sh']:>6}s  p99.9={pick['p999']:.0f}%  HF={pick['hf']:.3f}")
    if best:
        win = max(best, key=lambda c: c["hf"])
        print(f"\n--> Optimal: {win['ap']}, ISO {args.iso}, {win['sh']}s "
              f"(sharpest aperture at its ETTR)")
    print(f"RAWs: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
