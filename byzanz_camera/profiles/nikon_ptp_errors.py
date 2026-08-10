"""Nikon vendor PTP response codes — one vendor-wide table from Nikon's
PTP extension (PTP_RC_NIKON_* in libgphoto2's ptp.h)."""
from enum import StrEnum


class NikonPTPError(StrEnum):
    OutOfFocus = "0xa002"
