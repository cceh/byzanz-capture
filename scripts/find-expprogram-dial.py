#!/usr/bin/env python
"""Poll the camera's `expprogram` (read-only mirror of the physical mode dial)
and announce when it matches a target value.

The D90 dial's scene icons don't map 1:1 to the names gphoto2 reports, so this
helps you find which physical position produces a given gphoto label. Turn the
dial slowly, pausing ~2s on each position; the script logs every distinct value
it sees (with elapsed seconds, so you can correlate icon -> value) and exits the
moment the live value equals --target.

Usage:
    scripts/find-expprogram-dial.py [--target "Night Landscape"] [--timeout 120]
"""
from __future__ import annotations
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from byzanz_camera._gphoto2_paths import apply_paths  # noqa: E402

apply_paths(os.environ.get("CAMLIBS"), os.environ.get("IOLIBS"))
import gphoto2 as gp  # noqa: E402


def read_via_session(cam):
    cfg = cam.get_config()
    return cfg.get_child_by_name("expprogram").get_value()


def read_via_reinit():
    """Open a fresh camera session, read once, close. Reflects the physical
    dial even if a long-lived session caches the value. Clears the macOS
    PTPCamera daemon first so the USB re-claim doesn't fail with -53."""
    import subprocess
    subprocess.run(["killall", "PTPCamera"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cam = gp.Camera()
    cam.init()
    try:
        cfg = cam.get_config()
        return cfg.get_child_by_name("expprogram").get_value()
    finally:
        cam.exit()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="Night Landscape")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--interval", type=float, default=0.3)
    ap.add_argument("--heartbeat", type=float, default=3.0,
                    help="print the current value at least this often, even if unchanged")
    ap.add_argument("--reinit", action="store_true",
                    help="reconnect the camera each read (defeats session caching of the dial)")
    args = ap.parse_args()

    cam = None
    if not args.reinit:
        cam = gp.Camera()
        cam.init()
    mode = "reinit-per-read" if args.reinit else "single-session"
    print(f"Polling ({mode}) for expprogram == {args.target!r}. "
          f"Turn the dial slowly; pause ~2s per scene icon.\n", flush=True)
    start = time.monotonic()
    last = object()
    last_print = -1e9
    seen = []
    try:
        while True:
            elapsed = time.monotonic() - start
            if elapsed > args.timeout:
                print(f"\nTIMEOUT after {args.timeout:.0f}s. "
                      f"Distinct values seen: {seen}", flush=True)
                print(f"Target {args.target!r} "
                      f"{'WAS' if args.target in seen else 'was NOT'} reachable.",
                      flush=True)
                return 1
            try:
                val = read_via_reinit() if args.reinit else read_via_session(cam)
            except gp.GPhoto2Error as e:
                print(f"[{elapsed:5.1f}s] (read error: {e})", flush=True)
                time.sleep(args.interval)
                continue
            changed = val != last
            if changed or (elapsed - last_print) >= args.heartbeat:
                if val not in seen:
                    seen.append(val)
                marker = "  <-- MATCH" if str(val) == str(args.target) else ""
                tag = "" if changed else "  (heartbeat)"
                print(f"[{elapsed:5.1f}s] expprogram = {val!r}{marker}{tag}", flush=True)
                last = val
                last_print = elapsed
            if str(val) == str(args.target):
                print(f"\n>>> MATCH at {elapsed:.1f}s: dial is now on the position "
                      f"that reports {args.target!r}. Leave it here.", flush=True)
                return 0
            time.sleep(args.interval)
    finally:
        if cam is not None:
            cam.exit()


if __name__ == "__main__":
    raise SystemExit(main())
