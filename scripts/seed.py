"""Idempotent reference data for local development, demos and tests.

Run with `python -m scripts.seed`. Re-running updates master data in place and
resets inventory to the seeded quantities; it never touches orders.

IDs are fixed so the README's curl examples work out of the box.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Base, Customer, InventoryItem, Product, Warehouse
from app.db.session import SessionFactory, create_engine, create_session_factory


@dataclass(frozen=True)
class Ids:
    # Customers
    ADA = UUID("00000000-0000-4000-8000-000000000001")
    GRACE = UUID("00000000-0000-4000-8000-000000000002")
    # Products
    KEYBOARD = UUID("10000000-0000-4000-8000-000000000001")
    MOUSE = UUID("10000000-0000-4000-8000-000000000002")
    MONITOR = UUID("10000000-0000-4000-8000-000000000003")
    HEADSET = UUID("10000000-0000-4000-8000-000000000004")
    WEBCAM = UUID("10000000-0000-4000-8000-000000000005")
    DESK_MAT = UUID("10000000-0000-4000-8000-000000000006")
    # Warehouses
    LAX = UUID("20000000-0000-4000-8000-000000000001")
    DFW = UUID("20000000-0000-4000-8000-000000000002")
    ORD = UUID("20000000-0000-4000-8000-000000000003")
    EWR = UUID("20000000-0000-4000-8000-000000000004")


CUSTOMERS = [
    {"id": Ids.ADA, "email": "ada@example.com", "full_name": "Ada Lovelace"},
    {"id": Ids.GRACE, "email": "grace@example.com", "full_name": "Grace Hopper"},
]

PRODUCTS = [
    {
        "id": Ids.KEYBOARD,
        "sku": "KB-MECH-01",
        "name": "Mechanical Keyboard",
        "unit_price_cents": 8999,
    },
    {"id": Ids.MOUSE, "sku": "MS-WRLS-01", "name": "Wireless Mouse", "unit_price_cents": 3999},
    {"id": Ids.MONITOR, "sku": "MN-27-4K", "name": '27" 4K Monitor', "unit_price_cents": 29999},
    {
        "id": Ids.HEADSET,
        "sku": "HS-ANC-01",
        "name": "Noise-cancelling Headset",
        "unit_price_cents": 19999,
    },
    {"id": Ids.WEBCAM, "sku": "WC-4K-01", "name": "4K Webcam", "unit_price_cents": 12999},
    {"id": Ids.DESK_MAT, "sku": "DM-XL-01", "name": "Desk Mat XL", "unit_price_cents": 2499},
]

WAREHOUSES = [
    {
        "id": Ids.LAX,
        "code": "LAX",
        "name": "Los Angeles",
        "latitude": 33.9416,
        "longitude": -118.4085,
    },
    {"id": Ids.DFW, "code": "DFW", "name": "Dallas", "latitude": 32.8998, "longitude": -97.0403},
    {"id": Ids.ORD, "code": "ORD", "name": "Chicago", "latitude": 41.9742, "longitude": -87.9073},
    {"id": Ids.EWR, "code": "EWR", "name": "Newark", "latitude": 40.6895, "longitude": -74.1745},
]

# warehouse -> product -> quantity. Gaps are deliberate so the "single warehouse
# must have everything" rule actually bites:
#   * LAX has no headsets, DFW has no webcams, EWR has no monitors or desk mats.
#   * Only ORD stocks all six products.
INVENTORY: dict[UUID, dict[UUID, int]] = {
    Ids.LAX: {Ids.KEYBOARD: 50, Ids.MOUSE: 100, Ids.MONITOR: 10, Ids.WEBCAM: 25, Ids.DESK_MAT: 200},
    Ids.DFW: {Ids.KEYBOARD: 20, Ids.MOUSE: 40, Ids.MONITOR: 5, Ids.HEADSET: 15, Ids.DESK_MAT: 80},
    Ids.ORD: {
        Ids.KEYBOARD: 30,
        Ids.MOUSE: 60,
        Ids.MONITOR: 8,
        Ids.HEADSET: 20,
        Ids.WEBCAM: 10,
        Ids.DESK_MAT: 150,
    },
    Ids.EWR: {Ids.KEYBOARD: 10, Ids.MOUSE: 30, Ids.HEADSET: 25, Ids.WEBCAM: 5},
}


async def _upsert(
    session: AsyncSession, model: type[Base], rows: list[dict[str, object]], key: str
) -> None:
    stmt = insert(model).values(rows)
    update_cols = {c: getattr(stmt.excluded, c) for c in rows[0] if c != key}
    await session.execute(stmt.on_conflict_do_update(index_elements=[key], set_=update_cols))


async def seed(session_factory: SessionFactory) -> None:
    async with session_factory() as session, session.begin():
        await _upsert(session, Customer, CUSTOMERS, "id")
        await _upsert(session, Product, [{**p, "currency": "USD"} for p in PRODUCTS], "id")
        await _upsert(session, Warehouse, WAREHOUSES, "id")

        inventory_rows: list[dict[str, object]] = [
            {"warehouse_id": wid, "product_id": pid, "quantity": qty}
            for wid, stock in INVENTORY.items()
            for pid, qty in stock.items()
        ]
        stmt = insert(InventoryItem).values(inventory_rows)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["warehouse_id", "product_id"],
                set_={"quantity": stmt.excluded.quantity},
            )
        )


async def main() -> None:
    engine = create_engine(get_settings())
    try:
        await seed(create_session_factory(engine))
    finally:
        await engine.dispose()
    print("seeded reference data")


if __name__ == "__main__":
    asyncio.run(main())
