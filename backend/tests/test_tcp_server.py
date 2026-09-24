"""Tests for TCP framing and per-client report handling."""

import asyncio
from typing import Any

import pytest

from backend.app.binary_protocol import (
    STATUS_BAD_RECORD,
    STATUS_OK,
    STATUS_STORAGE_FAILED,
    STATUS_UNSUPPORTED_VERSION,
    TrackerBatch,
    build_position_frame,
    crc16_ccitt,
    decode_record,
    encode_record,
    imei_to_bcd,
)
from backend.app.config import TCPSettings
from backend.app.models import TrackerReport
from backend.app.tcp_server import (
    ACK_ERROR,
    ACK_OK,
    FrameBuffer,
    FrameTooLargeError,
    ReportStreamParser,
    TrackerTCPServer,
)


STANDARD_MESSAGE = (
    b"$PTRK,1,862288087606784,150926,094508,A,3045.83496,N,"
    b"10354.09348,E,575.0,0.111,193.85,9,2.21,31,1*69"
)

# ASCII V3 report published by the tracker firmware, terminated by CR/LF.
V3_MESSAGE = (
    b"$PTRK,3,862288087606784,305419896,1788251489,1788251489,010926,083129,"
    b"A,1,3045.81768,N,10354.07883,E,516.2,0.591,152.99,15,0.80,31,3700,1*76"
)

IMEI = "862288087606784"
GENERATION_ID = 0x12345678
BATCH_ID = 1788251489
UPLOAD_FRAME_HEX = (
    "A55A0101310002862288087606784F78563412618D966A0100"
    "618D966A618D966A382856121215EE3D04021E00C33B740E080F1F0BEA51"
    "77FE"
)
RECORD_HEX = "618D966A618D966A382856121215EE3D04021E00C33B740E080F1F0BEA51"


def upload_frame() -> bytes:
    return bytes.fromhex(UPLOAD_FRAME_HEX)


def parse_binary_ack(data: bytes) -> dict[str, Any]:
    """Decode an acknowledgement frame, asserting its wire-level invariants."""
    assert data[0:2] == b"\xa5\x5a"
    assert data[2:4] == bytes([0x01, 0x81])
    payload_length = int.from_bytes(data[4:6], "little")
    assert payload_length == 24
    assert len(data) == payload_length + 8
    assert crc16_ccitt(data[2:30]) == int.from_bytes(data[30:32], "little")
    return {
        "version": data[6],
        "device_id": data[7:15],
        "generation_id": int.from_bytes(data[15:19], "little"),
        "batch_id": int.from_bytes(data[19:23], "little"),
        "count": int.from_bytes(data[23:25], "little"),
        "status": data[25],
        "server_time": int.from_bytes(data[26:30], "little"),
    }


class FakeService:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.reports: list[TrackerReport] = []
        self.batches: list[TrackerBatch] = []

    async def process_report(self, report: TrackerReport) -> int:
        self.reports.append(report)
        if self.error is not None:
            raise self.error
        return 42

    async def process_batch(self, batch: TrackerBatch) -> int:
        self.batches.append(batch)
        if self.error is not None:
            raise self.error
        return batch.record_count


class FakeWriter:
    def __init__(self) -> None:
        self.output = bytearray()
        self.drain_count = 0
        self.closed = False

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        if name == "peername":
            return ("127.0.0.1", 45678)
        return default

    def write(self, data: bytes) -> None:
        self.output.extend(data)

    async def drain(self) -> None:
        self.drain_count += 1

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


async def run_client(
    chunks: list[bytes],
    service: FakeService,
    *,
    timeout: float = 1.0,
    send_eof: bool = True,
    max_frame_size: int = 2048,
) -> FakeWriter:
    reader = asyncio.StreamReader()
    for chunk in chunks:
        reader.feed_data(chunk)
    if send_eof:
        reader.feed_eof()

    writer = FakeWriter()
    server = TrackerTCPServer(
        service,
        TCPSettings(
            host="127.0.0.1",
            port=8686,
            read_timeout_seconds=timeout,
        ),
        max_frame_size=max_frame_size,
    )
    await server.handle_client(reader, writer)  # type: ignore[arg-type]
    return writer


def test_one_read_with_one_complete_frame() -> None:
    buffer = FrameBuffer()
    assert buffer.feed(STANDARD_MESSAGE + b"\n") == [STANDARD_MESSAGE]


def test_frame_split_across_two_reads() -> None:
    buffer = FrameBuffer()
    split_at = len(STANDARD_MESSAGE) // 2

    assert buffer.feed(STANDARD_MESSAGE[:split_at]) == []
    assert buffer.feed(STANDARD_MESSAGE[split_at:] + b"\n") == [STANDARD_MESSAGE]


