"""Stitching support — the connectivity check for segment sets.

Oversized objects (Stitch toggle in the capture row) are photographed as
overlapping segments per bucket, plus one reference photo (ColorChecker +
scale). This module answers the assistant's question *while re-shooting is
still possible*: "did I photograph everything, with enough overlap, and
nothing foreign in the set?" It does NOT build the archival panorama —
that happens in post-processing from the RAWs.

Controller module in the `calibration.py` pattern: `StitchController` is
owned and wired by main.py; the actual work runs as a `QRunnable` on the
global thread pool (`_CopyRunner` pattern). The check itself is cheap
(< 1 s): feature detection + affine pairwise matching at ~0.6 MP on the
RAWs' embedded JPEG previews, then OUR graph evaluation over the pairwise
confidence matrix — which segment pairs overlap (confidence > 1.0, the
cv2 pipeline's "same panorama" bar), which segments are isolated, whether
the set splits into disconnected groups.

Results are a `StitchReport`, persisted to `<bucket>/_stitch/report.json`
(the subdirectory is invisible to capture scans). The disk file IS the
cache: `StitchController.fresh_report` re-reads it and validates its
`source_files` signature against the current bucket, so there is no
second in-memory state to keep in sync. The graph evaluation and message
generation are pure functions — unit-testable without Qt or images.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO

import cv2
import numpy as np
from PIL import Image
from PyQt6.QtCore import QObject, QRunnable, QThreadPool, QTimer, pyqtSignal
from stitching.feature_detector import FeatureDetector
from stitching.feature_matcher import FeatureMatcher
from stitching.images import Images

from byzanz_camera.load_image_worker import read_embedded_jpeg
from papyri.object_layout import stitch_dir_for, stitch_report_path_for

_logger = logging.getLogger("stitching")

# A pair of segments counts as overlapping above this pairwise match
# confidence — OpenCV's conventional "same panorama" bar (confidence is
# roughly num_inliers / (8 + 0.3 * num_matches)).
CONFIDENCE_THRESHOLD = 1.0
# Edges between threshold and this get a "thin overlap" hint: connected,
# but with little margin — worth an extra in-between shot.
THIN_BAND_MAX = 1.3
# Feature detection/matching resolution. Matching quality is determined
# at this scale; the 60 MP native size never enters the check.
CHECK_TARGET_MEGAPIX = 0.6


# ---- report ----------------------------------------------------------------

@dataclass(frozen=True)
class StitchReport:
    """Result of one connectivity check over a bucket's segment set.

    `stems` are the checked segments in capture-index order — the
    reference photo is excluded by design (it overlaps nothing).
    `source_files` (stem → mtime_ns of the checked file) is the staleness
    signature: a report is only trusted while it matches the bucket's
    current disk state."""

    verdict: str            # ok | thin | split | isolated | too_few | unreadable | error
    message: str            # user-facing line, names files where possible
    stems: tuple[str, ...]
    reference_stem: str | None
    source_files: dict[str, int]
    checked_at: str         # ISO timestamp
    confidence: tuple[tuple[float, ...], ...] = ()
    components: tuple[tuple[str, ...], ...] = ()
    thin_pairs: tuple[tuple[str, str, float], ...] = ()
    bridge_only: bool = False

    def is_green(self) -> bool:
        """The set is complete enough to stitch (thin overlap is a hint,
        not a blocker)."""
        return self.verdict in ("ok", "thin")

    @property
    def level(self) -> str:
        """Severity for the status bar QSS: ok / warn / error / neutral."""
        if self.verdict in ("split", "isolated", "unreadable", "error"):
            return "error"
        if self.verdict == "thin" or self.bridge_only:
            return "warn"
        if self.verdict == "ok":
            return "ok"
        return "neutral"    # too_few

    def status_by_stem(self) -> dict[str, str]:
        """Per-segment dot status for the filmstrip: a segment is
        "connected" when it overlaps at least one other segment,
        "isolated" when it overlaps none. Empty when no matrix was
        computed (too_few / unreadable / error) — dots stay unchecked."""
        if not self.confidence:
            return {}
        connected = {
            stem for component in self.components if len(component) > 1
            for stem in component
        }
        return {
            stem: "connected" if stem in connected else "isolated"
            for stem in self.stems
        }

    def to_json(self) -> dict:
        return {
            "verdict": self.verdict,
            "message": self.message,
            "segments": list(self.stems),
            "reference": self.reference_stem,
            "source_files": dict(self.source_files),
            "checked_at": self.checked_at,
            "confidence": [list(row) for row in self.confidence],
            "components": [list(c) for c in self.components],
            "thin_pairs": [list(p) for p in self.thin_pairs],
            "bridge_only": self.bridge_only,
        }

    @classmethod
    def from_json(cls, data: dict) -> "StitchReport":
        return cls(
            verdict=str(data.get("verdict", "error")),
            message=str(data.get("message", "")),
            stems=tuple(data.get("segments", ())),
            reference_stem=data.get("reference"),
            source_files={str(k): int(v) for k, v
                          in (data.get("source_files") or {}).items()},
            checked_at=str(data.get("checked_at", "")),
            confidence=tuple(tuple(float(v) for v in row)
                             for row in data.get("confidence", ())),
            components=tuple(tuple(c) for c in data.get("components", ())),
            thin_pairs=tuple((p[0], p[1], float(p[2]))
                             for p in data.get("thin_pairs", ())),
            bridge_only=bool(data.get("bridge_only", False)),
        )


def load_report(report_path: str) -> StitchReport | None:
    """Read a persisted report; None when absent/malformed."""
    try:
        with open(report_path) as f:
            return StitchReport.from_json(json.load(f))
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return None


def _write_report(report: StitchReport, report_path: str) -> None:
    """Atomic write inside `_stitch/` — the bucket's directory watcher
    never sees the file mid-write (and `_stitch/` itself is not watched).
    The temp name is unique per write so two checks racing on the same
    bucket can't clobber each other's partial file."""
    tmp = f"{report_path}.{id(report):x}.part"
    with open(tmp, "w") as f:
        json.dump(report.to_json(), f, indent=2, ensure_ascii=False)
    os.replace(tmp, report_path)


# ---- graph evaluation (pure functions) -------------------------------------

def _connected_components(count: int, edges: set[tuple[int, int]]) -> list[list[int]]:
    """BFS components over segment indices 0..count-1."""
    remaining = set(range(count))
    neighbors: dict[int, set[int]] = {i: set() for i in range(count)}
    for a, b in edges:
        neighbors[a].add(b)
        neighbors[b].add(a)
    components: list[list[int]] = []
    while remaining:
        queue = [remaining.pop()]
        component = []
        while queue:
            node = queue.pop()
            component.append(node)
            for other in neighbors[node]:
                if other in remaining:
                    remaining.remove(other)
                    queue.append(other)
        components.append(sorted(component))
    components.sort(key=len, reverse=True)
    return components


def _is_bridge_only(count: int, edges: set[tuple[int, int]]) -> bool:
    """True when the set is connected, but ONLY thanks to an edge between
    non-adjacent captures (positions in capture order differing by > 1).

    Why this exists: we match ALL segment pairs (robust against
    out-of-order retakes and 2D grid layouts, unlike the order-dependent
    `range_width` matcher). The one risk of all-pairs is a FALSE match
    between distant segments — papyrus fiber texture is repetitive — and
    such a false long-range edge could silently bridge a real gap in the
    strip, turning an incomplete set green. This guard makes that pattern
    visible: connected with all edges, but disconnected using only
    adjacent-in-order edges → surface a △ warning ("check the preview")
    instead of a clean ✓."""
    if len(_connected_components(count, edges)) != 1:
        return False
    adjacent_edges = {(a, b) for a, b in edges if b - a == 1}
    return len(_connected_components(count, adjacent_edges)) != 1


def _idx(stem: str) -> str:
    """Display index of a capture stem ("Foo_a_vis_003" → "003");
    falls back to the full stem when there is no trailing number."""
    match = re.search(r"(\d+)$", stem)
    return match.group(1) if match else stem


def _format_group(stems: tuple[str, ...] | list[str]) -> str:
    """Human range for a component: contiguous indices → "001–003",
    otherwise a comma list."""
    indices = [_idx(s) for s in stems]
    numeric = [int(i) for i in indices if i.isdigit()]
    if (len(numeric) == len(indices) and len(numeric) > 1
            and numeric == list(range(numeric[0], numeric[0] + len(numeric)))):
        return f"{indices[0]}–{indices[-1]}"
    return ", ".join(indices)


def build_report(
    stems: tuple[str, ...],
    reference_stem: str | None,
    confidence: tuple[tuple[float, ...], ...],
    source_files: dict[str, int],
    checked_at: str,
) -> StitchReport:
    """Evaluate a pairwise confidence matrix into verdict + message.
    Pure function — the unit-testable core of the check."""
    count = len(stems)
    edges = {
        (a, b)
        for a in range(count) for b in range(a + 1, count)
        if confidence[a][b] > CONFIDENCE_THRESHOLD
    }
    components_idx = _connected_components(count, edges)
    components = tuple(tuple(stems[i] for i in c) for c in components_idx)
    thin_pairs = tuple(
        (stems[a], stems[b], round(confidence[a][b], 2))
        for a, b in sorted(edges)
        if confidence[a][b] < THIN_BAND_MAX
    )
    bridge_only = _is_bridge_only(count, edges)

    if len(components_idx) == 1:
        verdict = "thin" if thin_pairs else "ok"
        message = f"✓ All {count} segments connect."
        if verdict == "ok":
            message = f"✓ All {count} segments connect without gaps."
        for a, b, _conf in thin_pairs:
            message += (f" △ Overlap between {_idx(a)} and {_idx(b)} is thin"
                        " — take an extra in-between shot if in doubt.")
        if bridge_only:
            bridging = sorted((a, b) for a, b in edges if b - a > 1)
            a, b = bridging[0]
            message += (f" △ Segments only connect through non-adjacent"
                        f" shots ({_idx(stems[a])} ↔ {_idx(stems[b])})"
                        " — check the preview.")
    elif (len(components_idx) == 2
            and min(len(c) for c in components_idx) == 1):
        verdict = "isolated"
        loner = min(components, key=len)[0]
        message = (f"⚠ {loner} does not overlap any other segment. Either"
                   " it does not belong to this object, or a shot between"
                   " it and the rest is missing.")
    else:
        verdict = "split"
        groups = " and ".join(_format_group(c) for c in components)
        message = (f"⚠ Segments {groups} connect within themselves but not"
                   " to each other — a shot in between is probably missing.")

    return StitchReport(
        verdict=verdict, message=message, stems=stems,
        reference_stem=reference_stem, source_files=source_files,
        checked_at=checked_at, confidence=confidence,
        components=components, thin_pairs=thin_pairs,
        bridge_only=bridge_only,
    )


# ---- image loading ----------------------------------------------------------

# JPEG-level fractional decode: pick the strongest reduction that still
# lands at or above the target size. Chosen PER FILE from the JPEG header
# (a fixed factor would under-resolve smaller previews, e.g. the IR
# camera's 12 MP vs the VIS camera's 60 MP).
_REDUCTIONS = (
    (8, cv2.IMREAD_REDUCED_COLOR_8),
    (4, cv2.IMREAD_REDUCED_COLOR_4),
    (2, cv2.IMREAD_REDUCED_COLOR_2),
)


def _decode_near_target(data: bytes, target_megapix: float) -> np.ndarray | None:
    """Decode JPEG bytes near the target size, or None if undecodable.
    Reads the JPEG header for the pixel count (cheap, no full decode) to
    pick the reduction; a corrupt header falls through to a full decode,
    which cv2 turns into None for truly broken data."""
    flag = cv2.IMREAD_COLOR
    try:
        with Image.open(BytesIO(data)) as im:
            pixels = im.size[0] * im.size[1]
    except Exception:
        pixels = 0
    for factor, reduced_flag in _REDUCTIONS:
        if pixels and pixels / factor ** 2 >= target_megapix * 1e6:
            flag = reduced_flag
            break
    return cv2.imdecode(np.frombuffer(data, np.uint8), flag)


# ---- bucket snapshot ---------------------------------------------------------

@dataclass(frozen=True)
class _BucketSnapshot:
    """Plain-data view of one bucket, taken on the GUI thread — runners
    never touch the live `Object`."""
    obj_dir: str
    side: str
    spectrum: str
    reference_stem: str | None
    segments: tuple[tuple[str, str], ...]   # (stem, file path), index order
    source_files: dict[str, int]            # stem → mtime_ns
    stitch_dir: str
    report_path: str


def _snapshot_bucket(obj, side: str, spectrum: str) -> _BucketSnapshot:
    """Segments = the bucket's captures minus the reference photo.
    Prefers the JPG of a pair (no rawpy parse needed), falls back to RAW."""
    reference = obj.reference(side, spectrum)
    reference_stem = reference.stem if reference else None
    segments = []
    source_files: dict[str, int] = {}
    for capture in obj.captures(side, spectrum):
        if capture.stem == reference_stem:
            continue
        path = capture.jpg_path or capture.raw_path
        if path is None:
            continue
        segments.append((capture.stem, path))
        try:
            source_files[capture.stem] = os.stat(path).st_mtime_ns
        except OSError:
            source_files[capture.stem] = -1
    return _BucketSnapshot(
        obj_dir=obj.dir, side=side, spectrum=spectrum,
        reference_stem=reference_stem, segments=tuple(segments),
        source_files=source_files,
        stitch_dir=stitch_dir_for(obj.dir, side, spectrum),
        report_path=stitch_report_path_for(obj.dir, side, spectrum),
    )


# ---- check runner ------------------------------------------------------------

class _CheckSignals(QObject):
    """QRunnable can't host signals — QObject sidecar (see _CopyRunner)."""
    finished = pyqtSignal(object)   # StitchReport


