"""Async PostgreSQL persistence for tracker reports.

Legacy ASCII V1 reports are stored through :func:`save_report`. Binary V2
frames and ASCII V3 reports are stored through :func:`save_records`, which adds
the per-record identity columns used for idempotent re-delivery.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

from .config import DatabaseSettings, load_database_settings
from .models import TrackerRecord, TrackerReport


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "sql" / "schema.sql"

INSERT_TRACK_POINT_SQL = """
INSERT INTO track_points (
    imei, gps_time, valid, latitude, longitude, altitude, speed, course,
    satellites, hdop, csq, wake_code, raw_data
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
RETURNING id
"""

INSERT_POSITION_RECORD_SQL = """
INSERT INTO track_points (
    imei, gps_time, valid, latitude, longitude, altitude, speed, course,
    satellites, hdop, csq, wake_code, raw_data,
    generation_id, record_seq, batch_id, battery_mv, time_valid
)
VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16,
    $17, $18
)
ON CONFLICT (imei, generation_id, record_seq) DO NOTHING
RETURNING id
"""

UPSERT_DEVICE_LATEST_SQL = """
INSERT INTO devices (
    imei, first_seen, last_seen, last_gps_time, last_valid, last_lat, last_lon,
    last_altitude, last_speed, last_course, last_satellites, last_hdop,
    last_csq, last_wake_code, last_battery_mv
)
VALUES (
    $1, NOW(), NOW(), $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13
)
ON CONFLICT (imei) DO UPDATE SET
    last_seen = NOW(),
    last_gps_time = CASE
        WHEN devices.last_gps_time IS NULL
          OR EXCLUDED.last_gps_time >= devices.last_gps_time
        THEN EXCLUDED.last_gps_time ELSE devices.last_gps_time END,
    last_valid = CASE
        WHEN devices.last_gps_time IS NULL
          OR EXCLUDED.last_gps_time >= devices.last_gps_time
        THEN EXCLUDED.last_valid ELSE devices.last_valid END,
    last_lat = CASE
        WHEN devices.last_gps_time IS NULL
          OR EXCLUDED.last_gps_time >= devices.last_gps_time
        THEN EXCLUDED.last_lat ELSE devices.last_lat END,
    last_lon = CASE
        WHEN devices.last_gps_time IS NULL
          OR EXCLUDED.last_gps_time >= devices.last_gps_time
        THEN EXCLUDED.last_lon ELSE devices.last_lon END,
    last_altitude = CASE
        WHEN devices.last_gps_time IS NULL
          OR EXCLUDED.last_gps_time >= devices.last_gps_time
        THEN EXCLUDED.last_altitude ELSE devices.last_altitude END,
    last_speed = CASE
        WHEN devices.last_gps_time IS NULL
          OR EXCLUDED.last_gps_time >= devices.last_gps_time
        THEN EXCLUDED.last_speed ELSE devices.last_speed END,
    last_course = CASE
        WHEN devices.last_gps_time IS NULL
          OR EXCLUDED.last_gps_time >= devices.last_gps_time
        THEN EXCLUDED.last_course ELSE devices.last_course END,
    last_satellites = CASE
        WHEN devices.last_gps_time IS NULL
          OR EXCLUDED.last_gps_time >= devices.last_gps_time
        THEN EXCLUDED.last_satellites ELSE devices.last_satellites END,
    last_hdop = CASE
        WHEN devices.last_gps_time IS NULL
          OR EXCLUDED.last_gps_time >= devices.last_gps_time
        THEN EXCLUDED.last_hdop ELSE devices.last_hdop END,
    last_csq = CASE
        WHEN devices.last_gps_time IS NULL
          OR EXCLUDED.last_gps_time >= devices.last_gps_time
        THEN EXCLUDED.last_csq ELSE devices.last_csq END,
    last_wake_code = CASE
        WHEN devices.last_gps_time IS NULL
          OR EXCLUDED.last_gps_time >= devices.last_gps_time
        THEN EXCLUDED.last_wake_code ELSE devices.last_wake_code END,
    last_battery_mv = CASE
        WHEN devices.last_gps_time IS NULL
          OR EXCLUDED.last_gps_time >= devices.last_gps_time
        THEN EXCLUDED.last_battery_mv ELSE devices.last_battery_mv END
