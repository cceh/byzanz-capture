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
from PyQt6.QtGui import QImage
from stitching import AffineStitcher
from stitching.feature_detector import FeatureDetector
from stitching.feature_matcher import FeatureMatcher
from stitching.images import Images
from stitching.stitching_error import StitchingError

from byzanz_camera.load_image_worker import read_embedded_jpeg
from papyri.object_layout import (
    GREEN_VERDICTS, stitch_dir_for, stitch_preview_path_for,
    stitch_report_path_for,
)

_logger = logging.getLogger("stitching")

# A pair of segments counts as CONFIDENTLY overlapping above this pairwise
# match confidence — OpenCV's conventional "same panorama" bar (confidence is
# roughly num_inliers / (8 + 0.3 * num_matches)). Tuned for papyrus, where
# overlapping segments score ~2–2.5 and non-overlapping ones ~0.3.
CONFIDENCE_THRESHOLD = 1.0
# Below the green bar but above this is the "uncertain" gray zone: the
# segments probably overlap but the texture is too low to say confidently
# (e.g. a smooth surface, or a papyrus overlap through a blank/damaged
# region). Chosen above papyrus's non-overlap floor (~0.3) so a real gap
# still reads as isolated, not uncertain — the safe direction.
UNCERTAIN_THRESHOLD = 0.5
# Green edges between the bar and this get a "thin overlap" hint: connected,
# but with little margin — worth an extra in-between shot.
THIN_BAND_MAX = 1.3
# Feature detection/matching resolution. Matching quality is determined
# at this scale; the 60 MP native size never enters the check.
CHECK_TARGET_MEGAPIX = 0.6

# Preview composite: segments are decoded a little above the final size so
# the affine warp downsamples (never upscales) into the panorama. A 2 MP
# preview is plenty to eyeball the seams; the archival panorama is a
# separate post-processing step from the RAWs.
PREVIEW_TARGET_MEGAPIX = 3.0
PREVIEW_FINAL_MEGAPIX = 2
# A marginal (uncertain) overlap makes the RANSAC-based match + camera
# estimation non-deterministic — the same segments can miss on one draw and
# composite on the next. A few retries turn a ~50/50 draw into a reliable
# preview; each miss fails fast at the subsetter, so retries are cheap.
PREVIEW_STITCH_ATTEMPTS = 6
# Safety cap on the composited canvas. A degenerate affine estimate (from a
# marginal overlap) does not merely fail — it warps a segment onto a canvas of
# BILLIONS of pixels (observed: 155858550×34559848 → ~60 GB) and the process is
# OOM-killed before any error surfaces. A legitimate preview of the largest
# realistic segment set is well under this; `warp_rois` predicts the canvas
# from geometry alone, so we reject the degenerate draw BEFORE allocating.
PREVIEW_MAX_CANVAS_MEGAPIX = 150
# Physical-plausibility guard on the estimated cameras. The repro stand is
# translational: between segments the fragment SLIDES (relative rotation =
# accidental nudge, a few degrees at most) at a FIXED camera height (equal
# scale). Repetitive papyrus/fiber texture lets RANSAC occasionally find a
# consistent-but-WRONG solution that satisfies neither — observed on a real
# 9-segment set: bad draws had 14–47° rotation spread and 1.9–2.9× scale
# spread (a visually scattered, shrunken composite), while good draws of the
# same set stayed ≤ 3.4° and ≤ 1.06×. Such draws pass the canvas cap (their
# canvas is small!), so they need their own rejection.
PREVIEW_MAX_ANGLE_SPREAD_DEG = 10.0
PREVIEW_MAX_SCALE_SPREAD = 1.25
# Registration + compositing quality, from the 2026-07 parameter study on a
# real 9-segment set (docs/papyri-stitching-concept.md, "Winning stitcher
# settings"). Order matters conceptually: registration first (features +
# working resolution — REMOVES misalignment), seams/blending second (HIDES
# the remainder, appropriate for this QA preview; the archival composite in
# post uses the accuracy variant instead). gc_color must never be adopted
# without the registration settings — with poor registration graph-cut seams
# cut straight through content. Costs ~2 s vs ~0.6 s per preview.
PREVIEW_QUALITY_SETTINGS = {
    "nfeatures": 5000,             # default 500 is far too few → gross offsets
    "medium_megapix": 3.0,         # registration accuracy (default 0.6)
    "compensator": "gain_blocks",  # AffineStitcher default is NO exposure
                                   # compensation → brightness steps at seams
    "finder": "gc_color",          # graph-cut seams route around content;
                                   # default dp_color cuts through strokes
    "blend_strength": 15,          # hides remaining hairline misregistration
}


