"""Cross-check the trimmed JT/T 808-2019 wire profile."""

from datetime import datetime, timezone
import struct

import pytest

from backend.app.jt808_protocol import (
    BODY_MAX,
    EXTRA_BATTERY,
    EXTRA_HDOP,
    EXTRA_RECORD_ID,
    EXTRA_SATELLITES,
    EXTRA_SIGNAL,
    MAX_BATCH_RECORDS,
    MSG_LOCATION,
    MSG_LOCATION_BATCH,
    MSG_PLATFORM_COMMON_ACK,
    MSG_SET_PARAMETERS,
    MSG_TERMINAL_REGISTER,
    FrameError,
    JT808FrameBuffer,
    MessageReassembler,
    build_common_response,
    build_frame,
    build_set_parameters,
    configuration_parameter_values,
    decode_frame,
    decode_location_message,
    parse_registration,
)
from backend.app.models import TerminalConfiguration


IMEI = "862288087606784"
GENERATION = 0x12345678
TERMINAL_PHONE = "13800138000"


def extra(item_id: int, value: bytes) -> bytes:
    return bytes([item_id, len(value)]) + value


def location_body(sequence: int = 100, *, battery_mv: int = 3700) -> bytes:
    status = 0x00040002  # positioned, GPS used, north/east
    base = struct.pack(
        ">IIIIHHH",
        0,
        status,
        30_763_628,
        103_901_314,
        516,
        11,  # 1.1 km/h
        153,
    ) + bytes.fromhex("260901163129")  # GMT+8; UTC is 08:31:29
    flags = 0x03  # trusted time, timer wake
    return b"".join(
        (
            base,
            extra(EXTRA_SIGNAL, bytes([31])),
            extra(EXTRA_SATELLITES, bytes([15])),
            extra(EXTRA_RECORD_ID, struct.pack(">BII B", 1, GENERATION, sequence, flags)),
            extra(EXTRA_BATTERY, struct.pack(">HB", battery_mv, 25)),
            extra(EXTRA_HDOP, bytes([8])),
        )
    )


def batch_body(count: int) -> bytes:
    items = [struct.pack(">HB", count, 1)]
    for index in range(count):
        body = location_body(100 + index)
        items.append(struct.pack(">H", len(body)) + body)
    return b"".join(items)


def packaged_frames(message_id: int, serial: int, body: bytes) -> list[bytes]:
    chunks = [body[offset : offset + BODY_MAX] for offset in range(0, len(body), BODY_MAX)]
    return [
        build_frame(
            message_id,
            IMEI,
            serial,
            chunk,
            package_total=len(chunks),
            package_index=index,
        )
        for index, chunk in enumerate(chunks, 1)
    ]


def registration_item(item_id: int, value: bytes) -> bytes:
    return bytes([item_id, len(value)]) + value


def test_eptr_registration_fields_are_decoded() -> None:
    items = (
        registration_item(0x01, b"SP-001"),
        registration_item(0x02, "资产-01".encode()),
        registration_item(0x03, b"JZG043A"),
        registration_item(0x04, b"SN0001"),
        registration_item(0x05, IMEI.encode()),
        registration_item(0x06, b"89860064221642652497"),
        registration_item(0x07, b"Air780EGP"),
        registration_item(0x08, b"001.999.014"),
        registration_item(0x09, b"ORG-01"),
        registration_item(0x0A, b"202610"),
        registration_item(0x0B, struct.pack(">I", 3)),
    )
    body = b"".join((
        struct.pack(">HH", 51, 5101),
        b"JZG43".ljust(11, b"\x00"),
        b"EPTR",
        struct.pack(">BBIIB", 1, 4, 0x135F, 300, len(items)),
        *items,
    ))
    frame = decode_frame(build_frame(MSG_TERMINAL_REGISTER, TERMINAL_PHONE, 7, body))
    registration = parse_registration(frame)

    assert registration.terminal_phone == TERMINAL_PHONE
    assert registration.province_id == 51
    assert registration.city_id == 5101
    assert registration.manufacturer_id == "JZG43"
    assert registration.terminal_model == "JZG043A"
    assert registration.terminal_sn == "SN0001"
    assert registration.imei == IMEI
    assert registration.iccid == "89860064221642652497"
    assert registration.report_interval_sec == 300
    assert registration.profile_revision == 3
    assert registration.asset_id == "资产-01"


