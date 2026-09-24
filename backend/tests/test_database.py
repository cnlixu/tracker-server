"""Unit tests for the async PostgreSQL data access layer."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pytest

from backend.app import database as database_module
from backend.app.binary_protocol import decode_frame, decode_record
from backend.app.config import DatabaseSettings
from backend.app.database import (
    GET_DEVICE_LATEST_SQL,
    GET_DEVICES_SQL,
    GET_TRACK_POINTS_SQL,
    INSERT_POSITION_RECORD_SQL,
    INSERT_TRACK_POINT_SQL,
    SCHEMA_PATH,
    UPSERT_DEVICE_LATEST_SQL,
    DatabaseConnectionError,
    DatabaseValueError,
    DeviceSnapshot,
    TrackPoint,
    close_pool,
    get_device_latest,
    get_devices,
    get_track_points,
    init_pool,
    init_schema,
    record_to_device_params,
    record_to_track_point_params,
    report_to_device_params,
    report_to_track_point_params,
    save_records,
    save_report,
)
from backend.app.models import TrackerRecord
from backend.app.protocol import generate_checksum, parse_message
from backend.app.services import TrackerService


STANDARD_BODY = (
    "PTRK,1,862288087606784,150926,094508,A,3045.83496,N,"
    "10354.09348,E,575.0,0.111,193.85,9,2.21,31,1"
)
STANDARD_MESSAGE = f"${STANDARD_BODY}*69"

# ASCII V3 report published by the tracker firmware.
V3_MESSAGE = (
    "$PTRK,3,862288087606784,305419896,1788251489,1788251489,010926,083129,"
    "A,1,3045.81768,N,10354.07883,E,516.2,0.591,152.99,15,0.80,31,3700,1*76"
)

# Binary V2 sample published by the tracker firmware.
UPLOAD_FRAME_HEX = (
    "A55A0101310002862288087606784F78563412618D966A0100"
    "618D966A618D966A382856121215EE3D04021E00C33B740E080F1F0BEA51"
    "77FE"
)
RECORD_HEX = "618D966A618D966A382856121215EE3D04021E00C33B740E080F1F0BEA51"
IMEI = "862288087606784"
GENERATION_ID = 0x12345678
BATCH_ID = 1788251489


def firmware_record() -> TrackerRecord:
    return decode_record(bytes.fromhex(RECORD_HEX))


def build_message(body: str) -> str:
    return f"${body}*{generate_checksum(body)}"


class FakeTransaction:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection

    async def __aenter__(self) -> "FakeTransaction":
        self.connection.transaction_entered += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> bool:
        self.connection.transaction_exit_types.append(exc_type)
        return False


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchval_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchval_result: Any = None
        self.fetchval_error: Exception | None = None
        self.fetch_result: list[dict[str, Any]] = []
        self.fetchrow_result: dict[str, Any] | None = None
        self.transaction_entered = 0
        self.transaction_exit_types: list[type[BaseException] | None] = []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append("execute")
        self.execute_calls.append((sql, args))
        return "OK"

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.calls.append("fetchval")
        self.fetchval_calls.append((sql, args))
        if self.fetchval_error is not None:
            raise self.fetchval_error
        return self.fetchval_result

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append("fetch")
        self.fetch_calls.append((sql, args))
        return self.fetch_result

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append("fetchrow")
        self.fetchrow_calls.append((sql, args))
        return self.fetchrow_result


class FakeAcquire:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, *args: Any) -> None:
        return None


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.closed = False

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)

    async def close(self) -> None:
        self.closed = True


def device_record() -> dict[str, Any]:
    seen = datetime(2026, 9, 15, 9, 45, 8, tzinfo=timezone.utc)
    return {
        "imei": "862288087606784",
        "name": None,
        "first_seen": seen,
        "last_seen": seen,
        "last_gps_time": seen,
        "last_valid": True,
        "last_lat": 30.763916,
        "last_lon": 103.901558,
        "last_altitude": 575.0,
        "last_speed": 0.111,
        "last_course": 193.85,
        "last_satellites": 9,
        "last_hdop": 2.21,
        "last_csq": 31,
        "last_wake_code": 1,
    }


def track_record() -> dict[str, Any]:
    seen = datetime(2026, 9, 15, 9, 45, 8, tzinfo=timezone.utc)
    return {
        "id": 42,
        "imei": "862288087606784",
        "gps_time": seen,
        "server_time": seen,
        "valid": True,
        "latitude": 30.763916,
        "longitude": 103.901558,
        "altitude": 575.0,
        "speed": 0.111,
        "course": 193.85,
        "satellites": 9,
        "hdop": 2.21,
        "csq": 31,
        "wake_code": 1,
        "raw_data": STANDARD_MESSAGE,
    }


def test_schema_has_required_columns_and_query_index() -> None:
    schema = SCHEMA_PATH.read_text(encoding="utf-8")

    assert "imei VARCHAR(20) PRIMARY KEY" in schema
    assert "name VARCHAR(64)" in schema
    assert "last_valid BOOLEAN" in schema
    assert "valid BOOLEAN NOT NULL" in schema
    assert "raw_data TEXT NOT NULL" in schema
    assert "ON track_points (imei, gps_time)" in schema
    assert "UNIQUE (imei, seq)" not in schema


def test_tracker_report_parameter_mapping() -> None:
    report = parse_message(STANDARD_MESSAGE)

    history_params = report_to_track_point_params(report)
    device_params = report_to_device_params(report)

    assert history_params == (
        "862288087606784",
        datetime(2026, 9, 15, 9, 45, 8, tzinfo=timezone.utc),
        True,
        pytest.approx(30.763916),
        pytest.approx(103.901558),
        575.0,
        0.111,
        193.85,
        9,
        2.21,
        31,
        1,
        STANDARD_MESSAGE,
    )
    assert device_params == history_params[:12]


def test_invalid_fix_maps_nullable_gnss_fields_to_none() -> None:
    fields = STANDARD_BODY.split(",")
    fields[5] = "V"
    fields[6:15] = [""] * 9
    report = parse_message(build_message(",".join(fields)))

    params = report_to_track_point_params(report)

    assert params[2] is False
    assert params[3:10] == (None, None, None, None, None, None, None)


def test_save_report_uses_one_transaction_and_parameterized_sql() -> None:
    connection = FakeConnection()
    connection.fetchval_result = 42
    pool = FakePool(connection)
    report = parse_message(STANDARD_MESSAGE)

    track_point_id = asyncio.run(
        save_report(pool, report)  # type: ignore[arg-type]
    )

    assert track_point_id == 42
    assert connection.transaction_entered == 1
    assert connection.transaction_exit_types == [None]
    assert connection.calls == ["fetchval", "execute"]
    assert connection.fetchval_calls[0][0] == INSERT_TRACK_POINT_SQL
    assert connection.execute_calls[0][0] == UPSERT_DEVICE_LATEST_SQL
    assert connection.fetchval_calls[0][1] == report_to_track_point_params(report)
    assert connection.execute_calls[0][1] == report_to_device_params(report)


def test_insert_failure_prevents_device_update_and_exits_transaction_with_error() -> None:
    connection = FakeConnection()
    connection.fetchval_error = RuntimeError("insert failed")
    pool = FakePool(connection)
    report = parse_message(STANDARD_MESSAGE)

    with pytest.raises(RuntimeError, match="insert failed"):
        asyncio.run(save_report(pool, report))  # type: ignore[arg-type]

    assert connection.execute_calls == []
    assert connection.transaction_exit_types == [RuntimeError]


def test_get_devices_and_get_device_latest_map_records() -> None:
    connection = FakeConnection()
    connection.fetch_result = [device_record()]
    connection.fetchrow_result = device_record()
    pool = FakePool(connection)

    devices = asyncio.run(get_devices(pool))  # type: ignore[arg-type]
    latest = asyncio.run(
        get_device_latest(pool, "862288087606784")  # type: ignore[arg-type]
    )

    assert devices == [DeviceSnapshot(**device_record())]
    assert latest == DeviceSnapshot(**device_record())
    assert connection.fetch_calls[0] == (GET_DEVICES_SQL, ())
    assert connection.fetchrow_calls[0] == (
        GET_DEVICE_LATEST_SQL,
        ("862288087606784",),
    )


def test_get_track_points_uses_half_open_utc_boundaries_and_ordered_query() -> None:
    connection = FakeConnection()
    connection.fetch_result = [track_record()]
    pool = FakePool(connection)
    start = datetime(2026, 9, 15, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 9, 16, 0, 0, tzinfo=timezone.utc)

    points = asyncio.run(
        get_track_points(
            pool,  # type: ignore[arg-type]
            "862288087606784",
            start,
            end,
            limit=500,
        )
    )

    assert points == [TrackPoint(**track_record())]
    sql, args = connection.fetch_calls[0]
    assert sql == GET_TRACK_POINTS_SQL
    assert "gps_time >= $2 AND gps_time < $3" in sql
    assert "ORDER BY gps_time ASC" in sql
    assert args == ("862288087606784", start, end, 500)


def test_time_range_rejects_reverse_and_naive_boundaries() -> None:
    pool = FakePool(FakeConnection())
    aware = datetime(2026, 9, 15, tzinfo=timezone.utc)

    with pytest.raises(DatabaseValueError, match="timezone-aware"):
        asyncio.run(
            get_track_points(  # type: ignore[arg-type]
                pool,
                "862288087606784",
                datetime(2026, 9, 15),
                aware,
            )
        )
    with pytest.raises(DatabaseValueError, match="start_time"):
        asyncio.run(
            get_track_points(  # type: ignore[arg-type]
                pool,
                "862288087606784",
                aware,
                datetime(2026, 9, 14, tzinfo=timezone.utc),
            )
        )


def test_database_unavailable_has_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unavailable_pool(**kwargs: Any) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(database_module.asyncpg, "create_pool", unavailable_pool)
    settings = DatabaseSettings(
        host="127.0.0.1",
        port=5432,
        database="tracker",
        user="tracker",
        password="not-a-real-password",
    )

    with pytest.raises(
        DatabaseConnectionError,
        match=r"unable to connect to PostgreSQL at 127\.0\.0\.1:5432/tracker",
    ):
        asyncio.run(init_pool(settings))


def test_schema_initialization_and_pool_close() -> None:
    connection = FakeConnection()
    pool = FakePool(connection)

    asyncio.run(init_schema(pool))  # type: ignore[arg-type]
    asyncio.run(close_pool(pool))  # type: ignore[arg-type]

    assert connection.execute_calls == [
        (SCHEMA_PATH.read_text(encoding="utf-8"), ()),
    ]
    assert pool.closed is True


def test_schema_adds_record_identity_columns_and_unique_index() -> None:
    schema = SCHEMA_PATH.read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS generation_id BIGINT" in schema
    assert "ADD COLUMN IF NOT EXISTS record_seq BIGINT" in schema
    assert "ADD COLUMN IF NOT EXISTS batch_id BIGINT" in schema
    assert "ADD COLUMN IF NOT EXISTS battery_mv INTEGER" in schema
    assert "ADD COLUMN IF NOT EXISTS time_valid BOOLEAN" in schema
    assert "UNIQUE INDEX IF NOT EXISTS uq_track_points_record_identity" in schema
    assert "ON track_points (imei, generation_id, record_seq)" in schema


def test_position_record_parameter_mapping() -> None:
    record = firmware_record()

    params = record_to_track_point_params(IMEI, GENERATION_ID, BATCH_ID, record)

    assert params[:13] == (
        IMEI,
        record.gps_time,
        True,
        pytest.approx(30.763628),
        pytest.approx(103.9013138),
        516.0,
        pytest.approx(30 / (185_200 / 3_600)),
        152.99,
        15,
        0.8,
        31,
        1,
        RECORD_HEX,
    )
    assert params[13:] == (GENERATION_ID, BATCH_ID, BATCH_ID, 3700, True)
    assert record_to_device_params(IMEI, record) == params[:12]


def test_save_records_uses_one_transaction_in_sequence_order() -> None:
    connection = FakeConnection()
    connection.fetchval_result = 1
    pool = FakePool(connection)
    record = firmware_record()
    newer = replace(record, sequence=record.sequence + 1)

    inserted = asyncio.run(
        save_records(  # type: ignore[arg-type]
            pool,
            imei=IMEI,
            generation_id=GENERATION_ID,
            batch_id=BATCH_ID,
            records=[newer, record],
        )
    )

    assert inserted == 2
    assert connection.transaction_entered == 1
    assert connection.transaction_exit_types == [None]
    assert connection.calls == ["fetchval", "fetchval", "execute"]
    assert [sql for sql, _ in connection.fetchval_calls] == [
        INSERT_POSITION_RECORD_SQL,
        INSERT_POSITION_RECORD_SQL,
    ]
    assert connection.fetchval_calls[0][1] == record_to_track_point_params(
        IMEI, GENERATION_ID, BATCH_ID, record
    )
    assert connection.fetchval_calls[1][1] == record_to_track_point_params(
        IMEI, GENERATION_ID, BATCH_ID, newer
    )
    assert connection.execute_calls[0][0] == UPSERT_DEVICE_LATEST_SQL
    assert connection.execute_calls[0][1] == record_to_device_params(IMEI, newer)


def test_save_records_treats_conflicts_as_already_stored() -> None:
    connection = FakeConnection()
    # ON CONFLICT DO NOTHING returns no row for a re-delivered record.
    connection.fetchval_result = None
    pool = FakePool(connection)

    inserted = asyncio.run(
        save_records(  # type: ignore[arg-type]
            pool,
            imei=IMEI,
            generation_id=GENERATION_ID,
            batch_id=BATCH_ID,
            records=[firmware_record()],
        )
    )

    assert inserted == 0
    assert connection.calls == ["fetchval", "execute"]


def test_save_records_failure_exits_transaction_with_error() -> None:
    connection = FakeConnection()
    connection.fetchval_error = RuntimeError("insert failed")
    pool = FakePool(connection)

    with pytest.raises(RuntimeError, match="insert failed"):
        asyncio.run(
            save_records(  # type: ignore[arg-type]
                pool,
                imei=IMEI,
                generation_id=GENERATION_ID,
                batch_id=BATCH_ID,
                records=[firmware_record()],
            )
        )

    assert connection.execute_calls == []
    assert connection.transaction_exit_types == [RuntimeError]


def test_save_records_rejects_an_empty_batch_or_bad_imei() -> None:
    pool = FakePool(FakeConnection())

    with pytest.raises(DatabaseValueError, match="records must not be empty"):
        asyncio.run(
            save_records(  # type: ignore[arg-type]
                pool,
                imei=IMEI,
                generation_id=GENERATION_ID,
                batch_id=BATCH_ID,
                records=[],
            )
        )
    with pytest.raises(DatabaseValueError, match="IMEI"):
        asyncio.run(
            save_records(  # type: ignore[arg-type]
                pool,
                imei="862288087606784A",
                generation_id=GENERATION_ID,
                batch_id=BATCH_ID,
                records=[firmware_record()],
            )
        )


def test_service_routes_v1_reports_to_the_single_insert_path() -> None:
    connection = FakeConnection()
    connection.fetchval_result = 42
    service = TrackerService(FakePool(connection))  # type: ignore[arg-type]

    result = asyncio.run(service.process_report(parse_message(STANDARD_MESSAGE)))

    assert result == 42
    assert connection.calls == ["fetchval", "execute"]
    assert connection.fetchval_calls[0][0] == INSERT_TRACK_POINT_SQL
    assert connection.fetchval_calls[0][1] == report_to_track_point_params(
        parse_message(STANDARD_MESSAGE)
    )


def test_service_routes_v3_reports_to_the_record_path() -> None:
    connection = FakeConnection()
    connection.fetchval_result = 1
    service = TrackerService(FakePool(connection))  # type: ignore[arg-type]
    report = parse_message(V3_MESSAGE)

    # save_records returns the number of newly inserted records.
    result = asyncio.run(service.process_report(report))

    assert result == 1
    assert connection.calls == ["fetchval", "execute"]
    assert connection.fetchval_calls[0][0] == INSERT_POSITION_RECORD_SQL

    params = connection.fetchval_calls[0][1]
    assert params[0] == report.imei
    assert params[13] == report.generation_id
    assert params[14] == report.record_sequence
    assert params[15] == report.batch_id
    assert params[16] == report.battery_mv
    assert params[17] is True


def test_service_persists_a_decoded_binary_batch() -> None:
    connection = FakeConnection()
    connection.fetchval_result = 1
    service = TrackerService(FakePool(connection))  # type: ignore[arg-type]
    batch = decode_frame(bytes.fromhex(UPLOAD_FRAME_HEX))

    inserted = asyncio.run(service.process_batch(batch))

    assert inserted == 1
    assert connection.calls == ["fetchval", "execute"]
    assert connection.fetchval_calls[0][0] == INSERT_POSITION_RECORD_SQL

    params = connection.fetchval_calls[0][1]
    assert params[0] == batch.imei
    assert params[13] == batch.generation_id
    assert params[14] == batch.records[0].sequence
    assert params[15] == batch.batch_id
    assert params[16] == 3700
