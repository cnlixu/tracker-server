"""Async TCP server for the trimmed JT/T 808-2019 tracker profile."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from typing import Protocol

import asyncpg

from .config import TCPSettings, load_tcp_settings
from .database import close_pool, init_pool
from .jt808_protocol import (
    MSG_LOCATION,
    MSG_LOCATION_BATCH,
    MSG_TERMINAL_AUTH,
    MSG_TERMINAL_REGISTER,
    FrameError,
    JT808Frame,
    JT808FrameBuffer,
    MessageError,
    MessageReassembler,
    TrackerBatch,
    build_common_response,
    build_registration_response,
    decode_frame,
    decode_location_message,
    parse_authentication,
    parse_registration,
)
from .services import TrackerService


LOGGER = logging.getLogger(__name__)
READ_CHUNK_SIZE = 4096


class ReportService(Protocol):
    async def process_batch(self, batch: TrackerBatch) -> int:
        """Persist one decoded location message atomically."""


@dataclass(slots=True)
class ConnectionState:
    reassembler: MessageReassembler = field(default_factory=MessageReassembler)
    authenticated: bool = False
    imei: str | None = None


class TrackerTCPServer:
    def __init__(
        self,
        service: ReportService,
        settings: TCPSettings,
        *,
        max_frame_size: int = 4096,
    ) -> None:
        self.service = service
        self.settings = settings
        self.max_frame_size = max_frame_size
        self._server: asyncio.Server | None = None
        self._client_tasks: set[asyncio.Task[None]] = set()
        self._client_writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("tracker TCP server is already running")
        self._server = await asyncio.start_server(
            self._client_connected,
            host=self.settings.host,
            port=self.settings.port,
        )
        LOGGER.info(
            "JT/T 808 TCP server listening host=%s port=%s",
            self.settings.host,
            self.settings.port,
        )

    async def serve_forever(self) -> None:
        if self._server is None:
            raise RuntimeError("tracker TCP server has not been started")
        await self._server.serve_forever()

    async def close(self) -> None:
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
        peer = _format_peer(writer.get_extra_info("peername"))
        parser = JT808FrameBuffer(self.max_frame_size)
        state = ConnectionState()
        LOGGER.info("tracker connected client=%s", peer)
        try:
            while True:
                try:
                    data = await asyncio.wait_for(
                        reader.read(READ_CHUNK_SIZE),
                        timeout=self.settings.read_timeout_seconds,
                    )
                except TimeoutError:
                    LOGGER.info("tracker read timeout client=%s", peer)
                    break
                if not data:
                    break
                for raw_frame in parser.feed(data):
                    await self._handle_frame(raw_frame, writer, state, peer)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError) as exc:
            LOGGER.info("tracker connection error client=%s error=%s", peer, exc)
        except Exception:
            LOGGER.exception("unexpected tracker error client=%s", peer)
        finally:
            self._client_writers.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            LOGGER.info("tracker disconnected client=%s", peer)

    async def _handle_frame(
        self,
        raw_frame: bytes,
        writer: asyncio.StreamWriter,
        state: ConnectionState,
        peer: str,
    ) -> None:
        try:
            package = decode_frame(raw_frame)
            frame = state.reassembler.push(package)
        except FrameError as exc:
            # Header/checksum cannot be trusted, therefore no response is sent.
            LOGGER.warning("invalid JT/T 808 frame client=%s error=%s", peer, exc)
            return
        if frame is None:
            return

        if frame.message_id == MSG_TERMINAL_REGISTER:
            await self._handle_registration(frame, writer, peer)
            return
        if frame.message_id == MSG_TERMINAL_AUTH:
            await self._handle_authentication(frame, writer, state, peer)
            return
        if not state.authenticated or state.imei != frame.terminal_phone:
            LOGGER.warning("unauthenticated JT/T 808 message client=%s", peer)
            await _write(writer, build_common_response(frame, result=1))
            return
        if frame.message_id not in (MSG_LOCATION, MSG_LOCATION_BATCH):
            LOGGER.warning(
                "unsupported JT/T 808 message client=%s message_id=0x%04X",
                peer,
                frame.message_id,
            )
            await _write(writer, build_common_response(frame, result=3))
            return

        try:
            batch = decode_location_message(frame)
        except MessageError as exc:
            LOGGER.warning("invalid location message client=%s error=%s", peer, exc)
            await _write(writer, build_common_response(frame, result=2))
            return
        try:
            inserted = await self.service.process_batch(batch)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception(
                "location persistence failed client=%s imei=%s generation=%s batch=%s",
                peer,
                batch.imei,
                batch.generation_id,
                batch.batch_id,
            )
            await _write(writer, build_common_response(frame, result=1))
            return
        LOGGER.info(
            "location persisted client=%s imei=%s records=%s inserted=%s",
            peer,
            batch.imei,
            batch.record_count,
            inserted,
        )
        # ACK is emitted only after the database transaction has committed.
        await _write(writer, build_common_response(frame, result=0))

    async def _handle_registration(
        self,
        frame: JT808Frame,
        writer: asyncio.StreamWriter,
        peer: str,
    ) -> None:
        try:
            parse_registration(frame)
        except MessageError as exc:
            LOGGER.warning("invalid registration client=%s error=%s", peer, exc)
            await _write(
                writer,
                build_registration_response(frame, self.settings.auth_code, result=4),
            )
            return
        await _write(
            writer,
            build_registration_response(frame, self.settings.auth_code, result=0),
        )

    async def _handle_authentication(
        self,
        frame: JT808Frame,
        writer: asyncio.StreamWriter,
        state: ConnectionState,
        peer: str,
    ) -> None:
        try:
            auth_code, imei = parse_authentication(frame)
        except (MessageError, UnicodeError) as exc:
            LOGGER.warning("invalid authentication client=%s error=%s", peer, exc)
            await _write(writer, build_common_response(frame, result=2))
            return
        accepted = auth_code == self.settings.auth_code and imei == frame.terminal_phone
        if accepted:
            state.authenticated = True
            state.imei = imei
        LOGGER.info("tracker authentication client=%s imei=%s ok=%s", peer, imei, accepted)
        await _write(writer, build_common_response(frame, result=0 if accepted else 1))


async def _write(writer: asyncio.StreamWriter, data: bytes) -> None:
    writer.write(data)
    await writer.drain()


def _format_peer(peername: object) -> str:
    if isinstance(peername, tuple) and len(peername) >= 2:
        return f"{peername[0]}:{peername[1]}"
    return str(peername or "unknown")


async def run_server() -> None:
    tcp_settings = load_tcp_settings()
    pool: asyncpg.Pool = await init_pool()
    server = TrackerTCPServer(TrackerService(pool), tcp_settings)
    try:
        await server.start()
        await server.serve_forever()
    finally:
        await server.close()
        await close_pool(pool)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        LOGGER.info("tracker TCP server interrupted")
