"""The sharpness capture audit: is this capture in focus?

Wraps the vendored v2 metric (byzanz_camera.sharpness) in Papyri policy:
configurable per-modality warn thresholds, the stable `_meta.json` entry
shape, classification, and the German operator feedback. One module per
check — a future check (height, chart presence, …) gets its own sibling
file and a registry line in `papyri.audits`.
"""
from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import QSettings

from byzanz_camera.capture_audit import AuditFinding, AuditModality, SHARPNESS_AUDIT
from byzanz_camera.settings_migration import (
    PAPYRI_SHARPNESS_ENABLED_KEY,
    PAPYRI_SHARPNESS_IR_THRESHOLD_KEY,
    PAPYRI_SHARPNESS_VIS_THRESHOLD_KEY,
)
from byzanz_camera.sharpness import METRIC_VERSION

CHECK = SHARPNESS_AUDIT

DEFAULT_VIS_THRESHOLD = 2.60
DEFAULT_IR_THRESHOLD = 1.75


@dataclass(frozen=True)
class SharpnessAuditSettings:
    enabled: bool
    vis_warn_from: float
    ir_warn_from: float

    def threshold_for(self, modality: AuditModality) -> float:
        return self.vis_warn_from if modality == "vis" else self.ir_warn_from


def read_settings(settings: QSettings) -> SharpnessAuditSettings:
    """This check's settings slice. The DEFAULT_* fallbacks here are the
    single place defaults live — keys appear in the store once the
    settings dialog first writes them."""
    return SharpnessAuditSettings(
        enabled=settings.value(
            PAPYRI_SHARPNESS_ENABLED_KEY, True, type=bool),
        vis_warn_from=float(settings.value(
            PAPYRI_SHARPNESS_VIS_THRESHOLD_KEY, DEFAULT_VIS_THRESHOLD)),
        ir_warn_from=float(settings.value(
            PAPYRI_SHARPNESS_IR_THRESHOLD_KEY, DEFAULT_IR_THRESHOLD)),
    )


def _status(
    value: float | None,
    modality: AuditModality,
    settings: SharpnessAuditSettings,
) -> str:
    if value is None:
        return "none"
    return "warn" if value >= settings.threshold_for(modality) else "ok"


def _number(value, converter):
    try:
        return converter(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def finding_to_entry(
    finding: AuditFinding,
    modality: AuditModality,
    settings: SharpnessAuditSettings,
) -> dict:
    """Normalize one runtime finding to the stable `_meta.json` shape."""
    if finding.check != CHECK:
        raise ValueError(f"not a sharpness finding: {finding.check!r}")
    data = finding.data or {}
    sharp_px = _number(data.get("sharp_px"), float)
    balance = _number(
        data.get("balance", data.get("orientation_balance")), float)
    excluded = data.get("excluded")
    return {
        "sharp_px": sharp_px,
        "median_px": _number(data.get("median_px"), float),
        "n_edges": _number(data.get("n_edges"), int),
        "balance": balance,
        "excluded": dict(excluded) if isinstance(excluded, dict) else {},
        "metric_version": finding.metric_version,
        "warn_threshold": settings.threshold_for(modality),
        "status": _status(sharp_px, modality, settings),
    }


def is_current_entry(entry: object) -> bool:
    """True if a persisted entry is usable: well-formed and measured with
    the exact metric version this build ships. Anything else counts as
    absent, so the check is re-measured on the next full decode."""
    return (isinstance(entry, dict)
            and entry.get("metric_version") == METRIC_VERSION)


def status_for(
    entry: dict,
    modality: AuditModality,
    settings: SharpnessAuditSettings,
) -> str:
    """Status of a current entry against the CURRENT settings. The
    persisted `status` field is a snapshot from write time; display
    re-classifies so threshold changes take effect retroactively."""
    return _status(_number(entry.get("sharp_px"), float), modality, settings)


def summary_for_entry(entry: dict | None) -> str:
    """Compact fragment for the always-on status line. Deliberately states
    the MEASUREMENT without a verdict word ("scharf" would claim more than
    an advisory metric with known blind spots can promise). None = check
    requested but no current entry yet (still measuring)."""
    if entry is None:
        return "Schärfe …"
    value = _number(entry.get("sharp_px"), float)
    if value is None:
        return "Schärfe –"
    return "Schärfe " + f"{value:.2f}".replace(".", ",") + " px"


def presentation_for_entry(
    entry: dict,
    modality: AuditModality,
    settings: SharpnessAuditSettings,
) -> tuple[str, str]:
    """(status, German operator text) for a persisted entry.

    The orientation balance is deliberately NOT surfaced here, although it
    is measured and persisted with every entry: sharp-content minima from
    the corpus overlap any plausible suspicion threshold (strongly
    directional fibre fragments reach ~0.05 without any motion), and a
    wrong "Verwacklung" attribution steers the operator away from the
    more likely remedy (refocus). Re-add a motion hint only once a
    threshold has been validated against deliberately shaken test
    captures."""
    status = status_for(entry, modality, settings)
    value = _number(entry.get("sharp_px"), float)
    if status == "none":
        return status, "– SCHÄRFE NICHT MESSBAR"

    value_text = f"{value:.2f}".replace(".", ",")
    if status == "warn":
        return status, (f"⚠ MÖGLICHE UNSCHÄRFE · {value_text} px · "
                        "BITTE GENAU PRÜFEN")
    return status, f"✓ SCHARF · {value_text} px"
