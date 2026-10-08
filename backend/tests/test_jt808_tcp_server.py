"""Connection-level authentication, persistence, and ACK tests."""

import asyncio
import struct

from backend.app.config import TCPSettings
from backend.app.jt808_protocol import (
    EXTRA_BATTERY,
    EXTRA_HDOP,
    EXTRA_RECORD_ID,
    EXTRA_SATELLITES,
    EXTRA_SIGNAL,
    MSG_LOCATION,
    MSG_PLATFORM_COMMON_ACK,
    MSG_TERMINAL_AUTH,
    build_frame,
    decode_frame,
)
from backend.app.jt808_tcp_server import ConnectionState, TrackerTCPServer


IMEI = "862288087606784"
AUTH_CODE = "device-secret"


class FakeService:
    def __init__(self) -> None:
        self.batches = []

    async def process_batch(self, batch):
        self.batches.append(batch)
        return len(batch.records)


class FakeWriter:
    def __init__(self) -> None:
        self.output = bytearray()

    def write(self, data: bytes) -> None:
        self.output.extend(data)

    async def drain(self) -> None:
        return None


def extra(item_id: int, value: bytes) -> bytes:
    return bytes([item_id, len(value)]) + value


def location_body() -> bytes:
    base = struct.pack(">IIIIHHH", 0, 0x00040002, 30_763_628, 103_901_314, 516, 11, 153)
    base += bytes.fromhex("260901163129")
    return b"".join(
        (
            base,
            extra(EXTRA_SIGNAL, b"\x1f"),
            extra(EXTRA_SATELLITES, b"\x0f"),
            extra(EXTRA_RECORD_ID, struct.pack(">BIIB", 1, 0x12345678, 100, 3)),
            extra(EXTRA_BATTERY, struct.pack(">HB", 3700, 25)),
            extra(EXTRA_HDOP, b"\x08"),
        )
    )


def auth_body() -> bytes:
    encoded = AUTH_CODE.encode("ascii")
    return bytes([len(encoded)]) + encoded + IMEI.encode("ascii") + b"1.0".ljust(20, b"\x00")


def test_authentication_is_required_and_ack_follows_persistence() -> None:
    async def scenario() -> None:
        service = FakeService()
        server = TrackerTCPServer(
            service,
            TCPSettings(auth_code=AUTH_CODE),
        )
        writer = FakeWriter()
        state = ConnectionState()

        await server._handle_frame(
            build_frame(MSG_TERMINAL_AUTH, IMEI, 1, auth_body()),
            writer,
            state,
            "test",
        )
        auth_ack = decode_frame(bytes(writer.output))
        assert auth_ack.message_id == MSG_PLATFORM_COMMON_ACK
        assert auth_ack.body == struct.pack(">HHB", 1, MSG_TERMINAL_AUTH, 0)
        assert state.authenticated is True

        writer.output.clear()
        await server._handle_frame(
            build_frame(MSG_LOCATION, IMEI, 100, location_body()),
            writer,
            state,
            "test",
        )
        assert len(service.batches) == 1
        assert service.batches[0].records[0].battery_percent == 25
        location_ack = decode_frame(bytes(writer.output))
        assert location_ack.body == struct.pack(">HHB", 100, MSG_LOCATION, 0)

    asyncio.run(scenario())


def test_location_before_authentication_is_rejected() -> None:
    async def scenario() -> None:
        service = FakeService()
        server = TrackerTCPServer(service, TCPSettings(auth_code=AUTH_CODE))
        writer = FakeWriter()
        await server._handle_frame(
            build_frame(MSG_LOCATION, IMEI, 100, location_body()),
            writer,
            ConnectionState(),
            "test",
        )
        assert service.batches == []
        response = decode_frame(bytes(writer.output))
        assert response.body == struct.pack(">HHB", 100, MSG_LOCATION, 1)

    asyncio.run(scenario())
