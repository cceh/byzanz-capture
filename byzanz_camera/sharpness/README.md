# Sharpness metric

This package is vendored from the canonical implementation in
`croc-viewer/api/sharpness/` so capture feedback can run without a server.

Make metric changes in the canonical source first, validate them over the
corpus there, then update these copies together. Capture-specific thresholds,
status mapping, persistence, and UI remain in `byzanz-capture`.

`object_blur.py` here is a deliberate SUBSET of its canonical counterpart:
only the shared primitives the metric chain (v2 → v3 → v4) consumes
(`_gray_full`, `_edge_sites`, `N_EDGES`). The v1 estimator and its
`measure()` are not vendored — this app never runs v1. The active metric
is whatever `__init__.py` re-exports (currently `object_blur_v4`); the
older versions stay vendored because each delta module imports its
predecessor.

Local divergence (2026-09, to port back to the canonical source with the
next sync): `exclusion_masks` returns detected regions instead of bare
polygons (`detect_regions` / `mask_from_polygons` / `jsonable_regions`;
the scale detector attaches its tick-comb measurements), and
`object_blur_v4.measure` reports them as `regions` so the capture audit
can persist the geometry per capture. Measurement values are unchanged —
`metric_version` stays "v4".

## Planned extraction

Sharpness, ColorChecker detection, scale-bar detection, and future image
audits are currently shared by copying validated modules. They should
eventually move into one versioned image-analysis library consumed by both
Capture and Viewer. Until that boundary exists, avoid divergent local edits:
develop and validate the metric in the canonical Viewer source first.
