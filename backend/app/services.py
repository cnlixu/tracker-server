"""Application services connecting validated reports to persistence."""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from .database import save_report
from .models import TrackerReport


@dataclass(slots=True)
class TrackerService:
    """Persist validated tracker reports through the database transaction."""

    pool: asyncpg.Pool

    async def process_report(self, report: TrackerReport) -> int:
        """Persist history and the device snapshot atomically."""
        return await save_report(self.pool, report)
