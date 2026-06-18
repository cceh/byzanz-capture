# IR papyrus capture — validated settings (Nikon D90 + CoastalOpt 60/4)

Reference settings and procedure for infrared reflectography of papyrus
fragments on the IR rig. Derived empirically against the real hardware and a
papyrus fragment (June 2026); see "How they were found" below.

## The lens
**Coastal Optical Systems 60 mm f/4 UV-VIS-IR Macro Apo** — a chipped **AI-P**
lens:

- **Aperture is electronically controlled by the body** (there is a motorised
  iris). The physical aperture **ring must be parked and locked at f/45** (the
  red marking). Off f/45 the body throws **"fEE"** and `f-number` reads the
  `f/655.35` placeholder. With the ring at f/45 the aperture is set from the
  camera / app / gphoto2 — verified moving (f/8 → reads f/8, f/16 → f/16).
- **Focus is manual only** — no AF motor, no AF coupling. `manualfocusdrive`
  is rejected with `[-1]`; `focusmode` is read-only "Manual". Focus by hand on
  the ring. The lens is **apochromatic across UV-VIS-IR**, so focus set in
  **visible light** holds in IR (no IR focus shift).

## Standard settings

| Parameter | Value |
|---|---|
| Aperture | **f/5.6** |
| ISO | **100** |
| Shutter | **1/60 s** |
| Aperture ring | **f/45, locked** (body drives the actual aperture) |
| Focus | manual, set in visible light |

These are tuned to **this rig's lamp height/intensity**. If the lighting
changes, re-find the shutter (see `ir-exposure-test.py` / `ir-matrix-test.py`);
aperture/ISO carry over.

### Why these values
- **f/5.6** — sharpest aperture for the central fragment. The matrix showed
  f/5.6 > f/8 (~15 % more HF energy, visually near-identical); **f/4 is soft**
  (wide-open aberration) and **f/11 is soft** (diffraction on the DX sensor).
  Depth of field at f/5.6 is several mm to a few cm depending on framing — far
  more than a flat papyrus needs, so DOF is not a constraint.
- **ISO 100** — best quality; there is ample IR light, so no need to raise it
  (ISO 200 costs ~0.3 stops, only if a shorter time is ever required).
- **1/60 s** — ETTR on the papyrus *substrate*: brightest exposure with **no
  clipping of the fragment** (substrate at ~89 % of white). The white
  background is irrelevant and may clip. 1/50 s already clips the substrate.

## Hot lamp — minimise on-time
The IR lamp runs **hot**. Do all setup with the lamp **off**:
1. Position + focus the fragment under **visible light** (lamp off). To make
   the dim live view brighter you can open the aperture to f/4 temporarily, or
   use a cool IR LED illuminator — note the D90 has **no manual live-view gain**.
2. Lamp **on** only for the actual exposure(s), **off** immediately after.

## Uneven illumination — flat-field correction
The photofloods can't be positioned for fully even coverage (space
constraints); the measured gradient is ~0.4 stops (top ~30 % brighter than
bottom). Correct it in post with a **flat field** instead of fighting the lamps:

1. Shoot a **flat**: a uniform, matte, featureless surface filling the frame
   (white background or grey card), under the **same lamps (not moved), same
   aperture f/5.6**, at a shutter that keeps it **unclipped** (faster than the
   papyrus exposure — a bright white card clips at 1/60, try ~1/125). Take a few
   for averaging. Re-shoot the flat whenever the lamps move or the aperture
   changes.
2. (Optional) a **dark frame**: lens capped, lamp off, same settings.
3. Correct:
   ```bash
   .venv/bin/python scripts/ir-flatfield.py \
       capture1.nef capture2.nef --flat flat1.nef flat2.nef [--dark dark.nef]
   ```
   Outputs `<name>_ff.tif` (16-bit grey) + `_ff.png` (preview). By default the
   flat is smoothed (corrects only the gradient + vignetting); `--no-smooth` does
   full per-pixel correction (then average many flats). Put captures first, then
   `--flat` (argparse consumes the flat list after the flag).

`corrected = capture / flat x mean(flat)`. It fixes low-frequency gradients,
vignetting and per-pixel response; it does **not** fix subject-dependent
specular glare or clipped data.

## Measurement / re-tuning tools (`scripts/`)
All prompt you to switch the lamp ON for a tight capture loop, then OFF, and
auto-detect the fragment (no grid / no manual ROI: the white background is flat
and border-connected, the papyrus has a textured edge):

- **`ir-exposure-test.py`** — fixed aperture, brackets the shutter (RAW),
  recommends the ETTR shutter.
- **`ir-aperture-test.py`** — sweeps aperture at constant exposure (shutter
  compensates); verifies aperture against EXIF. Used for the sharpness series.
- **`ir-matrix-test.py`** — 2D aperture × exposure matrix in one lamp session;
  reports per-aperture ETTR and sharpness, picks the optimum. Also the quickest
  way to re-confirm one aperture's exposure, e.g.:
  ```bash
  .venv/bin/python scripts/ir-matrix-test.py \
      --apertures f/5.6 --anchor f/5.6 --shutter 1/60 --offsets -0.33 0 0.33 0.67
  ```

### Methodology notes
- Measure exposure on the **fragment substrate**, not the whole frame — the
  bright background otherwise dominates and causes underexposure of the papyrus.
- Measure sharpness across the fragment (it is exposure-independent when the
  metric is contrast-normalised); pick the aperture that is sharpest where it
  matters, robust to slight unevenness.
- If `-53 "Could not claim the USB device"`: the Papyri or Preview app (or
  macOS PTPCamera) holds the camera — quit it / `killall PTPCamera`, then retry.
