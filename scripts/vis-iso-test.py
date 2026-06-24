#!/usr/bin/env python
"""VIS ISO quality study for the Sony A7R V.

Answers: "how much do I actually lose by raising ISO to shorten the exposure?"

For each ISO it ETTR-MATCHES the exposure (shutter scaled inversely with ISO, so
the substrate brightness stays constant) and shoots a PAIR of frames. Noise is
measured by SUBTRACTING the pair: the static papyrus texture cancels, leaving
only noise — so the fibre detail that fools a single-frame sharpness metric
(HF-ratio) can't masquerade as quality here. SNR is reported per ISO, plus the
detail level (HF on the noise-reduced pair average) to show detail stays put
while only noise grows.

Physics, so the numbers aren't surprising: matched-ETTR + higher ISO = fewer
photons (shorter exposure), so SNR drops ~sqrt(exposure ratio) from shot noise
ALONE. That cost is fundamental — it's the lost light, not the gain. This test
shows whether the sensor piles EXTRA degradation on top (read noise, the
dual-gain step around ISO 640 on the A7R V) and whether the surviving SNR is
good enough to justify the shorter exposure.

Capture is the exact validated Sony path from vis-matrix-test.py (port-pinned so
the D90 can stay plugged in, async-lag-aware set, trigger_capture + FILE_ADDED).
Focus is locked for the run; focus once beforehand and do not move the fragment.

Usage:
    .venv/bin/python scripts/vis-iso-test.py --aperture f/8 \
        --base-iso 100 --base-shutter 1 --isos 100 200 400 800 1600 3200
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import os
import sys
import time

# Reuse the validated capture + analysis helpers from the matrix script verbatim
# (it ran apply_paths and the gp/np/rawpy/PIL/scipy imports at module load).
_VM_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vis-matrix-test.py")
_spec = importlib.util.spec_from_file_location("vis_matrix", _VM_PATH)
_vm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_vm)

free_usb = _vm.free_usb
open_sony = _vm.open_sony
get = _vm.get
choices = _vm.choices
set_confirm = _vm.set_confirm
set_soft = _vm.set_soft
capture_to = _vm.capture_to
fval = _vm.fval
shutter_seconds = _vm.shutter_seconds
snap_shutter = _vm.snap_shutter
snap_fnumber = _vm.snap_fnumber
gray = _vm.gray
fragment_mask = _vm.fragment_mask
raw_frac = _vm.raw_frac
hf_ratio = _vm.hf_ratio

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402


def robust_sigma(x: np.ndarray) -> float:
    """MAD-based noise sigma — resists the few high-contrast edge/outlier
    pixels that survive the pair subtraction (e.g. tiny misregistration)."""
    med = np.median(x)
    return 1.4826 * float(np.median(np.abs(x - med)))


def mask_for(frag, shape) -> np.ndarray:
    return np.asarray(Image.fromarray((frag * 255).astype("uint8"))
                      .resize((shape[1], shape[0]))) > 127


# ------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--aperture", default="f/8")
    ap.add_argument("--base-iso", default="100", help="ISO at which --base-shutter ETTRs")
    ap.add_argument("--base-shutter", default="1",
                    help="shutter that hits ETTR at --base-iso (e.g. 1 = 1s)")
    ap.add_argument("--isos", nargs="+", default=["100", "200", "400", "800", "1600", "3200"])
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_dir = args.out or os.path.join(
        os.path.expanduser("~"), "CaptureTests", f"vis-iso-{stamp}")
    os.makedirs(out_dir, exist_ok=True)

    free_usb()
    cam, model = open_sony()
    try:
        print(f"Connected: {model}")
        set_soft(cam, "500e", "4")
        set_soft(cam, "whitebalance", "Daylight")
        set_soft(cam, "focusmode", "Manual")
        set_confirm(cam, "imagequality", "RAW")
        sschoices = choices(cam, "shutterspeed")
        set_confirm(cam, "f-number", snap_fnumber(fval(args.aperture), choices(cam, "f-number")))

        base_iso = float(args.base_iso)
        base_t = shutter_seconds(args.base_shutter)
        # Matched ETTR: keep ISO*time constant, so every ISO sees the same
        # substrate brightness — only the photon count (exposure time) changes.
        plan = []  # (iso, shutter_label)
        for iso in args.isos:
            t = snap_shutter(base_t * base_iso / float(iso), sschoices)
            plan.append((iso, t))

        print(f"\nISO study @ {args.aperture}, ETTR-matched, 2 frames/ISO:")
        for iso, t in plan:
            print(f"  ISO {iso:>5}  {t:>6}s")
        print(f"Output: {out_dir}")
        print("\n" + "=" * 56)
        input(">>> Focus set & locked? Switch VIS LIGHT *ON*, then ENTER ")
        print("=" * 56)
        t0 = time.time()
        rows = []
        for iso, sh in plan:
            set_confirm(cam, "iso", iso)
            set_confirm(cam, "shutterspeed", sh)
            pair = []
            for k in (1, 2):
                dest = os.path.join(out_dir, f"iso{iso}_{sh.replace('/', '-')}s_{k}.arw")
                capture_to(cam, dest)
                pair.append(dest)
            rows.append({"iso": iso, "sh": sh, "a": pair[0], "b": pair[1]})
            print(f"  ISO {iso:>5}  {sh:>6}s  (pair captured)")
        print("\n" + "#" * 56)
        print(f"###  DONE — switch VIS LIGHT *OFF* NOW.  (on for {time.time()-t0:.1f}s)")
        print("#" * 56)
    finally:
        cam.exit()

    # ---- light OFF: mask once, then per-ISO noise from the pair subtraction --
    ref = rows[0]
    frag = fragment_mask(gray(ref["a"]))
    print(f"\nFragment auto-detected: {frag.mean()*100:.1f}% of frame "
          f"(ref {os.path.basename(ref['a'])})")

    for r in rows:
        a = raw_frac(r["a"])
        b = raw_frac(r["b"])
        fm = mask_for(frag, a.shape)
        sa, sb = a[fm], b[fm]
        # Per-frame noise = sigma(a-b)/sqrt(2): texture cancels, noise adds in
        # quadrature. Robust sigma so surviving edges don't dominate.
        sigma = robust_sigma(sa - sb) / np.sqrt(2)
        signal = float(np.median(sa))               # mid-tone substrate level
        r["ettr"] = float(np.percentile(sa, 99.9)) * 100
        r["clip"] = float(np.mean(sa >= 0.999)) * 100
        r["noise"] = sigma * 100                     # % of white
        r["snr_db"] = 20 * np.log10(signal / sigma) if sigma > 0 else float("inf")
        # Detail on the noise-reduced average (a+b)/2 — should stay ~constant
        # across ISO if the only thing changing is noise, not resolution.
        g = 0.5 * (gray(r["a"]) + gray(r["b"]))
        gm = mask_for(frag, g.shape)
        ys, xs = np.where(gm)
        r["hf"] = hf_ratio(g[ys.min():ys.max()+1, xs.min():xs.max()+1])

    base_db = rows[0]["snr_db"]
    print("\n== ISO quality (ETTR-matched) ==")
    print(f"  {'ISO':>5}  {'shutter':>8}  {'ETTR%':>6}  {'noise%':>7}  "
          f"{'SNR dB':>7}  {'ΔSNR':>6}  {'detail':>7}")
    for r in rows:
        mark = "!" if r["clip"] > 0.5 else " "
        print(f"  {r['iso']:>5}  {r['sh']:>7}s  {r['ettr']:5.0f}{mark}  "
              f"{r['noise']:6.2f}%  {r['snr_db']:6.1f}  {r['snr_db']-base_db:+5.1f}  "
              f"{r['hf']:.3f}")

    # Highest ISO still within 3 dB of base SNR (≈ half the noise power budget).
    ok = [r for r in rows if r["snr_db"] >= base_db - 3.0 and r["clip"] <= 0.5]
    print("\nΔSNR is vs the base ISO; detail ~constant = the loss is noise, not "
          "resolution.\n3 dB ≈ 1.4x noise (a common 'still fine' bar).")
    if ok:
        top = max(ok, key=lambda r: float(r["iso"]))
        print(f"--> Highest ISO within 3 dB of base: ISO {top['iso']} at {top['sh']}s "
              f"(SNR {top['snr_db']:.1f} dB, {top['noise']:.2f}% noise).")
    print(f"RAWs: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
