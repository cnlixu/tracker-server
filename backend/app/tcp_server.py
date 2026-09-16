"""Async TCP transport for newline-delimited PTRK reports."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from .config import TCPSettings, load_tcp_settings
from .database import close_pool, init_pool
from .models import TrackerReport
from .protocol import ProtocolError, parse_message
from .services import TrackerService


LOGGER = logging.getLogger(__name__)

MAX_FRAME_SIZE = 2048
READ_CHUNK_SIZE = 4096
ACK_OK = b"$ACK,OK\r\n"
ACK_ERROR = b"$ACK,ERROR\r\n"


class FrameTooLargeError(ValueError):
    """Raised when one unterminated or completed frame exceeds its limit."""

    def __init__(
        self,
        message: str,
        completed_frames: tuple[bytes, ...] = (),
    ) -> None:
        super().__init__(message)
        self.completed_frames = completed_frames


class ReportService(Protocol):
    """Service boundary used by the TCP transport."""

    async def process_report(self, report: TrackerReport) -> int:
        """Persist or otherwise process one validated report."""


class FrameBuffer:
    """Incrementally split a byte stream on LF, accepting optional CR."""

    def __init__(self, max_frame_size: int = MAX_FRAME_SIZE) -> None:
        if max_frame_size < 1:
            raise ValueError("max_frame_size must be positive")
        self.max_frame_size = max_frame_size
        self._buffer = bytearray()

    @property
    def buffered_bytes(self) -> int:
        """Return the number of bytes waiting for a line terminator."""
        return len(self._buffer)

    def feed(self, data: bytes) -> list[bytes]:
        """Add bytes and return every complete frame in arrival order."""
        self._buffer.extend(data)
        frames: list[bytes] = []

        while True:
            newline_index = self._buffer.find(b"\n")
            if newline_index < 0:
                if len(self._buffer) > self.max_frame_size:
                    raise FrameTooLargeError(
                        f"unterminated frame exceeds {self.max_frame_size} bytes",
                        tuple(frames),
                    )
                return frames

            frame = bytes(self._buffer[:newline_index])
            del self._buffer[: newline_index + 1]
            if frame.endswith(b"\r"):
                frame = frame[:-1]
            if len(frame) > self.max_frame_size:
                raise FrameTooLargeError(
                    f"frame exceeds {self.max_frame_size} bytes",
                    tuple(frames),
                )
            frames.append(frame)


class TrackerTCPServer:
    """Manage the TCP listener and isolated tasks for tracker clients."""

    def __init__(
        self,
        service: ReportService,
        settings: TCPSettings,
        *,
        max_frame_size: int = MAX_FRAME_SIZE,
    ) -> None:
        self.service = service
        self.settings = settings
        self.max_frame_size = max_frame_size
        self._server: asyncio.Server | None = None
        self._client_tasks: set[asyncio.Task[None]] = set()
        self._client_writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        """Start accepting clients."""
        if self._server is not None:
            raise RuntimeError("TCP server is already running")
        self._server = await asyncio.start_server(
            self._client_connected,
            host=self.settings.host,
            port=self.settings.port,
        )
        LOGGER.info(
            "tracker TCP server listening host=%s port=%s",
            self.settings.host,
            self.settings.port,
        )

    async def serve_forever(self) -> None:
        """Serve until cancelled or explicitly closed."""
        if self._server is None:
            raise RuntimeError("TCP server has not been started")
        await self._server.serve_forever()

    async def close(self) -> None:
        """Stop accepting clients and close every active connection task."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        for writer in tuple(self._client_writers):
            writer.close()
        for task in tuple(self._client_tasks):
            task.cancel()
        if self._client_tasks:
            await asyncio.gather(*self._client_tasks, return_exceptions=True)
        LOGGER.info("tracker TCP server stopped")

    def _client_connected(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._client_writers.add(writer)
        task = asyncio.create_task(self.handle_client(reader, writer))
        self._client_tasks.add(task)
        task.add_done_callback(self._client_tasks.discard)

    async def handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Read, frame, validate and dispatch reports for one client."""
        peer = _format_peer(writer.get_extra_info("peername"))
        frame_buffer = FrameBuffer(self.max_frame_size)
        LOGGER.info("tracker client connected client=%s", peer)

        try:
            while True:
                try:
                    data = await asyncio.wait_for(
                        reader.read(READ_CHUNK_SIZE),
                        timeout=self.settings.read_timeout_seconds,
                    )
                except TimeoutError:
                    LOGGER.info(
                        "tracker client read timeout client=%s timeout_seconds=%s",
                        peer,
                        self.settings.read_timeout_seconds,
                    )
                    break

                if not data:
                    break

                close_after_frames = False
                try:
                    frames = frame_buffer.feed(data)
                except FrameTooLargeError as exc:
                    LOGGER.warning("tracker frame too large client=%s error=%s", peer, exc)
                    frames = list(exc.completed_frames)
                    close_after_frames = True

                for frame in frames:
                    await self._handle_frame(frame, writer, peer)
                if close_after_frames:
                    break
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError) as exc:
            LOGGER.info("tracker client connection error client=%s error=%s", peer, exc)
        except Exception:
            LOGGER.exception("unexpected tracker client error client=%s", peer)
        finally:
            self._client_writers.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            LOGGER.info("tracker client disconnected client=%s", peer)

    async def _handle_frame(
        self,
        frame: bytes,
        writer: asyncio.StreamWriter,
        peer: str,
    ) -> None:
        try:
            message = frame.decode("ascii")
        except UnicodeDecodeError as exc:
            LOGGER.warning("non-ASCII tracker frame client=%s error=%s", peer, exc)
            await _write_ack(writer, ACK_ERROR)
            return

        try:
            report = parse_message(message)
        except ProtocolError as exc:
            LOGGER.warning("tracker protocol error client=%s error=%s", peer, exc)
            await _write_ack(writer, ACK_ERROR)
            return

        LOGGER.info("tracker report received client=%s imei=%s", peer, report.imei)
        try:
            await self.service.process_report(report)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception(
                "tracker service error client=%s imei=%s",
                peer,
                report.imei,
            )
            await _write_ack(writer, ACK_ERROR)
            return

        await _write_ack(writer, ACK_OK)


async def _write_ack(writer: asyncio.StreamWriter, ack: bytes) -> None:
    writer.write(ack)
    await writer.drain()


def _format_peer(peername: object) -> str:
    if isinstance(peername, tuple) and len(peername) >= 2:
        return f"{peername[0]}:{peername[1]}"
    return str(peername or "unknown")


async def run_server() -> None:
    """Run the configured TCP listener with its database pool."""
    tcp_settings = load_tcp_settings()
    pool = await init_pool()
    server = TrackerTCPServer(TrackerService(pool), tcp_settings)
    try:
        await server.start()
        await server.serve_forever()
    finally:
        await server.close()
        await close_pool(pool)


def main() -> None:
    """Command-line entry point for ``python -m backend.app.tcp_server``."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        LOGGER.info("tracker TCP server interrupted")


if __name__ == "__main__":
    main()
