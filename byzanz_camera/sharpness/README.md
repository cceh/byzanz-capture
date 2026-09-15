# Sharpness metric

This package is vendored from the canonical implementation in
`croc-viewer/api/sharpness/` so capture feedback can run without a server.
Current sync: metric v6 (croc-viewer commit 00d0fd1) — `object_blur.py`
(measurement + result assembly), `cards.py` (ColorChecker via OpenCV `mcc`,
scale card + exclusion mask), `scalecard.py` (barcode-localiser + comb
lattice, px/mm from the image), `edge_blur.py` (tick-edge sharpness of the
scale card, metric "scalecard-v2" — the scalecard audit on stitching
reference frames). The modules are byte-identical to the canonical source;
`__init__.py` re-exports what the capture app consumes.

Make metric changes in the canonical source first, validate them over the
corpus there, then copy the modules over unchanged. Capture-specific
thresholds, status mapping, persistence, and UI remain in `byzanz-capture`
(`papyri/audits/`).

## Planned extraction

Sharpness, ColorChecker detection, scale-card detection, and future image
audits are currently shared by copying validated modules. They should
eventually move into one versioned image-analysis library consumed by both
Capture and Viewer. Until that boundary exists, avoid divergent local edits:
develop and validate the metric in the canonical Viewer source first.
