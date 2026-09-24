"""Focused database tests for editable device metadata."""

import asyncio
from datetime import datetime, timezone
from typing import Any

from backend.app.database import (
    UPDATE_DEVICE_NAME_SQL,
    DeviceSnapshot,
    update_device_name,
)


class Acquire:
    def __init__(self, connection: "Connection") -> None:
        self.connection = connection

    async def __aenter__(self) -> "Connection":
        return self.connection

    async def __aexit__(self, *args: Any) -> None:
        return None


class Connection:
    def __init__(self, record: dict[str, Any] | None) -> None:
        self.record = record
        self.call: tuple[str, tuple[Any, ...]] | None = None

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.call = (sql, args)
        return self.record


class Pool:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def acquire(self) -> Acquire:
        return Acquire(self.connection)


def device_record() -> dict[str, Any]:
    seen = datetime(2026, 9, 15, 9, 45, 8, tzinfo=timezone.utc)
    return {
        "imei": "862288087606784",
        "name": "测试车",
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
        "last_battery_mv": 3700,
    }


def test_update_device_name_uses_parameterized_sql() -> None:
    connection = Connection(device_record())
    result = asyncio.run(
        update_device_name(
            Pool(connection),  # type: ignore[arg-type]
            "862288087606784",
            "  测试车  ",
        )
    )
    assert result == DeviceSnapshot(**device_record())
    assert connection.call == (
        UPDATE_DEVICE_NAME_SQL,
        ("862288087606784", "测试车"),
    )


def test_blank_name_clears_alias_and_unknown_device_returns_none() -> None:
    connection = Connection(None)
    result = asyncio.run(
        update_device_name(
            Pool(connection),  # type: ignore[arg-type]
            "862288087606784",
            "   ",
        )
    )
    assert result is None
    assert connection.call == (
        UPDATE_DEVICE_NAME_SQL,
        ("862288087606784", None),
    )
