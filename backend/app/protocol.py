"""Reliable, transport-independent parsing for the PTRK V1 protocol."""

from __future__ import annotations

from datetime import datetime, timezone
import math
import re

from .models import TrackerReport


PTRK_MESSAGE_TYPE = "PTRK"
SUPPORTED_PROTOCOL_VERSION = 1
V1_FIELD_COUNT = 17

_CHECKSUM_PATTERN = re.compile(r"[0-9A-Fa-f]{2}")
_IMEI_PATTERN = re.compile(r"[0-9]{15}")


class ProtocolError(ValueError):
    """Base class for invalid PTRK reports."""


class ProtocolFormatError(ProtocolError):
    """Raised when a report does not have the required wire format."""


class ChecksumError(ProtocolError):
    """Raised when a checksum is malformed or does not match the body."""


class FieldError(ProtocolError):
    """Raised when an individual protocol field is invalid."""


class UnsupportedVersionError(FieldError):
    """Raised when a syntactically valid but unsupported version is received."""


# Compatibility names retained while other application layers are developed.
TrackerProtocolError = ProtocolError
TrackerFormatError = ProtocolFormatError
TrackerChecksumError = ChecksumError
TrackerFieldError = FieldError
TrackerMessage = TrackerReport


def calculate_checksum(body: str | bytes) -> int:
    """Return the XOR of the ASCII bytes in a body without ``$`` or ``*XX``."""
    body_bytes = _to_ascii_bytes(body, value_name="checksum body")
    checksum = 0
    for value in body_bytes:
        checksum ^= value
    return checksum


def generate_checksum(body: str | bytes) -> str:
    """Generate an uppercase, two-character hexadecimal checksum."""
    return f"{calculate_checksum(body):02X}"


def validate_checksum(body: str | bytes, supplied_checksum: str) -> None:
    """Validate a supplied checksum, accepting upper- or lowercase hex."""
    if _CHECKSUM_PATTERN.fullmatch(supplied_checksum) is None:
        raise ChecksumError(
            "checksum must contain exactly two hexadecimal characters"
        )

    supplied_value = int(supplied_checksum, 16)
    calculated_value = calculate_checksum(body)
    if supplied_value != calculated_value:
        raise ChecksumError(
            f"checksum mismatch: received 0x{supplied_value:02X}, "
            f"calculated 0x{calculated_value:02X}"
        )


def xor_checksum(body: str | bytes) -> int:
    """Compatibility wrapper for :func:`calculate_checksum`."""
    return calculate_checksum(body)


def parse_utc_datetime(date_text: str, time_text: str) -> datetime:
    """Parse PTRK ``ddmmyy`` and ``hhmmss`` fields as timezone-aware UTC."""
    if re.fullmatch(r"[0-9]{6}", date_text) is None:
        raise FieldError(f"UTC date must use ddmmyy format: {date_text!r}")
    if re.fullmatch(r"[0-9]{6}", time_text) is None:
        raise FieldError(f"UTC time must use hhmmss format: {time_text!r}")

    day = int(date_text[0:2])
    month = int(date_text[2:4])
    year = 2000 + int(date_text[4:6])
    hour = int(time_text[0:2])
    minute = int(time_text[2:4])
    second = int(time_text[4:6])
    try:
        return datetime(
            year,
            month,
            day,
            hour,
            minute,
            second,
            tzinfo=timezone.utc,
        )
    except ValueError as exc:
        raise FieldError(
            f"invalid UTC date/time: {date_text!r} {time_text!r}"
        ) from exc


def nmea_to_decimal(value: str, hemisphere: str) -> float:
    """Convert an NMEA latitude or longitude to signed WGS84 degrees."""
    if hemisphere in {"N", "S"}:
        degree_digits = 2
        maximum_degrees = 90
        coordinate_name = "latitude"
    elif hemisphere in {"E", "W"}:
        degree_digits = 3
        maximum_degrees = 180
        coordinate_name = "longitude"
    else:
        raise FieldError(f"invalid hemisphere: {hemisphere!r}")

    integer_digits = degree_digits + 2
    if re.fullmatch(rf"[0-9]{{{integer_digits}}}\.[0-9]+", value) is None:
        raise FieldError(
            f"{coordinate_name} must use NMEA degrees/minutes format: {value!r}"
        )

    degrees = int(value[:degree_digits])
    minutes = float(value[degree_digits:])
    if minutes >= 60:
        raise FieldError(f"{coordinate_name} minutes must be less than 60")
    if degrees > maximum_degrees or (degrees == maximum_degrees and minutes != 0):
        raise FieldError(f"{coordinate_name} is outside its valid range")

    result = degrees + minutes / 60
    if hemisphere in {"S", "W"}:
        result = -result
    return result