# ---- report ----------------------------------------------------------------

@dataclass(frozen=True)
class StitchReport:
    """Result of one connectivity check over a bucket's segment set.

    `stems` are the checked segments in capture-index order — the
    reference photo is excluded by design (it overlaps nothing).
    `source_files` (stem → mtime_ns of the checked file) is the staleness
    signature: a report is only trusted while it matches the bucket's
    current disk state."""

    verdict: str            # ok | thin | uncertain | split | isolated | too_few | unreadable | error
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
        """The set is CONFIDENTLY complete (thin overlap is a hint, not a
        blocker). Gates completeness — `uncertain` deliberately does NOT
        count, so an unverified low-texture set stays incomplete."""
        return self.verdict in GREEN_VERDICTS

    def allows_preview(self) -> bool:
        """Whether the preview button is offered: green OR uncertain — for
        uncertain the whole point is to let the user verify visually."""
        return self.verdict in GREEN_VERDICTS or self.verdict == "uncertain"

    @property
    def level(self) -> str:
        """Severity for the status bar QSS: ok / warn / error / neutral."""
        if self.verdict in ("split", "isolated", "unreadable", "error"):
            return "error"
        if self.verdict in ("thin", "uncertain") or self.bridge_only:
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


@dataclass(frozen=True)
class PreviewResult:
    """Outcome of a preview composite. `image` is a QImage (built off the
    GUI thread; the main thread wraps it in a QPixmap) on success, None on
    failure. `message` is the user-facing line for the status bar."""
    ok: bool
    message: str
    n_segments: int
    image: QImage | None = None
    preview_path: str | None = None


def _add_preview_to_report(report_path: str, preview_file: str,
                           megapix: int, generated_at: str) -> None:
    """Record the generated preview in the bucket's report.json (a check
    rewrites the report and drops this block — a new preview then re-adds
    it, so the block always describes the current preview.jpg)."""
    report = load_report(report_path)
    if report is None:
        return
    data = report.to_json()
    data["preview"] = {"file": preview_file, "megapix": megapix,
                       "generated_at": generated_at}
    tmp = f"{report_path}.{id(data):x}.part"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, report_path)


# ---- shared matching primitives ---------------------------------------------
# The canonical ORB-detect / affine-pair-match pair — shared between the
# connectivity check below and the overlap coach (`papyri/overlap_coach.py`).
# Do NOT re-instantiate detector/matcher pipelines at call sites.

def detect_features(img: np.ndarray):
    """ORB features of one image at its given scale (the check decodes
    near CHECK_TARGET_MEGAPIX; match quality is determined at that scale)."""
    return FeatureDetector().detect_features(img)


def match_pair(feat_a, feat_b) -> tuple[float, np.ndarray | None]:
    """Affine-match two feature sets → `(confidence, H)` where `H` is the
    3×3 affine mapping a-coordinates into b-coordinates (None when no
    transform could be estimated). Confidence is on the same scale as the
    check's matrix, so `CONFIDENCE_THRESHOLD` / `UNCERTAIN_THRESHOLD`
    apply unchanged."""
    matches = FeatureMatcher(matcher_type="affine").match_features(
        [feat_a, feat_b])
    confidence = float(FeatureMatcher.get_confidence_matrix(matches)[0][1])
    h = matches[1].H    # pairwise index (0 → 1)
    if h is None:
        return confidence, None
    h = np.asarray(h, dtype=np.float64)
    if h.shape == (2, 3):   # cv2 may return the affine without the [0,0,1] row
        h = np.vstack([h, (0.0, 0.0, 1.0)])
    return confidence, h


