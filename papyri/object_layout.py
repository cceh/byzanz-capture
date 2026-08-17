"""Object layout — the on-disk contract of the FULL-mode papyri object
family: directory scheme, marker file, per-bucket scans, completeness
rules. Pure path functions, no Qt — shared between `Object` (main.py) and
the objects sidebar without a main↔sidebar import cycle.

One layout module per storage family, all built alike (tree diagram,
naming, completeness): this one for objects, `calibration_layout.py` for
calibration runs; simple mode has no layout module (flat folder, naming
inline in `simple_target.py`). The axes and file-level primitives that
all families share live in `capture_vocab.py`.

Layout:
    <working_dir>/
        <object_name>/
            _meta.json                          -- presence marks "this is a managed object dir";
                                                --   also holds per-bucket take markers (see below)
            side_a/
                visible/
                    <name>_a_vis_NNN.{jpg,arw,...}
                    _stitch/                    -- stitching objects only; derived artifacts
                        report.json             --   connectivity-check result
                        preview.jpg             --   stitched preview composite
                infrared/
                    <name>_a_ir_NNN.{jpg,arw,...}
            side_b/
                visible/  ...
                infrared/ ...

Take markers (which capture is "chosen" for display, which is the stitch
"reference" photo) live in `_meta.json` under `markers`, keyed by bucket:

    "markers": {
        "side_a/visible": {"chosen": "<stem>", "reference": "<stem>"}
    }

Per role, an absent key means the default (chosen → latest capture,
reference → first capture); a stem pins that capture; `reference: null`
means the user explicitly cleared it (no reference — every capture is a
checked segment). No separate marker files — one object-state file.

`_meta.json` also carries `layout_version`, a monotonic version of the
object's whole on-disk form; `migrate_object` upgrades legacy objects (see
the migration section). Reserved top-level keys are the `MetaKey` enum.

Completeness rules (drive the sidebar chips):
    - a SPECTRUM is complete when every side has ≥1 capture
      (`is_spectrum_complete`); the calibration counterpart is the
      `required` flag in `calibration_layout.py`
    - metadata completeness is a separate, schema-driven rule — see
      `papyri._metadata.is_metadata_complete`

Naming rule: identifiers that mean "side" (A/B) use SIDE_*; identifiers
that mean "spectrum" (visible/infrared) use SPECTRUM_*. Earlier code used
"side" to mean spectrum — that terminology has been retired.
"""
from __future__ import annotations
import json
import logging
import os
from enum import StrEnum

from papyri.capture_vocab import (
    capture_index,
    CAPTURE_EXTENSIONS, SIDE_A, SIDE_B, SIDES, SPECTRA, SPECTRUM_INFRARED,
    SPECTRUM_VISIBLE, is_hidden_file,
)

_logger = logging.getLogger("object_layout")


class MetaKey(StrEnum):
    """Reserved top-level keys in `_meta.json` — written by the app itself,
    never metadata-form fields. A schema field colliding with one of these
    would let the form clobber app state, so `_metadata.py` guards against
    it at import. Iterate the enum (`set(MetaKey)`) for the full set."""
    LAYOUT_VERSION = "layout_version"
    MARKERS        = "markers"
    STITCHING      = "stitching"
    HEIGHT_VIS     = "capture_height_vis"
    HEIGHT_IR      = "capture_height_ir"
    BOX_NR         = "box_nr"
    AUDITS         = "audits"


class MarkerRole(StrEnum):
    """Per-bucket take markers, stored under `_meta.json` → markers →
    <bucket>. Not top-level keys (nested under MARKERS), so not reserved —
    but named here so the roles are never bare strings."""
    CHOSEN    = "chosen"
    REFERENCE = "reference"

META_FILENAME = "_meta.json"

# Subdir name = side or spectrum identifier directly.
_SIDE_SUBDIRS = {SIDE_A: "side_a", SIDE_B: "side_b"}
_SPECTRUM_SUBDIRS = {SPECTRUM_VISIBLE: "visible", SPECTRUM_INFRARED: "infrared"}

# All four (side, spectrum) buckets in a stable iteration order — the
# object family's bucket universe (counterpart: CALIBRATION_BUCKETS).
BUCKETS: tuple[tuple[str, str], ...] = tuple(
    (s, sp) for s in SIDES for sp in SPECTRA
)


