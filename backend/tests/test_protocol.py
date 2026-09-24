"""Tests for reliable, transport-independent PTRK V1 parsing."""

from datetime import datetime, timezone

import pytest

from backend.app.models import TrackerReport
from backend.app.protocol import (
    ChecksumError,
    FieldError,
    ProtocolFormatError,
    UnsupportedVersionError,
    calculate_checksum,
    generate_checksum,
    nmea_to_decimal,
    parse_message,
    validate_checksum,
)


STANDARD_BODY = (
    "PTRK,1,862288087606784,150926,094508,A,3045.83496,N,"
    "10354.09348,E,575.0,0.111,193.85,9,2.21,31,1"
)
STANDARD_MESSAGE = f"${STANDARD_BODY}*69"

# The V3 example published by the tracker firmware, including its checksum.
V3_BODY = (
    "PTRK,3,862288087606784,305419896,1788251489,1788251489,010926,083129,"
    "A,1,3045.81768,N,10354.07883,E,516.2,0.591,152.99,15,0.80,31,3700,1"
)
V3_MESSAGE = f"${V3_BODY}*76"


def build_message(body: str) -> str:
    return f"${body}*{generate_checksum(body)}"


def replace_field(index: int, value: str, body: str = STANDARD_BODY) -> str:
    fields = body.split(",")
    fields[index] = value
    return ",".join(fields)


def test_normal_report() -> None:
    report = parse_message(STANDARD_MESSAGE)

    assert isinstance(report, TrackerReport)
    assert report.protocol_version == 1
    assert report.imei == "862288087606784"
    assert report.gps_time == datetime(2026, 9, 15, 9, 45, 8, tzinfo=timezone.utc)
    assert report.valid is True
    assert report.latitude == pytest.approx(30.763916)
    assert report.longitude == pytest.approx(103.901558)
    assert report.altitude == 575.0
    assert report.speed == 0.111
    assert report.course == 193.85
    assert report.satellites == 9
    assert report.hdop == 2.21
    assert report.csq == 31
    assert report.wake_code == 1


def test_checksum_calculation_generation_and_validation() -> None:
    assert calculate_checksum(STANDARD_BODY) == 0x69
    assert generate_checksum(STANDARD_BODY) == "69"
    validate_checksum(STANDARD_BODY, "69")


def test_checksum_error() -> None:
    with pytest.raises(ChecksumError, match="checksum mismatch"):
        parse_message(f"${STANDARD_BODY}*68")


def test_missing_dollar() -> None:
    with pytest.raises(ProtocolFormatError, match="start with"):
        parse_message(STANDARD_MESSAGE[1:])


def test_missing_asterisk() -> None:
    with pytest.raises(ProtocolFormatError, match="checksum separator"):
        parse_message(f"${STANDARD_BODY}69")


def test_wrong_message_type() -> None:
    body = replace_field(0, "OTHER")
    with pytest.raises(ProtocolFormatError, match="message type"):
        parse_message(build_message(body))


def test_too_few_fields() -> None:
    body = ",".join(STANDARD_BODY.split(",")[:-1])
    with pytest.raises(ProtocolFormatError, match="requires 17 fields"):
        parse_message(build_message(body))


def test_too_many_fields() -> None:
    body = f"{STANDARD_BODY},extra"
    with pytest.raises(ProtocolFormatError, match="requires 17 fields"):
        parse_message(build_message(body))


@pytest.mark.parametrize("imei", ["86228808760678X", "12345", ""])
def test_invalid_imei(imei: str) -> None:
    body = replace_field(2, imei)
    with pytest.raises(FieldError, match="IMEI"):
        parse_message(build_message(body))


def test_invalid_time() -> None:
    body = replace_field(4, "246000")
    with pytest.raises(FieldError, match="invalid UTC date/time"):
        parse_message(build_message(body))


def test_invalid_date() -> None:
    body = replace_field(3, "310226")
    with pytest.raises(FieldError, match="invalid UTC date/time"):
        parse_message(build_message(body))


def test_north_latitude() -> None:
    assert nmea_to_decimal("3045.83496", "N") == pytest.approx(30.763916)


def test_south_latitude() -> None:
    assert nmea_to_decimal("3045.83496", "S") == pytest.approx(-30.763916)


def test_east_longitude() -> None:
    assert nmea_to_decimal("10354.09348", "E") == pytest.approx(103.901558)


def test_west_longitude() -> None:
    assert nmea_to_decimal("10354.09348", "W") == pytest.approx(-103.901558)


@pytest.mark.parametrize(
    ("field_index", "coordinate"),
    [(6, "9100.00000"), (8, "18100.00000")],
)
def test_coordinate_out_of_range(field_index: int, coordinate: str) -> None:
    body = replace_field(field_index, coordinate)
    with pytest.raises(FieldError, match="outside its valid range"):
        parse_message(build_message(body))


def test_status_a() -> None:
    report = parse_message(STANDARD_MESSAGE)
    assert report.valid is True


def test_status_v_allows_empty_gnss_fields() -> None:
    fields = STANDARD_BODY.split(",")
    fields[5] = "V"
    fields[6:15] = [""] * 9
    report = parse_message(build_message(",".join(fields)))

    assert report.valid is False
    assert report.latitude is None
    assert report.longitude is None
    assert report.altitude is None
    assert report.speed is None
    assert report.course is None
    assert report.satellites is None
    assert report.hdop is None
    assert report.csq == 31
    assert report.wake_code == 1


