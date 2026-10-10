"""Explicit response schemas for the Tracker Web API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


OnlineStatus = Literal["online", "warning", "offline"]


class HealthResponse(BaseModel):
    """Service liveness response."""

    status: Literal["ok"]


class LoginRequest(BaseModel):
    """Administrator credentials accepted only by the login endpoint."""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class AuthSessionResponse(BaseModel):
    """Public information about the current authenticated session."""

    authenticated: Literal[True]
    username: str


class DeviceNameUpdate(BaseModel):
    """Editable device metadata; a null or blank name clears the alias."""

    name: str | None = Field(default=None, max_length=64)


class TerminalConfigurationUpdate(BaseModel):
    """The only supported 0x8103 setting: parameter 0x0029."""

    report_interval_sec: int | None = Field(default=None, ge=5, le=86400)


class TerminalConfigurationResponse(BaseModel):
    terminal_phone: str
    revision: int
    applied_revision: int
    status: Literal["pending", "sent", "applied", "failed"]
    report_interval_sec: int


class TerminalRegistrationResponse(BaseModel):
    """Standard registration and 0x0107 attributes without credentials."""

    terminal_phone: str
    imei: str
    province_id: int
    city_id: int
    manufacturer_id: str
    terminal_model: str
    terminal_id: str
    plate_color: int
    plate_number: str
    terminal_type: int
    attribute_manufacturer_id: str
    attribute_terminal_model: str
    attribute_terminal_id: str
    iccid: str
    hardware_version: str
    software_version: str
    gnss_attributes: int
    communication_attributes: int
    registered_at: datetime
    last_registered_at: datetime
    last_authenticated_at: datetime | None
    attributes_updated_at: datetime | None


class DeviceResponse(BaseModel):
    """Latest public state for one tracker device."""

    imei: str
    name: str | None
    last_seen: datetime
    online_status: OnlineStatus
    last_gps_time: datetime | None
    valid: bool | None
    latitude: float | None
    longitude: float | None
    altitude: float | None
    speed: float | None
    course: float | None
    satellites: int | None
    hdop: float | None
    csq: int | None
    wake_code: int | None
    battery_mv: int | None
    battery_percent: int | None


class TrackPointResponse(BaseModel):
    """One historical point returned to map clients."""

    id: int
    imei: str
    gps_time: datetime
    server_time: datetime
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
    battery_mv: int | None = None
    battery_percent: int | None = None
    time_valid: bool | None = None
    record_seq: int | None = None
