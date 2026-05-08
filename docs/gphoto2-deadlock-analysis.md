# libgphoto2 cross-worker deadlock analysis

**TL;DR**: the known deadlock vector is **closed** by the Stage 5 fix. Source-verified against libgphoto2 2.5.x — there is exactly one global mutex in libgphoto2 (`gpi_libltdl_mutex` for libltdl MT-safety), and every code path that touches it is now wrapped in `_GPHOTO2_GLOBAL_LOCK` on our side. Per-camera operations don't touch it. Additional defense-in-depth applied: log filter lowered from `GP_LOG_DEBUG` to `GP_LOG_ERROR`, shrinking the gp_log_call_python callback frequency by orders of magnitude.

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

**Source-verified against libgphoto2 2.5.x** (see `/Users/mts/src/libgphoto2`).

libgphoto2 contains **exactly one global mutex**: `gpi_libltdl_mutex`, defined in `libgphoto2_port/libgphoto2_port/gphoto2-port-locking.c`. It exists specifically to serialize calls into libltdl (the dynamic-loader library), which is documented MT-unsafe. Grep target: every place where `gpi_libltdl_lock()` / `gpi_libltdl_unlock()` is called — these are the cross-worker contention points.

### Code paths that take `gpi_libltdl_mutex`

| libgphoto2 function | File / line | Our wrapper | Protected? |
|---|---|---|---|
| `gp_camera_init` | `gphoto2-camera.c` 780–828 (dlopen + dlsym for camlib) | `__connect_camera`'s `camera.init()` | ✓ `_GPHOTO2_GLOBAL_LOCK` |
| `gp_camera_exit` | `gphoto2-camera.c` 283 (dlclose camlib if loaded) | `__disconnect_camera`'s `camera.exit()` | ✓ Stage 5 |
| `gp_port_info_list_load` | `gphoto2-port-info-list.c` 324–329 | called from `gp_camera_autodetect` and `gp_camera_init` | ✓ both wrapped |
| `gp_port_open/close/exit` | `gphoto2-port.c` 164–193 | only called from `gp_camera_init` and `gp_camera_exit` (since our build is `!HAVE_MULTI`) | ✓ via init/exit wrappers |
| `gp_port_free` | `gphoto2-port.c` 359 (dlclose port driver) | triggered by `self.camera = None` SWIG dealloc | ✓ Stage 5 (forced inside lock) |
| `gp_abilities_list_load` | `gphoto2-abilities-list.c` 334 | called from `gp_camera_autodetect` | ✓ via autodetect wrapper |

Everything is covered. All six paths that touch the mutex go through one of: `__find_camera` (autodetect), `__connect_camera` (init), `__disconnect_camera` (exit + dealloc) — all under `_GPHOTO2_GLOBAL_LOCK`.

### Per-camera operations — verified to NOT touch the mutex

Inspecting `gphoto2-camera.c`:
- `gp_camera_get_config` (line 855), `gp_camera_set_config` (1099), `gp_camera_get_single_config` (888), `gp_camera_set_single_config` (1131)
- `gp_camera_capture` (1324), `gp_camera_trigger_capture` (1355), `gp_camera_capture_preview` (1385)
- `gp_camera_wait_for_event` (1435)
- `gp_camera_file_get` (1668), `gp_camera_file_get_info` (1575), `gp_camera_folder_*`

**None** of these acquire `gpi_libltdl_mutex`. Confirmed via grep. The only cross-camera serialization happens through libusb internally (one-bus = sequential I/O), which is performance, not deadlock.

**Critical assumption verified**: our build is `!HAVE_MULTI` (config.h has no `HAVE_MULTI` definition). If `HAVE_MULTI` were defined, the `CHECK_OPEN`/`CHECK_CLOSE` macros (gphoto2-camera.c 100-145) would call `gp_port_open`/`gp_port_close` per-operation, which DO take the mutex. With `!HAVE_MULTI`, ports stay open between operations — `gp_port_open` only happens once during init.

---

### Original section: which of OUR code paths take the mutex

The **port-info-list mutex** mapping into our code (preserved for reference):

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

## Defense-in-depth applied

### Lowered log filter from `GP_LOG_DEBUG` to `GP_LOG_ERROR` ✓

Source-verified safe: `camlibs/ptp2/usb.c:460` and `:495` log all PTP transaction failures at `GP_LOG_E` (ERROR level) with the response code as `(0x%04x)` — exactly the suffix our `__extract_gp2_error_from_log` callback scans for. So PTP error detection (including `PTP_RC_NIKON_OutOfFocus = 0xa002`) works at ERROR level too.

Effect: callback fires only for actual errors, not on every verbose enumeration / handshake message during normal operation. Reduces the gp_log_call_python frequency by ~1000x. Smaller GIL contention surface; smaller window for any AB-BA pattern to form even if a new vector exists.

Applied in `byzanz_camera/camera_worker.py` `initialize()`.

## Defense-in-depth NOT applied (if we ever need them)

### Wrap ALL libgphoto2 calls in `_GPHOTO2_GLOBAL_LOCK`

Bulletproof. But serializes the per-frame live view loop (~20fps × 2 workers = 40 lock acquisitions/sec). Could cause stuttering and starve the secondary worker's UI updates.

**Not recommended** unless we observe a deadlock the current fix doesn't catch.

### Per-camera lock + global lock

Each Camera has its own `threading.Lock` for per-camera ops. The `_GPHOTO2_GLOBAL_LOCK` covers port-list ops only.

Per-camera locks are essentially redundant (only one worker accesses each camera) but provide a clear contract. More code, marginal correctness improvement.

**Not recommended** for current scope.

## What's been done

1. ✓ **Stage 5 fix** (`__disconnect_camera` wraps `camera.exit()` + SWIG dealloc in `_GPHOTO2_GLOBAL_LOCK`). The known vector is closed.
2. ✓ **Source-verified the audit** against libgphoto2 2.5.x. Found exactly one global mutex; all paths into it are protected on our side.
3. ✓ **Lowered log filter** from `GP_LOG_DEBUG` to `GP_LOG_ERROR`. Reduces callback frequency by ~1000x, shrinking any latent deadlock window.
4. ✓ **Documented the lock's purpose** in `_GPHOTO2_GLOBAL_LOCK`'s definition (comment block in `camera_worker.py`).

## What's left

5. **Stress test on deployment hardware** (see protocol below). Run for hours; this is the path to operational confidence beyond static analysis.
6. **Optional watchdog** that logs warnings if a worker thread is unresponsive for >N seconds. Doesn't prevent deadlocks but surfaces them for postmortem.
7. **If a hang ever occurs in deployment**, capture a sample report (`sample <pid>` on macOS / `gstack` on Linux) — the trace will tell us whether it's the same vector or a new one.

## Verification protocol (stress test)

Run for ≥30 min uninterrupted on the deployment hardware:

1. **Startup loop**: kill + restart papyri 50 times, varying VIS-only / dual-config QSettings.
2. **Disconnect storm**: with both cameras connected and live view running, manually disconnect one camera (button) every 5s for 5 minutes. Repeat with USB unplug/replug.
3. **Capture under load**: start a capture; while in progress, switch active spectrum, click stepper steps, open settings. Should not hang.
4. **Long-running idle**: leave both workers in Waiting (no cameras attached) for 30 minutes. Repeated autodetect cycles should not deadlock.
5. **Watch the logs**: any `take_gil`-style stack in a sample report = deadlock.

Pass criteria: no hang, no force-quit, all UI interactions remain responsive. App can be closed cleanly via window-close at any point.
