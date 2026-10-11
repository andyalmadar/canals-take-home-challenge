"""SQLAlchemy ORM models.

Conventions:
* UUID primary keys generated application-side so ids exist before flush.
* Money is stored as integer minor units (cents) alongside an ISO-4217 currency.
* Statuses are short strings guarded by CHECK constraints rather than Postgres
  enums, because adding a value to a CHECK is a cheap, non-locking migration.
* Every constraint and index gets a deterministic name via the naming convention
  so Alembic migrations stay reviewable and reversible.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class OrderStatus(StrEnum):
    PENDING_PAYMENT = "pending_payment"
    PAID = "paid"
    PAYMENT_FAILED = "payment_failed"
    # The gateway did not answer in time: it may or may not have charged the card.
    # Stock stays reserved until a reconciliation process settles the payment.
    PAYMENT_UNKNOWN = "payment_unknown"


class PaymentStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class IdempotencyStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


def _status_check(column: str, enum: type[StrEnum]) -> CheckConstraint:
    allowed = ", ".join(f"'{v.value}'" for v in enum)
    return CheckConstraint(f"{column} IN ({allowed})", name=f"{column}_allowed")


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (CheckConstraint("unit_price_cents > 0", name="unit_price_positive"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    sku: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    unit_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class Warehouse(Base):
    __tablename__ = "warehouses"
    __table_args__ = (
        CheckConstraint("latitude BETWEEN -90 AND 90", name="latitude_range"),
        CheckConstraint("longitude BETWEEN -180 AND 180", name="longitude_range"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    latitude: Mapped[float] = mapped_column(nullable=False)
    longitude: Mapped[float] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class InventoryItem(Base):
    """Stock of one product at one warehouse.

    `quantity` is the available-to-promise count. Reservations decrement it
    atomically with a conditional UPDATE; the CHECK is the last line of defence
    against ever going negative.
    """

    __tablename__ = "inventory"
    __table_args__ = (
        CheckConstraint("quantity >= 0", name="quantity_non_negative"),
        Index("ix_inventory_product_id_warehouse_id", "product_id", "warehouse_id"),
    )

    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("warehouses.id", ondelete="CASCADE"), primary_key=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), primary_key=True
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = _updated_at()


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        _status_check("status", OrderStatus),
        CheckConstraint("total_cents >= 0", name="total_non_negative"),
        CheckConstraint(
            "shipping_latitude BETWEEN -90 AND 90 AND shipping_longitude BETWEEN -180 AND 180",
            name="shipping_coordinates_range",
        ),
        Index("ix_orders_customer_id_created_at", "customer_id", "created_at"),
        Index("ix_orders_status_created_at", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    total_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Snapshot of the shipping address as entered. Addresses are order facts,
    # not references to a mutable customer profile.
    shipping_line1: Mapped[str] = mapped_column(String(200), nullable=False)
    shipping_line2: Mapped[str | None] = mapped_column(String(200))
    shipping_city: Mapped[str] = mapped_column(String(100), nullable=False)
    shipping_region: Mapped[str | None] = mapped_column(String(100))
    shipping_postal_code: Mapped[str] = mapped_column(String(20), nullable=False)
    shipping_country: Mapped[str] = mapped_column(String(2), nullable=False)
    shipping_latitude: Mapped[float] = mapped_column(nullable=False)
    shipping_longitude: Mapped[float] = mapped_column(nullable=False)

    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    customer: Mapped[Customer] = relationship(lazy="raise")
    warehouse: Mapped[Warehouse] = relationship(lazy="raise")
    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="raise"
    )
    payments: Mapped[list[Payment]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="Payment.created_at",
        lazy="raise",
    )


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price_cents > 0", name="unit_price_positive"),
    )

    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), primary_key=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    # Price at the time of purchase. Product prices change; order history must not.
    unit_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)

    order: Mapped[Order] = relationship(back_populates="items", lazy="raise")
    product: Mapped[Product] = relationship(lazy="raise")


class Payment(Base):
    """One attempt to charge the customer for an order.

    Only the last four digits of the card are stored. The PAN is passed through
    to the gateway and never written anywhere, including logs.
    """

    __tablename__ = "payments"
    __table_args__ = (
        _status_check("status", PaymentStatus),
        CheckConstraint("amount_cents >= 0", name="amount_non_negative"),
        Index("ix_payments_order_id", "order_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    card_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    # Sent to the gateway so a retried charge can never double-bill.
    gateway_idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    provider_reference: Mapped[str | None] = mapped_column(String(128))
    failure_reason: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    order: Mapped[Order] = relationship(back_populates="payments", lazy="raise")


class IdempotencyKey(Base):
    """Server-side record of an `Idempotency-Key` header and the response it produced."""

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        _status_check("status", IdempotencyStatus),
        Index("ix_idempotency_keys_expires_at", "expires_at"),
    )

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GeocodeCacheEntry(Base):
    """Geocoding results keyed by a hash of the normalised address."""

    __tablename__ = "geocode_cache"

    address_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    normalized_address: Mapped[str] = mapped_column(Text, nullable=False)
    latitude: Mapped[float] = mapped_column(nullable=False)
    longitude: Mapped[float] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = _created_at()
