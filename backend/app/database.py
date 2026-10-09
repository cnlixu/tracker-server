"""Async PostgreSQL persistence for JT/T 808 tracker locations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path

import asyncpg

from .config import DatabaseSettings, load_database_settings
from .models import TerminalConfiguration, TerminalRegistration, TrackerRecord


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "sql" / "schema.sql"

INSERT_POSITION_RECORD_SQL = """
INSERT INTO track_points (
    imei, gps_time, valid, latitude, longitude, altitude, speed, course,
    satellites, hdop, csq, wake_code, raw_data,
    generation_id, record_seq, batch_id, battery_mv, battery_percent, time_valid
)
VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16,
    $17, $18, $19
)
ON CONFLICT (imei, generation_id, record_seq) DO NOTHING
RETURNING id
"""

# One set-based statement keeps a 512-record upload inside the device's ACK
# latency budget and, together with the surrounding transaction, makes the ACK
# mean that every accepted row and the latest-device snapshot are durable.
INSERT_POSITION_RECORDS_SQL = """
WITH inserted AS (
    INSERT INTO track_points (
        imei, gps_time, valid, latitude, longitude, altitude, speed, course,
        satellites, hdop, csq, wake_code, raw_data,
        generation_id, record_seq, batch_id, battery_mv, battery_percent, time_valid
    )
    SELECT *
    FROM UNNEST(
        $1::TEXT[], $2::TIMESTAMPTZ[], $3::BOOLEAN[], $4::DOUBLE PRECISION[],
        $5::DOUBLE PRECISION[], $6::REAL[], $7::REAL[], $8::REAL[],
        $9::SMALLINT[], $10::REAL[], $11::SMALLINT[], $12::SMALLINT[],
        $13::TEXT[], $14::BIGINT[], $15::BIGINT[], $16::BIGINT[],
        $17::INTEGER[], $18::SMALLINT[], $19::BOOLEAN[]
    )
    ON CONFLICT (imei, generation_id, record_seq) DO NOTHING
    RETURNING 1
)
SELECT COUNT(*) FROM inserted
"""

UPSERT_DEVICE_LATEST_SQL = """
INSERT INTO devices (
    imei, first_seen, last_seen, last_gps_time, last_valid, last_lat, last_lon,
    last_altitude, last_speed, last_course, last_satellites, last_hdop,
    last_csq, last_wake_code, last_battery_mv, last_battery_percent
)
VALUES (
    $1, NOW(), NOW(), $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14
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
        THEN EXCLUDED.last_battery_mv ELSE devices.last_battery_mv END,
    last_battery_percent = CASE
        WHEN devices.last_gps_time IS NULL
          OR EXCLUDED.last_gps_time >= devices.last_gps_time
        THEN EXCLUDED.last_battery_percent ELSE devices.last_battery_percent END
"""

GET_DEVICES_SQL = """
SELECT
    imei, name, first_seen, last_seen, last_gps_time, last_valid, last_lat,
    last_lon, last_altitude, last_speed, last_course, last_satellites,
    last_hdop, last_csq, last_wake_code, last_battery_mv, last_battery_percent
FROM devices
ORDER BY last_seen DESC, imei ASC
"""

GET_DEVICE_LATEST_SQL = """
SELECT
    imei, name, first_seen, last_seen, last_gps_time, last_valid, last_lat,
    last_lon, last_altitude, last_speed, last_course, last_satellites,
    last_hdop, last_csq, last_wake_code, last_battery_mv, last_battery_percent
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
    last_hdop, last_csq, last_wake_code, last_battery_mv, last_battery_percent
"""

GET_TRACK_POINTS_SQL = """
SELECT
    id, imei, gps_time, server_time, valid, latitude, longitude, altitude,
    speed, course, satellites, hdop, csq, wake_code, raw_data,
    battery_mv, battery_percent, time_valid, record_seq
FROM track_points
WHERE imei = $1 AND gps_time >= $2 AND gps_time < $3
ORDER BY gps_time ASC, id ASC
LIMIT $4
"""

UPSERT_TERMINAL_REGISTRATION_SQL = """
INSERT INTO jt808_terminals (
    terminal_phone, imei, auth_code_hash, province_id, city_id,
    manufacturer_id, terminal_model, terminal_id, plate_color, plate_number,
    profile_magic, profile_version, terminal_type, capabilities,
    report_interval_sec, service_point_id, asset_id, terminal_sn, iccid,
    hardware_version, software_version, organization_code, production_batch,
    profile_revision,
    registered_at, last_registered_at
)
VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, 0, '', $9, $10, $11, $12,
    $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, NOW(), NOW()
)
ON CONFLICT (terminal_phone) DO UPDATE SET
    auth_code_hash = EXCLUDED.auth_code_hash,
    imei = EXCLUDED.imei,
    province_id = EXCLUDED.province_id,
    city_id = EXCLUDED.city_id,
    manufacturer_id = EXCLUDED.manufacturer_id,
    terminal_model = EXCLUDED.terminal_model,
    terminal_id = EXCLUDED.terminal_id,
    profile_magic = EXCLUDED.profile_magic,
    profile_version = EXCLUDED.profile_version,
    terminal_type = EXCLUDED.terminal_type,
    capabilities = EXCLUDED.capabilities,
    report_interval_sec = EXCLUDED.report_interval_sec,
    service_point_id = EXCLUDED.service_point_id,
    asset_id = EXCLUDED.asset_id,
    terminal_sn = EXCLUDED.terminal_sn,
    iccid = EXCLUDED.iccid,
    hardware_version = EXCLUDED.hardware_version,
    software_version = EXCLUDED.software_version,
    organization_code = EXCLUDED.organization_code,
    production_batch = EXCLUDED.production_batch,
    profile_revision = EXCLUDED.profile_revision,
    last_registered_at = NOW()
