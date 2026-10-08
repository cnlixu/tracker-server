"""Lightweight domain objects shared by tracker application modules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class TrackerRecord:
    """One decoded JT/T 808 location record."""

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
    battery_percent: int | None = None
    generation_id: int | None = None
