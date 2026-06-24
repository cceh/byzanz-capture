#!/usr/bin/env python
"""Restore camera settings from a JSON snapshot produced by
`scripts/dump-camera-settings.py`.

Applies every writable setting from the snapshot back onto the connected
camera, so you can return it to a known state.

Usage (camera connected, awake, in PTP mode):
    .venv/bin/python scripts/apply-camera-settings.py <snapshot.json> [options]

Options:
    --dry-run          Show what would change; don't touch the camera.
    --include-raw      Also apply the low-level /other/ PTP-opcode widgets
                       (d0xx / 50xx). These mostly duplicate the named
                       settings and are riskier — off by default.
    --include-actions  Also apply /actions/ widgets. DANGEROUS: these are
                       triggers (autofocusdrive, bulb, ...), not settings.
    --set-clock        Also restore `datetime`. Off by default so an old
                       snapshot doesn't roll the camera clock backwards.

By default the script skips: read-only widgets, /status/ (read-only info),
/actions/ triggers, the camera clock, and /other/ raw opcodes. What's left
is the meaningful, persistent, user-facing settings.

`expprogram` (the mode dial) is applied first and pushed on its own, because
it determines which other widgets are writable; the rest follow in a second
push. If a batched push fails, the script falls back to applying settings one
at a time so a single bad value can't block the whole restore.
"""
from __future__ import annotations
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from byzanz_camera._gphoto2_paths import apply_paths  # noqa: E402

apply_paths(os.environ.get("CAMLIBS"), os.environ.get("IOLIBS"))
import gphoto2 as gp  # noqa: E402

# Widget kinds that never represent a settable value.
NON_VALUE_TYPES = {"section", "window", "button"}


def should_apply(widget, *, include_raw, include_actions, set_clock):
    """Return (apply: bool, skip_reason: str | None) for one snapshot widget."""
    if widget["readonly"]:
        return False, "read-only"
    if widget["type"] in NON_VALUE_TYPES:
        return False, f"type={widget['type']}"
    section = widget["path"].split("/")[1] if "/" in widget["path"] else ""
    if section == "status":
        return False, "status (info)"
    if section == "actions" and not include_actions:
        return False, "action trigger"
    if widget["name"] == "datetime" and not set_clock:
        return False, "clock (use --set-clock)"
    if section == "other" and not include_raw:
        return False, "raw opcode (use --include-raw)"
    return True, None


def push_one(camera, cfg, name, value):
    """Set a single widget on a fresh config tree and push it. Returns
    (ok, error_message)."""
    try:
        child = cfg.get_child_by_name(name)
        child.set_value(value)
        camera.set_config(cfg)
        return True, None
    except gp.GPhoto2Error as e:
        return False, str(e)


def apply_batch(camera, names_values):
    """Set many widgets on one tree, then push once. On failure, fall back to
    one-at-a-time so a single bad value can't block the rest. Returns
    (applied: list[str], failed: list[tuple[str, str]])."""
    if not names_values:
        return [], []
    cfg = camera.get_config()
    staged = []
    failed = []
    for name, value in names_values:
        try:
            cfg.get_child_by_name(name).set_value(value)
            staged.append(name)
        except gp.GPhoto2Error as e:
            failed.append((name, f"set_value: {e}"))
    try:
        camera.set_config(cfg)
        return staged, failed
    except gp.GPhoto2Error:
        # Batched push rejected — isolate by applying each individually.
        applied, failed2 = [], []
        for name, value in names_values:
            cfg = camera.get_config()
            ok, err = push_one(camera, cfg, name, value)
            (applied if ok else failed2).append(name if ok else (name, err))
        return applied, failed + [f for f in failed2 if isinstance(f, tuple)]


def main() -> int:
    ap = argparse.ArgumentParser(description="Restore camera settings from a JSON snapshot.")
    ap.add_argument("snapshot", help="Path to the snapshot JSON file.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--include-raw", action="store_true")
    ap.add_argument("--include-actions", action="store_true")
    ap.add_argument("--set-clock", action="store_true")
    args = ap.parse_args()

    with open(args.snapshot) as f:
        snap = json.load(f)
    widgets = snap.get("all_widgets") or snap.get("settings") or []

    targets, skipped = [], []
    for wdg in widgets:
        ok, reason = should_apply(
            wdg,
            include_raw=args.include_raw,
            include_actions=args.include_actions,
            set_clock=args.set_clock,
        )
        (targets if ok else skipped).append((wdg, reason))

    print(f"Snapshot: {snap.get('camera_model','?')} "
          f"(serial {snap.get('serial_number','?')}), "
          f"captured {snap.get('captured_date','?')}")
    print(f"{len(targets)} settings to apply, {len(skipped)} skipped.")

    if args.dry_run:
        print("\n-- DRY RUN: would apply --")
        for wdg, _ in targets:
            print(f"  {wdg['name']:24} = {wdg['value']!r}")
        return 0

    detected = list(gp.Camera.autodetect())
    if not detected:
        print("No camera detected. Connect it, wake it (half-press shutter), "
              "ensure PTP mode.", file=sys.stderr)
        return 1

    # Verify the snapshot matches the connected body before writing to it.
    camera = gp.Camera()
    camera.init()
    try:
        cfg = camera.get_config()
        try:
            live_serial = cfg.get_child_by_name("serialnumber").get_value()
        except gp.GPhoto2Error:
            live_serial = None
        snap_serial = snap.get("serial_number")
        if snap_serial and live_serial and str(snap_serial) != str(live_serial):
            print(f"REFUSING: snapshot serial {snap_serial} != connected "
                  f"camera serial {live_serial}.", file=sys.stderr)
            return 2

        # Phase 1: mode dial first (decides which widgets are writable).
        by_name = {w["name"]: w["value"] for w, _ in targets}
        applied, failed = [], []
        if "expprogram" in by_name:
            a, f = apply_batch(camera, [("expprogram", by_name["expprogram"])])
            applied += a
            failed += f

        # Phase 2: everything else.
        rest = [(w["name"], w["value"]) for w, _ in targets if w["name"] != "expprogram"]
        a, f = apply_batch(camera, rest)
        applied += a
        failed += f

        # Verify by reading back.
        cfg = camera.get_config()
        matched = mismatched = 0
        mism_list = []
        for w, _ in targets:
            try:
                cur = cfg.get_child_by_name(w["name"]).get_value()
            except gp.GPhoto2Error:
                continue
            if str(cur) == str(w["value"]):
                matched += 1
            else:
                mismatched += 1
                mism_list.append((w["name"], w["value"], cur))
    finally:
        camera.exit()

    print(f"\nApplied: {len(applied)}   Failed: {len(failed)}")
    print(f"Verified: {matched} match, {mismatched} still differ")
    if failed:
        print("\n-- failed --")
        for name, err in failed:
            print(f"  {name}: {err}")
    if mism_list:
        print("\n-- still differ (often mode-locked / interdependent) --")
        for name, want, got in mism_list:
            print(f"  {name}: wanted {want!r}, got {got!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