"""

INSERT_INITIAL_TERMINAL_CONFIG_SQL = """
INSERT INTO jt808_terminal_configs (
    terminal_phone, desired_revision, applied_revision,
    desired_parameters, applied_parameters, status, last_applied_at
)
VALUES ($1, $2, $2, $3::JSONB, $3::JSONB, 'applied', NOW())
ON CONFLICT (terminal_phone) DO NOTHING
"""

RECONCILE_REGISTERED_TERMINAL_CONFIG_SQL = """
UPDATE jt808_terminal_configs
SET desired_revision = CASE
        WHEN $2 > desired_revision
          OR ($2 = desired_revision AND desired_parameters <> $3::JSONB)
        THEN $2 + 1 ELSE desired_revision END,
    applied_revision = $2,
    applied_parameters = $3::JSONB,
    status = CASE
        WHEN $2 = desired_revision AND desired_parameters = $3::JSONB
        THEN 'applied' ELSE 'pending' END,
    last_error = NULL,
    last_applied_at = NOW(),
    updated_at = NOW()
WHERE terminal_phone = $1
"""

GET_PENDING_TERMINAL_CONFIG_SQL = """
SELECT terminal_phone, desired_revision, applied_revision,
       desired_parameters, status
FROM jt808_terminal_configs
WHERE terminal_phone = $1 AND desired_revision > applied_revision
"""

MARK_TERMINAL_CONFIG_SENT_SQL = """
UPDATE jt808_terminal_configs
SET status = 'sent', last_sent_at = NOW(), updated_at = NOW()
WHERE terminal_phone = $1 AND desired_revision = $2
"""

CONFIRM_TERMINAL_CONFIG_SQL = """
UPDATE jt808_terminal_configs
SET applied_revision = CASE WHEN $3 THEN $2 ELSE applied_revision END,
    applied_parameters = CASE WHEN $3 THEN desired_parameters ELSE applied_parameters END,
    status = CASE WHEN $3 THEN 'applied' ELSE 'failed' END,
    last_error = CASE WHEN $3 THEN NULL ELSE $4 END,
    last_applied_at = CASE WHEN $3 THEN NOW() ELSE last_applied_at END,
    updated_at = NOW()
WHERE terminal_phone = $1 AND desired_revision = $2
"""

GET_TERMINAL_CONFIG_BY_IMEI_SQL = """
SELECT c.terminal_phone, c.desired_revision, c.applied_revision,
       c.desired_parameters, c.status
FROM jt808_terminal_configs c
JOIN jt808_terminals t ON t.terminal_phone = c.terminal_phone
WHERE t.imei = $1
"""

GET_TERMINAL_REGISTRATION_BY_IMEI_SQL = """
SELECT terminal_phone, imei, province_id, city_id, manufacturer_id,
       profile_magic, profile_version, terminal_type, capabilities,
       report_interval_sec, service_point_id, asset_id, terminal_model,
       terminal_sn, iccid, hardware_version, software_version,
       organization_code, production_batch, profile_revision,
       registered_at, last_registered_at, last_authenticated_at
FROM jt808_terminals
WHERE imei = $1
"""

UPDATE_TERMINAL_CONFIG_SQL = """
UPDATE jt808_terminal_configs c
SET desired_revision = c.desired_revision + 1,
    desired_parameters = $2::JSONB,
    status = 'pending', last_error = NULL, updated_at = NOW()
FROM jt808_terminals t
WHERE t.imei = $1 AND t.terminal_phone = c.terminal_phone
RETURNING c.terminal_phone, c.desired_revision, c.applied_revision,
          c.desired_parameters, c.status
