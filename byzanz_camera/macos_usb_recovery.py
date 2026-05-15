"""macOS USB-claim recovery for tethered camera initialization.

On macOS 13+, libgphoto2's `camera.init()` sometimes fails with
error -53 ("Could not claim the USB device") because Apple's
`ptpcamerad` daemon is holding the USB interface on behalf of
another process (Image Capture, Photos, Preview, cloud sync clients
with photo auto-import, pro photo software, printer/scanner helpers,
etc.). `ptpcamerad` is launched on demand by `launchd`, so a one-shot
kill loses the race against subsequent respawns.

This module's `attempt()` runs ONLY when the caller has already seen
the claim error — never preemptively. It tries three tiers, in order:

  1. Trigger launchd's respawn throttle by killing `ptpcamerad`,
     letting it respawn, and killing it again — verified via two
     delayed absence samples. When the throttle engages, retry the
     init once with a clean ~8-second window.
  2. Continuous kill loop: a background thread SIGTERMs every
     `ptpcamerad` it sees at ~10 Hz while the caller retries init
     on the main thread. Bounded by a hard 3-second ceiling.
  3. Enumerate currently-running processes against the curated
     `OFFENDER_PROCESSES` list and return the matches — the caller
     surfaces them in the UI ("quit Preview and try again").

On non-macOS platforms `attempt()` is a no-op: `RecoveryResult(
attempted=False, ...)`.

This module has NO Qt or UI dependencies. The caller emits any
signal it likes from the returned offender list.

Design notes:
  - Process matching uses prefix `ptpcamera` (Apple has renamed
    similar daemons before).
  - `_kill_ptpcamerad` iterates over ALL matching processes since
    several can briefly coexist during a respawn.
  - The kill loop is guaranteed to stop via a `finally`-style
    cleanup AND a hard timeout — so an exception escaping the
    caller's `init_fn` won't leave a thread aggressively killing a
    system daemon indefinitely.
  - The throttle's window is not hardcoded — empirical observation
    (delayed absence polling) is the source of truth.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Optional

import psutil


# Test hooks. Setting either env var to "1" forces the matching tier to
# behave as if it failed, so the next tier (or tier 3's offender dialog)
# runs. Used to verify the fallback chain without contriving a
# situation where the throttle/kill-loop genuinely don't work.
_FORCE_FAIL_THROTTLE = os.environ.get("PAPYRI_TEST_DISABLE_THROTTLE") == "1"
_FORCE_FAIL_KILL_LOOP = os.environ.get("PAPYRI_TEST_DISABLE_KILL_LOOP") == "1"


# ---- offender catalog ---------------------------------------------------

# Curated list of applications known to grab PTP cameras via Apple's
# ImageCaptureCore framework. Extensible: as new offenders are
# discovered, add `process_name: friendly_label` pairs here. Matching
# is case-insensitive prefix, so e.g. "Adobe Lightroom" matches both
# "Adobe Lightroom" and "Adobe Lightroom Classic" if you want only one
# entry — but listing both is clearer.
OFFENDER_PROCESSES: dict[str, str] = {
    # Apple built-ins
    "Image Capture": "Image Capture",
    "Photos": "Photos",
    "Preview": "Preview",
    "Photo Booth": "Photo Booth",
    # Cloud sync with photo auto-import
    "Dropbox": "Dropbox (Camera Uploads)",
    "Google Drive": "Google Drive (photo backup)",
    "OneDrive": "OneDrive (camera upload)",
    # Professional photo software
    "Adobe Lightroom": "Adobe Lightroom",
    "Adobe Lightroom Classic": "Adobe Lightroom Classic",
    "Capture One": "Capture One",
    "Sony Imaging Edge Desktop": "Sony Imaging Edge Desktop",
    "Sony Imaging Edge Remote": "Sony Imaging Edge Remote",
    "Cascable Pro Webcam": "Cascable Pro Webcam",
    # Printer / scanner driver helpers (least obvious offenders)
    "HPDeviceMonitoring": "HP printer driver",
    "Canon IJ Network Tool": "Canon network tool",
    "Epson Scanner": "Epson scanner helper",
}


# ---- result type --------------------------------------------------------

@dataclass
class RecoveryResult:
    attempted: bool                                          # False on non-macOS
    success: bool                                            # whether init_fn() ultimately succeeded
    tier: Optional[str] = None                               # "throttle" / "kill_loop" / None
    offenders: list[tuple[str, str]] = field(default_factory=list)


# ---- public API ---------------------------------------------------------

def attempt(
    init_fn: Callable[[], None],
    logger: logging.Logger,
) -> RecoveryResult:
    """Run tiered recovery, calling `init_fn` to retry the camera init
    between tiers. Returns a `RecoveryResult` describing what happened.

    On success, `init_fn` has been invoked (and returned without raising)
    and the caller can proceed as if its original init had succeeded.
    On failure, the caller should re-raise its original error and may
    surface `result.offenders` to the user."""
    if sys.platform != "darwin":
        return RecoveryResult(attempted=False, success=False)

    # Tier 1: try to engage launchd's respawn throttle.
    if _FORCE_FAIL_THROTTLE:
        logger.warning("tier 1 (throttle) forced-fail via "
                       "PAPYRI_TEST_DISABLE_THROTTLE=1")
    elif _trigger_throttle(logger):
        try:
            init_fn()
            return RecoveryResult(attempted=True, success=True, tier="throttle")
        except Exception as err:
            logger.info("init still failed after throttle: %r", err)

    # Tier 2: continuous kill loop while init retries multiple times.
    # One init attempt isn't enough — init can fail in well under the
    # kill loop's first 50 Hz interval, leaving the loop's hammering
    # essentially unused. Looping init within the kill loop gives the
    # race a real chance: each retry is a fresh attempt while the
    # background thread keeps ptpcamerad dead.
    if _FORCE_FAIL_KILL_LOOP:
        logger.warning("tier 2 (kill loop) forced-fail via "
                       "PAPYRI_TEST_DISABLE_KILL_LOOP=1")
    else:
        with _kill_loop(logger, max_seconds=4.0):
            deadline = time.monotonic() + 3.5
            last_err: Exception | None = None
            while time.monotonic() < deadline:
                try:
                    init_fn()
                    return RecoveryResult(
                        attempted=True, success=True, tier="kill_loop",
                    )
                except Exception as err:
                    last_err = err
                    # Let the kill loop hammer for a moment before retrying.
                    time.sleep(0.2)
            logger.info("init still failed after kill loop: %r", last_err)

    # Tier 3: enumerate offenders for the UI to surface.
    offenders = _enumerate_offenders()
    return RecoveryResult(
        attempted=True, success=False, tier=None, offenders=offenders,
    )


# ---- ptpcamerad helpers -------------------------------------------------

def _ptpcamerad_pids() -> list[int]:
    """All currently-running ptpcamerad-ish PIDs. Defensive prefix match
    in case Apple ever renames the daemon (they've renamed similar
    daemons before)."""
    pids: list[int] = []
    for proc in psutil.process_iter(["pid", "name"]):
        name = proc.info.get("name") or ""
        if name.startswith("ptpcamera"):
            pids.append(proc.info["pid"])
    return pids


def _is_ptpcamerad_running() -> bool:
    return bool(_ptpcamerad_pids())


def _kill_ptpcamerad() -> int:
    """SIGKILL every ptpcamerad-matching process. Iterates over all
    matches — several can briefly coexist during a respawn cycle.

    SIGKILL (not SIGTERM): a graceful exit looks normal to launchd and
    doesn't count toward its respawn-throttle threshold. SIGKILL is an
    abnormal exit and DOES count, which is exactly what we need to
    trigger tier 1's throttle. SIGKILL is also instant — no chance for
    ptpcamerad to clean up and reclaim the device before dying."""
    killed = 0
    for proc in psutil.process_iter(["pid", "name"]):
        name = proc.info.get("name") or ""
        if not name.startswith("ptpcamera"):
            continue
        try:
            proc.kill()
            killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return killed


def _wait_until_present(timeout: float) -> bool:
    """Poll until ptpcamerad is observed running, or `timeout` expires.
    Returns True iff seen running before the timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _is_ptpcamerad_running():
            return True
        time.sleep(0.05)
    return False