def parse_message(frame: str | bytes) -> TrackerReport:
    """Validate and parse one complete PTRK V1 report.

    An optional trailing CR/LF transport terminator is removed. ``raw_data``
    retains the complete logical report from ``$`` through ``*XX``.
    """
    raw_data = _to_ascii_text(frame).rstrip("\r\n")
    if not raw_data.startswith("$"):
        raise ProtocolFormatError("message must start with '$'")
    if "*" not in raw_data:
        raise ProtocolFormatError("message must contain a '*' checksum separator")
    if raw_data.count("*") != 1:
        raise ProtocolFormatError("message must contain exactly one '*' separator")

    body, supplied_checksum = raw_data[1:].split("*", 1)
    if not body:
        raise ProtocolFormatError("message body must not be empty")
    validate_checksum(body, supplied_checksum)

    fields = body.split(",")
    if len(fields) != V1_FIELD_COUNT:
        raise ProtocolFormatError(
            f"PTRK V1 requires {V1_FIELD_COUNT} fields, received {len(fields)}"
        )
    if fields[0] != PTRK_MESSAGE_TYPE:
        raise ProtocolFormatError(f"unsupported message type: {fields[0]!r}")

    protocol_version = _parse_required_int(fields[1], "protocol_version")
    if protocol_version != SUPPORTED_PROTOCOL_VERSION:
        raise UnsupportedVersionError(
            f"unsupported PTRK protocol version: {protocol_version}"
        )

    imei = fields[2]
    if _IMEI_PATTERN.fullmatch(imei) is None:
        raise FieldError("IMEI must contain exactly 15 ASCII decimal digits")

    status = fields[5]
    if status not in {"A", "V"}:
        raise FieldError(f"valid status must be 'A' or 'V': {status!r}")
    valid = status == "A"

    latitude = _parse_coordinate_pair(
        fields[6], fields[7], "latitude", {"N", "S"}, required=valid
    )
    longitude = _parse_coordinate_pair(
        fields[8], fields[9], "longitude", {"E", "W"}, required=valid
    )

    return TrackerReport(
        protocol_version=protocol_version,
        imei=imei,
        gps_time=parse_utc_datetime(fields[3], fields[4]),
        valid=valid,
        latitude=latitude,
        longitude=longitude,
        altitude=_parse_optional_float(fields[10], "altitude", required=valid),
        speed=_parse_optional_float(fields[11], "speed", required=valid),
        course=_parse_optional_float(fields[12], "course", required=valid),
        satellites=_parse_optional_int(fields[13], "satellites", required=valid),
        hdop=_parse_optional_float(fields[14], "HDOP", required=valid),
        csq=_parse_required_int(fields[15], "CSQ"),
        wake_code=_parse_required_int(fields[16], "wake_code"),
        raw_data=raw_data,
    )


def _parse_coordinate_pair(
    value: str,
    hemisphere: str,
    name: str,
    allowed_hemispheres: set[str],
    *,
    required: bool,
) -> float | None:
    if not value and not hemisphere:
        if required:
            raise FieldError(f"{name} and its hemisphere are required for status A")
        return None
    if not value or not hemisphere:
        raise FieldError(f"{name} and its hemisphere must be provided together")
    if hemisphere not in allowed_hemispheres:
        allowed = "/".join(sorted(allowed_hemispheres))
        raise FieldError(f"{name} hemisphere must be {allowed}: {hemisphere!r}")
    return nmea_to_decimal(value, hemisphere)


def _parse_required_int(value: str, name: str) -> int:
    if re.fullmatch(r"[0-9]+", value) is None:
        raise FieldError(f"{name} must be a non-negative integer: {value!r}")
    return int(value)


def _parse_optional_int(value: str, name: str, *, required: bool) -> int | None:
    if not value:
        if required:
            raise FieldError(f"{name} is required for status A")
        return None
    return _parse_required_int(value, name)


def _parse_optional_float(
    value: str,
    name: str,
    *,
    required: bool,
) -> float | None:
    if not value:
        if required:
            raise FieldError(f"{name} is required for status A")
        return None
    try:
        result = float(value)
    except ValueError as exc:
        raise FieldError(f"{name} must be a number: {value!r}") from exc
    if not math.isfinite(result):
        raise FieldError(f"{name} must be a finite number: {value!r}")
    return result


def _to_ascii_bytes(value: str | bytes, *, value_name: str) -> bytes:
    if isinstance(value, bytes):
        try:
            value.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ProtocolFormatError(
                f"{value_name} must contain ASCII characters only"
            ) from exc
        return value
    if isinstance(value, str):
        try:
            return value.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ProtocolFormatError(
                f"{value_name} must contain ASCII characters only"
            ) from exc
    raise ProtocolFormatError(f"{value_name} must be str or bytes")


def _to_ascii_text(frame: str | bytes) -> str:
    if isinstance(frame, str):
        _to_ascii_bytes(frame, value_name="message")
        return frame
    return _to_ascii_bytes(frame, value_name="message").decode("ascii")
