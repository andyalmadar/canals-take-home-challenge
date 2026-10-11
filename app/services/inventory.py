"""Atomic stock reservation and release.

Each product row is updated with `WHERE quantity >= :n`. Under READ COMMITTED,
Postgres re-evaluates that predicate after waiting on a row lock, so two orders
racing for the last unit cannot both succeed: one sees rowcount 0 and backs out.
Rows are always touched in product-id order so concurrent orders for overlapping
products acquire locks in the same sequence and cannot deadlock each other.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import InventoryItem


async def try_reserve(session: AsyncSession, warehouse_id: UUID, items: Mapping[UUID, int]) -> bool:
    """Decrement stock for every item at `warehouse_id`.

    Returns False as soon as one product lacks stock. The caller is expected to
    roll back the enclosing savepoint in that case; partial decrements made
    before the failure are undone by that rollback.
    """
    for product_id in sorted(items):
        quantity = items[product_id]
        result = cast(
            "CursorResult[Any]",
            await session.execute(
                update(InventoryItem)
                .where(
                    InventoryItem.warehouse_id == warehouse_id,
                    InventoryItem.product_id == product_id,
                    InventoryItem.quantity >= quantity,
                )
                .values(quantity=InventoryItem.quantity - quantity, updated_at=func.now())
            ),
        )
        if result.rowcount != 1:
            return False
    return True


async def release(session: AsyncSession, warehouse_id: UUID, items: Mapping[UUID, int]) -> None:
    """Return previously reserved stock. Used when a payment is declined."""
    for product_id in sorted(items):
        quantity = items[product_id]
        await session.execute(
            update(InventoryItem)
            .where(
                InventoryItem.warehouse_id == warehouse_id,
                InventoryItem.product_id == product_id,
            )
            .values(quantity=InventoryItem.quantity + quantity, updated_at=func.now())
        )
