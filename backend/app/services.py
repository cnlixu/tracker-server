"""Application services connecting validated reports to persistence."""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from .database import save_records
from .jt808_protocol import TrackerBatch


@dataclass(slots=True)
class TrackerService:
    """Persist validated tracker reports through database transactions."""

    pool: asyncpg.Pool

    async def process_batch(self, batch: TrackerBatch) -> int:
        """Persist every JT/T 808 location record idempotently."""
        return await save_records(
            self.pool,
            imei=batch.imei,
            generation_id=batch.generation_id,
            batch_id=batch.batch_id,
            records=batch.records,
        )
