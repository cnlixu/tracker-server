"""Application services connecting validated reports to persistence."""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from .binary_protocol import TrackerBatch
from .database import save_records, save_report
from .models import TrackerRecord, TrackerReport
from .protocol import V3_PROTOCOL_VERSION


@dataclass(slots=True)
class TrackerService:
    """Persist validated tracker reports through database transactions."""

    pool: asyncpg.Pool

    async def process_report(self, report: TrackerReport) -> int:
        """Persist one ASCII report and its device snapshot atomically.

        ASCII V3 reports carry the same record identity as the binary protocol,
        so they use the idempotent record path. Legacy V1 reports, which have no
        identity, keep the original single-insert path.
        """
        if report.protocol_version == V3_PROTOCOL_VERSION:
            generation_id, batch_id = _v3_identity(report)
            return await save_records(
                self.pool,
                imei=report.imei,
                generation_id=generation_id,
                batch_id=batch_id,
                records=(record_from_report(report),),
            )
        return await save_report(self.pool, report)

    async def process_batch(self, batch: TrackerBatch) -> int:
        """Persist every record of one decoded binary frame idempotently."""
        return await save_records(
            self.pool,
            imei=batch.imei,
            generation_id=batch.generation_id,
            batch_id=batch.batch_id,
            records=batch.records,
        )


def record_from_report(report: TrackerReport) -> TrackerRecord:
    """Adapt an ASCII V3 report to the shared position-record representation."""
    if report.record_sequence is None:
        raise ValueError("report does not carry a record sequence")
    return TrackerRecord(
        sequence=report.record_sequence,
        gps_time=report.gps_time,
        time_valid=bool(report.time_valid),
        valid=report.valid,
        latitude=report.latitude,
        longitude=report.longitude,
        altitude=report.altitude,
        speed=report.speed,
        course=report.course,
        satellites=report.satellites,
        hdop=report.hdop,
        csq=report.csq,
        battery_mv=report.battery_mv,
        wake_code=report.wake_code,
        raw_data=report.raw_data,
    )


def _v3_identity(report: TrackerReport) -> tuple[int, int]:
    if report.generation_id is None or report.batch_id is None:
        raise ValueError("ASCII V3 report is missing its generation or batch id")
    return report.generation_id, report.batch_id
