"""Tests for the binary PTRK V2 codec.

The sample frames are the byte sequences documented by the tracker firmware in
``project/tracker/README.md``, so these tests fail if the server drifts from the
device implementation.
"""

from datetime import datetime, timezone

import pytest

from backend.app.binary_protocol import (
    MAX_FRAME_PAYLOAD_SIZE,
    RETRYABLE_STATUSES,
    STATUS_BAD_RECORD,
    STATUS_OK,
    STATUS_SERVER_BUSY,
    STATUS_STORAGE_FAILED,
    STATUS_UNSUPPORTED_VERSION,
    BinaryFrameBuffer,
    BinaryProtocolError,
    FrameChecksumError,
    FrameFormatError,
    RecordError,
    UnsupportedVersionError,
    build_position_ack,
    build_position_frame,
    crc16_ccitt,
    decode_frame,
    decode_record,
    encode_record,
    imei_from_bcd,
    imei_to_bcd,
)


IMEI = "862288087606784"
GENERATION_ID = 0x12345678
BATCH_ID = 1788251489

# One-record upload frame: 57 bytes, payload length 0x0031 = 49.
UPLOAD_FRAME_HEX = (
    "A55A0101310002862288087606784F78563412618D966A0100"
    "618D966A618D966A382856121215EE3D04021E00C33B740E080F1F0BEA51"
    "77FE"
)
RECORD_HEX = "618D966A618D966A382856121215EE3D04021E00C33B740E080F1F0BEA51"

# Matching success acknowledgement: 32 bytes, payload length 24.
ACK_HEX = "A55A0181180002862288087606784F78563412618D966A010000618D966A4296"


def upload_frame() -> bytes:
    return bytes.fromhex(UPLOAD_FRAME_HEX)


def record_bytes() -> bytes:
    return bytes.fromhex(RECORD_HEX)


def sample_record() -> bytes:
    return encode_record(decode_record(record_bytes()))


def test_crc16_matches_the_standard_ccitt_check_value() -> None:
    assert crc16_ccitt(b"") == 0xFFFF
    assert crc16_ccitt(b"123456789") == 0x29B1


def test_frame_and_ack_sample_crcs_match_the_firmware() -> None:
    frame = upload_frame()
    payload_length = int.from_bytes(frame[4:6], "little")
    body_end = 6 + payload_length
    assert crc16_ccitt(frame[2:body_end]) == int.from_bytes(
        frame[body_end : body_end + 2], "little"
    )
    assert crc16_ccitt(frame[2:body_end]) == 0xFE77

    ack = bytes.fromhex(ACK_HEX)
    assert crc16_ccitt(ack[2:30]) == int.from_bytes(ack[30:32], "little")
    assert crc16_ccitt(ack[2:30]) == 0x9642


def test_imei_bcd_round_trip() -> None:
    assert imei_to_bcd(IMEI).hex().upper() == "862288087606784F"
    assert imei_from_bcd(imei_to_bcd(IMEI)) == IMEI


def test_decode_upload_frame_from_the_firmware_readme() -> None:
    batch = decode_frame(upload_frame())

    assert batch.imei == IMEI
    assert batch.device_id == imei_to_bcd(IMEI)
    assert batch.version == 2
    assert batch.generation_id == GENERATION_ID
    assert batch.batch_id == BATCH_ID
    assert batch.record_count == 1
    assert batch.raw_data == UPLOAD_FRAME_HEX

    record = batch.records[0]
    assert record.sequence == BATCH_ID
    assert record.gps_time == datetime.fromtimestamp(BATCH_ID, tz=timezone.utc)
    assert record.time_valid is True
    assert record.valid is True
    assert record.latitude == pytest.approx(30.763628)
    assert record.longitude == pytest.approx(103.9013138)
    assert record.altitude == 516.0
    assert record.speed == pytest.approx(30 / (185_200 / 3_600))
    assert record.course == pytest.approx(152.99)
    assert record.satellites == 15
    assert record.hdop == pytest.approx(0.8)
    assert record.csq == 31
    assert record.battery_mv == 3700
    assert record.wake_code == 1
    assert record.raw_data == RECORD_HEX


def test_record_encoding_round_trips() -> None:
    assert sample_record() == record_bytes()
    assert decode_record(sample_record()) == decode_record(record_bytes())


def test_encode_frame_round_trip() -> None:
    frame = build_position_frame(
        imei=IMEI,
        generation_id=GENERATION_ID,
        batch_id=BATCH_ID,
        records=[sample_record()],
    )
    assert frame == upload_frame()


def test_build_position_ack_matches_the_firmware_expectation() -> None:
    ack = build_position_ack(
        device_id=imei_to_bcd(IMEI),
        generation_id=GENERATION_ID,
        batch_id=BATCH_ID,
        count=1,
        status=STATUS_OK,
        server_time=BATCH_ID,
    )
    assert ack == bytes.fromhex(ACK_HEX)
    assert len(ack) == 32


def test_build_position_ack_rejects_unknown_device_id_length() -> None:
    with pytest.raises(BinaryProtocolError, match="8 bytes"):
        build_position_ack(
            device_id=b"\x01",
            generation_id=1,
            batch_id=1,
            count=0,
            status=STATUS_OK,
        )