def test_two_frames_coalesced_in_one_read() -> None:
    buffer = FrameBuffer()
    data = STANDARD_MESSAGE + b"\n" + STANDARD_MESSAGE + b"\n"
    assert buffer.feed(data) == [STANDARD_MESSAGE, STANDARD_MESSAGE]


def test_three_frames_with_mixed_split_and_coalescing() -> None:
    buffer = FrameBuffer()
    first_split = 17
    third_split = 23

    assert buffer.feed(STANDARD_MESSAGE[:first_split]) == []
    frames = buffer.feed(
        STANDARD_MESSAGE[first_split:]
        + b"\r\n"
        + STANDARD_MESSAGE
        + b"\n"
        + STANDARD_MESSAGE[:third_split]
    )
    assert frames == [STANDARD_MESSAGE, STANDARD_MESSAGE]
    assert buffer.feed(STANDARD_MESSAGE[third_split:] + b"\r\n") == [
        STANDARD_MESSAGE
    ]


def test_lf_terminator() -> None:
    assert FrameBuffer().feed(b"frame\n") == [b"frame"]


def test_crlf_terminator() -> None:
    assert FrameBuffer().feed(b"frame\r\n") == [b"frame"]


def test_oversized_unterminated_frame() -> None:
    buffer = FrameBuffer(max_frame_size=8)
    with pytest.raises(FrameTooLargeError, match="unterminated"):
        buffer.feed(b"123456789")


def test_completed_frames_are_retained_before_oversized_tail() -> None:
    buffer = FrameBuffer(max_frame_size=8)

    with pytest.raises(FrameTooLargeError, match="unterminated") as error:
        buffer.feed(b"frame\n123456789")

    assert error.value.completed_frames == (b"frame",)


def test_valid_frame_before_oversized_tail_is_processed_before_disconnect() -> None:
    service = FakeService()
    writer = asyncio.run(
        run_client(
            [STANDARD_MESSAGE + b"\n" + b"x" * (len(STANDARD_MESSAGE) + 1)],
            service,
            max_frame_size=len(STANDARD_MESSAGE),
        )
    )

    assert len(service.reports) == 1
    assert service.reports[0].imei == "862288087606784"
    assert bytes(writer.output) == ACK_OK
    assert writer.closed is True


def test_non_ascii_frame_returns_error_ack_without_service_call() -> None:
    service = FakeService()
    writer = asyncio.run(run_client([b"\xff\n"], service))

    assert service.reports == []
    assert bytes(writer.output) == ACK_ERROR
    assert writer.closed is True


def test_bad_checksum_returns_error_then_connection_accepts_valid_frame() -> None:
    service = FakeService()
    bad_message = STANDARD_MESSAGE[:-2] + b"68"
    writer = asyncio.run(
        run_client([bad_message + b"\n" + STANDARD_MESSAGE + b"\n"], service)
    )

    assert len(service.reports) == 1
    assert service.reports[0].imei == "862288087606784"
    assert bytes(writer.output) == ACK_ERROR + ACK_OK


def test_valid_report_calls_service() -> None:
    service = FakeService()
    asyncio.run(run_client([STANDARD_MESSAGE + b"\r\n"], service))

    assert len(service.reports) == 1
    assert service.reports[0].raw_data == STANDARD_MESSAGE.decode("ascii")


def test_service_exception_returns_error_ack() -> None:
    service = FakeService(RuntimeError("database unavailable"))
    writer = asyncio.run(run_client([STANDARD_MESSAGE + b"\n"], service))

    assert len(service.reports) == 1
    assert bytes(writer.output) == ACK_ERROR


def test_success_ack_is_written_and_drained() -> None:
    service = FakeService()
    writer = asyncio.run(run_client([STANDARD_MESSAGE + b"\n"], service))

    assert bytes(writer.output) == ACK_OK
    assert writer.drain_count == 1


def test_idle_client_is_closed_after_read_timeout() -> None:
    service = FakeService()
    writer = asyncio.run(
        run_client([], service, timeout=0.001, send_eof=False)
    )

    assert service.reports == []
    assert writer.closed is True


def sample_record() -> bytes:
    return encode_record(decode_record(bytes.fromhex(RECORD_HEX)))


def test_stream_parser_selects_the_binary_framer() -> None:
    parser = ReportStreamParser()

    assert parser.feed(b"") == []
    assert parser.feed(upload_frame()) == [upload_frame()]
    assert parser.uses_binary_frames is True
    assert parser.ascii_buffer is None


