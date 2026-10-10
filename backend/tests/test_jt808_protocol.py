"""Cross-check the trimmed JT/T 808-2019 wire profile."""

from datetime import datetime, timezone
import struct

import pytest

from backend.app.jt808_protocol import (
    BODY_MAX,
    EXTRA_BATTERY,
    EXTRA_HDOP,
    EXTRA_SATELLITES,
    EXTRA_SIGNAL,
    MAX_BATCH_RECORDS,
    MSG_LOCATION,
    MSG_LOCATION_BATCH,
    MSG_PLATFORM_COMMON_ACK,
    MSG_SET_PARAMETERS,
    MSG_TERMINAL_ATTRIBUTES,
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
    parse_terminal_attributes,
)
from backend.app.models import TerminalConfiguration


IMEI = "862288087606784"
TERMINAL_PHONE = "13800138000"


def extra(item_id: int, value: bytes) -> bytes:
    return bytes([item_id, len(value)]) + value


def location_body(sequence: int = 100, *, battery_mv: int = 3700) -> bytes:
    status = 0x00040002  # positioned, GPS used, north/east
    base = struct.pack(
        ">IIIIHHH",
        0,
        status,
        30_763_628 + sequence,
        103_901_314,
        516,
        11,  # 1.1 km/h
        153,
    ) + bytes.fromhex("260901163129")  # GMT+8; UTC is 08:31:29
    return b"".join(
        (
            base,
            extra(EXTRA_SIGNAL, bytes([31])),
            extra(EXTRA_SATELLITES, bytes([15])),
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


def test_standard_registration_fields_are_decoded() -> None:
    body = b"".join((
        struct.pack(">HH", 51, 5101),
        b"JZG043".ljust(11, b"\x00"),
        b"JZG043A".rjust(30, b"\x00"),
        b"89860064221642652497".ljust(30, b"\x00"),
        b"\x00",
        b"0",
    ))
    frame = decode_frame(build_frame(MSG_TERMINAL_REGISTER, TERMINAL_PHONE, 7, body))
    registration = parse_registration(frame)

    assert registration.terminal_phone == TERMINAL_PHONE
    assert registration.province_id == 51
    assert registration.city_id == 5101
    assert registration.manufacturer_id == "JZG043"
    assert registration.terminal_model == "JZG043A"
    assert registration.terminal_id == "89860064221642652497"
    assert registration.plate_color == 0
    assert registration.plate_number == "0"


def test_standard_terminal_attributes_are_decoded() -> None:
    body = b"".join((
        struct.pack(">H", 0),
        b"JZG43",
        b"JZG043A".ljust(30, b"\x00"),
        IMEI.encode().ljust(30, b"\x00"),
        bytes.fromhex("89860064221642652497"),
        bytes([len(b"Air780EGP")]), b"Air780EGP",
        bytes([len(b"001.999.014")]), b"001.999.014",
        b"\x0f\xa0",
    ))
    frame = decode_frame(build_frame(MSG_TERMINAL_ATTRIBUTES, TERMINAL_PHONE, 8, body))
    attributes = parse_terminal_attributes(frame)

    assert attributes.terminal_phone == TERMINAL_PHONE
    assert attributes.terminal_type == 0
    assert attributes.manufacturer_id == "JZG43"
    assert attributes.terminal_model == "JZG043A"
    assert attributes.terminal_id == IMEI
    assert attributes.iccid == "89860064221642652497"
    assert attributes.hardware_version == "Air780EGP"
    assert attributes.software_version == "001.999.014"
    assert attributes.gnss_attributes == 0x0F
    assert attributes.communication_attributes == 0xA0


def test_single_location_decodes_standard_and_optional_telemetry_fields() -> None:
    frame = decode_frame(build_frame(MSG_LOCATION, IMEI, 100, location_body()))
    batch = decode_location_message(frame)

    assert batch.imei == IMEI
    assert batch.batch_id == 100
    assert batch.record_count == 1
    record = batch.records[0]
    assert record.gps_time == datetime(2026, 9, 1, 8, 31, 29, tzinfo=timezone.utc)
    assert record.latitude == pytest.approx(30.763728)
    assert record.longitude == pytest.approx(103.901314)
    assert record.altitude == 516
    assert record.battery_mv == 3700
    assert record.battery_percent == 25
    assert record.csq == 31
    assert record.satellites == 15
    assert record.hdop == pytest.approx(0.8)
    assert record.time_valid is None
    assert record.wake_code == 0
    assert len(record.record_fingerprint) == 64


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
    assert batch.records[0].record_fingerprint != batch.records[-1].record_fingerprint


def test_single_offline_record_is_valid_batch_supplement() -> None:
    frame = decode_frame(
        build_frame(MSG_LOCATION_BATCH, IMEI, 612, batch_body(1))
    )
    batch = decode_location_message(frame)

    assert batch.record_count == 1
    assert batch.records[0].battery_mv == 3700


def test_standard_location_without_manufacturer_extensions_is_accepted() -> None:
    body = struct.pack(
        ">IIIIHHH",
        0,
        0x00040002,
        30_763_628,
        103_901_314,
        516,
        11,
        153,
    ) + bytes.fromhex("260901163129")
    frame = decode_frame(build_frame(MSG_LOCATION, IMEI, 101, body))
    record = decode_location_message(frame).records[0]

    assert record.battery_mv is None
    assert record.battery_percent is None
    assert record.csq == 0
    assert record.satellites == 0
    assert record.hdop is None


def test_platform_common_response_echoes_terminal_serial_and_message_id() -> None:
    terminal = decode_frame(build_frame(MSG_LOCATION, IMEI, 321, location_body()))
    response = decode_frame(build_common_response(terminal, 77))
    assert response.message_id == MSG_PLATFORM_COMMON_ACK
    assert response.message_serial == 77
    assert response.body == struct.pack(">HHB", 321, MSG_LOCATION, 0)


def test_set_parameters_contains_only_standard_report_interval() -> None:
    config = TerminalConfiguration(
        terminal_phone=TERMINAL_PHONE,
        revision=9,
        applied_revision=8,
        report_interval_sec=900,
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
    assert actual == {0x0029: struct.pack(">I", 900)}
