# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Byzanz-capture is a PyQt6 camera capture application for RTI (Reflectance Transformation Imaging) workflows. It drives a DSLR/mirrorless camera (via libgphoto2) in lockstep with a 60-LED RTI dome (via BLE), producing image sets plus an LP file for downstream RTI processing. Originally built at the Cologne Center for eHumanities (CCeH) for the DigiByzSeal project on Byzantine seals.

## Development Commands

### Setup
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Running
```bash
python main.py
# or:
./byzanz-capture.sh
```

### Building for Windows
The local `build_win.sh` is **not** cross-platform — it requires an MSYS2/MINGW64 shell on Windows (uses `/mingw64/lib/...` paths and the Windows 7-Zip binary). The canonical build is the GitHub Actions workflow `.github/workflows/build-win.yml`, which provisions MSYS2, installs `libgphoto2`/`qt6-base`/`pyqt6` from pacman, then runs `build_win.sh` to produce a PyInstaller onedir bundle.

`build_win_hook.py` is a PyInstaller runtime hook that points `IOLIBS`/`CAMLIBS` at `sys._MEIPASS` so the bundled gphoto2 camera/port drivers are found at runtime.

## Architecture Overview

### Package layout
The project is split into a reusable camera-plumbing package (`byzanz_camera/`) and the RTI-specific app shell at the repo root. The split exists so non-RTI workflows (e.g. the planned papyri tool) can import the camera/photo-browser code without dragging RTI assumptions along — see `docs/adapting-for-other-projects.md` for the rationale.

- `byzanz_camera/` — shared, project-agnostic: `camera_worker.py`, `helpers.py`, `photo_browser.py`, `photo_viewer.py`, `load_image_worker.py`, `spinner.py`, `profiles/`. Internal references use relative imports (`from .helpers import ...`).
- Repo root — RTI-specific: `main.py`, `bt_controller_controller.py`, `camera_config_dialog.py`, `settings_dialog.py`, `open_session_dialog.py`, `cceh-dome-template.lp`, `ui/`. Imports the package as `from byzanz_camera.X import Y`.

### Threading and event loops
Three execution contexts coexist; cross-context calls go through Qt signals:
- **Main UI thread**: PyQt6 widgets and UI updates.
- **Camera worker thread** (`byzanz_camera/camera_worker.py`): all gphoto2 operations. The UI sends commands via `CameraCommands` signals (e.g. `capture_images`, `connect_camera`, `set_config`) and listens for `state_changed` / `property_changed` / `preview_image`.
- **qasync event loop**: a single asyncio loop integrated into Qt for BLE (`bt_controller_controller.py`, root). BLE work is dispatched with `asyncio.run_coroutine_threadsafe` and results delivered back via per-request `BtControllerRequest.signals`.
- **`byzanz_camera/load_image_worker.py`** uses a `QThreadPool` for thumbnail loading in the photo browser.

### Camera profiles
`byzanz_camera/profiles/base.py` defines the abstract `Profile` interface that camera-specific subclasses implement. The interface intentionally exposes both **gphoto2 property names** (e.g. `iso_property_name()`, `shutterspeed_property_name()`, `image_format_property_name()`) and **settings dicts** for each lifecycle step (`initial_settings`, `start_live_view_settings`, `start_capture_settings`, `capture_format_jpeg_settings`, etc.) — this is how the same `CameraWorker` drives different cameras whose property names and required ordering differ. Behavior flags (`use_burst()`, `manual_trigger()`, `enable_capture_controls_in_live_preview()`, `poll_config()`) let the worker branch on profile capabilities rather than camera identity.

Profiles ship today as `CCeHDomeNikonD800E`, `ParisDomeSonyIlce7RM5`, and `MoritzA7III` (the corodile / papyri test profile). They are registered in the `PROFILES` dict in `main.py`. The active profile is persisted via `QSettings` under the `"profile"` key.

### Camera state machine
`CameraStates` in `byzanz_camera/camera_worker.py` is a namespace of plain Python classes (not an `Enum`) used as immutable state objects: `Waiting`, `Found`, `Connecting`, `Connected`/`Ready`, `LiveViewStarted`/`Active`/`Stopped`, `CaptureInProgress`, `CaptureFinished`, `CaptureCanceled`, `CaptureError`, `ConnectionError`, etc. State transitions are emitted as `state_changed(object)` and the UI reacts via `isinstance` checks. Camera-method errors are wrapped by the private `__handle_camera_error` decorator, which transitions to `ConnectionError` and disconnects.

