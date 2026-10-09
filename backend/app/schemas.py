"""Explicit response schemas for the Tracker Web API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator


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
    """Mutable EPTR fields; omitted values retain their current setting."""

    province_id: int | None = Field(default=None, ge=0, le=65535)
    city_id: int | None = Field(default=None, ge=0, le=65535)
    terminal_type: int | None = Field(default=None, ge=1, le=255)
    report_interval_sec: int | None = Field(default=None, ge=5, le=86400)
    service_point_id: str | None = Field(default=None, max_length=32)
    asset_id: str | None = Field(default=None, max_length=64)
    organization_code: str | None = Field(default=None, max_length=32)

    @field_validator("service_point_id", "asset_id", "organization_code")
    @classmethod
    def validate_wire_length(
        cls, value: str | None, info: ValidationInfo
    ) -> str | None:
        if value is None:
            return None
        limits = {"service_point_id": 32, "asset_id": 64, "organization_code": 32}
        if len(value.encode("utf-8")) > limits[info.field_name]:
            raise ValueError(f"{info.field_name} exceeds its UTF-8 wire length")
        return value


class TerminalConfigurationResponse(BaseModel):
    terminal_phone: str
    revision: int
    applied_revision: int
    status: Literal["pending", "sent", "applied", "failed"]
    province_id: int
    city_id: int
    terminal_type: int
    report_interval_sec: int
    service_point_id: str
    asset_id: str
    organization_code: str


class TerminalRegistrationResponse(BaseModel):
    """Read-only EPTR registration profile; authentication secrets are excluded."""

    terminal_phone: str
    imei: str
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
    iccid: str
    hardware_version: str
    software_version: str
    organization_code: str
    production_batch: str
    profile_revision: int
    registered_at: datetime
    last_registered_at: datetime
    last_authenticated_at: datetime | None


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
