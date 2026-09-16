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
