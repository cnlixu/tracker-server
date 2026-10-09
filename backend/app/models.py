"""Lightweight domain objects shared by tracker application modules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class TerminalRegistration:
    """Validated JT/T 808 terminal registration fields."""

    terminal_phone: str
    province_id: int
    city_id: int
    manufacturer_id: str
    profile_magic: str
    profile_version: int
    terminal_type: int
    capabilities: int
    report_interval_sec: int
    service_point_id: str
    asset_id: str
    terminal_model: str
    terminal_sn: str
    imei: str
    iccid: str
    hardware_version: str
    software_version: str
    organization_code: str
    production_batch: str
    profile_revision: int


@dataclass(frozen=True, slots=True)
class TerminalConfiguration:
    """One complete mutable EPTR parameter set distributed with 0x8103."""

    terminal_phone: str
    revision: int
    province_id: int
    city_id: int
    terminal_type: int
    report_interval_sec: int
    service_point_id: str
    asset_id: str
    organization_code: str
    status: str = "pending"
    applied_revision: int = 0


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
