# libgphoto2 cross-worker deadlock analysis

**TL;DR**: the known deadlock vector is **closed** by the Stage 5 fix. Defense-in-depth options exist but aren't strictly necessary for correctness. The biggest remaining risk is *unknown unknowns* in libgphoto2 internals — mitigated by stress testing.

## What we observed (Stage 5 verification)

Reproducible hang during single-camera testing. macOS sample report (`/tmp/trace.txt`) showed two worker threads in classic AB-BA deadlock:

- **Worker A** (`Thread 0x57be71`): `gp_camera_autodetect` → `gp_port_info_list_load` → `lt_dlforeachfile` → `foreach_func` → `gp_log` → **`gp_log_call_python`** → `PyGILState_Ensure` → `take_gil` → blocked.
- **Worker B** (`Thread 0x57be72`): SWIG `_wrap_delete_Camera` → `gp_camera_unref` → `gp_camera_free` → **`gp_port_free`** → `__pthread_mutex_firstfit_lock_slow` → blocked.

Trace's smoking gun: `psynch_mtxcontinue (blocked by turnstile waiting for Python [69076] thread 0x57be71)`. So Worker B is waiting for a mutex Worker A holds.

## The mechanism

Two ingredients combine to make this deadlock possible:

1. **SWIG-generated python-gphoto2 bindings hold the GIL during C calls.** A Python destructor (`_wrap_delete_Camera`) called from GC enters C without releasing the GIL.
2. **libgphoto2's debug log callback (`gp_log_call_python`) needs the GIL** to invoke our registered Python handler.

When Worker A is inside `gp_camera_autodetect` — which holds the libgphoto2 port-info-list mutex internally — and the C code calls `gp_log` to emit a verbose enumeration message, the log callback wants the GIL. If Worker B at that moment is in a SWIG call (e.g. a Camera dealloc triggered by Python ref-count → 0), Worker B holds the GIL AND wants the port-info-list mutex (the destructor calls `gp_port_free`).

```
Worker A: holds port_info_list mutex, wants GIL
Worker B: holds GIL,                  wants port_info_list mutex
```

Neither releases without the other. Hard hang. Force-quit required.

## Why Stage 5 closes this specific vector

Stage 5 wraps the disconnect path in `_GPHOTO2_GLOBAL_LOCK`:

```python
def __disconnect_camera(self, auto_reconnect=True):
    self.__set_state(CameraStates.Disconnecting())
    with _GPHOTO2_GLOBAL_LOCK:
        try:
            self.camera.exit()
        except (gp.GPhoto2Error, AttributeError):
            pass
        self.camera = None  # ← triggers SWIG _wrap_delete_Camera here, INSIDE the lock
    ...
```

Crucial property: `threading.Lock.acquire()` **releases the GIL during the wait**. So:

1. Worker B enters `__disconnect_camera`, attempts to acquire `_GPHOTO2_GLOBAL_LOCK`.
2. Worker A is in `__find_camera`'s `with _GPHOTO2_GLOBAL_LOCK: detected = _gphoto2_autodetect()` — currently holds the lock.
3. Worker B's wait **releases the GIL**.
4. Worker A's autodetect is via ctypes (which releases the GIL during the C call). When libgphoto2 fires the log callback, the callback acquires the GIL (now free), runs the Python handler, releases.
5. Autodetect returns. Worker A's `with` block exits, releases `_GPHOTO2_GLOBAL_LOCK`.
6. Worker B acquires the lock, runs `camera.exit()` and `self.camera = None`. The SWIG destructor runs synchronously **inside the lock** — when it calls `gp_port_free`, the port-info-list mutex is uncontended (nobody else is in libgphoto2 because they'd be waiting for our lock).

The chain breaks because the lock acquisition happens on the **Python side**, not the C side. Python locks release the GIL during waits; C mutexes don't.

## Why other unprotected calls don't deadlock

The deadlock requires **two workers contending for the same libgphoto2 mutex**. libgphoto2 has two relevant categories of mutex:

### Global mutexes — cross-worker contention possible

The **port-info-list mutex** is the only one we've observed in the trace. Operations that touch it:

| Operation | Source | Currently |
|---|---|---|
| `gp_camera_autodetect` | `__find_camera` | `_GPHOTO2_GLOBAL_LOCK` ✓ |
| `gp.Camera()` constructor | `__connect_camera` | `_GPHOTO2_GLOBAL_LOCK` ✓ |
| `set_port_info` | `__connect_camera` | `_GPHOTO2_GLOBAL_LOCK` ✓ |
| `camera.init()` | `__connect_camera` | `_GPHOTO2_GLOBAL_LOCK` ✓ |
| `camera.exit()` | `__disconnect_camera` | `_GPHOTO2_GLOBAL_LOCK` ✓ (Stage 5) |
| `gp_camera_free` (via `self.camera = None`) | `__disconnect_camera` | `_GPHOTO2_GLOBAL_LOCK` ✓ (Stage 5) |

**All paths that touch the global port-info-list are now serialized.** Two workers cannot be in port-list ops simultaneously, so the AB-BA deadlock can no longer form.

### Per-camera mutexes — no cross-worker contention

These are the unprotected calls in `camera_worker.py`:

- `camera.set_config(cfg)`, `camera.get_config()`, `camera.get_single_config(name)` — read/write this camera's config
- `camera.capture_preview()` — fetch a preview frame from this camera (~20fps in live view)
- `camera.trigger_capture()` — fire this camera's shutter
- `camera.wait_for_event(timeout)` — poll for events on this camera
- `camera.file_get(...)`, `camera.file_delete(...)` — fetch/delete files from this camera

Each of these operates on a **specific gp.Camera handle** owned by a single worker. They don't share state with the other worker's Camera. **No cross-worker mutex contention via libgphoto2's locks.**

GIL contention still exists (both workers want GIL to enter their respective SWIG calls), but that's just sequential scheduling — no deadlock. Worker A's SWIG call eventually returns, releases the GIL, Worker B proceeds.

## Remaining risk: unknown unknowns

I'm reasoning from the trace evidence, not from libgphoto2 source code. There may be **other global state in libgphoto2** I'm not aware of:

- The camlib-loading code (`lt_dlopen` etc.) — runs during `gp.Camera()` construction. Already protected.
- The logging system itself (`gp_log_add_func`, log handler list) — has internal lock. Only an issue if we register/unregister handlers while logging happens. We register once at startup, never deregister.
- libusb internals — outside libgphoto2's control. May serialize USB I/O at the bus level. Performance impact, not deadlock.

**For confidence beyond reasoning, do stress testing**: see "Verification protocol" below.

## Defense-in-depth options (not strictly required)

If we want belt-and-suspenders protection:

### Option 1: Lower the log filter

Change `gp.gp_log_add_func(gp.GP_LOG_DEBUG, ...)` to `gp.GP_LOG_ERROR`. Reduces callback frequency by orders of magnitude (no callback for normal operations, only errors).

**Risk**: Nikon PTP error detection in `__trigger_autofocus` reads PTP error codes from log messages. Need to verify Nikon driver logs PTP errors at ERROR level (not just DEBUG). If PTP errors only come through at DEBUG, lowering the filter loses the OutOfFocus distinction in autofocus.

**Untestable here** without a Nikon body. The CCeH dome RTI workflow uses Nikon D800E — the RTI app would lose autofocus error detail. Papyri's Sony cameras likely don't use Nikon-specific PTP error codes.

### Option 2: Wrap ALL libgphoto2 calls in `_GPHOTO2_GLOBAL_LOCK`

Bulletproof. But serializes the per-frame live view loop (~20fps × 2 workers = 40 lock acquisitions/sec). Could cause stuttering and starve the secondary worker's UI updates.

**Not recommended** unless we observe a deadlock the current fix doesn't catch.

### Option 3: Per-camera lock + global lock

Each Camera has its own `threading.Lock` for per-camera ops. The `_GPHOTO2_GLOBAL_LOCK` covers port-list ops only.

Per-camera locks are essentially redundant (only one worker accesses each camera) but provide a clear contract. More code, marginal correctness improvement.

**Not recommended** for current scope.

## Recommended action

1. **Keep the current Stage 5 fix as-is.** The known deadlock vector is closed.
2. **Stress test before declaring rock-solid** (see protocol below). Run for hours on the actual deployment hardware.
3. **Add a watchdog** that logs warnings if a worker thread is blocked for >N seconds. Doesn't prevent deadlocks, but surfaces them in logs for postmortem analysis.
4. **Document the analysis in the codebase** — link to this doc from `_GPHOTO2_GLOBAL_LOCK`'s definition + `__disconnect_camera` so future maintainers understand the pattern.

If the deployment shows ANY hang, capture a sample report (`sample <pid>` on macOS / `gstack` on Linux) — the trace will tell us whether it's the same vector or a new one.

## Verification protocol (stress test)

Run for ≥30 min uninterrupted on the deployment hardware:

1. **Startup loop**: kill + restart papyri 50 times, varying VIS-only / dual-config QSettings.
2. **Disconnect storm**: with both cameras connected and live view running, manually disconnect one camera (button) every 5s for 5 minutes. Repeat with USB unplug/replug.
3. **Capture under load**: start a capture; while in progress, switch active spectrum, click stepper steps, open settings. Should not hang.
4. **Long-running idle**: leave both workers in Waiting (no cameras attached) for 30 minutes. Repeated autodetect cycles should not deadlock.
5. **Watch the logs**: any `take_gil`-style stack in a sample report = deadlock.

Pass criteria: no hang, no force-quit, all UI interactions remain responsive. App can be closed cleanly via window-close at any point.
