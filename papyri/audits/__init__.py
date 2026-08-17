"""Per-capture audits — Papyri's local post-capture checks.

"Audit" here means the checks run right after a capture (sharpness today),
not the server-side corpus audit in the viewer — that one stays
authoritative and runs independently.

Structure: this module is the infrastructure (registry, settings snapshot,
persistence); each CHECK lives in its own sibling module (`sharpness.py`,
future: height, chart presence, …). A check module provides `CHECK`,
`read_settings`, `finding_to_entry`, `is_current_entry`, and its
display helpers. Its slice in `CaptureAuditSettings` is named after the
check, which is what lets `persist_fresh_capture_audit` dispatch with
`getattr(settings, finding.check)`.

Display state always comes from the persisted `_meta.json` entries (see
`object_layout.read_capture_audits`); the check modules interpret those
entries against the CURRENT settings, so threshold changes re-classify
retroactively without re-measuring.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QSettings

from byzanz_camera.capture_audit import AuditFinding, CaptureAuditContext
from papyri.audits import sharpness
from papyri.capture_vocab import SPECTRUM_INFIX
from papyri.object_layout import (
    BUCKETS, effective_capture_stem, meta_path_for, read_capture_audits,
    store_capture_audit,
)

CHECKS = {
    sharpness.CHECK: sharpness,
}


@dataclass(frozen=True)
class CaptureAuditSettings:
    """Immutable snapshot consumed by one UI reconciliation pass.
    Field names equal check names (see module docstring)."""

    sharpness: sharpness.SharpnessAuditSettings

    @property
    def enabled_checks(self) -> frozenset[str]:
        return frozenset(
            check for check in CHECKS
            if getattr(self, check).enabled
        )


def read_audit_settings(settings: QSettings) -> CaptureAuditSettings:
    """Read and type-normalize the complete capture-audit configuration."""
    return CaptureAuditSettings(
        sharpness=sharpness.read_settings(settings),
    )


def entry_is_current(check: str, entry: object) -> bool:
    """True if a persisted entry for `check` is usable in this build
    (known check, exact metric version). Anything else counts as absent."""
    module = CHECKS.get(check)
    return module is not None and module.is_current_entry(entry)


def warned_checks(
    entries: dict, modality: str, settings: CaptureAuditSettings,
) -> frozenset[str]:
    """Checks whose persisted entry for one capture classifies as `warn`
    under the CURRENT settings. The shared rule behind every warning
    rendering (pills, badges, bucket cards, sidebar rollup)."""
    return frozenset(
        check for check, module in CHECKS.items()
        if (entry := entries.get(check)) is not None
        and module.is_current_entry(entry)
        and module.status_for(
            entry, modality, getattr(settings, check)) == "warn"
    )


def bucket_effective_warnings(
    object_dir: str, settings: CaptureAuditSettings,
) -> set[tuple[str, str]]:
    """Buckets whose EFFECTIVE capture (pinned chosen, else newest — the
    take that actually represents the bucket) has at least one enabled
    check warning. Old warned takes that were superseded or re-marked do
    not count: retaking, re-choosing, or deleting resolves the warning."""
    enabled = settings.enabled_checks
    if not enabled:
        return set()
    audits_tree = read_capture_audits(meta_path_for(object_dir))
    warned: set[tuple[str, str]] = set()
    for side, spectrum in BUCKETS:
        stem = effective_capture_stem(object_dir, side, spectrum)
        if stem is None:
            continue
        checks = warned_checks(
            audits_tree.get(stem, {}), SPECTRUM_INFIX[spectrum], settings)
        if checks & enabled:
            warned.add((side, spectrum))
    return warned


def persist_fresh_capture_audit(
    path: str,
    finding: AuditFinding,
    context: CaptureAuditContext,
    settings: CaptureAuditSettings,
) -> bool:
    """Persist a fresh finding if its capture and target are still live.

    The async transport provides an exact input path and a frozen opaque
    target id. Returns False when navigation-time mutations moved/deleted
    either exact identity — never recreate or guess a replacement target.
    No semantics are inferred from names: modality comes from the frozen
    context, and the filename stem is only the established per-capture
    storage key. The `_meta.json` shape/write stays in `object_layout`.
    """
    module = CHECKS.get(finding.check)
    if module is None:
        raise ValueError(f"unsupported capture audit: {finding.check!r}")
    if not Path(path).is_file() or not Path(context.target_id).is_file():
        return False
    entry = module.finding_to_entry(
        finding, context.request.modality, getattr(settings, finding.check))
    store_capture_audit(
        context.target_id, Path(path).stem, finding.check, entry)
    return True
