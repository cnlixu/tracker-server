"""FastAPI routes for device state and historical tracks."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import logging
from typing import Annotated, Awaitable, TypeVar
from zoneinfo import ZoneInfo

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status

from .auth import AdminDependency
from .config import OnlineStatusSettings
from .database import (
    DatabaseError,
    DeviceSnapshot,
    TrackPoint,
    get_device_latest,
    get_devices,
    get_track_points,
    update_device_name,
)
from .schemas import (
    DeviceNameUpdate,
    DeviceResponse,
    HealthResponse,
    OnlineStatus,
    TrackPointResponse,
)


LOGGER = logging.getLogger(__name__)
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")

router = APIRouter(prefix="/api")

_T = TypeVar("_T")
_DATABASE_EXCEPTIONS = (DatabaseError, asyncpg.PostgresError, OSError)


def get_db_pool(request: Request) -> asyncpg.Pool:
    """Return the pool initialized by the application lifespan."""
    return request.app.state.db_pool


def get_online_status_settings(request: Request) -> OnlineStatusSettings:
    """Return configured online-status thresholds."""
    return request.app.state.online_status_settings


def get_utc_now() -> datetime:
    """Return current UTC time; exposed as a dependency for deterministic tests."""
    return datetime.now(timezone.utc)


PoolDependency = Annotated[asyncpg.Pool, Depends(get_db_pool)]
StatusSettingsDependency = Annotated[
    OnlineStatusSettings,
    Depends(get_online_status_settings),
]
NowDependency = Annotated[datetime, Depends(get_utc_now)]
ImeiPath = Annotated[str, Path(pattern=r"^[0-9]{1,20}$")]


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/devices", response_model=list[DeviceResponse])
async def list_devices(
    _admin: AdminDependency,
    pool: PoolDependency,
    thresholds: StatusSettingsDependency,
    now: NowDependency,
) -> list[DeviceResponse]:
    devices = await _run_database_call(get_devices(pool), "list devices")
    return [device_to_response(device, now, thresholds) for device in devices]


@router.get("/devices/{imei}/latest", response_model=DeviceResponse)
async def device_latest(
    imei: ImeiPath,
    _admin: AdminDependency,
    pool: PoolDependency,
    thresholds: StatusSettingsDependency,
    now: NowDependency,
) -> DeviceResponse:
    device = await _run_database_call(
        get_device_latest(pool, imei),
        "get latest device state",
    )
    if device is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="device not found",
        )
    return device_to_response(device, now, thresholds)


@router.get("/devices/{imei}/track", response_model=list[TrackPointResponse])
async def device_track(
    imei: ImeiPath,
    _admin: AdminDependency,
    pool: PoolDependency,
    local_date: Annotated[date | None, Query(alias="date")] = None,
    start_local: Annotated[str | None, Query(alias="start")] = None,
    end_local: Annotated[str | None, Query(alias="end")] = None,
) -> list[TrackPointResponse]:
    start_utc, end_utc = resolve_track_time_range(
        local_date,
        start_local,
        end_local,
    )
    points = await _run_database_call(
        get_track_points(pool, imei, start_utc, end_utc),
        "get device track",
    )
    points.sort(key=lambda point: (point.gps_time, point.id))
    return [track_point_to_response(point) for point in points]


@router.patch("/devices/{imei}", response_model=DeviceResponse)
async def change_device_name(
    imei: ImeiPath,
    update: DeviceNameUpdate,
    _admin: AdminDependency,
    pool: PoolDependency,
    thresholds: StatusSettingsDependency,
    now: NowDependency,
) -> DeviceResponse:
    device = await _run_database_call(
        update_device_name(pool, imei, update.name),
        "update device name",
    )
    if device is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="device not found",
        )
    return device_to_response(device, now, thresholds)


def shanghai_day_to_utc_range(local_date: date) -> tuple[datetime, datetime]:
    """Convert one Asia/Shanghai calendar day to a half-open UTC range."""
    local_start = datetime.combine(local_date, time.min, tzinfo=SHANGHAI_TIMEZONE)
    next_date = local_date + timedelta(days=1)
    local_end = datetime.combine(next_date, time.min, tzinfo=SHANGHAI_TIMEZONE)
    return (
        local_start.astimezone(timezone.utc),
        local_end.astimezone(timezone.utc),
    )


def shanghai_minutes_to_utc_range(
    start_local: str,
    end_local: str,
) -> tuple[datetime, datetime]:
    """Convert minute-precision Shanghai local values to a UTC half-open range."""
    try:
        start = datetime.strptime(start_local, "%Y-%m-%dT%H:%M")
        end = datetime.strptime(end_local, "%Y-%m-%dT%H:%M")
    except ValueError as exc:
        raise ValueError("start and end must use YYYY-MM-DDTHH:MM") from exc
    if start >= end:
        raise ValueError("start must be earlier than end")
    return (
        start.replace(tzinfo=SHANGHAI_TIMEZONE).astimezone(timezone.utc),
        end.replace(tzinfo=SHANGHAI_TIMEZONE).astimezone(timezone.utc),
    )


def resolve_track_time_range(
    local_date: date | None,
    start_local: str | None,
    end_local: str | None,
) -> tuple[datetime, datetime]:
    if local_date is not None:
        if start_local is not None or end_local is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="use either date or start/end, not both",
            )
        return shanghai_day_to_utc_range(local_date)
    if start_local is None or end_local is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="both start and end are required",
        )
    try:
        return shanghai_minutes_to_utc_range(start_local, end_local)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


def calculate_online_status(
    last_seen: datetime,
    now: datetime,
    thresholds: OnlineStatusSettings,
) -> OnlineStatus:
    """Classify a device solely from the age of its latest server receipt."""
    last_seen_utc = _require_aware_utc(last_seen, "last_seen")
    now_utc = _require_aware_utc(now, "now")
    age = max(now_utc - last_seen_utc, timedelta(0))
    if age <= timedelta(minutes=thresholds.online_minutes):
        return "online"
    if age <= timedelta(minutes=thresholds.warning_minutes):
        return "warning"
    return "offline"


def device_to_response(
    device: DeviceSnapshot,
    now: datetime,
    thresholds: OnlineStatusSettings,
) -> DeviceResponse:
    return DeviceResponse(
        imei=device.imei,
        name=device.name,
        last_seen=device.last_seen,
        online_status=calculate_online_status(device.last_seen, now, thresholds),
        last_gps_time=device.last_gps_time,
        valid=device.last_valid,
        latitude=device.last_lat,
        longitude=device.last_lon,
        altitude=device.last_altitude,
        speed=device.last_speed,
        course=device.last_course,
        satellites=device.last_satellites,
        hdop=device.last_hdop,
        csq=device.last_csq,
        wake_code=device.last_wake_code,
    )


def track_point_to_response(point: TrackPoint) -> TrackPointResponse:
    return TrackPointResponse(
        id=point.id,
        imei=point.imei,
        gps_time=point.gps_time,
        server_time=point.server_time,
        valid=point.valid,
        latitude=point.latitude,
        longitude=point.longitude,
        altitude=point.altitude,
        speed=point.speed,
        course=point.course,
        satellites=point.satellites,
        hdop=point.hdop,
        csq=point.csq,
        wake_code=point.wake_code,
    )


async def _run_database_call(
    operation: Awaitable[_T],
    operation_name: str,
) -> _T:
    try:
        return await operation
    except _DATABASE_EXCEPTIONS as exc:
        LOGGER.exception("database operation failed operation=%s", operation_name)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="database operation failed",
        ) from exc


def _require_aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)