"""

GET_DEVICES_SQL = """
SELECT
    imei, name, first_seen, last_seen, last_gps_time, last_valid, last_lat,
    last_lon, last_altitude, last_speed, last_course, last_satellites,
    last_hdop, last_csq, last_wake_code, last_battery_mv
FROM devices
ORDER BY last_seen DESC, imei ASC
"""

GET_DEVICE_LATEST_SQL = """
SELECT
    imei, name, first_seen, last_seen, last_gps_time, last_valid, last_lat,
    last_lon, last_altitude, last_speed, last_course, last_satellites,
    last_hdop, last_csq, last_wake_code, last_battery_mv
FROM devices
WHERE imei = $1
"""

UPDATE_DEVICE_NAME_SQL = """
UPDATE devices
SET name = $2
WHERE imei = $1
RETURNING
    imei, name, first_seen, last_seen, last_gps_time, last_valid, last_lat,
    last_lon, last_altitude, last_speed, last_course, last_satellites,
    last_hdop, last_csq, last_wake_code, last_battery_mv
"""

GET_TRACK_POINTS_SQL = """
SELECT
    id, imei, gps_time, server_time, valid, latitude, longitude, altitude,
    speed, course, satellites, hdop, csq, wake_code, raw_data,
    battery_mv, time_valid, record_seq
