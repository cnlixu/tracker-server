"""Tests for the authenticated Tracker Web API without PostgreSQL."""

from datetime import date, datetime, timedelta, timezone
from typing import Any

from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from backend.app import api as api_module
from backend.app.config import AuthSettings, OnlineStatusSettings
from backend.app.database import DatabaseError, DeviceSnapshot, TrackPoint
from backend.app.main import create_app


NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
IMEI = "862288087606784"
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "correct horse battery staple"
AUTH_SETTINGS = AuthSettings(
    admin_username=ADMIN_USERNAME,
    admin_password_hash=PasswordHasher(
        time_cost=1,
        memory_cost=8192,
        parallelism=1,
    ).hash(ADMIN_PASSWORD),
    session_secret="test-session-secret-that-is-longer-than-32-characters",
    session_max_age_seconds=3600,
    cookie_secure=False,
)


class FakePool:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def build_test_app() -> tuple[Any, FakePool]:
    pool = FakePool()

    async def pool_factory() -> Any:
        return pool

    application = create_app(
        pool_factory=pool_factory,
        online_status_settings=OnlineStatusSettings(
            online_minutes=20,
            warning_minutes=60,
        ),
        auth_settings=AUTH_SETTINGS,
    )
    application.dependency_overrides[api_module.get_utc_now] = lambda: NOW
    return application, pool


def login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200


def make_device(*, age_minutes: int, suffix: str = "", name: str | None = None) -> DeviceSnapshot:
    gps_time = NOW - timedelta(minutes=age_minutes)
    return DeviceSnapshot(
        imei=f"8622880876067{suffix or '84'}",
        name=name,
        first_seen=NOW - timedelta(days=1),
        last_seen=gps_time,
        last_gps_time=gps_time,
        last_valid=True,
        last_lat=30.763916,
        last_lon=103.901558,
        last_altitude=575.0,
        last_speed=0.111,
        last_course=193.85,
        last_satellites=9,
        last_hdop=2.21,
        last_csq=31,
        last_wake_code=1,
        last_battery_mv=3700,
    )


def make_track_point(point_id: int, gps_time: datetime) -> TrackPoint:
    return TrackPoint(
        id=point_id,
        imei=IMEI,
        gps_time=gps_time,
        server_time=gps_time + timedelta(seconds=1),
        valid=True,
        latitude=30.763916,
        longitude=103.901558,
        altitude=575.0,
        speed=0.111,
        course=193.85,
        satellites=9,
        hdop=2.21,
        csq=31,
        wake_code=1,
        raw_data="$PTRK,...*00",
        battery_mv=3700,
        time_valid=True,
        record_seq=1788251489,
    )


def test_health_is_public_and_pool_lifecycle_closes() -> None:
    application, pool = build_test_app()
    with TestClient(application) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert pool.closed is True


def test_device_api_requires_authentication() -> None:
    application, _ = build_test_app()
    with TestClient(application) as client:
        response = client.get("/api/devices")
    assert response.status_code == 401
    assert response.json() == {"detail": "authentication required"}


