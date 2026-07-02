# Calibration routine — when / what / why

How calibration is meant to be run on the papyri rig, and the reasoning
behind each cadence. Calibration produces the reference shots that let
downstream processing (and ML fragment-matching) correct even illumination
and lens geometry — so ~20,000 fragments come out under reproducible
conditions.

The in-app feature is described for developers at the end (§ Implementation).
This doc is the operator-facing routine.

## The mental model: two buckets

Everything we control falls into one of two buckets, which decides how
often it's redone:

- **Fixed in the optics → calibrate ONCE.** Lens distortion depends only on
  the camera + lens, which never change in this project. → the **Grid**
  (done **manually / externally**, not in this app — see below).
- **Tied to light / setup → calibrate REGULARLY.** Lamp warm-up + drift,
  slightly changing ambient light, tripod rebuilds, and (for IR) lamps that
  can't be repositioned identically → the **Flatfield**.

Camera **pose / tilt is deliberately NOT calibrated** — ML matching absorbs
it (see § Why geometry is handled once, manually).

## The routine

### ① ONCE — lens geometry (manual, outside the app)
- Shoot a printed **Grid** per camera once (lay it flat on the baseboard).
- **Why:** captures lens distortion — fixed, because the camera + lens never
  change. The camera does **not** need to be level (the grid documents
  whatever pose it has; downstream rectifies).
- This is **not** part of the in-app calibration. Redo only on a lens swap
  (≈ never).

### ② REGULARLY — Flatfield (in the app)
One easy rule:

> **Shoot a Flatfield for the *current height* — at session start, after a
> height change, and when the timer nudges.**

- **VIS:** Flatfield per height (the current rig height).
- **IR:** Flatfield ×1 (single fixed height).
- **Why each trigger:** a Flatfield is valid only for the light + framing it
  was shot under. *Session start* = lamps just on / setup fresh; *height
  changed* = different framing of the lit field; *nudge* = lamp warm-up drift
  over a long session.

### ③ EVENT-DRIVEN — re-shoot the Flatfield (manual, any time)
Whenever the light or framing changes:
- lighting changed (or you notice a change);
- tripod rebuilt (even "set up like before");
- **IR lamps repositioned** (they can't be placed identically → IR's
  per-session Flatfield is mandatory);
- IR camera touched (wobbly tripod → framing shifted).

### Colour — handled per shot, not by a calibration target
Most VIS shots carry a **ColorChecker Nano in-frame**, so colour is corrected
**per image** (no drift). The in-app **ColorChecker** target is therefore a
**VIS-only optional fallback** for shots without the Nano — it is *not*
nagged by the timer. **IR has no ColorChecker** (no colour signal; a standard
chart's IR values aren't characterised anyway).

### NOT calibrated (on purpose)
- **Camera pose / tilt** → absorbed by ML matching. No levelness aid.
- **Grid / distortion** → the manual once-step above.

## Why geometry is handled once, manually

A grid documents the camera's *actual* pose, so the camera needn't be level.
What it captures is **lens distortion** (camera + lens → fixed here). The
remaining geometric variation — tilt from a wobbly tripod, a rebuild, a
different height — is a perspective transform that fragment matching
estimates and removes **per pair at match time** (homography + scale). So it
needn't be pre-corrected → no repeated grids, and IR's unstable tripod is
fine (small residual drift at fixed height + stable focus is well within what
matching absorbs; **sharp focus matters more than geometric precision**).

## Per-camera specifics

| Aspect | VIS | IR |
|---|---|---|
| Flatfield | **per height** (current rig height) | **×1** (single fixed height) |
| ColorChecker | optional fallback, not nagged | none (no colour) |
| Grid | once, manual/external | once, manual/external |
| Lamps | — | not 100% repositionable → **per-session Flatfield is mandatory** |

## Height — one shared setting

Height is a **single sticky rig setting**: the **"Height" control in the
capture row** (VIS), with presets **configured in Settings → "Camera heights
VIS (cm)"**. It is set once per size group (fragments arrive size-sorted in
boxes of 100–200, so height changes in groups, not per fragment) and:

- **stamps** each captured object's `capture_height_vis` into `_meta.json`
  (no per-object typing), and
- **tags** the VIS Flatfield (per-height folders),

so downstream pairs each fragment with the Flatfield for its height. IR uses
a single fixed height (Settings → "Camera height IR (cm)").

## Implementation (for developers)

- **`papyri/calibration_layout.py`** — the single source of truth: which
  targets exist per camera (`CALIBRATION_TARGETS`), each with `per_height`
  (Flatfield) and `required` (ColorChecker is not). Edit this list to
  add/remove a target; asymmetry between cameras is fine.
- **`papyri/calibration_target.py`** — one *run* = one timestamped folder
  (per "Calibrate" click). Per-height targets get a height subfolder; the
  height comes from a provider MainWindow supplies (the shared setting):
  ```
  _calibration/2026-06-21_0930/
    visible/  flatfield/45/   flatfield_vis_45_001.jpg
              flatfield/60/   flatfield_vis_60_001.jpg
              colorchecker/   colorchecker_vis_001.jpg     (optional fallback)
    infrared/ flatfield/45/   flatfield_ir_45_001.jpg
  ```
- **`papyri/calibration.py`** — per-(camera, current height) due-tracking for
  the idle status chip; trigger = Settings (`Off / Time / At session start`),
  plus always-manual.
- **Shared height** — presets in `papyri/_metadata.py`
  (`parse_height_choices`), the capture-row `heightSelect` + persisted
  `currentHeight` / `irCaptureHeight`, stamped on capture in `main.py`; the
  metadata pane merge-writes so the stamp survives.