FROM track_points
WHERE imei = $1 AND gps_time >= $2 AND gps_time < $3
ORDER BY gps_time ASC, id ASC
LIMIT $4
"""


class DatabaseError(RuntimeError):
    """Base class for database-layer failures with application context."""


class DatabaseConnectionError(DatabaseError):
    """Raised when the PostgreSQL pool cannot be initialized."""


class DatabaseValueError(DatabaseError):
    """Raised when data passed to the database layer violates its contract."""


@dataclass(frozen=True, slots=True)
class DeviceSnapshot:
    """Latest persisted state for one tracker device."""

    imei: str
    name: str | None
    first_seen: datetime
    last_seen: datetime
    last_gps_time: datetime | None
    last_valid: bool | None
    last_lat: float | None
    last_lon: float | None
    last_altitude: float | None
    last_speed: float | None
    last_course: float | None
    last_satellites: int | None
    last_hdop: float | None
    last_csq: int | None
    last_wake_code: int | None
    last_battery_mv: int | None = None


@dataclass(frozen=True, slots=True)
class TrackPoint:
    """One persisted historical tracker report."""

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
    raw_data: str
    battery_mv: int | None = None
    time_valid: bool | None = None
    record_seq: int | None = None


async def init_pool(settings: DatabaseSettings | None = None) -> asyncpg.Pool:
    """Initialize an asyncpg pool and report connection failures clearly."""
    database_settings = settings or load_database_settings()
    try:
        pool = await asyncpg.create_pool(
            host=database_settings.host,
            port=database_settings.port,
            database=database_settings.database,
            user=database_settings.user,
            password=database_settings.password,
            min_size=database_settings.min_pool_size,
            max_size=database_settings.max_pool_size,
            timeout=database_settings.connect_timeout,
            command_timeout=database_settings.command_timeout,
        )
    except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
        target = (
            f"{database_settings.host}:{database_settings.port}/"
            f"{database_settings.database}"
        )
        raise DatabaseConnectionError(
            f"unable to connect to PostgreSQL at {target}"
        ) from exc

    if pool is None:
        raise DatabaseConnectionError("asyncpg did not return a connection pool")
    return pool


# Compatibility name retained for earlier callers.
create_pool = init_pool


async def close_pool(pool: asyncpg.Pool) -> None:
    """Close all connections owned by a pool."""
    await pool.close()


async def init_schema(pool: asyncpg.Pool) -> None:
    """Apply the version-controlled initial schema."""
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    async with pool.acquire() as connection:
        await connection.execute(schema_sql)


# Compatibility name retained for earlier callers.
initialize_schema = init_schema


def report_to_track_point_params(report: TrackerReport) -> tuple[object, ...]:
    """Map a report to the positional parameters of the history insert."""
    _validate_report_identity(report)
    return (
        report.imei,
        _as_utc(report.gps_time),
        report.valid,
        report.latitude,
        report.longitude,
        report.altitude,
        report.speed,
        report.course,
        report.satellites,
        report.hdop,
        report.csq,
        report.wake_code,
        report.raw_data,
    )


def report_to_device_params(report: TrackerReport) -> tuple[object, ...]:
    """Map a report to the positional parameters of the device upsert."""
    history_params = report_to_track_point_params(report)
    # Legacy ASCII V1 reports carry no battery voltage, so it stays NULL.
    return history_params[:12] + (report.battery_mv,)


def record_to_track_point_params(
    imei: str,
    generation_id: int,
    batch_id: int,
    record: TrackerRecord,
) -> tuple[object, ...]:
    """Map a position record to the positional parameters of its insert."""
    _validate_imei(imei)
    if not record.raw_data:
        raise DatabaseValueError("raw_data must not be empty")
    return (
        imei,
        _as_utc(record.gps_time),
        record.valid,
        record.latitude,
        record.longitude,
        record.altitude,
        record.speed,
        record.course,
        record.satellites,
        record.hdop,
        record.csq,
        record.wake_code,
        record.raw_data,
        generation_id,
        record.sequence,
        batch_id,
        record.battery_mv,
        record.time_valid,
    )


def record_to_device_params(
    imei: str,
    record: TrackerRecord,
) -> tuple[object, ...]:
    """Map a position record to the positional parameters of the device upsert."""
    _validate_imei(imei)
    return (
        imei,
        _as_utc(record.gps_time),
        record.valid,
        record.latitude,
        record.longitude,
        record.altitude,
        record.speed,
        record.course,
        record.satellites,
        record.hdop,
        record.csq,
        record.wake_code,
        record.battery_mv,
    )


async def insert_track_point(
    pool: asyncpg.Pool,
    report: TrackerReport,
) -> int:
    """Insert one history row outside the combined report transaction."""
    async with pool.acquire() as connection:
        return await _insert_track_point(connection, report)


async def upsert_device_latest(
    pool: asyncpg.Pool,
    report: TrackerReport,
) -> None:
    """Insert or update one device snapshot outside the combined transaction."""
    async with pool.acquire() as connection:
        await _upsert_device_latest(connection, report)


async def save_report(pool: asyncpg.Pool, report: TrackerReport) -> int:
    """Atomically store history and update the corresponding device snapshot."""
    async with pool.acquire() as connection:
        async with connection.transaction():
            track_point_id = await _insert_track_point(connection, report)
            await _upsert_device_latest(connection, report)
    return track_point_id


async def save_records(
    pool: asyncpg.Pool,
    *,
    imei: str,
    generation_id: int,
    batch_id: int,
    records: tuple[TrackerRecord, ...] | list[TrackerRecord],
) -> int:
    """Store records and the device snapshot atomically, idempotently.

    Each record is keyed by ``(imei, generation_id, record_seq)``, so a batch
    that the device re-sends after a lost acknowledgement only stores what is
    still missing. The returned count is the number of newly inserted records;
    an entirely duplicated batch returns ``0``.
    """
    if not records:
        raise DatabaseValueError("records must not be empty")
    _validate_imei(imei)
    ordered = sorted(records, key=lambda record: record.sequence)

    inserted = 0
    async with pool.acquire() as connection:
        async with connection.transaction():
            for record in ordered:
                if await _insert_position_record(
                    connection,
                    imei,
                    generation_id,
                    batch_id,
                    record,
                ):
                    inserted += 1
            await _upsert_device_from_record(connection, imei, ordered[-1])
    return inserted


async def get_devices(pool: asyncpg.Pool) -> list[DeviceSnapshot]:
    """Return all device snapshots, most recently seen first."""
    async with pool.acquire() as connection:
        records = await connection.fetch(GET_DEVICES_SQL)
    return [DeviceSnapshot(**dict(record)) for record in records]


async def get_device_latest(
    pool: asyncpg.Pool,
    imei: str,
) -> DeviceSnapshot | None:
    """Return one device snapshot, or ``None`` when the IMEI is unknown."""
    _validate_imei(imei)
    async with pool.acquire() as connection:
        record = await connection.fetchrow(GET_DEVICE_LATEST_SQL, imei)
    return None if record is None else DeviceSnapshot(**dict(record))


async def update_device_name(
    pool: asyncpg.Pool,
    imei: str,
    name: str | None,
) -> DeviceSnapshot | None:
    """Update a device alias with parameterized SQL and return its snapshot."""
    _validate_imei(imei)
    normalized_name = name.strip() if name is not None else None
    if normalized_name == "":
        normalized_name = None
    if normalized_name is not None and len(normalized_name) > 64:
        raise DatabaseValueError("device name must not exceed 64 characters")

    async with pool.acquire() as connection:
        record = await connection.fetchrow(
            UPDATE_DEVICE_NAME_SQL,
            imei,
            normalized_name,
        )
    return None if record is None else DeviceSnapshot(**dict(record))


async def get_track_points(
    pool: asyncpg.Pool,
    imei: str,
    start_time: datetime,
    end_time: datetime,
    *,
    limit: int = 10_000,
) -> list[TrackPoint]:
    """Query one device's points in a half-open ``[start, end)`` UTC range."""
    _validate_imei(imei)
    start_utc = _as_utc(start_time)
    end_utc = _as_utc(end_time)
    if start_utc > end_utc:
        raise DatabaseValueError("start_time must not be later than end_time")
    if limit < 1 or limit > 10_000:
        raise DatabaseValueError("limit must be between 1 and 10000")

    async with pool.acquire() as connection:
        records = await connection.fetch(
            GET_TRACK_POINTS_SQL,
            imei,
            start_utc,
            end_utc,
            limit,
        )
    return [TrackPoint(**dict(record)) for record in records]