"""

GET_TERMINAL_AUTH_HASH_SQL = """
SELECT t.auth_code_hash, t.imei
FROM jt808_terminals t
JOIN jt808_terminal_configs c ON c.terminal_phone = t.terminal_phone
WHERE t.terminal_phone = $1
"""

MARK_TERMINAL_AUTHENTICATED_SQL = """
UPDATE jt808_terminals
SET imei = COALESCE(imei, $2), last_authenticated_at = NOW()
WHERE terminal_phone = $1 AND (imei IS NULL OR imei = $2)
RETURNING terminal_phone
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
    last_battery_percent: int | None = None


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
    battery_percent: int | None = None
    time_valid: bool | None = None
    record_seq: int | None = None


@dataclass(frozen=True, slots=True)
class TerminalRegistrationSnapshot:
    """Persisted EPTR registration profile without credential material."""

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
        record.battery_percent,
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
        record.battery_percent,
    )


def records_to_bulk_params(
    imei: str,
    generation_id: int,
    batch_id: int,
    records: tuple[TrackerRecord, ...] | list[TrackerRecord],
) -> tuple[object, ...]:
    """Transpose validated records into PostgreSQL arrays for one UNNEST."""
    rows = [
        record_to_track_point_params(imei, generation_id, batch_id, record)
        for record in records
    ]
    if not rows:
        raise DatabaseValueError("records must not be empty")
    return tuple([row[column] for row in rows] for column in range(len(rows[0])))


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
    if any(record.generation_id not in (None, generation_id) for record in records):
        raise DatabaseValueError("record generation does not match batch generation")
    ordered = sorted(records, key=lambda record: record.sequence)

    bulk_params = records_to_bulk_params(
        imei,
        generation_id,
        batch_id,
        ordered,
    )
    async with pool.acquire() as connection:
        async with connection.transaction():
            inserted = await connection.fetchval(
                INSERT_POSITION_RECORDS_SQL,
                *bulk_params,
            )
            await _upsert_device_from_record(connection, imei, ordered[-1])
    return int(inserted or 0)


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


async def save_terminal_registration(
    pool: asyncpg.Pool,
    registration: TerminalRegistration,
    auth_code_hash: str,
) -> None:
    """Persist the credential issued by the latest successful registration."""
    _validate_terminal_phone(registration.terminal_phone)
    if len(auth_code_hash) != 64 or any(
        character not in "0123456789abcdef" for character in auth_code_hash
    ):
        raise DatabaseValueError("auth_code_hash must be a lowercase SHA-256 digest")
    parameters = _registration_parameters(registration)
    async with pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                UPSERT_TERMINAL_REGISTRATION_SQL,
                registration.terminal_phone,
                registration.imei,
                auth_code_hash,
                registration.province_id,
                registration.city_id,
                registration.manufacturer_id,
                registration.terminal_model,
                registration.terminal_sn,
                registration.profile_magic,
                registration.profile_version,
                registration.terminal_type,
                registration.capabilities,
                registration.report_interval_sec,
                registration.service_point_id,
                registration.asset_id,
                registration.terminal_sn,
                registration.iccid,
                registration.hardware_version,
                registration.software_version,
                registration.organization_code,
                registration.production_batch,
                registration.profile_revision,
            )
            await connection.execute(
                INSERT_INITIAL_TERMINAL_CONFIG_SQL,
                registration.terminal_phone,
                registration.profile_revision,
                json.dumps(parameters, ensure_ascii=False),
            )
            await connection.execute(
                RECONCILE_REGISTERED_TERMINAL_CONFIG_SQL,
                registration.terminal_phone,
                registration.profile_revision,
                json.dumps(parameters, ensure_ascii=False),
            )


async def get_pending_terminal_configuration(
    pool: asyncpg.Pool,
    terminal_phone: str,
) -> TerminalConfiguration | None:
    _validate_terminal_phone(terminal_phone)
    async with pool.acquire() as connection:
        row = await connection.fetchrow(GET_PENDING_TERMINAL_CONFIG_SQL, terminal_phone)
    return _terminal_configuration_from_row(row)


async def mark_terminal_configuration_sent(
    pool: asyncpg.Pool,
    terminal_phone: str,
    revision: int,
) -> None:
    async with pool.acquire() as connection:
        await connection.execute(
            MARK_TERMINAL_CONFIG_SENT_SQL, terminal_phone, revision
        )


async def confirm_terminal_configuration(
    pool: asyncpg.Pool,
    terminal_phone: str,
    revision: int,
    success: bool,
    error: str | None = None,
) -> None:
    async with pool.acquire() as connection:
        await connection.execute(
            CONFIRM_TERMINAL_CONFIG_SQL,
            terminal_phone,
            revision,
            success,
            error,
        )


