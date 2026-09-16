# PTRK V1 protocol

PTRK V1 is an ASCII, NMEA-style device report. The protocol module parses one
complete logical report at a time; TCP stream framing is outside its scope.

## Wire format

```text
$PTRK,1,862288087606784,150926,094508,A,3045.83496,N,10354.09348,E,575.0,0.111,193.85,9,2.21,31,1*69
```

The 17 comma-separated body fields are:

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
| 10 | altitude | floating-point value |
| 11 | speed | floating-point value |
| 12 | course | floating-point value |
| 13 | satellites | non-negative integer |
| 14 | HDOP | floating-point value |
| 15 | CSQ | non-negative integer |
| 16 | wake code | non-negative integer |

For status `V`, fields 6 through 14 may be empty. If a coordinate is present,
its hemisphere must also be present and valid. Status `A` requires every GNSS
field.

## Checksum

Starting with zero, XOR every ASCII byte between `$` and `*`. Neither delimiter
is included. The wire value is exactly two hexadecimal characters; parsing is
case-insensitive and generation uses uppercase.

The example body produces `0x69`.

## Parsed representation

- Time is a timezone-aware UTC `datetime`.
- Coordinates are signed WGS84 decimal degrees.
- `S` and `W` coordinates are negative.
- GNSS measurements are `None` when omitted in a `V` report.
- `raw_data` retains the validated logical report, excluding an optional CR/LF
  transport terminator.
