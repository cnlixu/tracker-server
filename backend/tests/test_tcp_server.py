"""Tests for TCP framing and per-client report handling."""

import asyncio
from typing import Any

import pytest

from backend.app.config import TCPSettings
from backend.app.models import TrackerReport
from backend.app.tcp_server import (
    ACK_ERROR,
    ACK_OK,
    FrameBuffer,
    FrameTooLargeError,
    TrackerTCPServer,
)


STANDARD_MESSAGE = (
    b"$PTRK,1,862288087606784,150926,094508,A,3045.83496,N,"
    b"10354.09348,E,575.0,0.111,193.85,9,2.21,31,1*69"
)


class FakeService:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.reports: list[TrackerReport] = []

    async def process_report(self, report: TrackerReport) -> int:
        self.reports.append(report)
        if self.error is not None:
            raise self.error
        return 42


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
