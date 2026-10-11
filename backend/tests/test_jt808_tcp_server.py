"""Connection-level authentication, persistence, and ACK tests."""

import asyncio
import struct

from backend.app.config import TCPSettings
from backend.app.jt808_protocol import (
    EXTRA_BATTERY,
    EXTRA_HDOP,
    EXTRA_SATELLITES,
    EXTRA_SIGNAL,
    MSG_LOCATION,
    MSG_PLATFORM_COMMON_ACK,
    MSG_REGISTER_ACK,
    MSG_SET_PARAMETERS,
    MSG_TERMINAL_ATTRIBUTES,
    MSG_TERMINAL_AUTH,
    MSG_TERMINAL_COMMON_ACK,
    MSG_TERMINAL_HEARTBEAT,
    MSG_TERMINAL_PARAMETERS,
    MSG_TERMINAL_REGISTER,
    build_frame,
    decode_frame,
    JT808FrameBuffer,
)
from backend.app.jt808_tcp_server import ConnectionState, TrackerTCPServer
from backend.app.models import TerminalConfiguration


IMEI = "862288087606784"
TERMINAL_PHONE = "13800138000"
AUTH_CODE = "device-secret"


class FakeService:
    def __init__(self) -> None:
        self.batches = []
        self.registrations = []
        self.credentials = {}
        self.pending_configuration = None
        self.confirmations = []
        self.attributes = []

    async def register_terminal(self, registration):
        self.registrations.append(registration)
        self.credentials[registration.terminal_phone] = (AUTH_CODE, None)
        return AUTH_CODE

    async def authenticate_terminal(self, terminal_phone, imei, auth_code):
        credential = self.credentials.get(terminal_phone)
        if credential is None:
            return False
        expected, bound_imei = credential
        accepted = auth_code == expected and bound_imei in (None, imei)
        if accepted:
            self.credentials[terminal_phone] = (expected, imei)
        return accepted

    async def process_batch(self, batch):
        self.batches.append(batch)
        return len(batch.records)

    async def store_terminal_attributes(self, attributes):
        self.attributes.append(attributes)
        return True

    async def get_pending_configuration(self, terminal_phone):
        return self.pending_configuration

    async def mark_configuration_sent(self, terminal_phone, revision):
        return None

    async def confirm_configuration(
        self, terminal_phone, revision, success, error=None
    ):
        self.confirmations.append((terminal_phone, revision, success, error))


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
            extra(EXTRA_BATTERY, struct.pack(">HB", 3700, 25)),
            extra(EXTRA_HDOP, b"\x08"),
        )
    )


def auth_body() -> bytes:
    encoded = AUTH_CODE.encode("ascii")
    return bytes([len(encoded)]) + encoded + IMEI.encode("ascii") + b"1.0".ljust(20, b"\x00")


def registration_body() -> bytes:
    return b"".join((
        struct.pack(">HH", 51, 5101),
        b"JZG043".ljust(11, b"\x00"),
        b"JZG043A".rjust(30, b"\x00"),
        b"89860064221642652497".ljust(30, b"\x00"),
        b"\x00",
        b"0",
    ))


def attributes_body() -> bytes:
    return b"".join((
        struct.pack(">H", 0),
        b"JZG43",
        b"JZG043A".ljust(30, b"\x00"),
        IMEI.encode().ljust(30, b"\x00"),
        bytes.fromhex("89860064221642652497"),
        b"\x09Air780EGP",
        b"\x0b001.999.014",
        b"\x0f\xa0",
    ))