def test_login_session_tampering_and_logout() -> None:
    application, _ = build_test_app()
    with TestClient(application) as client:
        rejected = client.post(
            "/api/auth/login",
            json={"username": ADMIN_USERNAME, "password": "wrong-password"},
        )
        assert rejected.status_code == 401
        assert "password" not in rejected.headers.get("set-cookie", "")

        login(client)
        cookie = client.cookies.get(AUTH_SETTINGS.cookie_name)
        assert cookie
        assert "HttpOnly" in client.post(
            "/api/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        ).headers["set-cookie"]
        assert client.get("/api/auth/session").json() == {
            "authenticated": True,
            "username": ADMIN_USERNAME,
        }

        client.cookies.set(AUTH_SETTINGS.cookie_name, f"{cookie}tampered")
        assert client.get("/api/auth/session").status_code == 401

        login(client)
        assert client.post("/api/auth/logout").status_code == 204
        assert client.get("/api/auth/session").status_code == 401


def test_unknown_device_returns_404(monkeypatch: Any) -> None:
    async def no_device(pool: Any, imei: str) -> None:
        return None

    monkeypatch.setattr(api_module, "get_device_latest", no_device)
    application, _ = build_test_app()
    with TestClient(application) as client:
        login(client)
        response = client.get(f"/api/devices/{IMEI}/latest")
    assert response.status_code == 404


def test_invalid_track_ranges_return_clear_errors() -> None:
    application, _ = build_test_app()
    with TestClient(application) as client:
        login(client)
        missing = client.get(f"/api/devices/{IMEI}/track?start=2026-09-15T08:00")
        reversed_range = client.get(
            f"/api/devices/{IMEI}/track?start=2026-09-15T09:00&end=2026-09-15T08:00"
        )
        malformed = client.get(
            f"/api/devices/{IMEI}/track?start=bad&end=2026-09-15T08:00"
        )
    assert missing.status_code == 400
    assert reversed_range.status_code == 400
    assert malformed.status_code == 400


def test_shanghai_time_helpers_convert_to_half_open_utc_windows() -> None:
    start, end = api_module.shanghai_day_to_utc_range(date(2026, 9, 15))
    assert start == datetime(2026, 9, 14, 16, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 9, 15, 16, 0, tzinfo=timezone.utc)

    start, end = api_module.shanghai_minutes_to_utc_range(
        "2026-09-15T08:30",
        "2026-09-16T10:45",
    )
    assert start == datetime(2026, 9, 15, 0, 30, tzinfo=timezone.utc)
    assert end == datetime(2026, 9, 16, 2, 45, tzinfo=timezone.utc)


def test_track_endpoint_passes_minute_range_and_sorts_points(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    earlier = datetime(2026, 9, 15, 1, 0, tzinfo=timezone.utc)
    later = datetime(2026, 9, 15, 2, 0, tzinfo=timezone.utc)

    async def fake_track_query(
        pool: Any,
        imei: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[TrackPoint]:
        captured.update(imei=imei, start_time=start_time, end_time=end_time)
        return [make_track_point(2, later), make_track_point(1, earlier)]

    monkeypatch.setattr(api_module, "get_track_points", fake_track_query)
    application, _ = build_test_app()
    with TestClient(application) as client:
        login(client)
        response = client.get(
            f"/api/devices/{IMEI}/track"
            "?start=2026-09-15T08:30&end=2026-09-16T10:45"
        )
    assert response.status_code == 200
    assert captured == {
        "imei": IMEI,
        "start_time": datetime(2026, 9, 15, 0, 30, tzinfo=timezone.utc),
        "end_time": datetime(2026, 9, 16, 2, 45, tzinfo=timezone.utc),
    }
    assert [point["id"] for point in response.json()] == [1, 2]


def test_date_query_remains_compatible(monkeypatch: Any) -> None:
    captured: dict[str, datetime] = {}

    async def fake_track_query(
        pool: Any, imei: str, start_time: datetime, end_time: datetime
    ) -> list[TrackPoint]:
        captured.update(start=start_time, end=end_time)
        return []

    monkeypatch.setattr(api_module, "get_track_points", fake_track_query)
    application, _ = build_test_app()
    with TestClient(application) as client:
        login(client)
        response = client.get(f"/api/devices/{IMEI}/track?date=2026-09-15")
    assert response.status_code == 200
    assert captured == {
        "start": datetime(2026, 9, 14, 16, 0, tzinfo=timezone.utc),
        "end": datetime(2026, 9, 15, 16, 0, tzinfo=timezone.utc),
    }


def test_devices_online_status_thresholds(monkeypatch: Any) -> None:
    async def fake_devices(pool: Any) -> list[DeviceSnapshot]:
        return [
            make_device(age_minutes=20, suffix="81"),
            make_device(age_minutes=40, suffix="82"),
            make_device(age_minutes=61, suffix="83"),
        ]

    monkeypatch.setattr(api_module, "get_devices", fake_devices)
    application, _ = build_test_app()
    with TestClient(application) as client:
        login(client)
        response = client.get("/api/devices")
    assert response.status_code == 200
    assert [device["online_status"] for device in response.json()] == [
        "online", "warning", "offline",
    ]


def test_device_name_can_be_updated(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    async def fake_update(pool: Any, imei: str, name: str | None) -> DeviceSnapshot:
        captured.update(imei=imei, name=name)
        return make_device(age_minutes=1, name=name)

    monkeypatch.setattr(api_module, "update_device_name", fake_update)
    application, _ = build_test_app()
    with TestClient(application) as client:
        login(client)
        response = client.patch(f"/api/devices/{IMEI}", json={"name": "测试车"})
    assert response.status_code == 200
    assert response.json()["name"] == "测试车"
    assert captured == {"imei": IMEI, "name": "测试车"}


def test_device_and_track_responses_expose_battery(monkeypatch: Any) -> None:
    async def fake_devices(pool: Any) -> list[DeviceSnapshot]:
        return [make_device(age_minutes=1)]

    async def fake_track_query(
        pool: Any,
        imei: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[TrackPoint]:
        return [make_track_point(1, NOW)]

    monkeypatch.setattr(api_module, "get_devices", fake_devices)
    monkeypatch.setattr(api_module, "get_track_points", fake_track_query)
    application, _ = build_test_app()
    with TestClient(application) as client:
        login(client)
        device = client.get("/api/devices").json()[0]
        points = client.get(
            f"/api/devices/{IMEI}/track"
            "?start=2026-09-15T00:00&end=2026-09-16T00:00"
        ).json()

    assert device["battery_mv"] == 3700
    assert points[0]["battery_mv"] == 3700
    assert points[0]["time_valid"] is True
    assert points[0]["record_seq"] == 1788251489


def test_database_error_returns_generic_500(monkeypatch: Any) -> None:
    async def failed_query(pool: Any) -> list[DeviceSnapshot]:
        raise DatabaseError("SQL and secret details must not be exposed")

    monkeypatch.setattr(api_module, "get_devices", failed_query)
    application, _ = build_test_app()
    with TestClient(application) as client:
        login(client)
        response = client.get("/api/devices")
    assert response.status_code == 500
    assert response.json() == {"detail": "database operation failed"}
    assert "secret" not in response.text
