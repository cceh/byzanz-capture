#!/usr/bin/env python
"""Dump every gphoto2 config widget of the connected camera to JSON.

Captures a complete snapshot of the camera's current settings — all
widgets, including low-level PTP opcode codes and read-only status
fields — so the state can be inspected or restored later.

Usage (camera connected, awake, in PTP mode):
    .venv/bin/python scripts/dump-camera-settings.py [output.json]

Writes to `<model>_settings_<date>.json` next to this note by default,
or to the path given as the first argument.
"""
from __future__ import annotations
import json
import os
import sys
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from byzanz_camera._gphoto2_paths import apply_paths  # noqa: E402

apply_paths(os.environ.get("CAMLIBS"), os.environ.get("IOLIBS"))
import gphoto2 as gp  # noqa: E402  (must come after apply_paths)

TYPE_NAMES = {
    gp.GP_WIDGET_WINDOW: "window",
    gp.GP_WIDGET_SECTION: "section",
    gp.GP_WIDGET_TEXT: "text",
    gp.GP_WIDGET_RANGE: "range",
    gp.GP_WIDGET_TOGGLE: "toggle",
    gp.GP_WIDGET_RADIO: "radio",
    gp.GP_WIDGET_MENU: "menu",
    gp.GP_WIDGET_BUTTON: "button",
    gp.GP_WIDGET_DATE: "date",
}


def walk(widget, path, entries):
    for child in widget.get_children():
        name = child.get_name()
        wtype = child.get_type()
        child_path = f"{path}/{name}"
        if wtype in (gp.GP_WIDGET_WINDOW, gp.GP_WIDGET_SECTION):
            walk(child, child_path, entries)
            continue
        try:
            value = child.get_value()
        except gp.GPhoto2Error:
            value = None
        rec = {
            "name": name,
            "path": child_path,
            "type": TYPE_NAMES.get(wtype, str(wtype)),
            "value": value,
            "readonly": bool(child.get_readonly()),
        }
        if wtype in (gp.GP_WIDGET_RADIO, gp.GP_WIDGET_MENU):
            try:
                rec["choices"] = [
                    child.get_choice(i) for i in range(child.count_choices())
                ]
            except gp.GPhoto2Error:
                pass
        entries.append(rec)


def main() -> int:
    detected = list(gp.Camera.autodetect())
    if not detected:
        print(
            "No camera detected. Make sure it is connected, powered on, "
            "awake (half-press the shutter), and in PTP mode.",
            file=sys.stderr,
        )
        return 1

    cam = gp.Camera()
    cam.init()
    try:
        cfg = cam.get_config()

        def get(name, default=""):
            try:
                return cfg.get_child_by_name(name).get_value()
            except gp.GPhoto2Error:
                return default

        model = get("cameramodel", "unknown")
        serial = get("serialnumber", "unknown")
        firmware = get("deviceversion", "unknown")

        entries: list[dict] = []
        walk(cfg, "", entries)
    finally:
        cam.exit()

    writable = [
        e for e in entries if not e["readonly"] and e["type"] != "button"
    ]
    today = datetime.date.today().isoformat()
    out = {
        "camera_model": model,
        "detected_as": detected[0][0],
        "serial_number": serial,
        "firmware": firmware,
        "captured_date": today,
        "note": (
            "Complete snapshot of all gphoto2 config widgets. To restore, set "
            "each writable widget's 'name' back to its 'value'. Read-only "
            "widgets (status/info) are included for reference but reject writes."
        ),
        "total_widgets": len(entries),
        "writable_count": len(writable),
        "all_widgets": entries,
    }

    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        safe_model = model.replace(" ", "_").replace("/", "_")
        path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "byzanz_camera",
            "profiles",
            f"{safe_model}_settings_{today}.json",
        )
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
    print(
        f"{model} (serial {serial}, fw {firmware}): "
        f"{len(entries)} widgets ({len(writable)} writable) -> {path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