# ---- core helpers ---------------------------------------------------------

def meta_path_for(object_dir: str) -> str:
    return os.path.join(object_dir, META_FILENAME)


def side_dir_for(object_dir: str, side: str) -> str:
    """Return `<object_dir>/<side>/`. Raises ValueError on unknown side."""
    if side not in _SIDE_SUBDIRS:
        raise ValueError(f"unknown side: {side!r}")
    return os.path.join(object_dir, _SIDE_SUBDIRS[side])


def dir_for_bucket(object_dir: str, side: str, spectrum: str) -> str:
    """Return `<object_dir>/<side>/<spectrum>/`. Raises on unknown side/spectrum."""
    if spectrum not in _SPECTRUM_SUBDIRS:
        raise ValueError(f"unknown spectrum: {spectrum!r}")
    return os.path.join(side_dir_for(object_dir, side), _SPECTRUM_SUBDIRS[spectrum])


def bucket_key(side: str, spectrum: str) -> str:
    """Stable key for a (side, spectrum) bucket inside `_meta.json` — e.g.
    `"side_a/visible"`. Used by the per-bucket take markers (chosen /
    reference), which live under the `markers` key in the object's meta."""
    if spectrum not in _SPECTRUM_SUBDIRS:
        raise ValueError(f"unknown spectrum: {spectrum!r}")
    return f"{side}/{spectrum}"


def stitch_dir_for(object_dir: str, side: str, spectrum: str) -> str:
    """Return `<bucket>/_stitch/` — derived stitch artifacts. As a
    subdirectory it is invisible to the per-bucket capture scans."""
    return os.path.join(dir_for_bucket(object_dir, side, spectrum), "_stitch")


def stitch_report_path_for(object_dir: str, side: str, spectrum: str) -> str:
    return os.path.join(stitch_dir_for(object_dir, side, spectrum), "report.json")


def stitch_preview_path_for(object_dir: str, side: str, spectrum: str) -> str:
    return os.path.join(stitch_dir_for(object_dir, side, spectrum), "preview.jpg")


# Verdicts that count as "the set stitches" — the single source of truth for
# "green", shared by StitchReport.is_green and the completeness rule below.
GREEN_VERDICTS = frozenset({"ok", "thin"})


def stitch_verdict(object_dir: str, side: str, spectrum: str) -> str | None:
    """The verdict string from a bucket's `_stitch/report.json`, or None if
    there is no report. Kept here (Qt-free) so disk-scan callers — the
    sidebar completeness rule — can read it without a StitchController."""
    try:
        with open(stitch_report_path_for(object_dir, side, spectrum)) as f:
            return json.load(f).get("verdict")
    except (FileNotFoundError, json.JSONDecodeError, OSError, AttributeError):
        return None


# ---- _meta.json I/O --------------------------------------------------------
# The single reader/writer pair for the object marker file. Callers with
# plain key updates (capture stamping, the Stitch toggle) go through
# `update_meta`; the metadata pane keeps its form-specific merge (it also
# DROPS cleared keys) but shares read_meta/write_meta.