class _CheckRunner(QRunnable):
    """Runs one connectivity check off the GUI thread: decode embedded
    previews near 0.6 MP → ORB features → affine all-pairs matching →
    graph evaluation. Writes report.json, then emits the report."""

    def __init__(self, snapshot: _BucketSnapshot):
        super().__init__()
        self._snap = snapshot
        self.signals = _CheckSignals()

    def run(self) -> None:
        # OpenCV builds its OpenCL ORB kernel with the C locale's decimal
        # separator; on a comma-locale machine (e.g. German) it comes out
        # as `0,039f`, fails to compile, and dumps a multi-line build error
        # to stderr before falling back to CPU. The setting is thread-local,
        # so disable it here on the worker thread that runs ORB. No loss:
        # our sub-megapixel matching sees no GPU benefit anyway.
        cv2.ocl.setUseOpenCL(False)
        snap = self._snap
        try:
            os.makedirs(snap.stitch_dir, exist_ok=True)
            report = self._check(snap)
        except Exception as e:   # fail loud: an unexpected error is a result
            _logger.warning("stitch check failed for %s/%s/%s: %r",
                            snap.obj_dir, snap.side, snap.spectrum, e,
                            exc_info=True)
            report = self._simple_report(
                snap, "error", f"✗ Stitch check failed: {e}")
        try:
            _write_report(report, snap.report_path)
        except OSError as e:
            _logger.warning("could not write %s: %r", snap.report_path, e)
        self.signals.finished.emit(report)

    @staticmethod
    def _simple_report(snap: _BucketSnapshot, verdict: str, message: str,
                       ) -> StitchReport:
        return StitchReport(
            verdict=verdict, message=message,
            stems=tuple(stem for stem, _ in snap.segments),
            reference_stem=snap.reference_stem,
            source_files=snap.source_files,
            checked_at=datetime.now().isoformat(timespec="seconds"),
        )

    def _check(self, snap: _BucketSnapshot) -> StitchReport:
        if len(snap.segments) < 2:
            return self._simple_report(
                snap, "too_few", "At least 2 segments are needed for stitching.")

        arrays = []
        for stem, path in snap.segments:
            data = read_embedded_jpeg(path)
            img = _decode_near_target(data, CHECK_TARGET_MEGAPIX) if data else None
            if img is None:
                return self._simple_report(
                    snap, "unreadable",
                    f"✗ {stem}: no readable JPEG preview — cannot check this file.")
            arrays.append(img)

        # The library's MEDIUM resize brings every image to the same
        # working scale, matching what a real stitch would see.
        medium = list(Images.of(arrays).resize(Images.Resolution.MEDIUM))
        detector = FeatureDetector()
        features = [detector.detect_features(img) for img in medium]
        # Affine matcher: the repro stand moves translationally over a
        # flat object — the affine model's exact domain.
        matches = FeatureMatcher(matcher_type="affine").match_features(features)
        confidence = tuple(
            tuple(float(v) for v in row)
            for row in FeatureMatcher.get_confidence_matrix(matches)
        )
        return build_report(
            stems=tuple(stem for stem, _ in snap.segments),
            reference_stem=snap.reference_stem,
            confidence=confidence,
            source_files=snap.source_files,
            checked_at=datetime.now().isoformat(timespec="seconds"),
        )