def load_segment_image(path: str,
                       target_megapix: float = CHECK_TARGET_MEGAPIX,
                       ) -> np.ndarray | None:
    """Decode ONE capture's embedded JPEG near the check's working scale —
    the overlap coach's anchor load. None when unreadable."""
    data = read_embedded_jpeg(path)
    if data is None:
        return None
    return _decode_segments([data], target_megapix)[0]


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
    Pure function — the unit-testable core of the check.

    Two connectivity levels: GREEN edges (confidently overlap, > 1.0) and
    GRAY edges (probably overlap, > 0.5). Connectivity for gap detection
    uses the gray graph (a low-texture but real overlap still connects); a
    set that only holds together via a gray-only seam is `uncertain` (verify
    via preview), not `split`/`isolated`."""
    count = len(stems)

    def edges_above(threshold: float) -> set[tuple[int, int]]:
        return {(a, b) for a in range(count) for b in range(a + 1, count)
                if confidence[a][b] > threshold}

    green_edges = edges_above(CONFIDENCE_THRESHOLD)     # > 1.0
    gray_edges = edges_above(UNCERTAIN_THRESHOLD)       # > 0.5 (superset)
    gray_comps_idx = _connected_components(count, gray_edges)
    green_comps_idx = _connected_components(count, green_edges)
    # Dots reflect gray-level connectivity — a gray-connected segment still
    # "connects" to the set (its seam is just low-confidence).
    components = tuple(tuple(stems[i] for i in c) for c in gray_comps_idx)
    thin_pairs = tuple(
        (stems[a], stems[b], round(confidence[a][b], 2))
        for a, b in sorted(green_edges)
        if confidence[a][b] < THIN_BAND_MAX
    )
    bridge_only = _is_bridge_only(count, green_edges)

    if len(gray_comps_idx) > 1:
        # Doesn't hold together even at the gray level → a real gap.
        if (len(gray_comps_idx) == 2
                and min(len(c) for c in gray_comps_idx) == 1):
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
    elif len(green_comps_idx) == 1:
        # Confidently connected end to end.
        verdict = "thin" if thin_pairs else "ok"
        message = (f"✓ All {count} segments connect without gaps."
                   if verdict == "ok" else f"✓ All {count} segments connect.")
        for a, b, _conf in thin_pairs:
            message += (f" △ Overlap between {_idx(a)} and {_idx(b)} is thin"
                        " — take an extra in-between shot if in doubt.")
        if bridge_only:
            a, b = sorted((a, b) for a, b in green_edges if b - a > 1)[0]
            message += (f" △ Segments only connect through non-adjacent"
                        f" shots ({_idx(stems[a])} ↔ {_idx(stems[b])})"
                        " — check the preview.")
    else:
        # Holds together, but only through gray-only seam(s) — probably
        # overlaps, too little texture to be sure. Name the weakest seam.
        gcomp_of = {i: k for k, comp in enumerate(green_comps_idx) for i in comp}
        bridges = [(a, b) for (a, b) in gray_edges
                   if (a, b) not in green_edges and gcomp_of[a] != gcomp_of[b]]
        a, b = min(bridges, key=lambda e: confidence[e[0]][e[1]])
        verdict = "uncertain"
        message = (f"△ Overlap between {_idx(stems[a])} and {_idx(stems[b])} is"
                   " uncertain (low texture) — check the preview to confirm.")

    return StitchReport(
        verdict=verdict, message=message, stems=stems,
        reference_stem=reference_stem, source_files=source_files,
        checked_at=checked_at, confidence=confidence,
        components=components, thin_pairs=thin_pairs,
        bridge_only=bridge_only,
    )


# ---- image loading ----------------------------------------------------------

# JPEG-level fractional decode factors (cv2 supports /8, /4, /2; /1 = full).
_REDUCTIONS = (
    (8, cv2.IMREAD_REDUCED_COLOR_8),
    (4, cv2.IMREAD_REDUCED_COLOR_4),
    (2, cv2.IMREAD_REDUCED_COLOR_2),
)


def _jpeg_pixels(data: bytes) -> int:
    """Pixel count from a JPEG header (cheap, no full decode); 0 if the
    header is unreadable."""
    try:
        with Image.open(BytesIO(data)) as im:
            return im.size[0] * im.size[1]
    except Exception:
        return 0


def _decode_segments(byte_list: list[bytes | None],
                     target_megapix: float) -> list[np.ndarray | None]:
    """Decode a set of segment JPEGs at ONE shared reduction factor (chosen
    from the LARGEST segment so nothing upscales), so every segment enters
    the stitcher at the SAME scale. Aligned to `byte_list`; None where a
    segment is missing/undecodable.

    Why one factor and not per-file: cv2's reductions are discrete (/2, /4,
    /8). Segments of near-equal size can straddle a threshold — one lands at
    /4, its neighbour at /2, i.e. twice the resolution. The affine matcher
    then absorbs that size difference as a per-segment SCALE and renders some
    segments tiny in the composite. Same factor for all keeps mm/pixel
    consistent → the registration is pure translation → a clean panorama."""
    max_pixels = max((_jpeg_pixels(d) for d in byte_list if d), default=0)
    flag = cv2.IMREAD_COLOR
    for factor, reduced_flag in _REDUCTIONS:
        if max_pixels and max_pixels / factor ** 2 >= target_megapix * 1e6:
            flag = reduced_flag
            break
    return [cv2.imdecode(np.frombuffer(d, np.uint8), flag) if d else None
            for d in byte_list]


def _bgr_to_qimage(bgr: np.ndarray) -> QImage:
    """OpenCV BGR array → QImage. `.copy()` detaches the QImage from the
    numpy buffer so it survives after the array is freed (safe to build off
    the GUI thread; QImage, unlike QPixmap, is not thread-affine)."""
    rgb = np.ascontiguousarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
    h, w, _ = rgb.shape
    return QImage(rgb.data, w, h, w * 3, QImage.Format.Format_RGB888).copy()


class _GuardedAffineStitcher(AffineStitcher):
    """AffineStitcher that rejects implausible camera estimates BEFORE the
    warp allocates or composites anything, raising a StitchingError the
    retry loop treats as a failed draw. Two guards, intercepted right after
    camera estimation (`warp_low_resolution` is the first consumer of the
    cameras):

    1. Canvas cap — a degenerate transform doesn't merely fail, it warps a
       segment onto a canvas of billions of pixels and the process is
       OOM-killed before any exception surfaces. `warp_rois` computes the
       bounding box from geometry alone (no allocation).
    2. Rig physics — rotation/scale spread across the cameras beyond what a
       sliding fragment under a fixed camera can produce means RANSAC found
       a consistent-but-wrong solution (small canvas, scattered composite);
       see PREVIEW_MAX_ANGLE_SPREAD_DEG / PREVIEW_MAX_SCALE_SPREAD."""

    def warp_low_resolution(self, imgs, cameras):
        # Each affine camera's 2×3 transform lives in R's top rows:
        # [[s·cosθ, s·sinθ, tx], [−s·sinθ, s·cosθ, ty]].
        angles, scales = [], []
        for camera in cameras:
            r = np.asarray(camera.R)
            scales.append(float(np.hypot(r[0, 0], r[0, 1])))
            angles.append(float(np.degrees(np.arctan2(r[0, 1], r[0, 0]))))
        # Spread relative to the first camera, wrapped to (−180, 180].
        rel = [(a - angles[0] + 180.0) % 360.0 - 180.0 for a in angles]
        angle_spread = max(rel) - min(rel)
        scale_spread = max(scales) / min(scales) if min(scales) > 0 else float("inf")
        if angle_spread > PREVIEW_MAX_ANGLE_SPREAD_DEG:
            raise StitchingError(
                f"camera estimate rotates segments {angle_spread:.0f}° apart "
                f"(cap {PREVIEW_MAX_ANGLE_SPREAD_DEG:.0f}°) — implausible for "
                "a sliding fragment, rejecting this draw")
        if scale_spread > PREVIEW_MAX_SCALE_SPREAD:
            raise StitchingError(
                f"camera estimate scales segments {scale_spread:.2f}× apart "
                f"(cap {PREVIEW_MAX_SCALE_SPREAD}×) — implausible for a fixed "
                "camera height, rejecting this draw")

        sizes = self.images.get_scaled_img_sizes(Images.Resolution.FINAL)
        aspect = self.images.get_ratio(
            Images.Resolution.MEDIUM, Images.Resolution.FINAL)
        corners, roi_sizes = self.warper.warp_rois(sizes, cameras, aspect)
        _, _, w, h = cv2.detail.resultRoi(corners, roi_sizes)
        if w * h > PREVIEW_MAX_CANVAS_MEGAPIX * 1_000_000:
            raise StitchingError(
                f"panorama canvas {w}×{h} px exceeds the "
                f"{PREVIEW_MAX_CANVAS_MEGAPIX} MP cap — the affine estimate is "
                "degenerate (marginal overlap)")
        return super().warp_low_resolution(imgs, cameras)


def _stitch_preview(arrays: list[np.ndarray]) -> tuple[np.ndarray, int]:
    """Affine-composite the segments, returning `(pano, n_composited)` where
    `n_composited` may be fewer than `len(arrays)` if the subsetter dropped a
    too-marginal segment. Retries on a failed draw: a marginal
    overlap makes RANSAC non-deterministic, so a single set of segments can
    fail one attempt and succeed the next (see PREVIEW_STITCH_ATTEMPTS). Three
    failure modes surface here: `StitchingError` when the camera *estimator*
    gives up, a raw `cv2.error` assertion when the bundle *adjuster* is handed
    too few inliers, and — most importantly — a degenerate transform whose
    canvas would blow past `_GuardedAffineStitcher`'s size cap. All three are
    transient and retried. Raises the last error only if every attempt fails
    (genuinely un-compositable)."""
    last: Exception | None = None
    for _ in range(PREVIEW_STITCH_ATTEMPTS):
        try:
            stitcher = _GuardedAffineStitcher(
                final_megapix=PREVIEW_FINAL_MEGAPIX, crop=False,
                confidence_threshold=UNCERTAIN_THRESHOLD,
                **PREVIEW_QUALITY_SETTINGS)
            pano = stitcher.stitch(arrays)
            # The subsetter may drop a segment too marginal to place; report
            # how many actually made it into the canvas, not how many we fed in.
            return pano, len(stitcher.images.names)
        except (StitchingError, cv2.error) as e:
            last = e
    raise last


def _write_jpeg_atomic(bgr: np.ndarray, path: str) -> None:
    """Write a BGR array as JPEG via a unique temp + rename, so a bucket
    watcher never sees a half-written `_stitch/preview.jpg`. Encoded in
    memory (imencode, not imwrite) so the format comes from `.jpg`, not the
    temp file's `.part` extension."""
    ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        raise OSError(f"JPEG encode failed for {path}")
    tmp = f"{path}.{id(bgr):x}.part"
    with open(tmp, "wb") as f:
        f.write(buf.tobytes())
    os.replace(tmp, path)


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


