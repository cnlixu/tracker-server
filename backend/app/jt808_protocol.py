"""JT/T 808-2019 codec for the trimmed power-tracker profile.

Supported terminal messages are registration (0x0100), authentication
(0x0102), one location (0x0200), and batched blind-area locations (0x0704).
The platform emits registration response (0x8100) and common response
(0x8001). Other JT/T 808 functions are intentionally outside this product.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from .models import TrackerRecord


FLAG = 0x7E
ESCAPE = 0x7D
PROTOCOL_VERSION = 0x01
BODY_MAX = 0x03FF
VERSION_FLAG = 0x4000
SUBPACKAGE_FLAG = 0x2000
MAX_BATCH_RECORDS = 512

MSG_TERMINAL_REGISTER = 0x0100
MSG_TERMINAL_AUTH = 0x0102
MSG_LOCATION = 0x0200
MSG_LOCATION_BATCH = 0x0704
MSG_PLATFORM_COMMON_ACK = 0x8001
MSG_REGISTER_ACK = 0x8100

EXTRA_SIGNAL = 0x30
EXTRA_SATELLITES = 0x31
EXTRA_RECORD_ID = 0xE0
EXTRA_BATTERY = 0xE1
EXTRA_HDOP = 0xE2


class JT808Error(ValueError):
    """Base class for malformed or unsupported JT/T 808 input."""


class FrameError(JT808Error):
    """A frame boundary, escape, length, or checksum is invalid."""


class MessageError(JT808Error):
    """A supported message has an invalid body."""


@dataclass(frozen=True, slots=True)
class JT808Frame:
    message_id: int
    terminal_phone: str
    message_serial: int
    body: bytes
    package_total: int | None = None
    package_index: int | None = None
    raw_data: str = ""


@dataclass(frozen=True, slots=True)
class TrackerBatch:
    imei: str
    generation_id: int
    batch_id: int
    records: tuple[TrackerRecord, ...]
    message_id: int
    message_serial: int
    raw_data: str

    @property
    def record_count(self) -> int:
        return len(self.records)


def xor_checksum(data: bytes) -> int:
    value = 0
    for byte in data:
        value ^= byte
    return value


def _escape(data: bytes) -> bytes:
    return data.replace(b"\x7d", b"\x7d\x01").replace(b"\x7e", b"\x7d\x02")


def _unescape(data: bytes) -> bytes:
    result = bytearray()
    index = 0
    while index < len(data):
        value = data[index]
        if value != ESCAPE:
            result.append(value)
            index += 1
            continue
        if index + 1 >= len(data):
            raise FrameError("truncated JT/T 808 escape")
        following = data[index + 1]
        if following == 0x01:
            result.append(ESCAPE)
        elif following == 0x02:
            result.append(FLAG)
        else:
            raise FrameError("invalid JT/T 808 escape")
        index += 2
    return bytes(result)


def _phone_to_bcd(phone: str) -> bytes:
    if not phone.isascii() or not phone.isdecimal() or len(phone) > 20:
        raise MessageError("terminal phone must contain at most 20 decimal digits")
    text = phone.zfill(20)
    return bytes(int(text[index : index + 2], 16) for index in range(0, 20, 2))


def _phone_from_bcd(value: bytes) -> str:
    if len(value) != 10:
        raise FrameError("2019 terminal phone must contain 10 BCD bytes")
    text = "".join(f"{byte:02X}" for byte in value)
    if not text.isdecimal():
        raise FrameError("terminal phone contains non-decimal BCD")
    return text.lstrip("0") or "0"


def build_frame(
    message_id: int,
    terminal_phone: str,
    message_serial: int,
    body: bytes = b"",
    *,
    package_total: int | None = None,
    package_index: int | None = None,
) -> bytes:
    if len(body) > BODY_MAX:
        raise MessageError("one JT/T 808 message body must not exceed 1023 bytes")
    attributes = VERSION_FLAG | len(body)
    packaged = package_total is not None or package_index is not None
    if packaged:
        if not package_total or not package_index or not 1 <= package_index <= package_total:
            raise MessageError("invalid JT/T 808 package metadata")
        attributes |= SUBPACKAGE_FLAG
    header = (
        message_id.to_bytes(2, "big")
        + attributes.to_bytes(2, "big")
        + bytes([PROTOCOL_VERSION])
        + _phone_to_bcd(terminal_phone)
        + message_serial.to_bytes(2, "big")
    )
    if packaged:
        header += package_total.to_bytes(2, "big") + package_index.to_bytes(2, "big")
    checked = header + body
    return bytes([FLAG]) + _escape(checked + bytes([xor_checksum(checked)])) + bytes([FLAG])


def decode_frame(frame: bytes) -> JT808Frame:
    if len(frame) < 3 or frame[0] != FLAG or frame[-1] != FLAG:
        raise FrameError("JT/T 808 frame must be enclosed by 0x7E")
    raw = _unescape(frame[1:-1])
    if len(raw) < 18:
        raise FrameError("JT/T 808 frame is too short")
    if xor_checksum(raw[:-1]) != raw[-1]:
        raise FrameError("JT/T 808 XOR checksum mismatch")
    message_id = int.from_bytes(raw[0:2], "big")
    attributes = int.from_bytes(raw[2:4], "big")
    if not attributes & VERSION_FLAG or raw[4] != PROTOCOL_VERSION:
        raise FrameError("only JT/T 808-2019 protocol version 1 is supported")
    body_length = attributes & BODY_MAX
    packaged = bool(attributes & SUBPACKAGE_FLAG)
    header_length = 21 if packaged else 17
    if len(raw) != header_length + body_length + 1:
        raise FrameError("JT/T 808 body length does not match header")
    package_total = package_index = None
    if packaged:
        package_total = int.from_bytes(raw[17:19], "big")
        package_index = int.from_bytes(raw[19:21], "big")
        if not package_total or not package_index or package_index > package_total:
            raise FrameError("invalid JT/T 808 package metadata")
    return JT808Frame(
        message_id=message_id,
        terminal_phone=_phone_from_bcd(raw[5:15]),
        message_serial=int.from_bytes(raw[15:17], "big"),
        body=raw[header_length : header_length + body_length],
        package_total=package_total,
        package_index=package_index,
        raw_data=frame.hex().upper(),
    )


class JT808FrameBuffer:
    """Incrementally split a TCP stream on JT/T 808 0x7E boundaries."""

    def __init__(self, max_frame_size: int = 4096) -> None:
        self.max_frame_size = max_frame_size
        self._buffer = bytearray()
        self.dropped_bytes = 0

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes) -> list[bytes]:
        self._buffer.extend(data)
        frames: list[bytes] = []
        while True:
            try:
                start = self._buffer.index(FLAG)
            except ValueError:
                self.dropped_bytes += len(self._buffer)
                self._buffer.clear()
                return frames
            if start:
                self.dropped_bytes += start
                del self._buffer[:start]
            try:
                end = self._buffer.index(FLAG, 1)
            except ValueError:
                if len(self._buffer) > self.max_frame_size:
                    self.dropped_bytes += 1
                    del self._buffer[0]
                    continue
                return frames
            if end == 1:
                del self._buffer[0]
                continue
            frame = bytes(self._buffer[: end + 1])
            del self._buffer[: end + 1]
            if len(frame) > self.max_frame_size:
                self.dropped_bytes += len(frame)
                continue
            frames.append(frame)


class MessageReassembler:
    """Reassemble one standard subpackaged message on a TCP connection."""

    def __init__(self) -> None:
        self._pending: dict[tuple[str, int, int, int], dict[int, bytes]] = {}

    def push(self, frame: JT808Frame) -> JT808Frame | None:
        if frame.package_total is None:
            return frame
        key = (
            frame.terminal_phone,
            frame.message_id,
            frame.message_serial,
            frame.package_total,
        )
        packages = self._pending.setdefault(key, {})
        previous = packages.get(frame.package_index)
        if previous is not None and previous != frame.body:
            self._pending.pop(key, None)
            raise FrameError("conflicting duplicate JT/T 808 subpackage")
        packages[frame.package_index] = frame.body
        if len(packages) != frame.package_total:
            return None
        body = b"".join(packages[index] for index in range(1, frame.package_total + 1))
        self._pending.pop(key, None)
        return replace(frame, body=body, package_total=None, package_index=None)


def parse_registration(frame: JT808Frame) -> None:
    if frame.message_id != MSG_TERMINAL_REGISTER or len(frame.body) < 76:
        raise MessageError("invalid terminal registration body")


def parse_authentication(frame: JT808Frame) -> tuple[str, str]:
    if frame.message_id != MSG_TERMINAL_AUTH or not frame.body:
        raise MessageError("invalid terminal authentication body")
    length = frame.body[0]
    expected = 1 + length + 15 + 20
    if len(frame.body) != expected:
        raise MessageError("terminal authentication body length mismatch")
    auth_code = frame.body[1 : 1 + length].decode("ascii")
    imei = frame.body[1 + length : 16 + length].rstrip(b"\x00").decode("ascii")
    if not imei.isdecimal():
        raise MessageError("terminal authentication IMEI is invalid")
    return auth_code, imei


def build_registration_response(frame: JT808Frame, auth_code: str, result: int = 0) -> bytes:
    body = frame.message_serial.to_bytes(2, "big") + bytes([result])
    if result == 0:
        body += auth_code.encode("ascii")
    return build_frame(MSG_REGISTER_ACK, frame.terminal_phone, 0, body)


def build_common_response(frame: JT808Frame, result: int = 0) -> bytes:
    body = (
        frame.message_serial.to_bytes(2, "big")
        + frame.message_id.to_bytes(2, "big")
        + bytes([result])
    )
    return build_frame(MSG_PLATFORM_COMMON_ACK, frame.terminal_phone, 0, body)


def decode_location_message(frame: JT808Frame) -> TrackerBatch:
    if frame.message_id == MSG_LOCATION:
        bodies = (frame.body,)
    elif frame.message_id == MSG_LOCATION_BATCH:
        bodies = _decode_batch_body(frame.body)
    else:
        raise MessageError(f"unsupported location message 0x{frame.message_id:04X}")
    records = tuple(_decode_location_body(body) for body in bodies)
    if not records:
        raise MessageError("location batch must not be empty")
    generations = {record.generation_id for record in records}
    if len(generations) != 1 or None in generations:
        raise MessageError("location batch mixes generation IDs")
    generation_id = generations.pop()
    assert generation_id is not None
    return TrackerBatch(
        imei=frame.terminal_phone,
        generation_id=generation_id,
        batch_id=records[-1].sequence,
        records=records,
        message_id=frame.message_id,
        message_serial=frame.message_serial,
        raw_data=frame.raw_data,
    )


def _decode_batch_body(body: bytes) -> tuple[bytes, ...]:
    if len(body) < 3:
        raise MessageError("location batch body is too short")
    count = int.from_bytes(body[0:2], "big")
    if not 1 <= count <= MAX_BATCH_RECORDS:
        raise MessageError("location batch count is outside 1..512")
    if body[2] not in (0, 1):
        raise MessageError("location batch type must be normal or blind-area")
    items: list[bytes] = []
    offset = 3
    for _ in range(count):
        if offset + 2 > len(body):
            raise MessageError("truncated location batch item length")
        length = int.from_bytes(body[offset : offset + 2], "big")
        offset += 2
        if length < 28 or offset + length > len(body):
            raise MessageError("truncated location batch item")
        items.append(body[offset : offset + length])
        offset += length
    if offset != len(body):
        raise MessageError("trailing bytes after location batch")
    return tuple(items)


def _decode_location_body(body: bytes) -> TrackerRecord:
    if len(body) < 28:
        raise MessageError("location body is shorter than 28 bytes")
    status = int.from_bytes(body[4:8], "big")
    valid = bool(status & 0x02)
    latitude = int.from_bytes(body[8:12], "big") / 1_000_000
    longitude = int.from_bytes(body[12:16], "big") / 1_000_000
    if status & 0x04:
        latitude = -latitude
    if status & 0x08:
        longitude = -longitude
    extras = _decode_extras(body[28:])
    identity = extras.get(EXTRA_RECORD_ID)
    power = extras.get(EXTRA_BATTERY)
    if identity is None or len(identity) != 10 or identity[0] != 1:
        raise MessageError("location is missing tracker record identity 0xE0")
    if power is None or len(power) != 3:
        raise MessageError("location is missing battery information 0xE1")
    generation = int.from_bytes(identity[1:5], "big")
    sequence = int.from_bytes(identity[5:9], "big")
    flags = identity[9]
    battery_mv = int.from_bytes(power[0:2], "big") or None
    battery_percent = None if power[2] == 0xFF else power[2]
    if battery_percent is not None and battery_percent > 100:
        raise MessageError("battery percentage exceeds 100")
    signal = extras.get(EXTRA_SIGNAL, b"\x00")
    satellites = extras.get(EXTRA_SATELLITES, b"\x00")
    hdop_raw = extras.get(EXTRA_HDOP, b"\xff")
    if len(signal) != 1 or len(satellites) != 1 or len(hdop_raw) != 1:
        raise MessageError("invalid signal, satellite, or HDOP extension")
    gps_time = _decode_bcd_time(body[22:28])
    return TrackerRecord(
        sequence=sequence,
        gps_time=gps_time,
        time_valid=bool(flags & 0x01),
        valid=valid,
        latitude=latitude if valid else None,
        longitude=longitude if valid else None,
        altitude=float(int.from_bytes(body[16:18], "big")) if valid else None,
        speed=(int.from_bytes(body[18:20], "big") / 10 / 1.852) if valid else None,
        course=float(int.from_bytes(body[20:22], "big")) if valid else None,
        satellites=satellites[0],
        hdop=None if hdop_raw[0] == 0xFF else hdop_raw[0] / 10,
        csq=signal[0],
        battery_mv=battery_mv,
        battery_percent=battery_percent,
        wake_code=(flags >> 1) & 0x07,
        raw_data=body.hex().upper(),
        generation_id=generation,
    )
def _decode_extras(data: bytes) -> dict[int, bytes]:
    extras: dict[int, bytes] = {}
    offset = 0
    while offset < len(data):
        if offset + 2 > len(data):
            raise MessageError("truncated location extension header")
        item_id, length = data[offset], data[offset + 1]
        offset += 2
        if offset + length > len(data):
            raise MessageError("truncated location extension value")
        extras[item_id] = data[offset : offset + length]
        offset += length
    return extras


def _decode_bcd_time(value: bytes) -> datetime:
    if len(value) != 6:
        raise MessageError("location time must contain 6 BCD bytes")
    text = "".join(f"{byte:02X}" for byte in value)
    if not text.isdecimal():
        raise MessageError("location time contains invalid BCD")
    try:
        local_time = datetime.strptime(text, "%y%m%d%H%M%S").replace(
            tzinfo=timezone(timedelta(hours=8))
        )
    except ValueError as exc:
        raise MessageError("location time is invalid") from exc
    return local_time.astimezone(timezone.utc)