# ---- controller ---------------------------------------------------------------

class StitchController(QObject):
    """Schedules connectivity checks and hands results back to main.py.

    Stateless apart from a debounce timer and a generation counter:
    report.json on disk is the only cache (`fresh_report` validates it
    against the bucket's current files). Results from a superseded
    generation — object switched, newer check started — are dropped."""

    DEBOUNCE_MS = 1500

    # obj_dir, side, spectrum, StitchReport — emitted only for current-gen
    # checks; the receiver still confirms the bucket is the active one.
    check_finished = pyqtSignal(str, str, str, object)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._gen = 0
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(self.DEBOUNCE_MS)
        self._debounce.timeout.connect(self._run_scheduled)
        self._scheduled: tuple | None = None    # (obj, side, spectrum)

    def schedule_check(self, obj, side: str, spectrum: str) -> None:
        """Debounced check — coalesces the JPG+RAW watcher events of a
        settling capture into one run. A new schedule retargets the timer
        (only the active bucket ever schedules)."""
        self._scheduled = (obj, side, spectrum)
        self._debounce.start()

    def run_check_now(self, obj, side: str, spectrum: str) -> None:
        """Immediate check — bucket switch, user is looking."""
        self._debounce.stop()
        self._scheduled = None
        self._start(obj, side, spectrum)

    def fresh_report(self, obj, side: str, spectrum: str) -> StitchReport | None:
        """The persisted report, if it still matches the bucket's current
        segment files and reference. None → caller should run a check."""
        snap = _snapshot_bucket(obj, side, spectrum)
        report = load_report(snap.report_path)
        if (report is not None
                and report.source_files == snap.source_files
                and report.reference_stem == snap.reference_stem):
            return report
        return None

    def invalidate(self) -> None:
        """Drop scheduled and in-flight work (object switch / close)."""
        self._gen += 1
        self._debounce.stop()
        self._scheduled = None

    # ---- internals -------------------------------------------------------

    def _run_scheduled(self) -> None:
        if self._scheduled is not None:
            obj, side, spectrum = self._scheduled
            self._scheduled = None
            self._start(obj, side, spectrum)

    def _start(self, obj, side: str, spectrum: str) -> None:
        snap = _snapshot_bucket(obj, side, spectrum)
        self._gen += 1
        gen = self._gen
        runner = _CheckRunner(snap)
        runner.signals.finished.connect(
            lambda report, g=gen, s=snap: self._on_finished(g, s, report))
        QThreadPool.globalInstance().start(runner)

    def _on_finished(self, gen: int, snap: _BucketSnapshot,
                     report: StitchReport) -> None:
        if gen != self._gen:
            return  # superseded (newer check or invalidate) — drop
        self.check_finished.emit(snap.obj_dir, snap.side, snap.spectrum, report)