def test_authentication_is_required_and_ack_follows_persistence() -> None:
    async def scenario() -> None:
        service = FakeService()
        server = TrackerTCPServer(service, TCPSettings())
        writer = FakeWriter()
        state = ConnectionState()

        await server._handle_frame(
            build_frame(MSG_TERMINAL_REGISTER, TERMINAL_PHONE, 7, registration_body()),
            writer,
            state,
            "test",
        )
        register_ack = decode_frame(bytes(writer.output))
        assert register_ack.message_id == MSG_REGISTER_ACK
        assert register_ack.message_serial == 0
        assert register_ack.body == struct.pack(">HB", 7, 0) + AUTH_CODE.encode()
        assert service.registrations[0].terminal_phone == TERMINAL_PHONE

        writer.output.clear()
        await server._handle_frame(
            build_frame(MSG_TERMINAL_AUTH, TERMINAL_PHONE, 1, auth_body()),
            writer,
            state,
            "test",
        )
        auth_ack = decode_frame(bytes(writer.output))
        assert auth_ack.message_id == MSG_PLATFORM_COMMON_ACK
        assert auth_ack.message_serial == 1
        assert auth_ack.body == struct.pack(">HHB", 1, MSG_TERMINAL_AUTH, 0)
        assert state.authenticated is True
        assert state.terminal_phone == TERMINAL_PHONE
        assert state.imei == IMEI

        writer.output.clear()
        await server._handle_frame(
            build_frame(MSG_TERMINAL_HEARTBEAT, TERMINAL_PHONE, 2, b""),
            writer,
            state,
            "test",
        )
        heartbeat_ack = decode_frame(bytes(writer.output))
        assert heartbeat_ack.body == struct.pack(
            ">HHB", 2, MSG_TERMINAL_HEARTBEAT, 0
        )

        writer.output.clear()
        await server._handle_frame(
            build_frame(MSG_TERMINAL_ATTRIBUTES, TERMINAL_PHONE, 3, attributes_body()),
            writer,
            state,
            "test",
        )
        assert service.attributes[0].iccid == "89860064221642652497"
        # 0x0107 is the dedicated response to 0x8107, not a request that
        # triggers an additional platform 0x8001 response.
        assert bytes(writer.output) == b""

        writer.output.clear()
        parameter_body = struct.pack(">HBIBI", 0x2345, 1, 0x00000029, 4, 900)
        await server._handle_frame(
            build_frame(MSG_TERMINAL_PARAMETERS, TERMINAL_PHONE, 4, parameter_body),
            writer,
            state,
            "test",
        )
        # 0x0104 completes the 0x8104/0x8106 exchange by itself.
        assert bytes(writer.output) == b""

        writer.output.clear()
        await server._handle_frame(
            build_frame(MSG_LOCATION, TERMINAL_PHONE, 100, location_body()),
            writer,
            state,
            "test",
        )
        assert len(service.batches) == 1
        assert service.batches[0].imei == IMEI
        assert service.batches[0].records[0].battery_percent == 25
        location_ack = decode_frame(bytes(writer.output))
        assert location_ack.message_serial == 3
        assert location_ack.body == struct.pack(">HHB", 100, MSG_LOCATION, 0)

    asyncio.run(scenario())


def test_location_before_authentication_is_rejected() -> None:
    async def scenario() -> None:
        service = FakeService()
        server = TrackerTCPServer(service, TCPSettings())
        writer = FakeWriter()
        await server._handle_frame(
            build_frame(MSG_LOCATION, TERMINAL_PHONE, 100, location_body()),
            writer,
            ConnectionState(),
            "test",
        )
        assert service.batches == []
        response = decode_frame(bytes(writer.output))
        assert response.body == struct.pack(">HHB", 100, MSG_LOCATION, 1)

    asyncio.run(scenario())


def test_pending_configuration_uses_unique_serial_and_requires_matching_ack() -> None:
    async def scenario() -> None:
        service = FakeService()
        service.credentials[TERMINAL_PHONE] = (AUTH_CODE, IMEI)
        service.pending_configuration = TerminalConfiguration(
            terminal_phone=TERMINAL_PHONE,
            revision=7,
            applied_revision=6,
            report_interval_sec=900,
        )
        server = TrackerTCPServer(service, TCPSettings())
        writer = FakeWriter()
        state = ConnectionState()
        await server._handle_frame(
            build_frame(MSG_TERMINAL_AUTH, TERMINAL_PHONE, 9, auth_body()),
            writer,
            state,
            "test",
        )
        wires = JT808FrameBuffer().feed(bytes(writer.output))
        assert len(wires) == 2
        auth_ack, setting = map(decode_frame, wires)
        assert auth_ack.message_serial == 0
        assert setting.message_id == MSG_SET_PARAMETERS
        assert setting.message_serial == 1

        writer.output.clear()
        terminal_ack = struct.pack(">HHB", setting.message_serial, MSG_SET_PARAMETERS, 0)
        await server._handle_frame(
            build_frame(MSG_TERMINAL_COMMON_ACK, TERMINAL_PHONE, 10, terminal_ack),
            writer,
            state,
            "test",
        )
        assert writer.output == b""
        assert service.confirmations == [(TERMINAL_PHONE, 7, True, None)]

    asyncio.run(scenario())
