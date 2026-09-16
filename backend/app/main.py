"""FastAPI application factory and database-pool lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI

from .api import router
from .auth import router as auth_router
from .config import (
    AuthSettings,
    OnlineStatusSettings,
    load_auth_settings,
    load_online_status_settings,
)
from .database import close_pool, init_pool


PoolFactory = Callable[[], Awaitable[asyncpg.Pool]]


def create_app(
    *,
    pool_factory: PoolFactory | None = None,
    online_status_settings: OnlineStatusSettings | None = None,
    auth_settings: AuthSettings | None = None,
) -> FastAPI:
    """Create the API app with injectable lifecycle dependencies for tests."""
    selected_pool_factory = pool_factory or init_pool
    selected_status_settings = (
        online_status_settings or load_online_status_settings()
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.auth_settings = (
            auth_settings or load_auth_settings()
        )
        pool = await selected_pool_factory()
        application.state.db_pool = pool
        application.state.online_status_settings = selected_status_settings
        try:
            yield
        finally:
            await close_pool(pool)

    application = FastAPI(title="Tracker API", lifespan=lifespan)
    application.include_router(auth_router)
    application.include_router(router)
    return application


app = create_app()