async def get_terminal_configuration(
    pool: asyncpg.Pool,
    imei: str,
) -> TerminalConfiguration | None:
    _validate_auth_imei(imei)
    async with pool.acquire() as connection:
        row = await connection.fetchrow(GET_TERMINAL_CONFIG_BY_IMEI_SQL, imei)
    return _terminal_configuration_from_row(row)


async def get_terminal_registration(
    pool: asyncpg.Pool,
    imei: str,
) -> TerminalRegistrationSnapshot | None:
    """Return the latest EPTR registration profile for one device."""
    _validate_auth_imei(imei)
    async with pool.acquire() as connection:
        row = await connection.fetchrow(GET_TERMINAL_REGISTRATION_BY_IMEI_SQL, imei)
    return None if row is None else TerminalRegistrationSnapshot(**dict(row))


async def update_terminal_configuration(
    pool: asyncpg.Pool,
    imei: str,
    changes: dict[str, object],
) -> TerminalConfiguration | None:
    _validate_auth_imei(imei)
    async with pool.acquire() as connection:
        async with connection.transaction():
            row = await connection.fetchrow(GET_TERMINAL_CONFIG_BY_IMEI_SQL, imei)
            if row is None:
                return None
            parameters = _decode_json_parameters(row["desired_parameters"])
            parameters.update(changes)
            row = await connection.fetchrow(
                UPDATE_TERMINAL_CONFIG_SQL,
                imei,
                json.dumps(parameters, ensure_ascii=False),
            )
    return _terminal_configuration_from_row(row)


def _registration_parameters(registration: TerminalRegistration) -> dict[str, object]:
    return {
        "province_id": registration.province_id,
        "city_id": registration.city_id,
        "terminal_type": registration.terminal_type,
        "report_interval_sec": registration.report_interval_sec,
        "service_point_id": registration.service_point_id,
        "asset_id": registration.asset_id,
        "organization_code": registration.organization_code,
    }


def _terminal_configuration_from_row(row: object) -> TerminalConfiguration | None:
    if row is None:
        return None
    parameters = _decode_json_parameters(row["desired_parameters"])
    return TerminalConfiguration(
        terminal_phone=str(row["terminal_phone"]),
        revision=int(row["desired_revision"]),
        applied_revision=int(row["applied_revision"]),
        status=str(row["status"]),
        province_id=int(parameters["province_id"]),
        city_id=int(parameters["city_id"]),
        terminal_type=int(parameters["terminal_type"]),
        report_interval_sec=int(parameters["report_interval_sec"]),
        service_point_id=str(parameters.get("service_point_id", "")),
        asset_id=str(parameters.get("asset_id", "")),
        organization_code=str(parameters.get("organization_code", "")),
    )


def _decode_json_parameters(value: object) -> dict[str, object]:
    if isinstance(value, str):
        decoded = json.loads(value)
    else:
        decoded = value
    if not isinstance(decoded, dict):
        raise DatabaseValueError("terminal parameters must be a JSON object")
    return decoded


async def get_terminal_auth_credential(
    pool: asyncpg.Pool,
    terminal_phone: str,
) -> tuple[str, str | None] | None:
    """Return one terminal's credential digest and bound IMEI."""
    _validate_terminal_phone(terminal_phone)
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            GET_TERMINAL_AUTH_HASH_SQL,
            terminal_phone,
        )
    if row is None:
        return None
    return str(row["auth_code_hash"]), row["imei"]


async def mark_terminal_authenticated(
    pool: asyncpg.Pool,
    terminal_phone: str,
    imei: str,
) -> bool:
    """Record the latest successful JT/T 808 authentication time."""
    _validate_terminal_phone(terminal_phone)
    _validate_auth_imei(imei)
    async with pool.acquire() as connection:
        value = await connection.fetchval(
            MARK_TERMINAL_AUTHENTICATED_SQL,
            terminal_phone,
            imei,
        )
    return value is not None


# Compatibility name retained for earlier callers.
query_track_points = get_track_points


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


def _validate_imei(imei: str) -> None:
    if not imei or len(imei) > 20 or not imei.isascii() or not imei.isdecimal():
        raise DatabaseValueError("IMEI must contain 1 to 20 ASCII decimal digits")


def _validate_terminal_phone(terminal_phone: str) -> None:
    if (
        not terminal_phone
        or len(terminal_phone) > 20
        or not terminal_phone.isascii()
        or not terminal_phone.isdecimal()
    ):
        raise DatabaseValueError(
            "terminal_phone must contain 1 to 20 ASCII decimal digits"
        )


def _validate_auth_imei(imei: str) -> None:
    if len(imei) != 15 or not imei.isascii() or not imei.isdecimal():
        raise DatabaseValueError("IMEI must contain exactly 15 ASCII decimal digits")
