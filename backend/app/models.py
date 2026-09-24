"""Lightweight domain objects shared by tracker application modules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class TrackerReport:
    """A validated PTRK ASCII report in application-friendly units.

    GNSS measurements may be ``None`` when ``valid`` is false. Coordinates are
    WGS84 decimal degrees and ``gps_time`` is timezone-aware UTC.

    ``generation_id``, ``record_sequence``, ``batch_id``, ``time_valid`` and
    ``battery_mv`` are only present in ASCII V3 reports; they stay ``None`` for
    legacy V1 reports, which carry no per-record identity.
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
    generation_id: int | None = None
    record_sequence: int | None = None
    batch_id: int | None = None
    time_valid: bool | None = None
    battery_mv: int | None = None

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


@dataclass(frozen=True, slots=True)
class TrackerRecord:
    """One fixed-size 30-byte position record.

    Binary V2 frames and ASCII V3 reports carry the same measurements, so both
    transports decode into this single record type. ``raw_data`` keeps the
    original wire bytes as uppercase hexadecimal for auditing.
    """

    sequence: int
    gps_time: datetime
    time_valid: bool
    valid: bool
    latitude: float | None
    longitude: float | None
    altitude: float | None
    speed: float | None
    course: float | None
    satellites: int | None
    hdop: float | None
    csq: int
    battery_mv: int | None
    wake_code: int
    raw_data: str