def test_invalid_status() -> None:
    body = replace_field(5, "X")
    with pytest.raises(FieldError, match="valid status"):
        parse_message(build_message(body))


@pytest.mark.parametrize(
    ("field_index", "value", "expected_message"),
    [(7, "X", "latitude hemisphere"), (9, "X", "longitude hemisphere")],
)
def test_invalid_hemisphere(
    field_index: int,
    value: str,
    expected_message: str,
) -> None:
    body = replace_field(field_index, value)
    with pytest.raises(FieldError, match=expected_message):
        parse_message(build_message(body))


@pytest.mark.parametrize(
    ("field_index", "value", "expected_message"),
    [(10, "bad", "altitude"), (13, "9.5", "satellites"), (15, "bad", "CSQ")],
)
def test_invalid_numeric_field(
    field_index: int,
    value: str,
    expected_message: str,
) -> None:
    body = replace_field(field_index, value)
    with pytest.raises(FieldError, match=expected_message):
        parse_message(build_message(body))


def test_lowercase_checksum_is_accepted() -> None:
    body = replace_field(16, "2")
    assert generate_checksum(body) == "6A"
    report = parse_message(f"${body}*6a")
    assert report.wake_code == 2


def test_raw_data_is_preserved() -> None:
    report = parse_message(STANDARD_MESSAGE)
    assert report.raw_data == STANDARD_MESSAGE


def test_protocol_version_must_be_numeric() -> None:
    body = replace_field(1, "one")
    with pytest.raises(FieldError, match="protocol_version"):
        parse_message(build_message(body))


def test_unsupported_protocol_version() -> None:
    body = replace_field(1, "2")
    with pytest.raises(UnsupportedVersionError, match="version: 2"):
        parse_message(build_message(body))


def test_status_a_requires_gnss_fields() -> None:
    body = replace_field(10, "")
    with pytest.raises(FieldError, match="altitude is required"):
        parse_message(build_message(body))


def test_v_coordinate_and_hemisphere_must_be_present_together() -> None:
    body = replace_field(5, "V")
    body = replace_field(6, "", body)
    with pytest.raises(FieldError, match="provided together"):
        parse_message(build_message(body))


def test_bytes_and_crlf_are_accepted() -> None:
    report = parse_message(f"{STANDARD_MESSAGE}\r\n".encode("ascii"))
    assert report.raw_data == STANDARD_MESSAGE


def test_v1_report_has_no_record_identity() -> None:
    report = parse_message(STANDARD_MESSAGE)

    assert report.protocol_version == 1
    assert report.generation_id is None
    assert report.record_sequence is None
    assert report.batch_id is None
    assert report.time_valid is None
    assert report.battery_mv is None


def test_v3_report_from_the_firmware_readme() -> None:
    report = parse_message(V3_MESSAGE)

    assert report.protocol_version == 3
    assert report.imei == "862288087606784"
    assert report.generation_id == 305419896
    assert report.record_sequence == 1788251489
    assert report.batch_id == 1788251489
    assert report.gps_time == datetime(2026, 9, 1, 8, 31, 29, tzinfo=timezone.utc)
    assert report.valid is True
    assert report.time_valid is True
    assert report.latitude == pytest.approx(30.763628)
    assert report.longitude == pytest.approx(103.9013138)
    assert report.altitude == 516.2
    assert report.speed == 0.591
    assert report.course == 152.99
    assert report.satellites == 15
    assert report.hdop == pytest.approx(0.80)
    assert report.csq == 31
    assert report.battery_mv == 3700
    assert report.wake_code == 1
    assert report.raw_data == V3_MESSAGE


def test_v3_bytes_with_crlf_are_accepted() -> None:
    report = parse_message(f"{V3_MESSAGE}\r\n".encode("ascii"))
    assert report.raw_data == V3_MESSAGE
    assert report.battery_mv == 3700


def test_v3_status_v_allows_empty_gnss_and_unknown_time() -> None:
    fields = V3_BODY.split(",")
    fields[8] = "V"
    fields[9] = "0"
    fields[10:19] = [""] * 9
    report = parse_message(build_message(",".join(fields)))

    assert report.valid is False
    assert report.time_valid is False
    assert report.latitude is None
    assert report.longitude is None
    assert report.altitude is None
    assert report.speed is None
    assert report.course is None
    assert report.satellites is None
    assert report.hdop is None
    assert report.csq == 31
    assert report.battery_mv == 3700
    assert report.record_sequence == 1788251489


def test_v3_time_valid_flag_must_be_zero_or_one() -> None:
    fields = V3_BODY.split(",")
    fields[9] = "2"
    with pytest.raises(FieldError, match="time_valid"):
        parse_message(build_message(",".join(fields)))


def test_v3_identity_fields_must_be_numeric() -> None:
    fields = V3_BODY.split(",")
    fields[3] = "abc"
    with pytest.raises(FieldError, match="generation_id"):
        parse_message(build_message(",".join(fields)))


def test_v1_field_count_with_v3_version_is_rejected() -> None:
    body = replace_field(1, "3")
    with pytest.raises(UnsupportedVersionError, match="version: 3"):
        parse_message(build_message(body))


def test_unexpected_field_count_reports_both_layouts() -> None:
    body = ",".join(V3_BODY.split(",")[:-1])
    with pytest.raises(
        ProtocolFormatError,
        match=r"requires 17 fields for V1 or 22 for V3",
    ):
        parse_message(build_message(body))
