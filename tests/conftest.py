"""Integration test fixtures.

Tests run against a real Postgres (TEST_DATABASE_URL, default matches
docker-compose). Migrations are applied once per session with Alembic; every
test starts from freshly seeded reference data with no orders.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from app.config import Settings
from app.db.session import SessionFactory
from app.main import create_app
from scripts.seed import Ids, seed

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://orders:orders@localhost:5432/orders_test"
)

ALL_TABLES = (
    "payments",
    "order_items",
    "orders",
    "idempotency_keys",
    "geocode_cache",
    "inventory",
    "products",
    "warehouses",
    "customers",
)


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(
        app_env="test",
        database_url=TEST_DATABASE_URL,
        log_json=False,
        log_level="WARNING",
        payment_timeout_seconds=1.0,
    )


@pytest.fixture(scope="session", autouse=True)
def migrated_database(settings: Settings) -> None:
    env = {**os.environ, "DATABASE_URL": settings.database_url}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True, env=env)


@pytest.fixture(scope="session")
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture(scope="session")
def session_factory(app: FastAPI) -> SessionFactory:
    factory: SessionFactory = app.state.session_factory
    return factory


@pytest.fixture(autouse=True)
async def clean_database(session_factory: SessionFactory) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(text(f"TRUNCATE {', '.join(ALL_TABLES)} CASCADE"))
    await seed(session_factory)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---- helpers -----------------------------------------------------------------

APPROVED_CARD = "4242424242424242"
DECLINED_CARD = "4000000000000002"
TIMEOUT_CARD = "4000000000000119"

SAN_FRANCISCO = {
    "line1": "1 Market St",
    "city": "San Francisco",
    "region": "CA",
    "postal_code": "94105",
    "country": "US",
}
NEW_YORK = {"line1": "350 5th Ave", "city": "New York", "postal_code": "10118", "country": "US"}


def order_payload(
    items: list[tuple[UUID, int]],
    *,
    address: dict[str, str] = SAN_FRANCISCO,
    card: str = APPROVED_CARD,
    customer_id: UUID = Ids.ADA,
) -> dict[str, Any]:
    return {
        "customer_id": str(customer_id),
        "shipping_address": dict(address),
        "items": [{"product_id": str(pid), "quantity": qty} for pid, qty in items],
        "payment": {"card_number": card},
    }


async def stock(session_factory: SessionFactory, warehouse_id: UUID, product_id: UUID) -> int:
    async with session_factory() as session:
        result = await session.execute(
            text("SELECT quantity FROM inventory WHERE warehouse_id=:w AND product_id=:p"),
            {"w": warehouse_id, "p": product_id},
        )
        return int(result.scalar_one())


async def set_stock(
    session_factory: SessionFactory, warehouse_id: UUID, product_id: UUID, quantity: int
) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(
            text("UPDATE inventory SET quantity=:q WHERE warehouse_id=:w AND product_id=:p"),
            {"q": quantity, "w": warehouse_id, "p": product_id},
        )