def read_meta(meta_path: str) -> dict:
    """Read `_meta.json` defensively. Returns empty dict on missing/malformed."""
    try:
        with open(meta_path) as f:
            return json.load(f) or {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def write_meta(meta_path: str, data: dict) -> None:
    """Write `_meta.json` in the canonical format. OSErrors propagate —
    callers decide whether a failed write is fatal for their flow."""
    with open(meta_path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def update_meta(meta_path: str, updates: dict) -> dict:
    """Merge `updates` into `_meta.json` (read → update → write) so keys
    owned by other writers survive. Returns the merged dict."""
    data = read_meta(meta_path)
    data.update(updates)
    write_meta(meta_path, data)
    return data


# ---- per-capture audits ---------------------------------------------------
# Audit entries are keyed by capture stem, then by check name. These helpers
# own that shape so callers never mutate or reinterpret the reserved top-level
# metadata key themselves.

def read_capture_audits(meta_path: str) -> dict[str, dict[str, dict]]:
    """Return the persisted audit tree, or an empty tree if malformed."""
    audits = read_meta(meta_path).get(MetaKey.AUDITS, {})
    if not isinstance(audits, dict):
        _logger.warning("ignored malformed capture audits in %s", meta_path)
        return {}
    return {
        stem: dict(findings)
        for stem, findings in audits.items()
        if isinstance(stem, str) and isinstance(findings, dict)
    }


def store_capture_audit(
    meta_path: str, stem: str, check: str, entry: dict,
) -> None:
    """Persist one finding without disturbing other metadata or checks."""
    audits = read_capture_audits(meta_path)
    audits.setdefault(stem, {})[check] = dict(entry)
    update_meta(meta_path, {MetaKey.AUDITS: audits})


def rename_capture_audits(meta_path: str, renames: dict[str, str]) -> None:
    """Move audit entries alongside captures whose stems were renamed."""
    if not renames:
        return
    audits = read_capture_audits(meta_path)
    changed = False
    for old_stem, new_stem in renames.items():
        findings = audits.pop(old_stem, None)
        if findings is None:
            continue
        audits.setdefault(new_stem, {}).update(findings)
        changed = True
    if changed:
        update_meta(meta_path, {MetaKey.AUDITS: audits})


def remove_capture_audits(meta_path: str, stem: str) -> None:
    """Remove findings for a capture that no longer belongs to the object."""
    audits = read_capture_audits(meta_path)
    if audits.pop(stem, None) is not None:
        update_meta(meta_path, {MetaKey.AUDITS: audits})


def is_managed_object_dir(object_dir: str) -> bool:
    """True if `object_dir` contains the `_meta.json` marker."""
    return os.path.isfile(meta_path_for(object_dir))


def is_stitching_object(object_dir: str) -> bool:
    """Read the object-wide stitching flag straight from `_meta.json` — for
    disk-scan callers (the sidebar) that hold no `Object` instance."""
    return read_meta(meta_path_for(object_dir)).get(MetaKey.STITCHING) is True


# ---- on-disk layout migration ----------------------------------------------
# `_meta.json` carries a monotonic LAYOUT_VERSION describing the object's
# whole on-disk form (directory scheme, filenames, metadata shape). A
# migration step may touch files, dirs and metadata — hence "layout". The
# version lives WITH the object (in its meta), so it travels when a folder
# is moved. Only papyri Objects have a meta file; simple/calibration targets
# persist no marker state, so nothing to version there yet.

CURRENT_LAYOUT_VERSION = 2


def _needs_migration(object_dir: str) -> bool:
    """True if the object is below the current layout version. The single
    source for that decision (used by migrate_object and dry-run scans)."""
    meta = read_meta(meta_path_for(object_dir))
    return int(meta.get(MetaKey.LAYOUT_VERSION, 0)) < CURRENT_LAYOUT_VERSION


def migrate_object(object_dir: str) -> bool:
    """Idempotently upgrade one managed object to CURRENT_LAYOUT_VERSION.
    Returns True if anything changed (for logging); cheap no-op once
    current. New objects are stamped at creation, so only genuine v0
    legacy dirs do work here."""
    if not _needs_migration(object_dir):
        return False
    meta = read_meta(meta_path_for(object_dir))
    if int(meta.get(MetaKey.LAYOUT_VERSION, 0)) < 1:
        _migrate_v1_markers_into_meta(object_dir, meta)
    if int(meta.get(MetaKey.LAYOUT_VERSION, 0)) < 2:
        _migrate_v2_capture_audits(meta)
    meta[MetaKey.LAYOUT_VERSION] = CURRENT_LAYOUT_VERSION
    write_meta(meta_path_for(object_dir), meta)
    return True


def migrate_working_dir(working_dir: str) -> int:
    """Migrate every managed object directly in a box to the current layout
    version. Returns how many changed. Call once when a box is opened,
    before anything reads object state."""
    migrated = sum(
        migrate_object(os.path.join(working_dir, name))
        for name in list_managed_objects(working_dir)
    )
    if migrated:
        _logger.info("migrated %d object(s) in %s to layout v%d",
                     migrated, working_dir, CURRENT_LAYOUT_VERSION)
    return migrated


def migrate_tree(root: str, dry_run: bool = False) -> tuple[int, int]:
    """Recursively migrate every managed object under `root`, at any depth
    (a folder of boxes, a single box, or one object dir). Returns
    (objects_found, objects_migrated). With `dry_run`, reports what would
    migrate without changing anything. Backs the batch migration CLI."""
    found = migrated = 0

    def _warn(err: OSError) -> None:
        _logger.warning("skipped unreadable path during scan: %r", err)

    for dirpath, dirnames, _ in os.walk(root, onerror=_warn):
        if not is_managed_object_dir(dirpath):
            continue
        found += 1
        if dry_run:
            if _needs_migration(dirpath):
                migrated += 1
                _logger.info("would migrate %s", dirpath)
        elif migrate_object(dirpath):
            migrated += 1
            _logger.info("migrated %s to layout v%d",
                         dirpath, CURRENT_LAYOUT_VERSION)
        # An object dir holds no nested objects (only side/spectrum dirs) —
        # don't descend into it.
        dirnames[:] = []
    return found, migrated


def _migrate_v1_markers_into_meta(object_dir: str, meta: dict) -> None:
    """v0 -> v1: chosen/reference takes moved from per-bucket side-dir
    `_chosen_<spectrum>.txt` / `_reference_<spectrum>.txt` files into
    `_meta.json` under `markers`. Absorbs the files, then renames each to
    `.migrated` (kept as a rollback net rather than deleted)."""
    markers = meta.setdefault(MetaKey.MARKERS, {})
    for side, spectrum in BUCKETS:
        side_dir = side_dir_for(object_dir, side)
        for role in MarkerRole:
            path = os.path.join(side_dir, f"_{role}_{spectrum}.txt")
            if not os.path.isfile(path):
                continue
            try:
                stem = open(path).read().strip()
            except OSError:
                continue
            if stem:
                bucket = markers.setdefault(bucket_key(side, spectrum), {})
                # the old reference "none" sentinel becomes JSON null
                bucket[role] = (None if role == MarkerRole.REFERENCE
                                and stem == "none" else stem)
            os.rename(path, path + ".migrated")


def _migrate_v2_capture_audits(meta: dict) -> None:
    """v1 -> v2: reserve the additive per-capture audit container."""
    meta.setdefault(MetaKey.AUDITS, {})


def capture_stems_for_bucket(
    object_dir: str, side: str, spectrum: str,
) -> list[str]:
    """Stems of the captures in one bucket, ordered by capture index —
    the same population `Object._scan_bucket` pairs into `Capture`s
    (supported extensions, hidden files and index-less names skipped),
    without touching the concrete JPG/RAW paths."""
    bucket_dir = dir_for_bucket(object_dir, side, spectrum)
    if not os.path.isdir(bucket_dir):
        return []
    by_stem: dict[str, int] = {}
    for name in os.listdir(bucket_dir):
        if is_hidden_file(name):
            continue
        stem, ext = os.path.splitext(name)
        if ext.lower() not in CAPTURE_EXTENSIONS:
            continue
        index = capture_index(name)
        if index is None:
            continue
        by_stem[stem] = index
    return sorted(by_stem, key=lambda stem: by_stem[stem])


def resolve_chosen_stem(
    markers: dict, side: str, spectrum: str, stems_in_order: list[str],
) -> str | None:
    """THE chosen-take rule: a pinned stem wins while it still exists;
    otherwise the LATEST capture is the keeper (a null/absent marker both
    mean latest; chosen has no cleared state). Single source — `Object`
    and the audit rollups both resolve through here."""
    pinned = markers.get(bucket_key(side, spectrum), {}).get(MarkerRole.CHOSEN)
    if pinned and pinned in stems_in_order:
        return pinned
    return stems_in_order[-1] if stems_in_order else None


def effective_capture_stem(
    object_dir: str, side: str, spectrum: str,
) -> str | None:
    """Stem of the take that currently REPRESENTS a bucket (pinned chosen,
    else newest), or None on an empty bucket. Path-only variant for
    consumers without a live `Object` (sidebar, audit rollups)."""
    markers = read_meta(meta_path_for(object_dir)).get(MetaKey.MARKERS, {})
    if not isinstance(markers, dict):
        markers = {}
    return resolve_chosen_stem(
        markers, side, spectrum,
        capture_stems_for_bucket(object_dir, side, spectrum))


def locate_capture(path: str) -> tuple[str, str, str] | None:
    """Reverse lookup: (object_dir, side, spectrum) for a capture file
    inside a managed object, or None when the path does not match the
    layout. Display/reporting helper only — audit routing keeps receiving
    its target and modality explicitly (see CLAUDE.md choke points),
    this never feeds a measurement decision."""
    spectrum_dir = os.path.dirname(os.path.abspath(path))
    side_dir = os.path.dirname(spectrum_dir)
    object_dir = os.path.dirname(side_dir)
    side = os.path.basename(side_dir)
    spectrum = os.path.basename(spectrum_dir)
    if (side in SIDES and spectrum in SPECTRA
            and is_managed_object_dir(object_dir)):
        return object_dir, side, spectrum
    return None


def has_captures_for_bucket(object_dir: str, side: str, spectrum: str) -> bool:
    """True if `<object_dir>/<side>/<spectrum>/` contains at least one supported file."""
    bucket_dir = dir_for_bucket(object_dir, side, spectrum)
    if not os.path.isdir(bucket_dir):
        return False
    for f in os.listdir(bucket_dir):
        if is_hidden_file(f):
            continue
        if os.path.splitext(f)[1].lower() in CAPTURE_EXTENSIONS:
            return True
    return False


def captured_sides_for_spectrum(object_dir: str, spectrum: str) -> int:
    """How many of the two sides have ≥1 capture for `spectrum` (0–2)."""
    return sum(
        1 for side in SIDES
        if has_captures_for_bucket(object_dir, side, spectrum)
    )


def is_stitch_bucket_complete(object_dir: str, side: str, spectrum: str) -> bool:
    """A stitching bucket is complete when its segments cover the fragment:
    the last check is green (≥2 connecting segments), OR it is a single shot
    (verdict `too_few` with ≥1 capture — the IR field often covers the whole
    fragment in one frame, so there is nothing to stitch). A broken
    multi-segment set (split / isolated) or an empty bucket is not complete."""
    verdict = stitch_verdict(object_dir, side, spectrum)
    if verdict in GREEN_VERDICTS:
        return True
    if verdict == "too_few":
        return has_captures_for_bucket(object_dir, side, spectrum)
    return False


def is_spectrum_complete(object_dir: str, spectrum: str) -> bool:
    """THE completeness rule of the object family. Normal objects: every
    physical side has ≥1 capture. Stitching objects (oversized, captured as
    overlapping segments): every side is stitch-complete (see
    `is_stitch_bucket_complete`)."""
    if is_stitching_object(object_dir):
        return all(is_stitch_bucket_complete(object_dir, side, spectrum)
                   for side in SIDES)
    return captured_sides_for_spectrum(object_dir, spectrum) == len(SIDES)


def newest_capture_mtime(object_dir: str) -> float | None:
    """mtime of the newest capture file across all four buckets, or None if
    the object has no captures. Drives the sidebar's per-object date line."""
    newest: float | None = None
    for side, spectrum in BUCKETS:
        bucket_dir = dir_for_bucket(object_dir, side, spectrum)
        if not os.path.isdir(bucket_dir):
            continue
        for f in os.listdir(bucket_dir):
            if is_hidden_file(f):
                continue
            if os.path.splitext(f)[1].lower() not in CAPTURE_EXTENSIONS:
                continue
            try:
                mtime = os.path.getmtime(os.path.join(bucket_dir, f))
            except OSError:
                continue    # file vanished between listdir and stat
            if newest is None or mtime > newest:
                newest = mtime
    return newest


# ---- working-dir-level helpers --------------------------------------------

def list_managed_objects(working_dir: str | None) -> list[str]:
    """Sorted names of managed object directories directly under `working_dir`."""
    if not working_dir or not os.path.isdir(working_dir):
        return []
    return sorted(
        name for name in os.listdir(working_dir)
        if os.path.isdir(os.path.join(working_dir, name))
        and is_managed_object_dir(os.path.join(working_dir, name))
    )

