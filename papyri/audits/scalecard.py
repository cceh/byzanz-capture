"""The scale-card capture audit: how sharp are the card's tick edges?

Wraps the vendored metric (byzanz_camera.sharpness.edge_blur) in Papyri
policy. Runs ONLY on stitching reference frames: those show nothing but
the cards on the plate, so the object metric reports "not measurable"
there and the card's tick edges are the frame's only focus probe. On
ordinary captures the sharpness check covers focus and the card sits
masked at the frame edge — a second measurement would add nothing.

Data supplier, not operator feedback: the entry feeds the viewer's
corpus review (which classifies in µm with its own thresholds); this
check carries no warn thresholds and never contributes warn badges.
"""
from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import QSettings

from byzanz_camera.capture_audit import AuditFinding, AuditModality, SCALECARD_AUDIT
from byzanz_camera.settings_migration import PAPYRI_SCALECARD_ENABLED_KEY
from byzanz_camera.sharpness.edge_blur import METRIC_VERSION

CHECK = SCALECARD_AUDIT


@dataclass(frozen=True)
class ScalecardAuditSettings:
    enabled: bool


def read_settings(settings: QSettings) -> ScalecardAuditSettings:
    """This check's settings slice — an on/off switch only (no dialog UI
    yet; a missing key means enabled)."""
    return ScalecardAuditSettings(
        enabled=settings.value(PAPYRI_SCALECARD_ENABLED_KEY, True, type=bool))


def applies_to(stem: str, *, stitching: bool, reference_stem: str | None,
               n_captures: int) -> bool:
    """Only the stitch reference frame — and only in a MULTI-capture
    bucket: a single-capture bucket's shot shows the papyrus (the object
    fits one frame there), so it is an ordinary capture; the card-only
    reference shot exists only where segments follow. Same rule as the
    viewer's import."""
    return stitching and n_captures > 1 and stem == reference_stem


def _status(entry: dict) -> str:
    return "none" if entry.get("edge_px") is None else "ok"


def finding_to_entry(
    finding: AuditFinding,
    modality: AuditModality,
    settings: ScalecardAuditSettings,
) -> dict:
    """One runtime finding as the `_meta.json` entry: the metric's result
    verbatim plus `status` — no thresholds, so no warn_threshold field."""
    if finding.check != CHECK:
        raise ValueError(f"not a scalecard finding: {finding.check!r}")
    entry = dict(finding.data or {})
    entry["metric_version"] = finding.metric_version
    entry["status"] = _status(entry)
    return entry


def is_current_entry(entry: object) -> bool:
    """True if a persisted entry is usable: well-formed and measured with
    the exact metric version this build ships. Anything else counts as
    absent, so the check is re-measured on the next full decode."""
    return (isinstance(entry, dict)
            and entry.get("metric_version") == METRIC_VERSION)


def status_for(
    entry: dict,
    modality: AuditModality,
    settings: ScalecardAuditSettings,
) -> str:
    """"ok" or "none" — this check never warns."""
    return _status(entry)


def _edge_um(entry: dict) -> float | None:
    """The comparable number: tick-edge rise in µm on the card, derived at
    display time from the persisted px value and the card's own pitch."""
    edge_px, px_per_cm = entry.get("edge_px"), entry.get("px_per_cm")
    if not edge_px or not px_per_cm:
        return None
    return edge_px / px_per_cm * 10000


def summary_for_entry(entry: dict | None) -> str:
    """Compact fragment for the always-on status line. In µm, because only
    the physical width compares across rig heights. None = check requested
    but no current entry yet (still measuring)."""
    if entry is None:
        return "Maßstab …"
    um = _edge_um(entry)
    if um is None:
        return "Maßstab –"
    return "Maßstab " + f"{um:.0f} µm"


def presentation_for_entry(
    entry: dict,
    modality: AuditModality,
    settings: ScalecardAuditSettings,
) -> tuple[str, str]:
    """(status, German operator text) for a persisted entry."""
    um = _edge_um(entry)
    if um is None:
        return "none", "– MASSSTAB NICHT MESSBAR"
    return "ok", f"✓ MASSSTAB · {um:.0f} µm"