# Compatibility name retained for earlier callers.
query_track_points = get_track_points


async def _insert_track_point(
    connection: asyncpg.Connection,
    report: TrackerReport,
) -> int:
    track_point_id = await connection.fetchval(
        INSERT_TRACK_POINT_SQL,
        *report_to_track_point_params(report),
    )
    if track_point_id is None:
        raise DatabaseError("track point insert did not return an ID")
    return int(track_point_id)


async def _upsert_device_latest(
    connection: asyncpg.Connection,
    report: TrackerReport,
) -> None:
    await connection.execute(
        UPSERT_DEVICE_LATEST_SQL,
        *report_to_device_params(report),
    )


async def _insert_position_record(
    connection: asyncpg.Connection,
    imei: str,
    generation_id: int,
    batch_id: int,
    record: TrackerRecord,
) -> bool:
    """Insert one record; return ``False`` when it was already stored."""
    row_id = await connection.fetchval(
        INSERT_POSITION_RECORD_SQL,
        *record_to_track_point_params(imei, generation_id, batch_id, record),
    )
    # ON CONFLICT DO NOTHING returns no row for an already stored record.
    return row_id is not None


async def _upsert_device_from_record(
    connection: asyncpg.Connection,
    imei: str,
    record: TrackerRecord,
) -> None:
    await connection.execute(
        UPSERT_DEVICE_LATEST_SQL,
        *record_to_device_params(imei, record),
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DatabaseValueError("database datetimes must be timezone-aware")
    return value.astimezone(timezone.utc)


def _validate_report_identity(report: TrackerReport) -> None:
    _validate_imei(report.imei)
    if not report.raw_data:
        raise DatabaseValueError("raw_data must not be empty")


def _validate_imei(imei: str) -> None:
    if not imei or len(imei) > 20 or not imei.isascii() or not imei.isdecimal():
        raise DatabaseValueError("IMEI must contain 1 to 20 ASCII decimal digits")