def snapshot_bucket(obj, side: str, spectrum: str) -> _BucketSnapshot:
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

        byte_list = [read_embedded_jpeg(path) for _, path in snap.segments]
        arrays = _decode_segments(byte_list, CHECK_TARGET_MEGAPIX)
        for (stem, _), img in zip(snap.segments, arrays):
            if img is None:
                return self._simple_report(
                    snap, "unreadable",
                    f"✗ {stem}: no readable JPEG preview — cannot check this file.")

        # The library's MEDIUM resize brings every image to the same
        # working scale, matching what a real stitch would see.
        medium = list(Images.of(arrays).resize(Images.Resolution.MEDIUM))
        features = [detect_features(img) for img in medium]
        # Affine matcher: the repro stand moves translationally over a
        # flat object — the affine model's exact domain. One all-pairs
        # match_features call, NOT n²/2 `match_pair` calls: it yields the
        # full confidence matrix in one go with identical numbers.
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


# ---- preview runner ----------------------------------------------------------

class _PreviewSignals(QObject):
    finished = pyqtSignal(object)   # PreviewResult


class _PreviewRunner(QRunnable):
    """Composites the bucket's segments into a preview panorama off the GUI
    thread (AffineStitcher — translational rig, flat object), writes
    `_stitch/preview.jpg`, records it in report.json, and emits the panorama
    as a QImage. Only started for a green set (the button is disabled
    otherwise), so `< 2 segments` shouldn't occur — guarded anyway."""

    def __init__(self, snapshot: _BucketSnapshot):
        super().__init__()
        self._snap = snapshot
        self.signals = _PreviewSignals()

    def run(self) -> None:
        cv2.ocl.setUseOpenCL(False)   # locale-fragile OpenCL kernel; see _CheckRunner
        snap = self._snap
        n = len(snap.segments)
        try:
            result = self._preview(snap, n)
        except (StitchingError, cv2.error) as e:
            result = PreviewResult(
                ok=False, n_segments=n,
                message=("✗ Preview couldn't be stitched — the overlap is too "
                         "marginal to composite reliably. The segments are "
                         "saved; stitching can be done in post-processing."))
            _logger.warning("stitch preview failed for %s/%s/%s: %r",
                            snap.obj_dir, snap.side, snap.spectrum, e)
        except Exception as e:
            result = PreviewResult(
                ok=False, n_segments=n, message=f"✗ Stitch preview failed: {e}")
            _logger.warning("stitch preview error for %s/%s/%s: %r",
                            snap.obj_dir, snap.side, snap.spectrum, e,
                            exc_info=True)
        self.signals.finished.emit(result)

    def _preview(self, snap: _BucketSnapshot, n: int) -> PreviewResult:
        if n < 2:
            return PreviewResult(
                ok=False, n_segments=n,
                message="At least 2 segments are needed for stitching.")
        byte_list = [read_embedded_jpeg(path) for _, path in snap.segments]
        arrays = _decode_segments(byte_list, PREVIEW_TARGET_MEGAPIX)
        for (stem, _), img in zip(snap.segments, arrays):
            if img is None:
                return PreviewResult(
                    ok=False, n_segments=n,
                    message=f"✗ {stem}: no readable JPEG preview.")

        pano, n_used = _stitch_preview(arrays)
        # The uncovered canvas (where no segment maps) comes out pure black.
        # Recolour it white so the preview reads as papyrus on the white
        # light table, not framed in black.
        pano[(pano == 0).all(axis=2)] = 255

        os.makedirs(snap.stitch_dir, exist_ok=True)
        preview_path = stitch_preview_path_for(
            snap.obj_dir, snap.side, snap.spectrum)
        _write_jpeg_atomic(pano, preview_path)
        _add_preview_to_report(
            snap.report_path, os.path.basename(preview_path),
            PREVIEW_FINAL_MEGAPIX, datetime.now().isoformat(timespec="seconds"))

        dropped = n - n_used
        message = (f"✓ Stitched preview of {n_used} segments."
                   if dropped == 0 else
                   f"△ Stitched {n_used} of {n} segments — "
                   f"{'one' if dropped == 1 else str(dropped)} too marginal to "
                   f"place, left out.")
        return PreviewResult(
            ok=True, n_segments=n,
            message=message,
            image=_bgr_to_qimage(pano), preview_path=preview_path)


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

    # obj_dir, side, spectrum, PreviewResult — same current-gen + active-bucket
    # discipline as check_finished.
    preview_finished = pyqtSignal(str, str, str, object)

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

    def run_preview(self, obj, side: str, spectrum: str) -> None:
        """Composite the bucket's segments into a preview (user clicked the
        button on a green set). Bumps the generation, so any in-flight check
        for this bucket is superseded — check and preview never both land."""
        snap = snapshot_bucket(obj, side, spectrum)
        self._gen += 1
        gen = self._gen
        runner = _PreviewRunner(snap)
        runner.signals.finished.connect(
            lambda result, g=gen, s=snap: self._on_preview_finished(g, s, result))
        QThreadPool.globalInstance().start(runner)

    def fresh_report(self, obj, side: str, spectrum: str) -> StitchReport | None:
        """The persisted report, if it still matches the bucket's current
        segment files and reference. None → caller should run a check."""
        snap = snapshot_bucket(obj, side, spectrum)
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
        snap = snapshot_bucket(obj, side, spectrum)
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

    def _on_preview_finished(self, gen: int, snap: _BucketSnapshot,
                             result: PreviewResult) -> None:
        if gen != self._gen:
            return  # superseded (object switched / new capture) — drop
        self.preview_finished.emit(
            snap.obj_dir, snap.side, snap.spectrum, result)