def test_multi_record_frame_decodes_every_record() -> None:
    frame = build_position_frame(
        imei=IMEI,
        generation_id=GENERATION_ID,
        batch_id=BATCH_ID,
        records=[sample_record(), record_bytes()],
    )
    batch = decode_frame(frame)
    assert batch.record_count == 2
    assert batch.records[0] == batch.records[1]


def test_frame_crc_mismatch_is_rejected() -> None:
    frame = bytearray(upload_frame())
    frame[20] ^= 0xFF
    with pytest.raises(FrameChecksumError, match="frame CRC16 mismatch"):
        decode_frame(bytes(frame))


def test_record_crc_mismatch_is_rejected_with_identity() -> None:
    record = bytearray(record_bytes())
    record[8] ^= 0x01
    frame = build_position_frame(
        imei=IMEI,
        generation_id=GENERATION_ID,
        batch_id=BATCH_ID,
        records=[bytes(record)],
    )
    with pytest.raises(RecordError, match="record 1/1 rejected") as error:
        decode_frame(frame)
    assert error.value.identity is not None
    assert error.value.identity.generation_id == GENERATION_ID


def test_truncated_frame_is_rejected() -> None:
    with pytest.raises(FrameFormatError, match="frame length mismatch"):
        decode_frame(upload_frame()[:-1])


def test_wrong_sync_marker_is_rejected() -> None:
    frame = bytearray(upload_frame())
    frame[0] = 0x00
    with pytest.raises(FrameFormatError, match="must start with"):
        decode_frame(bytes(frame))


def test_unsupported_payload_version_is_rejected_with_identity() -> None:
    frame = build_position_frame(
        imei=IMEI,
        generation_id=GENERATION_ID,
        batch_id=BATCH_ID,
        records=[sample_record()],
        version=3,
    )
    with pytest.raises(UnsupportedVersionError) as error:
        decode_frame(frame)
    assert error.value.identity is not None
    assert error.value.identity.version == 3
    assert error.value.identity.batch_id == BATCH_ID


def test_empty_batch_is_rejected() -> None:
    payload = bytes([2]) + imei_to_bcd(IMEI) + (1).to_bytes(4, "little")
    payload += (1).to_bytes(4, "little") + (0).to_bytes(2, "little")
    body = bytes([0x01, 0x01]) + len(payload).to_bytes(2, "little") + payload
    frame = bytes([0xA5, 0x5A]) + body + crc16_ccitt(body).to_bytes(2, "little")

    with pytest.raises(FrameFormatError, match="at least one record"):
        decode_frame(frame)


def test_retryable_statuses_match_the_firmware() -> None:
    assert RETRYABLE_STATUSES == {STATUS_SERVER_BUSY, STATUS_STORAGE_FAILED}
    assert STATUS_BAD_RECORD not in RETRYABLE_STATUSES
    assert STATUS_UNSUPPORTED_VERSION not in RETRYABLE_STATUSES


def test_frame_buffer_accepts_one_frame_in_one_read() -> None:
    buffer = BinaryFrameBuffer()
    assert buffer.feed(upload_frame()) == [upload_frame()]
    assert buffer.buffered_bytes == 0
    assert buffer.dropped_bytes == 0


def test_frame_buffer_reassembles_split_frames() -> None:
    buffer = BinaryFrameBuffer()
    frame = upload_frame()
    assert buffer.feed(frame[:10]) == []
    assert buffer.buffered_bytes == 10
    assert buffer.feed(frame[10:]) == [frame]


def test_frame_buffer_splits_coalesced_frames() -> None:
    buffer = BinaryFrameBuffer()
    frame = upload_frame()
    assert buffer.feed(frame + frame) == [frame, frame]


def test_frame_buffer_resynchronises_after_leading_junk() -> None:
    buffer = BinaryFrameBuffer()
    frame = upload_frame()
    frames = buffer.feed(b"\x00\x11\x22" + frame)

    assert frames == [frame]
    assert buffer.dropped_bytes == 3


def test_frame_buffer_resynchronises_after_an_implausible_header() -> None:
    buffer = BinaryFrameBuffer()
    frame = upload_frame()
    # A sync marker followed by a length field below the payload header size.
    junk = b"\xa5\x5a\x01\x01\x00\x00"
    frames = buffer.feed(junk + frame)

    assert frames == [frame]
    assert buffer.dropped_bytes == len(junk)


def test_frame_buffer_rejects_an_oversized_payload_header() -> None:
    buffer = BinaryFrameBuffer(max_payload_size=64)
    frame = upload_frame()
    oversized = b"\xa5\x5a\x01\x01" + (1024).to_bytes(2, "little")

    assert buffer.feed(oversized + frame) == [frame]
    assert buffer.dropped_bytes == len(oversized)


def test_default_binary_payload_limit_matches_the_protocol() -> None:
    assert MAX_FRAME_PAYLOAD_SIZE == 65_535
    assert BinaryFrameBuffer().max_payload_size == MAX_FRAME_PAYLOAD_SIZE
