"""Lightweight domain objects shared by tracker application modules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class TrackerReport:
    """A validated PTRK V1 report in application-friendly units.

    GNSS measurements may be ``None`` when ``valid`` is false. Coordinates are
    WGS84 decimal degrees and ``gps_time`` is timezone-aware UTC.
    """

    protocol_version: int
    imei: str
    gps_time: datetime
    valid: bool
    latitude: float | None
    longitude: float | None
    altitude: float | None
    speed: float | None
    course: float | None
    satellites: int | None
    hdop: float | None
    csq: int
    wake_code: int
    raw_data: str

    @property
    def utc(self) -> datetime:
        """Compatibility name for code written before ``gps_time`` was adopted."""
        return self.gps_time

    @property
    def is_valid(self) -> bool:
        """Compatibility name for code written before ``valid`` was adopted."""
        return self.valid

    @property
    def fix_status(self) -> str:
        """Return the original wire-level A/V status."""
        return "A" if self.valid else "V"