def _wait_until_absent(timeout: float) -> bool:
    """Poll until ptpcamerad is observed absent, or `timeout` expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _is_ptpcamerad_running():
            return True
        time.sleep(0.05)
    return False


# ---- tier 1: throttle trigger -------------------------------------------

def _trigger_throttle(logger: logging.Logger) -> bool:
    """Kill → wait for respawn → kill again → verify two delayed
    absence samples confirm launchd's respawn throttle engaged.
    Returns True iff the throttle is observably engaged.

    Total latency in the success path: ~1.5–2 seconds (one respawn
    wait + two verification samples).
    """
    logger.info("macOS USB recovery tier 1: trigger respawn throttle")
    if _kill_ptpcamerad() == 0:
        # Not currently running — try waiting briefly in case launchd
        # is about to start it (we can't trigger throttle without a
        # live instance to kill).
        if not _wait_until_present(timeout=0.5):
            logger.info("  ptpcamerad not running; skipping throttle trigger")
            return False
        _kill_ptpcamerad()

    # Wait for the respawn that the first kill should provoke. From
    # launchd's perspective only one crash has occurred so far — we need
    # the daemon back up before the second kill counts as a separate
    # incident.
    if not _wait_until_present(timeout=2.0):
        logger.info("  ptpcamerad didn't respawn after first kill; throttle "
                    "trigger inapplicable")
        return False

    _kill_ptpcamerad()

    # Verify with two delayed samples: if ptpcamerad stays absent across
    # both, the throttle engaged. The exact throttle window varies by
    # macOS version and load, so we don't hardcode it — only verify the
    # observable absence for at least ~1 second.
    time.sleep(0.5)
    if _is_ptpcamerad_running():
        logger.info("  ptpcamerad respawned within 0.5s — throttle not engaged")
        return False
    time.sleep(0.5)
    if _is_ptpcamerad_running():
        logger.info("  ptpcamerad respawned within 1.0s — throttle not engaged")
        return False

    logger.info("  throttle engaged ✓ (ptpcamerad absent for ≥1s)")
    return True


# ---- tier 2: continuous kill loop ---------------------------------------

@contextmanager
def _kill_loop(logger: logging.Logger, max_seconds: float = 3.0):
    """Spawn a background thread that SIGKILLs every ptpcamerad it sees
    at ~50 Hz, until the context exits or `max_seconds` elapses (hard
    ceiling so a stuck caller can't keep killing a system daemon
    indefinitely). 50 Hz is needed because launchd can respawn
    ptpcamerad in well under 100 ms when its client app (Preview etc.)
    is actively trying to reconnect. Exit cleanup is guaranteed via
    `finally`."""
    stop = threading.Event()
    started_at = time.monotonic()

    def loop():
        while not stop.is_set():
            if time.monotonic() - started_at > max_seconds:
                logger.warning(
                    "kill-loop hit %.1fs ceiling — stopping", max_seconds,
                )
                return
            _kill_ptpcamerad()
            stop.wait(0.02)  # ~50 Hz, interruptible

    logger.info("macOS USB recovery tier 2: continuous kill loop "
                "(ceiling %.1fs)", max_seconds)
    thread = threading.Thread(target=loop, name="ptpcamerad-killer",
                              daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1.0)


# ---- tier 3: enumerate offenders ----------------------------------------

def _enumerate_offenders() -> list[tuple[str, str]]:
    """One pass over running processes; case-insensitive prefix match
    against OFFENDER_PROCESSES keys. Returns `(process_name, label)`
    pairs in stable insertion order."""
    running_names: set[str] = set()
    for proc in psutil.process_iter(["name"]):
        name = proc.info.get("name") or ""
        if name:
            running_names.add(name)

    matches: list[tuple[str, str]] = []
    for proc_key, label in OFFENDER_PROCESSES.items():
        proc_key_lower = proc_key.lower()
        for running in running_names:
            if running.lower().startswith(proc_key_lower):
                matches.append((running, label))
                break
    return matches
