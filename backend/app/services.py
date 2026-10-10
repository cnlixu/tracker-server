"""Application services connecting validated reports to persistence."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import secrets

import asyncpg

from .database import (
    confirm_terminal_configuration,
    get_terminal_auth_credential,
    get_pending_terminal_configuration,
    mark_terminal_authenticated,
    mark_terminal_configuration_sent,
    save_records,
    save_terminal_attributes,
    save_terminal_registration,
)
from .jt808_protocol import TrackerBatch
from .models import TerminalAttributes, TerminalConfiguration, TerminalRegistration


@dataclass(slots=True)
class TrackerService:
    """Persist validated tracker reports through database transactions."""

    pool: asyncpg.Pool

    async def register_terminal(self, registration: TerminalRegistration) -> str:
        """Issue a new per-device code and invalidate any previously issued code."""
        auth_code = secrets.token_urlsafe(24)
        await save_terminal_registration(
            self.pool,
            registration,
            _auth_code_hash(auth_code),
        )
        return auth_code

    async def authenticate_terminal(
        self,
        terminal_phone: str,
        imei: str,
        auth_code: str,
    ) -> bool:
        """Validate a code previously issued to this terminal and IMEI."""
        credential = await get_terminal_auth_credential(
            self.pool,
            terminal_phone,
        )
        if credential is None:
            return False
        expected_hash, registered_imei = credential
        accepted = (
            registered_imei is None or registered_imei == imei
        ) and hmac.compare_digest(expected_hash, _auth_code_hash(auth_code))
        if accepted:
            accepted = await mark_terminal_authenticated(
                self.pool,
                terminal_phone,
                imei,
            )
        return accepted

    async def process_batch(self, batch: TrackerBatch) -> int:
        """Persist every JT/T 808 location record idempotently."""
        return await save_records(
            self.pool,
            imei=batch.imei,
            batch_id=batch.batch_id,
            records=batch.records,
        )

    async def store_terminal_attributes(self, attributes: TerminalAttributes) -> bool:
        """Persist the latest standard 0x0107 terminal attributes."""
        return await save_terminal_attributes(self.pool, attributes)

    async def get_pending_configuration(
        self,
        terminal_phone: str,
    ) -> TerminalConfiguration | None:
        return await get_pending_terminal_configuration(self.pool, terminal_phone)

    async def mark_configuration_sent(
        self,
        terminal_phone: str,
        revision: int,
    ) -> None:
        await mark_terminal_configuration_sent(self.pool, terminal_phone, revision)

    async def confirm_configuration(
        self,
        terminal_phone: str,
        revision: int,
        success: bool,
        error: str | None = None,
    ) -> None:
        await confirm_terminal_configuration(
            self.pool, terminal_phone, revision, success, error
        )


def _auth_code_hash(auth_code: str) -> str:
    return hashlib.sha256(auth_code.encode("ascii")).hexdigest()
