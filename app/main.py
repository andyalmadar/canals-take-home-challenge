from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health, orders
from app.api.errors import register_error_handlers
from app.api.middleware import RequestContextMiddleware
from app.config import Settings, get_settings
from app.db.session import create_engine, create_session_factory
from app.logging import configure_logging, get_logger
from app.services.orders import build_order_service

log = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        session_factory = create_session_factory(engine)
        app.state.settings = settings
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.order_service = build_order_service(settings, session_factory)
        log.info("startup", env=settings.app_env)
        try:
            yield
        finally:
            await engine.dispose()
            log.info("shutdown")

    app = FastAPI(
        title="Orders API",
        version="0.1.0",
        description="Minimal order management service for an e-commerce platform.",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)
    app.include_router(health.router)
    app.include_router(orders.router)
    return app


app = create_app()
