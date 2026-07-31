#!/usr/bin/env python
"""Seed a virtual camera (vusb) with any RAW or JPEG as its current material.

Writes the image as the capture seed plus a matching live-view frame
sequence into the gitignored per-machine override folder
`vcamera-sources/local/<vusb|vusb2>/`, which the apps resolve with
priority over the committed samples (see
byzanz_camera._gphoto2_paths.apply_vcamera_source_dirs; an explicitly
set VCAMERADIR/VCAMERADIR_2 env var still wins over everything).

Usage (from the repo root, inside the venv):

    ./venv/bin/python scripts/seed_vcam.py 1 ~/shots/flatfield_ir.nef
    ./venv/bin/python scripts/seed_vcam.py 2 ~/shots/papyrus.ARW
    ./venv/bin/python scripts/seed_vcam.py 2 --reset     # back to samples

Camera 1 is the "vusb:" instance, camera 2 the "vusb:2" instance (which
slot that is in papyri depends on the profile assignment in Settings).

RAWs are not demosaiced — the (typically full-size) embedded JPEG
preview is used, byte-identical, via the same helper the apps use
(load_image_worker.read_embedded_jpeg). The live-view frames are a
static view with per-frame sensor-style grain — the temporal noise
alone makes the stream visibly "live", the image itself does not move.
`--drift` adds a seamlessly looping position wobble on top (off by
default; useful to exercise motion-driven tools like the overlap
coach).

Timing of effect: the live-view folder is rescanned by the emulator on
every frame, and seed files are re-stat'ed on every read — so re-seeding
an ALREADY ACTIVE local folder applies mid-session, no reconnect. Only
when the folder is created for the first time (or removed via --reset)
does the running app need a restart, because the env vars are resolved
at startup and the emulator reads its object tree at connect.
"""
import argparse
import io
import math
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

CAMERAS = {"1": "vusb", "vusb": "vusb", "2": "vusb2", "vusb2": "vusb2"}


def build_liveview_frames(src_img, lv_dir: Path, frames: int, grain: float,
                          drift: float) -> tuple[int, int]:
    import numpy as np
    from PIL import Image, ImageDraw

    rng = np.random.default_rng()
    scale = 1100 / src_img.width
    small = src_img.resize((1100, round(src_img.height * scale)), Image.LANCZOS)
    w, h = small.size
    # Drift amplitude (fraction of each axis). 0 = static frame: the crop
    # window sits centered and never moves — only the grain differs per
    # frame, like real sensor noise on a fixed scene.
    mx, my = int(w * drift), int(h * drift)
    for i in range(frames):
        t = i / frames
        if mx or my:
            # Lissajous 1:2 — both axes complete whole cycles over the
            # sequence, so the loop is seamless.
            x = round(mx * (1 + math.sin(2 * math.pi * t)))
            y = round(my * (1 + math.sin(4 * math.pi * t)))
        else:
            x = y = 0
        frame = small.crop((x, y, x + w - 2 * mx, y + h - 2 * my))
        if grain > 0:
            arr = np.asarray(frame, dtype=np.float32)
            arr += rng.normal(0.0, grain, arr.shape).astype(np.float32)
            frame = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
        d = ImageDraw.Draw(frame)
        text = f"live view {i + 1:02d}/{frames}"
        d.text((13, frame.height - 27), text, fill=(0, 0, 0))
        d.text((12, frame.height - 28), text, fill=(235, 235, 235))
        frame.save(lv_dir / f"frame_{i:03d}.jpg", "JPEG", quality=72)
    return w - 2 * mx, h - 2 * my


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed a virtual camera with a RAW/JPEG (capture seed + live-view frames).")
    parser.add_argument("camera", choices=sorted(CAMERAS),
                        help="which virtual camera: 1/vusb = port 'vusb:', 2/vusb2 = port 'vusb:2'")
    parser.add_argument("image", nargs="?", type=Path,
                        help="RAW or JPEG to seed (omit with --reset)")
    parser.add_argument("--frames", type=int, default=24,
                        help="number of live-view frames (default 24, ~1.2 s loop at 20 fps)")
    parser.add_argument("--grain", type=float, default=5.0,
                        help="live-view grain strength, gaussian sigma in 8-bit levels "
                             "(default 5.0; 0 disables)")
    parser.add_argument("--drift", type=float, default=0.0,
                        help="live-view position wobble as a fraction of the frame "
                             "(default 0 = static image; e.g. 0.06 for a gentle "
                             "looping drift, useful for overlap-coach testing)")
    parser.add_argument("--reset", action="store_true",
                        help="remove this camera's local override (falls back to the "
                             "committed samples / compiled-in seed)")
    args = parser.parse_args()

    sub = CAMERAS[args.camera]
    target = REPO_ROOT / "vcamera-sources" / "local" / sub

    if args.reset:
        if args.image is not None:
            parser.error("--reset takes no image argument")
        if not target.is_dir():
            print(f"nothing to reset — {target} does not exist")
            return 0
        shutil.rmtree(target)
        print(f"removed {target}; the '{sub}' camera falls back to the committed "
              f"samples (or the compiled-in seed). Restart the app to pick this up.")
        return 0

    if args.image is None:
        parser.error("an image path is required (or use --reset)")
    if not args.image.is_file():
        parser.error(f"no such file: {args.image}")

    from PIL import Image, ImageOps
    from byzanz_camera.load_image_worker import read_embedded_jpeg

    jpeg = read_embedded_jpeg(str(args.image))
    if not jpeg:
        print(f"ERROR: {args.image} contains no decodable JPEG (RAW without "
              f"embedded preview?)", file=sys.stderr)
        return 1

    first_time = not target.is_dir()
    lv_dir = target / "liveview"
    shutil.rmtree(lv_dir, ignore_errors=True)
    lv_dir.mkdir(parents=True)
    (target / "GPH_0001.JPG").write_bytes(jpeg)

    src = ImageOps.exif_transpose(Image.open(io.BytesIO(jpeg)).convert("RGB"))
    fw, fh = build_liveview_frames(src, lv_dir, args.frames, args.grain, args.drift)

    print(f"seeded '{sub}' from {args.image.name}:")
    print(f"  capture seed: {target / 'GPH_0001.JPG'} ({len(jpeg) // 1024} KB, {src.size[0]}x{src.size[1]})")
    print(f"  live view:    {args.frames} frames ({fw}x{fh}, grain sigma {args.grain})")
    if first_time:
        print("  NOTE: folder is new — restart the app so the resolver and the "
              "emulator's object tree pick it up.")
    else:
        print("  applies immediately (live view per frame, captures per shot) — "
              "no reconnect needed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