def test_stream_parser_selects_the_ascii_framer() -> None:
    parser = ReportStreamParser()

    assert parser.feed(STANDARD_MESSAGE + b"\n") == [STANDARD_MESSAGE]
    assert parser.uses_binary_frames is False
    assert parser.ascii_buffer is not None
    assert parser.dropped_binary_bytes == 0


def test_binary_upload_frame_is_persisted_and_acknowledged() -> None:
    service = FakeService()
    writer = asyncio.run(run_client([upload_frame()], service))

    assert len(service.batches) == 1
    batch = service.batches[0]
    assert batch.imei == IMEI
    assert batch.generation_id == GENERATION_ID
    assert batch.batch_id == BATCH_ID
    assert batch.record_count == 1

    ack = parse_binary_ack(bytes(writer.output))
    assert ack["version"] == 2
    assert ack["status"] == STATUS_OK
    assert ack["count"] == 1
    assert ack["device_id"] == imei_to_bcd(IMEI)
    assert ack["generation_id"] == GENERATION_ID
    assert ack["batch_id"] == BATCH_ID
    assert writer.closed is True


def test_binary_frame_split_across_reads_is_reassembled() -> None:
    service = FakeService()
    frame = upload_frame()
    writer = asyncio.run(run_client([frame[:19], frame[19:]], service))

    assert len(service.batches) == 1
    assert parse_binary_ack(bytes(writer.output))["count"] == 1


def test_binary_multi_record_batch_reports_its_count() -> None:
    service = FakeService()
    frame = build_position_frame(
        imei=IMEI,
        generation_id=GENERATION_ID,
        batch_id=BATCH_ID,
        records=[sample_record(), sample_record()],
    )
    writer = asyncio.run(run_client([frame], service))

    assert service.batches[0].record_count == 2
    assert parse_binary_ack(bytes(writer.output))["count"] == 2


def test_binary_frame_with_bad_crc_is_not_acknowledged() -> None:
    service = FakeService()
    frame = bytearray(upload_frame())
    frame[20] ^= 0xFF
    writer = asyncio.run(run_client([bytes(frame)], service))

    assert service.batches == []
    assert bytes(writer.output) == b""
    assert writer.closed is True


def test_binary_frame_after_leading_junk_is_still_processed() -> None:
    service = FakeService()
    junk = b"\xa5\x5a\x01\x01\x00\x00"
    writer = asyncio.run(run_client([junk + upload_frame()], service))

    assert len(service.batches) == 1
    assert parse_binary_ack(bytes(writer.output))["status"] == STATUS_OK


def test_binary_unsupported_version_returns_status_ack() -> None:
    service = FakeService()
    frame = build_position_frame(
        imei=IMEI,
        generation_id=GENERATION_ID,
        batch_id=BATCH_ID,
        records=[sample_record()],
        version=3,
    )
    writer = asyncio.run(run_client([frame], service))

    assert service.batches == []
    ack = parse_binary_ack(bytes(writer.output))
    assert ack["status"] == STATUS_UNSUPPORTED_VERSION
    assert ack["count"] == 0
    assert ack["batch_id"] == BATCH_ID


def test_binary_bad_record_returns_status_ack() -> None:
    service = FakeService()
    record = bytearray(bytes.fromhex(RECORD_HEX))
    record[8] ^= 0x01
    frame = build_position_frame(
        imei=IMEI,
        generation_id=GENERATION_ID,
        batch_id=BATCH_ID,
        records=[bytes(record)],
    )
    writer = asyncio.run(run_client([frame], service))

    assert service.batches == []
    ack = parse_binary_ack(bytes(writer.output))
    assert ack["status"] == STATUS_BAD_RECORD
    assert ack["count"] == 0


def test_binary_service_error_returns_retryable_status_ack() -> None:
    service = FakeService(RuntimeError("database unavailable"))
    writer = asyncio.run(run_client([upload_frame()], service))

    assert len(service.batches) == 1
    ack = parse_binary_ack(bytes(writer.output))
    assert ack["status"] == STATUS_STORAGE_FAILED
    assert ack["count"] == 0


def test_v3_ascii_report_is_passed_to_the_service() -> None:
    service = FakeService()
    writer = asyncio.run(run_client([V3_MESSAGE + b"\r\n"], service))

    assert len(service.reports) == 1
    assert service.reports[0].protocol_version == 3
    assert service.reports[0].record_sequence == BATCH_ID
    assert service.reports[0].battery_mv == 3700
    assert bytes(writer.output) == ACK_OK

