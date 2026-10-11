from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from app.db.session import SessionFactory
from scripts.seed import Ids
from tests.conftest import (
    DECLINED_CARD,
    NEW_YORK,
    TIMEOUT_CARD,
    order_payload,
    stock,
)


async def test_picks_closest_warehouse_with_full_stock(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    before = await stock(session_factory, Ids.LAX, Ids.MOUSE)

    resp = await client.post("/orders", json=order_payload([(Ids.KEYBOARD, 1), (Ids.MOUSE, 2)]))

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "paid"
    assert body["warehouse"]["code"] == "LAX"
    assert body["total_cents"] == 8999 + 2 * 3999
    assert body["payment"]["status"] == "succeeded"
    assert body["payment"]["card_last4"] == "4242"
    assert body["payment"]["provider_reference"].startswith("mock_ch_")
    assert resp.headers["location"] == f"/orders/{body['id']}"
    assert await stock(session_factory, Ids.LAX, Ids.MOUSE) == before - 2

    fetched = await client.get(resp.headers["location"])
    assert fetched.status_code == 200
    assert fetched.json() == body


async def test_skips_nearer_warehouse_missing_a_product(client: httpx.AsyncClient) -> None:
    # LAX is closest to SF but carries no headsets; DFW is the next nearest with both.
    resp = await client.post("/orders", json=order_payload([(Ids.KEYBOARD, 1), (Ids.HEADSET, 1)]))
    assert resp.status_code == 201, resp.text
    assert resp.json()["warehouse"]["code"] == "DFW"


async def test_only_one_warehouse_has_everything(client: httpx.AsyncClient) -> None:
    # Monitor + headset + webcam together exist only at ORD, even for a NY customer.
    resp = await client.post(
        "/orders",
        json=order_payload([(Ids.MONITOR, 1), (Ids.HEADSET, 1), (Ids.WEBCAM, 1)], address=NEW_YORK),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["warehouse"]["code"] == "ORD"


async def test_duplicate_lines_are_merged(client: httpx.AsyncClient) -> None:
    resp = await client.post("/orders", json=order_payload([(Ids.MOUSE, 1), (Ids.MOUSE, 3)]))
    assert resp.status_code == 201, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["quantity"] == 4


async def test_no_warehouse_can_fulfill(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    resp = await client.post("/orders", json=order_payload([(Ids.MONITOR, 11)]))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "no_warehouse_can_fulfill"
    async with session_factory() as session:
        assert (await session.execute(text("SELECT count(*) FROM orders"))).scalar_one() == 0


async def test_declined_card_keeps_order_and_releases_stock(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    before = await stock(session_factory, Ids.LAX, Ids.MONITOR)

    resp = await client.post("/orders", json=order_payload([(Ids.MONITOR, 1)], card=DECLINED_CARD))

    assert resp.status_code == 402, resp.text
    body = resp.json()
    assert body["status"] == "payment_failed"
    assert body["payment"]["status"] == "failed"
    assert body["payment"]["failure_reason"] == "card_declined"
    assert await stock(session_factory, Ids.LAX, Ids.MONITOR) == before

    fetched = await client.get(f"/orders/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "payment_failed"


async def test_gateway_timeout_keeps_stock_reserved(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    before = await stock(session_factory, Ids.LAX, Ids.DESK_MAT)

    resp = await client.post("/orders", json=order_payload([(Ids.DESK_MAT, 2)], card=TIMEOUT_CARD))

    assert resp.status_code == 502, resp.text
    body = resp.json()
    assert body["status"] == "payment_unknown"
    assert body["payment"]["status"] == "unknown"
    # Outcome unknown: the card may have been charged, so the stock stays held.
    assert await stock(session_factory, Ids.LAX, Ids.DESK_MAT) == before - 2


async def test_unknown_customer(client: httpx.AsyncClient) -> None:
    resp = await client.post("/orders", json=order_payload([(Ids.MOUSE, 1)], customer_id=uuid4()))
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "customer_not_found"


async def test_unknown_product(client: httpx.AsyncClient) -> None:
    bogus = uuid4()
    resp = await client.post("/orders", json=order_payload([(Ids.MOUSE, 1), (bogus, 1)]))
    assert resp.status_code == 404
    error = resp.json()["error"]
    assert error["code"] == "product_not_found"
    assert error["details"]["product_ids"] == [str(bogus)]


async def test_get_missing_order(client: httpx.AsyncClient) -> None:
    resp = await client.get(f"/orders/{uuid4()}")
    assert resp.status_code == 404


@pytest.mark.parametrize(
    ("mutation", "expected_loc"),
    [
        (lambda p: p["items"].clear(), ["body", "items"]),
        (lambda p: p["items"][0].update(quantity=0), ["body", "items", 0, "quantity"]),
        (
            lambda p: p["payment"].update(card_number="4242424242424241"),
            ["body", "payment", "card_number"],
        ),
        (lambda p: p["payment"].update(card_number="12345"), ["body", "payment", "card_number"]),
        (
            lambda p: p["shipping_address"].update(country="USA"),
            ["body", "shipping_address", "country"],
        ),
        (lambda p: p.update(unexpected=True), ["body", "unexpected"]),
    ],
)
async def test_validation_errors(
    client: httpx.AsyncClient, mutation: object, expected_loc: list[object]
) -> None:
    payload = order_payload([(Ids.MOUSE, 1)])
    mutation(payload)  # type: ignore[operator]
    resp = await client.post("/orders", json=payload)
    assert resp.status_code == 422, resp.text
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert expected_loc in [e["loc"] for e in error["details"]["errors"]]
    # The response must never echo the submitted card number.
    assert "4242424242424241" not in resp.text


async def test_card_number_never_persisted(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    resp = await client.post(
        "/orders",
        json=order_payload([(Ids.MOUSE, 1)]),
        headers={"Idempotency-Key": "pan-check"},
    )
    assert resp.status_code == 201, resp.text
    async with session_factory() as session:
        dump = await session.execute(
            text(
                "SELECT string_agg(t::text, ' ') FROM ("
                "SELECT p::text t FROM payments p UNION ALL "
                "SELECT k::text FROM idempotency_keys k UNION ALL "
                "SELECT o::text FROM orders o) rows"
            )
        )
        assert "4242424242424242" not in (dump.scalar_one() or "")