### Bluetooth (optional)
`main.py:26-30` imports `bt_controller_controller` inside a `try/except` and sets `BT_AVAILABLE`. **All Bluetooth code paths must check `BT_AVAILABLE`** — the app must run on machines without `bleak`/BLE. The controller targets a fixed device (`DEVICE_NAME = "CCeH Dome Controller"`, hard-coded MAC and characteristic UUID) and auto-reconnects on disconnect.

### Sessions and file layout
`Session` (defined in `main.py`) owns three directories under a user-chosen working dir:
- `<session>/` — root
- `<session>/test/` — live preview / test shots (the variable is named `preview_dir` but the folder is `test`)
- `<session>/images/` — RTI capture set
The LP file for RTI processing is generated from `cceh-dome-template.lp`.

### UI loading
`.ui` files in `ui/` are loaded at runtime via `PyQt6.uic.loadUi` — there is no compile step. Always resolve UI/asset paths through `byzanz_camera.helpers.get_ui_path()`, which switches between the source tree and `sys._MEIPASS` when frozen by PyInstaller. Translations live in `i18n/` (`.ts` source, `.qm` compiled).

## Conventions

- **Never call gphoto2 from the UI thread.** Send a signal on `self.camera_worker.commands` instead. Example: `self.camera_worker.commands.capture_images.emit(CaptureImagesRequest(...))`.
- **Adding a camera**: subclass `Profile`, implement every abstract method (the property-name getters return strings matching gphoto2 config keys for that camera), then register the instance in `PROFILES` in `main.py`.
- **Capture format vs file count**: `CaptureImagesRequest` derives `expect_files` (1 for JPEG, 2 for JPEG+RAW) — keep this in sync if adding new formats.
- **No formal test suite.** Validate camera changes against real hardware or a gphoto2 dummy camera; BLE features must degrade gracefully when hardware is absent.

## Single-source rules ("choke points")

For each concern below there is **one** canonical home. Route through it; do
**not** re-implement the concern at a call site. This list exists because an
agent editing one call site can't see the others — so before adding a
cross-cutting behavior to a handler, **grep for sibling handlers of the same
event/sink; if there is more than one, funnel them through a single entry**
(that's how `_activate_box` came to be). Background:
`docs/missing-abstractions-agent-workflows.md`.

| Concern | Canonical home | Do NOT |
|---|---|---|
| Make a box active (migrate + show) | `PapyriMainWindow._activate_box` | call `objects_sidebar.set_working_directory` directly |
| Read/write an object's `_meta.json` | `object_layout.read_meta` / `write_meta` / `update_meta` | open/`json.dump` the file inline |
| Reserved `_meta.json` top-level keys | `object_layout.MetaKey` (StrEnum) | write the key as a bare string literal |
| Per-bucket take markers (chosen / reference) | `Object.set_chosen` / `set_reference` / `clear_reference`; roles are `MarkerRole` | store markers in sidecar files |
| On-disk layout migration | `object_layout.migrate_object` / `migrate_working_dir`, versioned by `layout_version` | change on-disk format without a version bump + migration step |
| Embedded-JPEG bytes from a RAW/JPEG | `load_image_worker.read_embedded_jpeg` | inline `rawpy.extract_thumb` |
| Capture file naming | `Object.next_stem` / `next_template` | build stem strings by hand |
| gphoto2 operations | `CameraWorker` command signals (see Conventions) | call gphoto2 from the UI thread |
| ORB detect + affine pair match (+ thresholds) | `stitching.detect_features` / `match_pair` / `CONFIDENCE_THRESHOLD` | re-instantiate detector/matcher pipelines or invent a second confidence bar |
| Segment set of a bucket (reference excluded) | `stitching.snapshot_bucket` | re-list captures and filter the reference inline |
| Overlays pinned over the photo viewer | `ViewerWidget.add_corner_overlay` (widget: `PillBadge`) | parent widgets into the viewer/viewport by hand |

**When you discover or introduce a new choke point, add a row here** — that's
the durable prevention (see the doc above).