def test_single_location_decodes_standard_and_custom_fields() -> None:
    frame = decode_frame(build_frame(MSG_LOCATION, IMEI, 100, location_body()))
    batch = decode_location_message(frame)

    assert batch.imei == IMEI
    assert batch.generation_id == GENERATION
    assert batch.batch_id == 100
    assert batch.record_count == 1
    record = batch.records[0]
    assert record.gps_time == datetime(2026, 9, 1, 8, 31, 29, tzinfo=timezone.utc)
    assert record.latitude == pytest.approx(30.763628)
    assert record.longitude == pytest.approx(103.901314)
    assert record.altitude == 516
    assert record.battery_mv == 3700
    assert record.battery_percent == 25
    assert record.csq == 31
    assert record.satellites == 15
    assert record.hdop == pytest.approx(0.8)
    assert record.time_valid is True
    assert record.wake_code == 1


def test_escape_and_stream_fragmentation_round_trip() -> None:
    wire = build_frame(0x0900, IMEI, 0x7E7D, b"\x7d\x7e")
    assert b"\x7d\x01" in wire and b"\x7d\x02" in wire
    parser = JT808FrameBuffer()
    assert parser.feed(wire[:5]) == []
    assert parser.feed(wire[5:]) == [wire]
    decoded = decode_frame(wire)
    assert decoded.message_serial == 0x7E7D
    assert decoded.body == b"\x7d\x7e"


def test_checksum_corruption_is_rejected() -> None:
    wire = bytearray(build_frame(MSG_LOCATION, IMEI, 1, location_body()))
    wire[10] ^= 0x01
    with pytest.raises(FrameError, match="checksum"):
        decode_frame(bytes(wire))


def test_512_record_batch_is_standard_subpackaged_and_reassembled() -> None:
    body = batch_body(MAX_BATCH_RECORDS)
    wires = packaged_frames(MSG_LOCATION_BATCH, 611, body)
    assert len(wires) > 1
    reassembler = MessageReassembler()
    complete = None
    for wire in wires:
        complete = reassembler.push(decode_frame(wire))
    assert complete is not None
    batch = decode_location_message(complete)
    assert batch.record_count == MAX_BATCH_RECORDS
    assert batch.records[0].sequence == 100
    assert batch.records[-1].sequence == 611


def test_platform_common_response_echoes_terminal_serial_and_message_id() -> None:
    terminal = decode_frame(build_frame(MSG_LOCATION, IMEI, 321, location_body()))
    response = decode_frame(build_common_response(terminal, 77))
    assert response.message_id == MSG_PLATFORM_COMMON_ACK
    assert response.message_serial == 77
    assert response.body == struct.pack(">HHB", 321, MSG_LOCATION, 0)


def test_set_parameters_contains_complete_versioned_profile() -> None:
    config = TerminalConfiguration(
        terminal_phone=TERMINAL_PHONE,
        revision=9,
        applied_revision=8,
        province_id=43,
        city_id=4301,
        terminal_type=4,
        report_interval_sec=900,
        service_point_id="SP-001",
        asset_id="资产-001",
        organization_code="ORG-01",
    )
    frame = decode_frame(build_set_parameters(config, 0x2345))
    assert frame.message_id == MSG_SET_PARAMETERS
    assert frame.message_serial == 0x2345
    expected = configuration_parameter_values(config)
    count = frame.body[0]
    offset = 1
    actual = {}
    for _ in range(count):
        parameter_id = int.from_bytes(frame.body[offset : offset + 4], "big")
        length = frame.body[offset + 4]
        offset += 5
        actual[parameter_id] = frame.body[offset : offset + length]
        offset += length
    assert offset == len(frame.body)
    assert actual == expected
