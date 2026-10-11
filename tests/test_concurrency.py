"""Stock must never be oversold, however many requests race for it."""

from __future__ import annotations

import asyncio

import httpx
from sqlalchemy import text

from app.db.session import SessionFactory
from scripts.seed import Ids
from tests.conftest import NEW_YORK, order_payload, set_stock, stock


async def test_last_units_are_sold_exactly_once(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    # Webcams: 3 left at EWR (nearest to NY), 0 anywhere else.
    await set_stock(session_factory, Ids.EWR, Ids.WEBCAM, 3)
    await set_stock(session_factory, Ids.LAX, Ids.WEBCAM, 0)
    await set_stock(session_factory, Ids.ORD, Ids.WEBCAM, 0)

    responses = await asyncio.gather(
        *(
            client.post(
                "/orders",
                json=order_payload([(Ids.WEBCAM, 1)], address=NEW_YORK),
                headers={"Idempotency-Key": f"race-{i}"},
            )
            for i in range(12)
        )
    )
    codes = [r.status_code for r in responses]

    assert codes.count(201) == 3, codes
    assert codes.count(409) == 9, codes
    assert await stock(session_factory, Ids.EWR, Ids.WEBCAM) == 0
    async with session_factory() as session:
        paid = await session.execute(text("SELECT count(*) FROM orders WHERE status='paid'"))
        assert paid.scalar_one() == 3


async def test_falls_through_to_next_warehouse_when_nearest_is_drained(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    # 1 webcam at EWR (nearest to NY), 10 at ORD. Ten concurrent orders: one gets
    # EWR, the rest must fall through to ORD instead of failing.
    await set_stock(session_factory, Ids.EWR, Ids.WEBCAM, 1)
    await set_stock(session_factory, Ids.LAX, Ids.WEBCAM, 0)

    responses = await asyncio.gather(
        *(
            client.post("/orders", json=order_payload([(Ids.WEBCAM, 1)], address=NEW_YORK))
            for _ in range(10)
        )
    )
    assert all(r.status_code == 201 for r in responses), [r.status_code for r in responses]
    warehouses = sorted(r.json()["warehouse"]["code"] for r in responses)
    assert warehouses.count("EWR") == 1
    assert warehouses.count("ORD") == 9
    assert await stock(session_factory, Ids.EWR, Ids.WEBCAM) == 0
    assert await stock(session_factory, Ids.ORD, Ids.WEBCAM) == 10 - 9
