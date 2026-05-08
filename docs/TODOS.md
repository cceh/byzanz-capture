# Post-refactor TODOs

Items deferred during the SessionState refactor (branch `refactor/session-state`, Stages 0–6, merged to main on **TBD**). Ordered by priority.

## P1 — Reliability (must address before broader rollout)

### F-GPHOTO2-LOCK — libgphoto2 cross-worker deadlock

**Severity (after analysis)**: low → medium. Known deadlock vector is **closed** by the Stage 5 fix. Remaining risk is unknown unknowns in libgphoto2 internals.

**Reference**: full analysis in `docs/gphoto2-deadlock-analysis.md`. Short version:

The deadlock is AB-BA between the GIL and the libgphoto2 port-info-list mutex. SWIG bindings hold the GIL during C calls; libgphoto2's debug log callback wants the GIL. If one worker is mid-port-info-list operation (holding the mutex) and the other holds the GIL while doing a SWIG call that also wants the mutex, neither can progress.

**Audit result**: every libgphoto2 path that touches the port-info-list mutex (`gp_camera_autodetect`, `gp.Camera()`, `set_port_info`, `camera.init()`, `camera.exit()`, `gp_camera_free`/`gp_port_free`) is now wrapped in `_GPHOTO2_GLOBAL_LOCK`. Two workers cannot be in port-list ops simultaneously, so the AB-BA pattern can no longer form.

**Other unprotected libgphoto2 calls** (`set_config`, `get_config`, `capture_preview`, `trigger_capture`, `wait_for_event`, `file_get`, `file_delete`) operate on per-camera handles and do **not** share libgphoto2 mutexes across workers. They contend for the GIL but that's just sequential scheduling — no deadlock.

**Recommended action** (not strictly required for correctness):
1. **Stress test** before declaring rock-solid (protocol in `docs/gphoto2-deadlock-analysis.md` §"Verification protocol"). Do this on the actual deployment hardware.
2. **Watchdog** — log warnings if a worker is unresponsive for >N seconds. Doesn't prevent deadlocks but surfaces them for postmortem.
3. **Lower log filter** from `GP_LOG_DEBUG` to `GP_LOG_ERROR` *if* PTP error detection still works at ERROR level (need Nikon hardware to verify; CCeH dome RTI workflow uses Nikon D800E).
4. If a NEW hang ever occurs in deployment, capture a sample report (`sample <pid>` on macOS) — the trace will tell us whether it's the same vector or a new one.

### ObjectsSidebar — auto-refresh on filesystem changes

**Severity**: medium — pre-existing limitation surfaced during F-LEAK testing.

When the user renames an object directory in Finder (or anything outside the app modifies the working directory), the sidebar doesn't update until the user clicks something that triggers `objects_sidebar.refresh()`.

**Fix**: add a `QFileSystemWatcher` on the working directory in `ObjectsSidebar.__init__`; connect `directoryChanged` to `refresh()`. Watch for: new dirs, deleted dirs, renamed dirs.

## P2 — UX polish

### F-STEPPER-NO-OBJECT — stepper clickable when no object loaded

**Severity**: low.

When no object is loaded, workflow stepper steps are still clickable. Click silently mutates `active_spectrum`, but the active highlight is hidden (Stage 3 fix), so the user can't tell their click did anything. Capture button is disabled too.

**Fix**: disable the stepper widget when `current_object is None`. (`workflow_stepper.setEnabled(self.session.current_object is not None)` from a `current_object_changed` receiver.)

**Tradeoff**: loses the side-affordance of switching active spectrum without an object loaded — though the camera-state widgets' connect/disconnect buttons cover the most likely use case.

## P3 — Latent / rare

### F-AMBIG — mid-capture spectrum switch hides capture status

**Severity**: medium but rare.

If the user switches active spectrum DURING a capture (camera A is in `CaptureInProgress`), `_refresh_camera_dependent_ui` reads `session.active_camera_state` — which is now camera B's state, not the in-flight capture's. Status label shows B's state, not A's "Capturing…" / "Captured: …".

**Fix options**:
- Gate the status label by the capture request's originator (track which spectrum's worker started it), independent of active_spectrum.
- Block spectrum switches during in-flight captures (prevent the case entirely).

## P4 — Cleanup

### Pre-existing `print()` debug calls

Pre-date the refactor; left untouched per scope discipline. Replace with `logger.debug()`:

- `byzanz_camera/photo_browser.py`: line ~466, ~545, ~548
- `byzanz_camera/camera_worker.py`: line ~355, ~542, ~566 (and a couple inside the PTP event handling)

## Verification gaps from single-camera testing

Cannot directly verify on current hardware (one camera = IR Nikon D800E):

- IR camera state transitions (J4 IR variant) — symmetry argument with VIS path
- VIS↔IR live-view handoff on spectrum switch (H15, J14, J15)
- Mid-capture spectrum switch (J1, F-AMBIG)
- IR capture path (B4 receivers actually firing on real IR state)

To be exercised once the dual-station setup is live. Per-axis logging (rule #6) makes the IR paths visible in logs even without IR hardware.
