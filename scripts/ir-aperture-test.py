#!/usr/bin/env python
"""IR aperture verification test for the Nikon D90 + CoastalOpt 60/4.

Captures one frame per aperture so you can (a) confirm the camera body really
drives the aperture and (b) view evenly-exposed comparison shots. Because the
IR lamp runs HOT, the script does ALL camera setup while the light is OFF,
then prompts you to switch the light ON only for the tight capture loop, and
tells you to switch it OFF the instant the last frame lands.

Exposure is held constant across the series: the shutter compensates for each
aperture (snapped to the camera's real shutter steps), so the frames are
mutually comparable and a *missing* aperture coupling would show up as a
brightness ramp. Pass --fixed-shutter to disable compensation.

Lens prerequisites (CoastalOpt 60/4 UV-VIS-IR Macro Apo, a chipped AI-P lens):
  * aperture RING must be parked + locked at f/45 (the red marking) or the
    body throws "fEE" and f-number reads the f/655.35 placeholder;
  * focus is MANUAL — focus in visible light first (the lens is apochromatic
    across VIS-IR, so focus holds in IR).

Usage (camera connected, awake, in PTP mode):
    .venv/bin/python scripts/ir-aperture-test.py
    .venv/bin/python scripts/ir-aperture-test.py \
        --iso 400 --anchor f/8 --shutter 1/30 \
        --apertures f/4 f/5.6 f/8 f/11 f/16 f/22 f/32 f/45

If the test frames come out too dark/bright, re-run with a different --shutter
(at the --anchor aperture); every other frame scales from it.
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
import gphoto2 as gp  # noqa: E402  (must come after apply_paths)

try:
    import exifread  # noqa: E402
except ImportError:
    exifread = None

DEFAULT_APERTURES = ["f/4", "f/5.6", "f/8", "f/11", "f/16", "f/22", "f/32", "f/45"]
INVALID_FNUMBER = "f/655.35"  # Nikon placeholder when no aperture is coupled


# ----------------------------------------------------------------- value parsing
def fnumber_value(label: str) -> float:
    """'f/5.6' -> 5.6, '5.6' -> 5.6."""
    return float(label.lower().replace("f/", "").strip())


def shutter_seconds(label: str) -> float | None:
    """Parse a D90 shutterspeed2 label to seconds. 'a/b' -> a/b, plain -> float,
    'Bulb' -> None."""
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
    """Nearest available shutter choice to target_s, compared in log space."""
    scored = []
    for c in choices:
        s = shutter_seconds(c)
        if s and s > 0:
            scored.append((abs(math.log(s) - math.log(target_s)), c))
    return min(scored)[1]


# ---------------------------------------------------------------- camera helpers
def free_usb() -> None:
    """Release the camera from macOS PTPCamera / Preview / the Papyri app."""
    for proc in ("PTPCamera", "ptpcamerad"):
        subprocess.run(["killall", proc], stderr=subprocess.DEVNULL)
    time.sleep(1)


def get_config(cam, name: str):
    return cam.get_config().get_child_by_name(name)


def set_confirm(cam, name: str, value, *, required: bool = True) -> None:
    """Set one widget and read it back. The D90 fails a whole set_config() if
    one widget is bad, so we set widgets individually. Raises on mismatch
    (required) or a missing widget (required)."""
    try:
        cfg = cam.get_config()
        widget = cfg.get_child_by_name(name)
    except gp.GPhoto2Error:
        if required:
            raise SystemExit(f"Camera has no '{name}' widget — wrong profile/body?")
        return
    widget.set_value(value)
    cam.set_config(cfg)
    readback = get_config(cam, name).get_value()
    if str(readback) != str(value):
        raise SystemExit(
            f"Setting {name}={value} did not take (reads {readback!r})."
        )


def capture_to(cam, path: str) -> None:
    """Capture a still and download it to `path` (image kept in camera SDRAM)."""
    file_path = cam.capture(gp.GP_CAPTURE_IMAGE)
    camera_file = cam.file_get(
        file_path.folder, file_path.name, gp.GP_FILE_TYPE_NORMAL
    )
    camera_file.save(path)
    try:
        cam.file_delete(file_path.folder, file_path.name)
    except gp.GPhoto2Error:
        pass


def exif_fnumber(path: str) -> str:
    if exifread is None:
        return "?(install exifread)"
    with open(path, "rb") as fh:
        tags = exifread.process_file(fh, details=False, stop_tag="FNumber")
    tag = tags.get("EXIF FNumber")
    if tag is None:
        return "?"
    try:
        return f"f/{eval(str(tag)):g}"  # 'EXIF FNumber' is a Ratio like 8 or 28/5
    except Exception:
        return str(tag)


# ------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--iso", default="400")
    ap.add_argument("--anchor", default="f/8",
                    help="aperture the --shutter belongs to")
    ap.add_argument("--shutter", default="1/30",
                    help="exposure time at the --anchor aperture")
    ap.add_argument("--apertures", nargs="+", default=DEFAULT_APERTURES)
    ap.add_argument("--fixed-shutter", action="store_true",
                    help="do NOT compensate: keep --shutter for every aperture")
    ap.add_argument("--format", choices=["jpeg", "raw", "both"], default="jpeg")
    ap.add_argument("--out", default=None, help="output directory")
    args = ap.parse_args()

    quality = {"jpeg": "JPEG Fine", "raw": "NEF (Raw)",
               "both": "NEF+Fine"}[args.format]

    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_dir = args.out or os.path.join(
        os.path.expanduser("~"), "CaptureTests", f"ir-aperture-test-{stamp}"
    )
    os.makedirs(out_dir, exist_ok=True)

    # ---- connect + ALL setup, light still OFF --------------------------------
    free_usb()
    if not list(gp.Camera.autodetect()):
        print("No camera detected (connected, awake, PTP mode?).", file=sys.stderr)
        return 1
    cam = gp.Camera()
    cam.init()
    try:
        model = get_config(cam, "cameramodel").get_value()
        print(f"Connected: {model}")

        # Aperture coupling sanity check BEFORE we ask for the hot light.
        fwidget = get_config(cam, "f-number")
        choices = [fwidget.get_choice(i) for i in range(fwidget.count_choices())]
        if choices == [INVALID_FNUMBER] or fwidget.get_value() == INVALID_FNUMBER:
            print(
                "\n*** Aperture not coupled (f-number = f/655.35). ***\n"
                "Set the lens aperture RING to f/45 (red mark) and lock it,\n"
                "then re-run. (This is the 'fEE' condition.)",
                file=sys.stderr,
            )
            return 2
        for a in args.apertures:
            want = f"f/{fnumber_value(a):g}"
            if want not in choices:
                print(f"Aperture {a} not offered by the body (have: {choices})",
                      file=sys.stderr)
                return 2

        shutter_choices = [
            get_config(cam, "shutterspeed2").get_choice(i)
            for i in range(get_config(cam, "shutterspeed2").count_choices())
        ]

        # Static settings (mirror down for a real mechanical-shutter exposure).
        set_confirm(cam, "expprogram", "M")
        set_confirm(cam, "capturetarget", "Internal RAM", required=False)
        set_confirm(cam, "recordingmedia", "SDRAM", required=False)
        set_confirm(cam, "viewfinder", 0, required=False)
        set_confirm(cam, "imagequality", quality)
        set_confirm(cam, "iso", args.iso)

        # Pre-compute the per-aperture plan (shutter compensation) up front.
        anchor_f = fnumber_value(args.anchor)
        anchor_t = shutter_seconds(args.shutter)
        if anchor_t is None:
            print(f"Bad --shutter {args.shutter!r}", file=sys.stderr)
            return 2
        plan = []
        for a in args.apertures:
            f = fnumber_value(a)
            if args.fixed_shutter:
                shutter = args.shutter
            else:
                target = anchor_t * (f / anchor_f) ** 2
                shutter = snap_shutter(target, shutter_choices)
            plan.append((f"f/{f:g}", shutter))

        ext = "nef" if args.format == "raw" else "jpg"
        print(f"\nPlan ({len(plan)} frames, ISO {args.iso}, {quality}):")
        for fl, sh in plan:
            print(f"  {fl:6} @ {sh:>6}s")
        print(f"\nOutput: {out_dir}")

        # ---- LIGHT ON: tight capture loop ------------------------------------
        print("\n" + "=" * 56)
        input(">>> Focus set (visible light)? Switch IR LIGHT *ON*, then ENTER ")
        print("=" * 56)
        t0 = time.time()
        results = []
        for fl, shutter in plan:
            set_confirm(cam, "f-number", fl)
            set_confirm(cam, "shutterspeed2", shutter)
            fname = f"ir_{fl.replace('/', '')}_{shutter.replace('/', '-')}s.{ext}"
            dest = os.path.join(out_dir, fname)
            capture_to(cam, dest)
            results.append((fl, shutter, dest))
            print(f"  captured {fl:6} @ {shutter:>6}s -> {fname}")
        elapsed = time.time() - t0
        print("\n" + "#" * 56)
        print(f"###  DONE — switch IR LIGHT *OFF* NOW.  (on for {elapsed:.1f}s)")
        print("#" * 56)
    finally:
        cam.exit()

    # ---- light OFF: verify aperture actually landed in each file -------------
    print("\nVerification (requested vs EXIF aperture):")
    all_ok = True
    for fl, shutter, dest in results:
        actual = exif_fnumber(dest)
        ok = actual.replace(" ", "") == fl.replace(" ", "")
        all_ok = all_ok and (ok or actual.startswith("?"))
        flag = "OK " if ok else ("?? " if actual.startswith("?") else "MISMATCH")
        print(f"  [{flag}] requested {fl:6} -> EXIF {actual:8} ({os.path.basename(dest)})")
    print(f"\n{'All apertures verified.' if all_ok else 'Mismatches above!'}")
    print(f"Photos: {out_dir}")
    return 0 if all_ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
