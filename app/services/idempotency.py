"""Idempotency-Key handling for POST /orders.

Flow:
  1. `claim` inserts the key with status in_progress. The primary key makes the
     insert the arbiter: exactly one concurrent request wins it.
  2. The winner processes the order, then `complete` stores the response.
  3. Later requests with the same key and body get the stored response back.
     Same key, different body -> 422. Same key while still in progress -> 409.
  4. If processing fails before anything was persisted, `release` deletes the
     claim so the client can retry.

Stored responses expire after a TTL; a periodic job deletes expired rows
(see README).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, update
from sqlalchemy.dialects.postgresql import insert

from app.db.models import IdempotencyKey, IdempotencyStatus
from app.db.session import SessionFactory
from app.errors import IdempotencyKeyReused, IdempotentRequestInProgress


def fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class StoredResponse:
    status_code: int
    body: dict[str, Any]


class IdempotencyStore:
    def __init__(self, session_factory: SessionFactory, ttl: timedelta) -> None:
        self._sessions = session_factory
        self._ttl = ttl

    async def claim(self, key: str, request_hash: str) -> StoredResponse | None:
        """Claim `key` for this request. Returns a stored response if one exists."""
        now = datetime.now(UTC)
        async with self._sessions() as session, session.begin():
            inserted = cast(
                "CursorResult[Any]",
                await session.execute(
                    insert(IdempotencyKey)
                    .values(
                        key=key,
                        request_hash=request_hash,
                        status=IdempotencyStatus.IN_PROGRESS,
                        expires_at=now + self._ttl,
                    )
                    .on_conflict_do_nothing(index_elements=[IdempotencyKey.key])
                ),
            )
            if inserted.rowcount == 1:
                return None

            existing = await session.get(IdempotencyKey, key)
            if existing is None:  # pragma: no cover - lost a race with release()
                raise IdempotentRequestInProgress()
            if existing.request_hash != request_hash:
                raise IdempotencyKeyReused()
            if existing.status == IdempotencyStatus.IN_PROGRESS:
                raise IdempotentRequestInProgress()
            if existing.response_status is None or existing.response_body is None:
                raise IdempotentRequestInProgress()  # pragma: no cover - defensive
            return StoredResponse(existing.response_status, existing.response_body)

    async def complete(self, key: str, response: StoredResponse) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(IdempotencyKey)
                .where(IdempotencyKey.key == key)
                .values(
                    status=IdempotencyStatus.COMPLETED,
                    response_status=response.status_code,
                    response_body=response.body,
                )
            )

    async def release(self, key: str) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(
                delete(IdempotencyKey).where(
                    IdempotencyKey.key == key,
                    IdempotencyKey.status == IdempotencyStatus.IN_PROGRESS,
                )
            )
