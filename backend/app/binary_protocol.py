"""Codec for the binary PTRK V2 protocol used by the tracker firmware.

The wire format is defined by ``project/tracker/tracker_protocol.lua`` in the
Air780EGP tracker repository. Every multi-byte integer is little endian.

Frame layout::

    A5 5A | CLASS(0x01) | ID(0x01) | LENGTH(uint16) | PAYLOAD | CRC16(uint16)

``LENGTH`` counts the payload bytes only. ``CRC16`` covers CLASS, ID, LENGTH and
PAYLOAD. Payload layout::

    VERSION(1)=2 | DEVICE_ID(8, IMEI BCD) | GENERATION_ID(uint32) |
    BATCH_ID(uint32) | COUNT(uint16) | COUNT * 30-byte position records

Position record layout (30 bytes)::

    0   uint32 sequence          12  int32  longitude x 1e7
    4   uint32 timestamp (UTC s) 16  int16  altitude (m, -32768 invalid)
    8   int32  latitude x 1e7    18  uint16 speed (cm/s)
    20  uint16 course x 100      22  uint16 battery (mV, 0 unknown)
    24  uint8  HDOP x 10 (255 invalid)   25 uint8 satellites
    26  uint8  CSQ               27  uint8 flags
    28  uint16 CRC16 over bytes 0..27

The device only deletes cached records after receiving an acknowledgement that
echoes DEVICE_ID, GENERATION_ID and BATCH_ID and reports ``STATUS_OK`` with
``ACK_COUNT`` equal to ``COUNT``, so the transport layer must answer with
:func:`build_position_ack`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import struct
import time

from .models import TrackerRecord


SYNC1 = 0xA5
SYNC2 = 0x5A
MESSAGE_CLASS_TELEMETRY = 0x01
MESSAGE_ID_POSITION = 0x01
MESSAGE_ID_POSITION_ACK = 0x81
PROTOCOL_VERSION = 2

FRAME_HEADER_SIZE = 6
FRAME_TRAILER_SIZE = 2
FRAME_OVERHEAD = FRAME_HEADER_SIZE + FRAME_TRAILER_SIZE
DEVICE_ID_SIZE = 8
PAYLOAD_HEADER_SIZE = 19
RECORD_SIZE = 30
RECORD_BODY_SIZE = 28
ACK_PAYLOAD_SIZE = 24
MAX_FRAME_PAYLOAD_SIZE = 65_535

STATUS_OK = 0x00
STATUS_SERVER_BUSY = 0x01
STATUS_UNSUPPORTED_VERSION = 0x02
STATUS_BAD_RECORD = 0x03
STATUS_AUTH_FAILED = 0x04
STATUS_STORAGE_FAILED = 0x05

# The tracker retries SERVER_BUSY and STORAGE_FAILED and gives up on the rest.
RETRYABLE_STATUSES = frozenset({STATUS_SERVER_BUSY, STATUS_STORAGE_FAILED})

FLAG_VALID = 0x01
FLAG_TIME_VALID = 0x02
FLAG_WAKE_CODE_SHIFT = 3
FLAG_WAKE_CODE_MASK = 0x07

ALTITUDE_INVALID = -32_768
HDOP_INVALID = 255
BATTERY_UNKNOWN = 0
SPEED_CM_PER_KNOT = 185_200 / 3_600


class BinaryProtocolError(ValueError):
    """Base class for malformed or unsupported binary frames."""

    def __init__(
        self,
        message: str,
        *,
        identity: "FrameIdentity | None" = None,
    ) -> None:
        super().__init__(message)
        self.identity = identity


class FrameFormatError(BinaryProtocolError):
    """Raised when a frame cannot be structurally decoded."""


class FrameChecksumError(BinaryProtocolError):
    """Raised when the frame CRC16 does not match its body."""


class RecordError(BinaryProtocolError):
    """Raised when a contained position record is invalid."""


class UnsupportedVersionError(BinaryProtocolError):
    """Raised for a structurally valid frame with an unknown payload version."""


@dataclass(frozen=True, slots=True)
class FrameIdentity:
    """Identity echoed back in acknowledgements."""

    device_id: bytes
    generation_id: int
    batch_id: int
    version: int


@dataclass(frozen=True, slots=True)
class TrackerBatch:
    """One decoded upload frame."""

    identity: FrameIdentity
    imei: str
    records: tuple[TrackerRecord, ...]
    raw_data: str

    @property
    def version(self) -> int:
        return self.identity.version

    @property
    def device_id(self) -> bytes:
        return self.identity.device_id

    @property
    def generation_id(self) -> int:
        return self.identity.generation_id

    @property
    def batch_id(self) -> int:
        return self.identity.batch_id

    @property
    def record_count(self) -> int:
        return len(self.records)


def crc16_ccitt(data: bytes) -> int:
    """Return CRC16-CCITT (poly 0x1021, init 0xFFFF, no reflection).

    This mirrors the Lua implementation used by ``tracker_protocol.lua``.
    """
    crc = 0xFFFF
    for value in data:
        crc ^= value << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def imei_from_bcd(device_id: bytes) -> str:
    """Convert the 8-byte IMEI BCD field into its decimal representation."""
    if len(device_id) != DEVICE_ID_SIZE:
        raise FrameFormatError("device id must contain 8 bytes")
    digits = device_id.hex().upper().rstrip("F")
    if not digits or not digits.isdecimal():
        raise FrameFormatError(f"device id is not IMEI BCD: {device_id.hex()!r}")
    return digits


def imei_to_bcd(imei: str) -> bytes:
    """Encode a decimal IMEI with trailing ``F`` padding into 8 BCD bytes."""
    digits = "".join(character for character in imei if character.isdecimal())
    if not digits or len(digits) > 16:
        raise FrameFormatError(f"IMEI must contain 1 to 16 digits: {imei!r}")
    padded = digits.ljust(16, "F")
    return bytes(int(padded[index : index + 2], 16) for index in range(0, 16, 2))


def decode_record(raw: bytes) -> TrackerRecord:
    """Decode one 30-byte position record and verify its CRC16."""
    if len(raw) != RECORD_SIZE:
        raise RecordError(f"record must contain {RECORD_SIZE} bytes")
    supplied_crc = int.from_bytes(raw[RECORD_BODY_SIZE:RECORD_SIZE], "little")
    actual_crc = crc16_ccitt(raw[:RECORD_BODY_SIZE])
    if supplied_crc != actual_crc:
        raise RecordError(
            f"record CRC16 mismatch: received 0x{supplied_crc:04X}, "
            f"calculated 0x{actual_crc:04X}"
        )

    (
        sequence,
        timestamp,
        latitude,
        longitude,
        altitude,
        speed,
        course,
        battery_mv,
        hdop,
        satellites,
        csq,
        flags,
    ) = struct.unpack("<IIiiHHHHBBBB", raw[:RECORD_BODY_SIZE])

    valid = bool(flags & FLAG_VALID)
    time_valid = bool(flags & FLAG_TIME_VALID)
    return TrackerRecord(
        sequence=sequence,
        gps_time=datetime.fromtimestamp(timestamp, tz=timezone.utc),
        time_valid=time_valid,
        valid=valid,
        latitude=latitude / 10_000_000 if valid else None,
        longitude=longitude / 10_000_000 if valid else None,
        altitude=(
            None if not valid or altitude == ALTITUDE_INVALID else float(altitude)
        ),
        speed=None if not valid else speed / SPEED_CM_PER_KNOT,
        course=None if not valid else (course % 36_000) / 100,
        satellites=None if not valid else satellites,
        hdop=None if not valid or hdop == HDOP_INVALID else hdop / 10,
        csq=csq,
        battery_mv=None if battery_mv == BATTERY_UNKNOWN else battery_mv,
        wake_code=(flags >> FLAG_WAKE_CODE_SHIFT) & FLAG_WAKE_CODE_MASK,
        raw_data=raw.hex().upper(),
    )


def decode_frame(frame: bytes) -> TrackerBatch:
    """Validate and decode one complete binary upload frame.

    Structural problems raise :class:`FrameFormatError`; once the frame identity
    is available it is attached to the exception so the transport layer can
    answer with the matching status code.
    """
    if len(frame) < FRAME_OVERHEAD:
        raise FrameFormatError("frame is shorter than its header and CRC")
    if frame[0] != SYNC1 or frame[1] != SYNC2:
        raise FrameFormatError(
            f"frame must start with 0x{SYNC1:02X} 0x{SYNC2:02X}"
        )
    if (
        frame[2] != MESSAGE_CLASS_TELEMETRY
        or frame[3] != MESSAGE_ID_POSITION
    ):
        raise FrameFormatError(
            f"unsupported message class/id: 0x{frame[2]:02X}/0x{frame[3]:02X}"
        )

    payload_length = int.from_bytes(frame[4:6], "little")
    expected_length = payload_length + FRAME_OVERHEAD
    if len(frame) != expected_length:
        raise FrameFormatError(
            f"frame length mismatch: length field implies {expected_length} bytes, "
            f"received {len(frame)}"
        )

    body_start = 2
    body_end = FRAME_HEADER_SIZE + payload_length
    checked = frame[body_start:body_end]
    supplied_crc = int.from_bytes(frame[body_end:expected_length], "little")
    actual_crc = crc16_ccitt(checked)
    if supplied_crc != actual_crc:
        raise FrameChecksumError(
            f"frame CRC16 mismatch: received 0x{supplied_crc:04X}, "
            f"calculated 0x{actual_crc:04X}"
        )

    payload = frame[FRAME_HEADER_SIZE:body_end]
    if len(payload) < PAYLOAD_HEADER_SIZE:
        raise FrameFormatError(
            f"payload must contain at least {PAYLOAD_HEADER_SIZE} bytes"
        )

    version = payload[0]
    device_id = payload[1:9]
    generation_id = int.from_bytes(payload[9:13], "little")
    batch_id = int.from_bytes(payload[13:17], "little")
    identity = FrameIdentity(
        device_id=device_id,
        generation_id=generation_id,
        batch_id=batch_id,
        version=version,
    )
    if version != PROTOCOL_VERSION:
        raise UnsupportedVersionError(
            f"unsupported binary protocol version: {version}", identity=identity
        )

    count = int.from_bytes(payload[17:19], "little")
    if count == 0:
        raise FrameFormatError(
            "a frame must contain at least one record", identity=identity
        )
    record_bytes = payload[PAYLOAD_HEADER_SIZE:]
    if len(record_bytes) != count * RECORD_SIZE:
        raise FrameFormatError(
            f"record count {count} does not match {len(record_bytes)} payload bytes",
            identity=identity,
        )

    imei = imei_from_bcd(device_id)
    records: list[TrackerRecord] = []
    for index in range(count):
        start = index * RECORD_SIZE
        raw_record = record_bytes[start : start + RECORD_SIZE]
        try:
            records.append(decode_record(raw_record))
        except RecordError as exc:
            raise RecordError(
                f"record {index + 1}/{count} rejected: {exc}", identity=identity
            ) from exc

    return TrackerBatch(
        identity=identity,
        imei=imei,
        records=tuple(records),
        raw_data=frame.hex().upper(),
    )


def _scaled(value: float | None, scale: float) -> int:
    if value is None:
        raise BinaryProtocolError("measurement must not be None")
    return round(value * scale)


def _as_unsigned(value: int, bits: int, name: str) -> int:
    integer = int(value)
    if not 0 <= integer < (1 << bits):
        raise BinaryProtocolError(f"{name} out of range for {bits} bits: {value!r}")
    return integer


def encode_record(record: TrackerRecord) -> bytes:
    """Encode one position record; the inverse of :func:`decode_record`."""
    valid = bool(record.valid)
    flags = 0
    if valid:
        flags |= FLAG_VALID
    if record.time_valid:
        flags |= FLAG_TIME_VALID
    flags |= (int(record.wake_code) & FLAG_WAKE_CODE_MASK) << FLAG_WAKE_CODE_SHIFT

    body = struct.pack(
        "<IIiiHHHHBBBB",
        _as_unsigned(record.sequence, 32, "sequence"),
        _as_unsigned(int(record.gps_time.timestamp()), 32, "timestamp"),
        _scaled(record.latitude, 10_000_000) if valid else 0,
        _scaled(record.longitude, 10_000_000) if valid else 0,
        (
            _scaled(record.altitude, 1)
            if valid and record.altitude is not None
            else ALTITUDE_INVALID
        ),
        (
            _scaled(record.speed, SPEED_CM_PER_KNOT)
            if valid and record.speed is not None
            else 0
        ),
        _scaled(record.course, 100) if valid and record.course is not None else 0,
        _as_unsigned(record.battery_mv or BATTERY_UNKNOWN, 16, "battery_mv"),
        (
            _scaled(record.hdop, 10)
            if valid and record.hdop is not None
            else HDOP_INVALID
        ),
        (
            _as_unsigned(record.satellites, 8, "satellites")
            if valid and record.satellites is not None
            else 0
        ),
        _as_unsigned(record.csq, 8, "csq"),
        _as_unsigned(flags, 8, "flags"),
    )
    return body + crc16_ccitt(body).to_bytes(2, "little")


def build_position_frame(
    *,
    generation_id: int,
    batch_id: int,
    records: list[bytes] | tuple[bytes, ...],
    imei: str | None = None,
    device_id: bytes | None = None,
    version: int = PROTOCOL_VERSION,
) -> bytes:
    """Assemble an upload frame from encoded records.

    Used by tests and by server-side tooling that replays stored batches.
    """
    if not records:
        raise BinaryProtocolError("a frame must contain at least one record")
    if device_id is None:
        if imei is None:
            raise BinaryProtocolError("imei or device_id is required")
        device_id = imei_to_bcd(imei)

    payload = (
        bytes([version])
        + device_id
        + _as_unsigned(generation_id, 32, "generation_id").to_bytes(4, "little")
        + _as_unsigned(batch_id, 32, "batch_id").to_bytes(4, "little")
        + len(records).to_bytes(2, "little")
        + b"".join(records)
    )
    checked = (
        bytes([MESSAGE_CLASS_TELEMETRY, MESSAGE_ID_POSITION])
        + len(payload).to_bytes(2, "little")
        + payload
    )
    return bytes([SYNC1, SYNC2]) + checked + crc16_ccitt(checked).to_bytes(2, "little")


def build_position_ack(
    *,
    device_id: bytes,
    generation_id: int,
    batch_id: int,
    count: int,
    status: int,
    server_time: int | None = None,
) -> bytes:
    """Build the acknowledgement frame the tracker waits for.

    The tracker only commits its Flash cache for ``status == STATUS_OK`` **and**
    ``count`` equal to the number of uploaded records.
    """
    if len(device_id) != DEVICE_ID_SIZE:
        raise BinaryProtocolError("device id must contain 8 bytes")
    payload = struct.pack(
        "<B8sIIHBI",
        PROTOCOL_VERSION,
        device_id,
        _as_unsigned(generation_id, 32, "generation_id"),
        _as_unsigned(batch_id, 32, "batch_id"),
        _as_unsigned(count, 16, "ack count"),
        _as_unsigned(status, 8, "status"),
        _as_unsigned(
            int(time.time()) if server_time is None else int(server_time),
            32,
            "server_time",
        ),
    )
    checked = (
        bytes([MESSAGE_CLASS_TELEMETRY, MESSAGE_ID_POSITION_ACK])
        + len(payload).to_bytes(2, "little")
        + payload
    )
    return bytes([SYNC1, SYNC2]) + checked + crc16_ccitt(checked).to_bytes(2, "little")


class BinaryFrameBuffer:
    """Incrementally split a byte stream into complete binary frames.

    Bytes before the ``A5 5A`` sync marker are dropped and counted in
    :attr:`dropped_bytes` so the transport layer can log lost synchronisation.
    """

    def __init__(self, max_payload_size: int = MAX_FRAME_PAYLOAD_SIZE) -> None:
        if max_payload_size < PAYLOAD_HEADER_SIZE:
            raise ValueError("max_payload_size is too small")
        self.max_payload_size = max_payload_size
        self.dropped_bytes = 0
        self._buffer = bytearray()

    @property
    def buffered_bytes(self) -> int:
        """Return the number of bytes waiting for a complete frame."""
        return len(self._buffer)

    def feed(self, data: bytes) -> list[bytes]:
        """Add bytes and return every complete frame in arrival order."""
        self._buffer.extend(data)
        frames: list[bytes] = []

        while True:
            if len(self._buffer) < 2:
                return frames
            if self._buffer[0] != SYNC1 or self._buffer[1] != SYNC2:
                del self._buffer[0]
                self.dropped_bytes += 1
                continue
            if len(self._buffer) < FRAME_HEADER_SIZE:
                return frames

            payload_length = int.from_bytes(self._buffer[4:6], "little")
            if (
                payload_length < PAYLOAD_HEADER_SIZE
                or payload_length > self.max_payload_size
            ):
                # An implausible header cannot start a frame; resync from the
                # next byte instead of buffering unbounded garbage.
                del self._buffer[0]
                self.dropped_bytes += 1
                continue

            frame_length = payload_length + FRAME_OVERHEAD
            if len(self._buffer) < frame_length:
                return frames

            frames.append(bytes(self._buffer[:frame_length]))
            del self._buffer[:frame_length]
