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
    FrameError,
    JT808FrameBuffer,
    MessageReassembler,
    build_common_response,
    build_frame,
    decode_frame,
    decode_location_message,
)


IMEI = "862288087606784"
GENERATION = 0x12345678


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
    response = decode_frame(build_common_response(terminal))
    assert response.message_id == MSG_PLATFORM_COMMON_ACK
    assert response.body == struct.pack(">HHB", 321, MSG_LOCATION, 0)
