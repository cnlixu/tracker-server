# Tracker protocols

The server accepts the two protocols emitted by the tracker firmware and keeps
the legacy ASCII V1 parser for older devices:

| Transport | First byte | Framing | Acknowledgement |
| --- | --- | --- | --- |
| Binary V2 (primary) | `0xA5` | `A5 5A` + class + id + length | 32-byte binary acknowledgement |
| ASCII V3 / V1 | `$` | CR/LF terminated NMEA-style line | `$ACK,OK` or `$ACK,ERROR` |

A connection carries exactly one flavour, selected by its first byte.

## ASCII protocol

`PTRK` is an NMEA-style device report. The protocol module parses one complete
logical report at a time; TCP stream framing is outside its scope.

### Wire format

```text
$PTRK,1,862288087606784,150926,094508,A,3045.83496,N,10354.09348,E,575.0,0.111,193.85,9,2.21,31,1*69
```

The V1 body has 17 comma-separated fields:

| Index | Name | Meaning |
| ---: | --- | --- |
| 0 | message type | `PTRK` |
| 1 | protocol version | `1` |
| 2 | IMEI | 15 ASCII decimal digits |
| 3 | UTC date | `ddmmyy`; years are interpreted as 2000-2099 |
| 4 | UTC time | `hhmmss` |
| 5 | valid | `A` for valid GNSS fix, `V` for invalid |
| 6 | latitude | NMEA `ddmm.mmmmm` |
| 7 | latitude hemisphere | `N` or `S` |
| 8 | longitude | NMEA `dddmm.mmmmm` |
| 9 | longitude hemisphere | `E` or `W` |
| 10 | altitude | metres |
| 11 | speed | knots |
| 12 | course | degrees |
| 13 | satellites | non-negative integer |
| 14 | HDOP | floating-point value |
| 15 | CSQ | non-negative integer |
| 16 | wake code | `0` power-on, `1` timer, `5` power key |

For status `V`, fields 6 through 14 may be empty. If a coordinate is present,
its hemisphere must also be present and valid. Status `A` requires every GNSS
field.

### V3 layout

The current firmware appends the same per-record identity the binary protocol
uses, giving a 22-field body:

```text
$PTRK,3,862288087606784,305419896,1788251489,1788251489,010926,083129,A,1,3045.81768,N,10354.07883,E,516.2,0.591,152.99,15,0.80,31,3700,1*76
```

| Index | Name | Meaning |
| ---: | --- | --- |
| 0-2 | message type, version, IMEI | as V1, with version `3` |
| 3 | generation ID | data generation the record belongs to |
| 4 | record sequence | strictly increasing per generation |
| 5 | batch ID | sequence of the last record in the upload batch |
| 6-7 | UTC date and time | `ddmmyy`, `hhmmss` |
| 8 | valid | `A` / `V` |
| 9 | time valid | `1` when the UTC timestamp is trustworthy, else `0` |
| 10-18 | GNSS block | identical layout to V1 fields 6-14 |
| 19 | CSQ | non-negative integer |
| 20 | battery | millivolts; `0` when unreadable |
| 21 | wake code | as V1 |

V3 reports are stored through the same idempotent record path as binary V2, so
re-deliveries cannot duplicate rows.

### Checksum

Starting with zero, XOR every ASCII byte between `$` and `*`. Neither delimiter
is included. The wire value is exactly two hexadecimal characters; parsing is
case-insensitive and generation uses uppercase.

The V1 example body produces `0x69`, the V3 example body `0x76`.

## Parsed representation

- Time is a timezone-aware UTC `datetime`.
- Coordinates are signed WGS84 decimal degrees.
- `S` and `W` coordinates are negative.
- GNSS measurements are `None` when omitted in a `V` report.
- `raw_data` retains the validated logical report, excluding an optional CR/LF
  transport terminator.
- `generation_id`, `record_sequence`, `batch_id`, `time_valid` and `battery_mv`
  are only set for V3 reports.

## Binary protocol (V2)

Binary V2 is the primary tracker transport. Frames are not line terminated, so
the server frames them by length.

```text
A5 5A | CLASS (0x01) | ID (0x01 position) | LENGTH (uint16, payload size in bytes) | PAYLOAD | CRC16 (uint16)
```

`CRC16` covers CLASS, ID, LENGTH and PAYLOAD and is CRC-16/CCITT-FALSE (poly
`0x1021`, init `0xFFFF`, no reflection), identical to the firmware
implementation. All multi-byte integers inside the payload are little endian.

### Upload payload

| Size | Field |
| ---: | --- |
| 1 | payload version, `2` |
| 8 | device id, 15-digit IMEI plus `F` padding in BCD nibbles |
| 4 | generation id |
| 4 | batch id (sequence of the last record in the batch) |
| 2 | record count |
| 30 × count | position records |

Each position record is 30 bytes:

| Offset | Size | Field |
| ---: | ---: | --- |
| 0 | 4 | record sequence |
| 4 | 4 | UTC timestamp, Unix seconds |
| 8 | 4 | latitude × 1e7 |
| 12 | 4 | longitude × 1e7 |
| 16 | 2 | altitude in metres, `-32768` when unknown |
| 18 | 2 | ground speed in cm/s |
| 20 | 2 | course × 100 |
| 22 | 2 | battery in mV, `0` when unknown |
| 24 | 1 | HDOP × 10, `255` when unknown |
| 25 | 1 | satellites used |
| 26 | 1 | CSQ |
| 27 | 1 | flags: bit0 fix valid, bit1 time valid, bits 3-5 wake code |
| 28 | 2 | CRC16 over bytes 0-27 |

The server converts speed to knots so binary and ASCII rows share one unit, and
stores `0`/`255`/`-32768` sentinels as `NULL`. `time_valid` records whether the
device trusted its clock, which matters for rows captured before a GNSS or SNTP
time fix.

### Acknowledgement

```text
A5 5A | 0x01 | 0x81 (position ACK) | 18 00 (payload length 24) | PAYLOAD (24 bytes) | CRC16
```

| Size | Field |
| ---: | --- |
| 1 | payload version, `2` |
| 8 | device id, echoed from the upload |
| 4 | generation id, echoed |
| 4 | batch id, echoed |
| 2 | acknowledged record count |
| 1 | status |
| 4 | server time, Unix seconds |

Status codes: `0x00` accepted, `0x01` server busy, `0x02` unsupported version,
`0x03` bad record, `0x04` authentication failed, `0x05` persistence failed.
`0x01` and `0x05` are retryable.

The tracker deletes cached records only after receiving `0x00` with a record
count equal to the uploaded count, so the server writes and commits the whole
batch first. Malformed frames (bad sync, length or CRC) are never acknowledged:
an unreliable acknowledgement could make the device discard records it never
delivered, so the device's retry timeout is the safer outcome.

Record identity `(imei, generation_id, record_seq)` is unique in the database,
so a batch re-sent after a lost acknowledgement is stored idempotently.

The binary reference frame and its expected acknowledgement are reproduced in
`backend/tests/test_binary_protocol.py` and
`backend/tests/test_tcp_server.py`.
