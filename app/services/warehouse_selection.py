"""Find warehouses that can fill an entire order, ranked by distance."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Integer, Uuid, column, func, select, values
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import InventoryItem, Warehouse
from app.services.geo import Coordinates, haversine_km


@dataclass(frozen=True, slots=True)
class WarehouseCandidate:
    id: UUID
    code: str
    name: str
    coordinates: Coordinates
    distance_km: float


async def rank_candidates(
    session: AsyncSession, items: Mapping[UUID, int], destination: Coordinates
) -> list[WarehouseCandidate]:
    """Warehouses holding at least the requested quantity of every product.

    The filter runs in SQL: join the requested (product, quantity) pairs against
    inventory and keep warehouses that match all of them. Only the survivors,
    typically a handful, are ranked in Python by great-circle distance. At a
    scale where that ranking matters, store warehouse locations as PostGIS
    geography and let a KNN index do it.
    """
    requested = values(
        column("product_id", Uuid),
        column("quantity", Integer),
        name="requested",
    ).data([(product_id, quantity) for product_id, quantity in items.items()])

    stmt = (
        select(
            Warehouse.id, Warehouse.code, Warehouse.name, Warehouse.latitude, Warehouse.longitude
        )
        .join(InventoryItem, InventoryItem.warehouse_id == Warehouse.id)
        .join(requested, requested.c.product_id == InventoryItem.product_id)
        .where(InventoryItem.quantity >= requested.c.quantity)
        .group_by(Warehouse.id)
        .having(func.count() == len(items))
    )
    rows = (await session.execute(stmt)).all()

    candidates = [
        WarehouseCandidate(
            id=row.id,
            code=row.code,
            name=row.name,
            coordinates=Coordinates(row.latitude, row.longitude),
            distance_km=haversine_km(destination, Coordinates(row.latitude, row.longitude)),
        )
        for row in rows
    ]
    # Tie-break on code so the choice is deterministic for equidistant warehouses.
    candidates.sort(key=lambda c: (c.distance_km, c.code))
    return candidates
