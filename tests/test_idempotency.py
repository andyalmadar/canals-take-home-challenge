from __future__ import annotations

import asyncio

import httpx
from sqlalchemy import text

from app.db.session import SessionFactory
from scripts.seed import Ids
from tests.conftest import DECLINED_CARD, TIMEOUT_CARD, order_payload, stock


async def test_replay_returns_original_response_without_second_order(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    payload = order_payload([(Ids.KEYBOARD, 1)])
    headers = {"Idempotency-Key": "k-1"}

    first = await client.post("/orders", json=payload, headers=headers)
    second = await client.post("/orders", json=payload, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()
    async with session_factory() as session:
        assert (await session.execute(text("SELECT count(*) FROM orders"))).scalar_one() == 1
    assert await stock(session_factory, Ids.LAX, Ids.KEYBOARD) == 49


async def test_replay_preserves_failure_status(client: httpx.AsyncClient) -> None:
    payload = order_payload([(Ids.KEYBOARD, 1)], card=TIMEOUT_CARD)
    headers = {"Idempotency-Key": "k-timeout"}

    first = await client.post("/orders", json=payload, headers=headers)
    second = await client.post("/orders", json=payload, headers=headers)

    assert first.status_code == 502
    assert second.status_code == 502
    assert second.json()["id"] == first.json()["id"]


async def test_same_key_different_body_is_rejected(client: httpx.AsyncClient) -> None:
    headers = {"Idempotency-Key": "k-2"}
    first = await client.post("/orders", json=order_payload([(Ids.KEYBOARD, 1)]), headers=headers)
    assert first.status_code == 201

    second = await client.post("/orders", json=order_payload([(Ids.KEYBOARD, 2)]), headers=headers)
    assert second.status_code == 422
    assert second.json()["error"]["code"] == "idempotency_key_reused"


async def test_declined_outcome_is_replayed_not_retried(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    payload = order_payload([(Ids.MONITOR, 1)], card=DECLINED_CARD)
    headers = {"Idempotency-Key": "k-declined"}

    first = await client.post("/orders", json=payload, headers=headers)
    second = await client.post("/orders", json=payload, headers=headers)

    assert first.status_code == second.status_code == 402
    async with session_factory() as session:
        assert (await session.execute(text("SELECT count(*) FROM payments"))).scalar_one() == 1


async def test_key_is_released_when_nothing_was_persisted(client: httpx.AsyncClient) -> None:
    headers = {"Idempotency-Key": "k-3"}
    rejected = await client.post(
        "/orders", json=order_payload([(Ids.MONITOR, 11)]), headers=headers
    )
    assert rejected.status_code == 409

    # Same key, same body, after stock has changed: should be processed afresh, not replayed.
    accepted = await client.post(
        "/orders", json=order_payload([(Ids.MONITOR, 11)]), headers=headers
    )
    assert accepted.status_code == 409  # still no stock, but it was evaluated again...

    retried = await client.post("/orders", json=order_payload([(Ids.MONITOR, 1)]), headers=headers)
    assert retried.status_code == 201  # ...and the key was free to be used with a new body


async def test_concurrent_same_key_creates_one_order(
    client: httpx.AsyncClient, session_factory: SessionFactory
) -> None:
    payload = order_payload([(Ids.MOUSE, 1)])
    headers = {"Idempotency-Key": "k-race"}

    responses = await asyncio.gather(
        *(client.post("/orders", json=payload, headers=headers) for _ in range(8))
    )
    codes = [r.status_code for r in responses]
    # Exactly one request wins the key. Losers see either the finished response (201,
    # replayed) or 409 if they raced ahead of completion. Never a second order.
    assert 201 in codes, codes
    assert set(codes) <= {201, 409}, codes
    async with session_factory() as session:
        assert (await session.execute(text("SELECT count(*) FROM orders"))).scalar_one() == 1
