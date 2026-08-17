# Papyri session state: the reactive pattern, and what happens when an object opens

Zero-context guide to the orchestration core of the papyri app. The code
homes are `papyri/session_state.py` (the state axes) and
`PapyriMainWindow._wire_session` in `papyri/main.py` (the receiver wiring).

## The pattern in three rules

1. **One axis, one setter, one signal.** Every piece of cross-cutting UI
   state ("which object is open", "which bucket is active", …) lives on
   `SessionState` as a single property with a single setter that emits a
   single change signal. No second path writes it.
2. **Receivers are stateless and read back.** A receiver never uses signal
   arguments — it re-reads `session.<axis>` itself. Consequence: any
   receiver can be invoked at any time (initial paint, re-wiring, repeated
   emits) and always renders the correct state. Connection order carries
   no meaning.
3. **Hydrate before publish.** `SessionState.publish_target(target)` calls
   `target.refresh()` BEFORE emitting `current_object_changed`. Receivers
   run synchronously in connection order; publishing an unhydrated object
   would make correctness depend on which receiver happens to run first.
   All workflow call sites (sidebar click, create, rename, calibration
   enter/exit, mode switch) go through `publish_target` — never through
   `set_current_object` directly (choke-point rule in CLAUDE.md).

## Axis legend

The `B<n>` labels in comments and receiver docstrings ("Reads B5.") are
the axis ids of the session-state migration plan:

| Label | Axis | Signal |
|---|---|---|
| B1 + B2 | active bucket = (side, spectrum), changed atomically | `active_bucket_changed` |
| B3 + B4 | camera state per spectrum (visible / infrared) | `camera_state_changed` |
| B5 | current object (the open capture target, or None) | `current_object_changed` |
| B6 | live-view paused intent | `live_view_paused_changed` |
| B7 | viewer mode (live / paused / preview / empty) | `view_mode_changed` |
| B8 | per-camera advanced-config dialog handle | (no UI signal) |

The migration that introduced these axes is deliberately unfinished: some
state still lives on `PapyriMainWindow`. Finishing it is a known, separate
project — do not extend the main window with new cross-cutting state; new
axes belong on `SessionState`.

## Flow: opening an object

```
user action (sidebar click, create, rename, calibration exit, …)
     │
     ▼
session.publish_target(obj)
     │  1. obj.refresh()            hydrate: scan the 4 bucket dirs,
     │                              resolve chosen/reference markers
     │                              from _meta.json
     │  2. set_current_object(obj)  identity guard, then emit
     ▼
current_object_changed  →  ~15 receivers, synchronous, order-independent,
     │                     each reading session.current_object itself:
     │
     ├─ metadata pane / title bar / sidebar (active row + entries)
     ├─ bucket cards: chosen thumbs + audit "!" markers
     ├─ capture button label, stitch toggle, height combo, lockout overlay
     ├─ move the obj.state_changed subscription old → new object
     ├─ _refresh_filmstrip_binding  →  filmstrip.bind_object(…)
     └─ _sync_live_view (stream on/off per its single rule)
     ▼
filmstrip.bind_object
     │  open_directory: FS watcher + async thumbnail load; the capture-
     │  audit context and the missing_checks gate are re-seeded
     ▼
the bucket's chosen take is the ONE file decoded FULL  →  image_decoded
     ▼
_show_still_image  →  viewer shows the image
                   →  _refresh_capture_feedback (reads persisted audits)
```

Later state changes on the SAME object (new capture lands, marker moved,
take deleted) do not re-publish: `Object.state_changed` fires and
`_on_object_state_changed` re-runs the derived-state receivers (thumbs,
badges, audit rollups, stitch re-check).

## Where to plug new reactions in

Subscribe an idempotent `_refresh_*` receiver to the axis signal(s) in
`_wire_session` and invoke it once for initial paint — see the wiring
docstring there for the naming conventions. The audit rollups
(`_refresh_bucket_audit_warnings`) are a canonical example: one connect
line per axis, one re-runnable receiver, no ordering assumptions.
