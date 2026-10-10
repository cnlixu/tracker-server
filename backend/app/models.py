"""Lightweight domain objects shared by tracker application modules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class TerminalRegistration:
    """Standard JT/T 808-2019 terminal registration fields."""

    terminal_phone: str
    province_id: int
    city_id: int
    manufacturer_id: str
    terminal_model: str
    terminal_id: str
    plate_color: int
    plate_number: str


@dataclass(frozen=True, slots=True)
class TerminalAttributes:
    """Standard 0x0107 terminal attribute response fields."""

    terminal_phone: str
    terminal_type: int
    manufacturer_id: str
    terminal_model: str
    terminal_id: str
    iccid: str
    hardware_version: str
    software_version: str
    gnss_attributes: int
    communication_attributes: int


@dataclass(frozen=True, slots=True)
class TerminalConfiguration:
    """The only remotely configurable tracker parameter: report interval."""

    terminal_phone: str
    revision: int
    report_interval_sec: int
    status: str = "pending"
    applied_revision: int = 0


@dataclass(frozen=True, slots=True)
class TrackerRecord:
    """One decoded JT/T 808 location record."""

    gps_time: datetime
    time_valid: bool | None
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
    record_fingerprint: str
    battery_percent: int | None = None
