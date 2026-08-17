"""App-icon generator for papyri.

Reads a raw mascot PNG, runs the corrective color pipeline (cool-cast
removal, warm tint, highlight compression, brightness lift,
saturation boost), and composites the result on a macOS-spec
rounded-squircle background with a drop shadow. Writes all standard
PNG sizes plus a 1024 master for `.icns` / `.ico` regeneration.

Also exposes `processed_mascot(...)` returning the corrected
transparent-background master, used by the in-UI mascot asset.

Run from the project venv:
    source venv/bin/activate
    python docs/design-experiments/icon_generator.py

Alternate icon from an already processed transparent mascot:
    python docs/design-experiments/icon_generator.py \
        --master papyri/ui/mascot_grandpa.png --icon-name app_icon_alt \
        --gradient-top E2E5E9 --gradient-bottom AEB5BE --icns

Tweak constants at the top to re-target source, gradient, or layout.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter


# ---- inputs / outputs --------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_MASCOT = Path('/Users/mts/Downloads/Design ohne Titel.png')
ICON_DIR   = REPO_ROOT / 'ui' / 'icon'
MASCOT_OUT = REPO_ROOT / 'papyri' / 'ui' / 'mascot.png'

# Gradient colors for the squircle background (TOP, BOT).
# Parchment — warm light cream / tan, gives the detailed mascot
# room to breathe without competing for attention.
GRADIENT_TOP = (245, 232, 206)
GRADIENT_BOT = (220, 200, 168)


# ---- mascot color pipeline ---------------------------------------------

def to_arr(img): return np.asarray(img.convert('RGBA'), dtype=np.float32)
def from_arr(a): return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), 'RGBA')

def _rgb_to_hsv(rgb):
    r, g, b = rgb[..., 0]/255, rgb[..., 1]/255, rgb[..., 2]/255
    mx, mn = np.maximum(np.maximum(r, g), b), np.minimum(np.minimum(r, g), b); d = mx - mn
    s = np.where(mx > 0, d / np.maximum(mx, 1e-6), 0)
    h = np.zeros_like(mx); m = d > 1e-6
    rc = (mx-r)/np.maximum(d, 1e-6); gc = (mx-g)/np.maximum(d, 1e-6); bc = (mx-b)/np.maximum(d, 1e-6)
    h = np.where((mx == r)&m, (bc-gc), h)
    h = np.where((mx == g)&m, 2.0 + (rc-bc), h)
    h = np.where((mx == b)&m, 4.0 + (gc-rc), h)
    return np.stack([(h*60) % 360, s, mx], axis=-1)

def _shift_cast(img, strength=0.9):
    """Warm up blue-leaning shadows / mid-tones (residual cool ambient
    cast from the source image's blue-sky lighting). Eye blue is
    protected via the saturation gate."""
    arr = to_arr(img); rgb = arr[..., :3]; a = arr[..., 3]; S = _rgb_to_hsv(rgb)[..., 1]
    be = rgb[..., 2] - (rgb[..., 0] + rgb[..., 1]) / 2
    t = (S < 0.55) & (be > 3) & (a > 10); sh = np.clip(be, 0, 80) * strength
    o = arr.copy()
    o[..., 0] = np.where(t, rgb[..., 0] + sh*0.6,  rgb[..., 0])
    o[..., 1] = np.where(t, rgb[..., 1] + sh*0.15, rgb[..., 1])
    o[..., 2] = np.where(t, rgb[..., 2] - sh*1.0,  rgb[..., 2])
    return from_arr(o)

def _global_warm(img):
    """Subtle warm tint over the whole figure — completes the cast
    correction with a per-channel multiply."""
    arr = to_arr(img); a = arr[..., 3]; fig = a > 10; o = arr.copy()
    for c, mul in ((0, 1.03), (1, 1.01), (2, 0.97)):
        o[..., c] = np.where(fig, arr[..., c]*mul, arr[..., c])
    return from_arr(o)

def _reduce_highlights(img, ceiling=0.75, threshold=120):
    """Compress the top end of brightness so specular highlights
    don't read 'sunlit'. Saturated pixels (eye) get partial
    protection so the focal color still pops."""
    arr = to_arr(img); rgb = arr[..., :3]; a = arr[..., 3]; S = _rgb_to_hsv(rgb)[..., 1]
    v = rgb.max(axis=-1); above = np.maximum(0, v - threshold)
    hr = max(1.0, 255 - threshold)
    scale = 1 - (above/hr) * (1 - ceiling); protect = np.clip(S*1.8, 0, 1)
    scale = scale*(1 - protect) + (1 - (1 - scale)*0.4)*protect
    fig = a > 10; o = arr.copy()
    for c in range(3): o[..., c] = np.where(fig, rgb[..., c]*scale, rgb[..., c])
    return from_arr(o)

def _brighten(img, gain=1.18):
    """Overall brightness lift with a soft knee near 240 so bright
    pixels don't all clamp to pure white."""
    arr = to_arr(img); rgb = arr[..., :3]; a = arr[..., 3]; fig = a > 10
    b = rgb*gain; over = np.maximum(0, b - 240)
    soft = b - over * (over / np.maximum(over + 15, 1e-6))
    o = arr.copy()
    for c in range(3): o[..., c] = np.where(fig, soft[..., c], rgb[..., c])
    return from_arr(o)

def _boost_saturation(img, factor=1.20):
    return ImageEnhance.Color(img).enhance(factor)


def processed_mascot(raw_path: Path = RAW_MASCOT) -> Image.Image:
    """Load the raw mascot, tight-crop to its non-transparent bbox,
    run the color pipeline. Returns an RGBA image (transparent
    background) sized to the figure's natural aspect."""
    src = Image.open(raw_path).convert('RGBA')
    side = max(src.size)
    pad = Image.new('RGBA', (side, side), (0, 0, 0, 0))
    pad.paste(src, ((side - src.width) // 2, (side - src.height) // 2), src)
    arr = np.array(pad); alpha = arr[..., 3]; nz = alpha > 10
    rows = np.any(nz, axis=1); cols = np.any(nz, axis=0)
    y0, y1 = np.where(rows)[0][[0, -1]]
    x0, x1 = np.where(cols)[0][[0, -1]]
    tight = pad.crop((x0, y0, x1 + 1, y1 + 1))
    return _boost_saturation(
        _brighten(_reduce_highlights(_global_warm(_shift_cast(tight, 0.9)), 0.75), 1.18),
        1.20,
    )


# ---- icon assembly -----------------------------------------------------

# Apple's macOS icon spec (canvas 1024×1024, squircle 824×824 centered):
SQUIRCLE_RATIO  = 824 / 1024     # 80.5% of canvas, leaves transparent gutter
SQUIRCLE_CORNER = 185.4 / 824    # 22.5% of squircle
SHADOW_BLUR_RATIO = 28 / 1024
SHADOW_Y_RATIO    = 12 / 1024
SHADOW_ALPHA      = 128          # 50% black

# Figure placement inside the squircle:
FIG_IN_SQUIRCLE = 0.96   # longest side fills 96% of squircle
H_SQUEEZE       = 0.96   # non-uniform: x-scale = 96% of proportional


def make_icon(size: int, master: Image.Image,
              top=GRADIENT_TOP, bot=GRADIENT_BOT) -> Image.Image:
    canvas = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    sq = int(round(size * SQUIRCLE_RATIO))
    off = (size - sq) // 2
    corner = int(round(sq * SQUIRCLE_CORNER))

    # Drop shadow under the squircle (skipped at sizes too small to
    # actually show blur).
    blur = size * SHADOW_BLUR_RATIO
    y_shift = int(round(size * SHADOW_Y_RATIO))
    if blur >= 0.5:
        shadow = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            [(off, off + y_shift), (off + sq - 1, off + sq - 1 + y_shift)],
            radius=corner, fill=(0, 0, 0, SHADOW_ALPHA))
        shadow = shadow.filter(ImageFilter.GaussianBlur(radius=blur))
        canvas = Image.alpha_composite(canvas, shadow)

    # Squircle with vertical gradient fill.
    mask = Image.new('L', (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [(off, off), (off + sq - 1, off + sq - 1)], radius=corner, fill=255)
    grad = Image.new('RGBA', (1, size)); px = grad.load()
    for y in range(size):
        t = y / max(1, size - 1)
        px[0, y] = (int(top[0] + (bot[0] - top[0]) * t),
                    int(top[1] + (bot[1] - top[1]) * t),
                    int(top[2] + (bot[2] - top[2]) * t), 255)
    grad = grad.resize((size, size), Image.BILINEAR)
    layer = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    layer.paste(grad, (0, 0), mask)
    canvas = Image.alpha_composite(canvas, layer)

    # Figure: bottom-anchored inside the squircle, horizontally
    # squeezed slightly so it doesn't read as too wide.
    target_long = int(round(sq * FIG_IN_SQUIRCLE))
    s = target_long / max(master.size)
    cw = int(master.size[0] * s * H_SQUEEZE)
    ch = int(master.size[1] * s)
    scaled = master.resize((cw, ch), Image.LANCZOS)
    fl = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    fl.paste(scaled, (off + (sq - cw) // 2, off + sq - ch), scaled)
    clipped = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    clipped.paste(fl, (0, 0), mask)
    canvas = Image.alpha_composite(canvas, clipped)
    return canvas


# ---- driver ------------------------------------------------------------

ICON_SIZES = (16, 24, 32, 48, 64, 128, 256, 512)


def _rgb(value: str) -> tuple[int, int, int]:
    value = value.removeprefix('#')
    if len(value) != 6:
        raise argparse.ArgumentTypeError("expected a six-digit RGB hex color")
    try:
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a six-digit RGB hex color") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master", type=Path,
                        help="already processed transparent mascot PNG")
    parser.add_argument("--icon-name", default="app_icon")
    parser.add_argument("--gradient-top", type=_rgb, default=GRADIENT_TOP)
    parser.add_argument("--gradient-bottom", type=_rgb, default=GRADIENT_BOT)
    parser.add_argument("--icns", action="store_true",
                        help="also write a macOS .icns bundle")
    args = parser.parse_args()

    if args.master:
        master = Image.open(args.master).convert('RGBA')
        alpha = np.asarray(master)[..., 3]
        rows = np.any(alpha > 10, axis=1)
        cols = np.any(alpha > 10, axis=0)
        y0, y1 = np.where(rows)[0][[0, -1]]
        x0, x1 = np.where(cols)[0][[0, -1]]
        master = master.crop((x0, y0, x1 + 1, y1 + 1))
    else:
        master = processed_mascot()

    # Drop the transparent mascot for in-UI use. Saved at native
    # resolution; PyQt can downsample as needed.
    if not args.master:
        MASCOT_OUT.parent.mkdir(parents=True, exist_ok=True)
        master.save(MASCOT_OUT)
        print(f"wrote {MASCOT_OUT.relative_to(REPO_ROOT)}  ({master.size[0]}×{master.size[1]})")

    # App icon PNGs at every standard size + a 1024 for .icns / .ico
    # regeneration time.
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    icons = {}
    for size in ICON_SIZES:
        icons[size] = make_icon(size, master, args.gradient_top, args.gradient_bottom)
        icons[size].save(ICON_DIR / f"{args.icon_name}_{size}.png")
    icons[1024] = make_icon(1024, master, args.gradient_top, args.gradient_bottom)
    icons[1024].save(ICON_DIR / f"{args.icon_name}_1024.png")
    print(f"wrote {args.icon_name}_*.png  (sizes {list(ICON_SIZES) + [1024]})")
    if args.icns:
        icons[1024].save(
            ICON_DIR / f"{args.icon_name}.icns",
            append_images=[icons[size] for size in (32, 64, 128, 256, 512)],
        )
        print(f"wrote {args.icon_name}.icns")


if __name__ == '__main__':
    main()
