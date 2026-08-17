"""Small immutable messages for asynchronous post-capture audits.

This module defines transport only. It deliberately knows nothing about
Papyri objects, metadata files, thresholds, badges, or selection state.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


AuditModality = Literal["vis", "ir"]
SHARPNESS_AUDIT = "sharpness"


@dataclass(frozen=True)
class AuditRequest:
    """Checks a FULL image worker should run after publishing the image."""

    modality: AuditModality
    checks: frozenset[str]

    def __post_init__(self) -> None:
        if self.modality not in ("vis", "ir"):
            raise ValueError(f"unknown audit modality: {self.modality!r}")

    def restricted_to(self, checks: set[str] | frozenset[str]) -> "AuditRequest | None":
        """Copy containing only ``checks``; None when no work remains."""
        selected = self.checks & frozenset(checks)
        return AuditRequest(self.modality, selected) if selected else None


@dataclass(frozen=True)
class CaptureAuditContext:
    """Origin frozen when an async audit job is queued.

    ``target_id`` is opaque transport data. A host may use a metadata path,
    UUID, or another stable identifier; shared camera components never parse
    or dereference it.
    """

    target_id: str
    request: AuditRequest


@dataclass(frozen=True)
class AuditFinding:
    """One versioned check result. ``data=None`` is a valid result."""

    check: str
    metric_version: str
    data: dict | None
