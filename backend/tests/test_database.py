"""Unit tests for the async PostgreSQL data access layer."""

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from backend.app import database as database_module
from backend.app.config import DatabaseSettings
from backend.app.database import (
    GET_DEVICE_LATEST_SQL,
    GET_DEVICES_SQL,
    GET_TRACK_POINTS_SQL,
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
    report_to_device_params,
    report_to_track_point_params,
    save_report,
)
from backend.app.protocol import generate_checksum, parse_message


STANDARD_BODY = (
    "PTRK,1,862288087606784,150926,094508,A,3045.83496,N,"
    "10354.09348,E,575.0,0.111,193.85,9,2.21,31,1"
)
STANDARD_MESSAGE = f"${STANDARD_BODY}*69"


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
