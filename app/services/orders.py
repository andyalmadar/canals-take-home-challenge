"""Order creation.

The flow is split into two database transactions around the payment call so no
transaction is ever held open while waiting on an external service:

  tx1  validate customer/products, pick the nearest warehouse that has every
       item, atomically reserve stock, persist the order as pending_payment.
  ---  call the payment gateway (bounded by a timeout, with an idempotency key).
  tx2  approved  -> paid
       declined  -> payment_failed, stock released
       unknown   -> payment_unknown, stock kept, flagged for reconciliation

A crash between tx1 and tx2 leaves an order in pending_payment with stock
reserved. That is deliberate: the reconciliation job (not part of this
exercise) queries the gateway by idempotency key and settles the order.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.db.models import (
    Customer,
    Order,
    OrderItem,
    OrderStatus,
    Payment,
    PaymentStatus,
    Product,
)
from app.db.session import SessionFactory
from app.errors import (
    CustomerNotFound,
    MixedCurrencies,
    NoWarehouseCanFulfill,
    OrderNotFound,
    ProductNotFound,
)
from app.integrations.geocoding import Address, Geocoder
from app.integrations.payments import (
    ChargeRequest,
    ChargeResult,
    PaymentGateway,
    PaymentGatewayError,
)
from app.logging import get_logger
from app.schemas.orders import (
    AddressOut,
    CreateOrderRequest,
    OrderItemIn,
    OrderItemOut,
    OrderResponse,
    PaymentOut,
    WarehouseOut,
)
from app.services import inventory
from app.services.geo import Coordinates, haversine_km
from app.services.idempotency import IdempotencyStore, StoredResponse, fingerprint
from app.services.warehouse_selection import rank_candidates

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CreateOrderOutcome:
    status_code: int
    order: OrderResponse

    def as_stored(self) -> StoredResponse:
        return StoredResponse(self.status_code, self.order.model_dump(mode="json"))


_STATUS_CODES: dict[str, int] = {
    OrderStatus.PAID: 201,
    OrderStatus.PAYMENT_FAILED: 402,
    OrderStatus.PAYMENT_UNKNOWN: 502,
}


def merge_items(items: Iterable[OrderItemIn]) -> dict[UUID, int]:
    """Collapse repeated product lines into one quantity per product."""
    merged: dict[UUID, int] = {}
    for item in items:
        merged[item.product_id] = merged.get(item.product_id, 0) + item.quantity
    return merged


class OrderService:
    def __init__(
        self,
        session_factory: SessionFactory,
        geocoder: Geocoder,
        payment_gateway: PaymentGateway,
        settings: Settings,
    ) -> None:
        self._sessions = session_factory
        self._geocoder = geocoder
        self._payments = payment_gateway
        self._payment_timeout = settings.payment_timeout_seconds
        self._idempotency = IdempotencyStore(
            session_factory, timedelta(hours=settings.idempotency_key_ttl_hours)
        )

    async def create_order(
        self, request: CreateOrderRequest, idempotency_key: str | None
    ) -> CreateOrderOutcome:
        if idempotency_key is None:
            return await self._create_order(request)

        stored = await self._idempotency.claim(
            idempotency_key, fingerprint(request.canonical_payload())
        )
        if stored is not None:
            log.info("order_idempotent_replay", idempotency_key=idempotency_key)
            return CreateOrderOutcome(stored.status_code, OrderResponse.model_validate(stored.body))

        try:
            outcome = await self._create_order(request)
        except BaseException:
            # Nothing was persisted for this key; let the client retry it.
            await self._idempotency.release(idempotency_key)
            raise
        await self._idempotency.complete(idempotency_key, outcome.as_stored())
        return outcome

    async def get_order(self, order_id: UUID) -> OrderResponse:
        async with self._sessions() as session:
            order = await _load_order(session, order_id)
        return _to_response(order)

    # -- internals -----------------------------------------------------------

    async def _create_order(self, request: CreateOrderRequest) -> CreateOrderOutcome:
        items = merge_items(request.items)
        address = Address(**request.shipping_address.model_dump())
        destination = await self._geocoder.geocode(address)

        order_id, warehouse_id, charge_request = await self._reserve_and_persist(
            request, items, destination
        )
        charge = await self._charge(charge_request)
        order = await self._settle(order_id, warehouse_id, items, charge)

        response = _to_response(order)
        log.info(
            "order_created",
            order_id=str(order.id),
            status=order.status,
            warehouse=response.warehouse.code,
            total_cents=order.total_cents,
        )
        return CreateOrderOutcome(_STATUS_CODES[order.status], response)

    async def _reserve_and_persist(
        self,
        request: CreateOrderRequest,
        items: dict[UUID, int],
        destination: Coordinates,
    ) -> tuple[UUID, UUID, ChargeRequest]:
        async with self._sessions() as session, session.begin():
            customer = await session.get(Customer, request.customer_id)
            if customer is None:
                raise CustomerNotFound(request.customer_id)

            products = {
                p.id: p
                for p in (await session.scalars(select(Product).where(Product.id.in_(items)))).all()
            }
            missing = sorted(set(items) - set(products), key=str)
            if missing:
                raise ProductNotFound(missing)

            currencies = {p.currency for p in products.values()}
            if len(currencies) != 1:
                raise MixedCurrencies(currencies)
            currency = currencies.pop()

            candidates = await rank_candidates(session, items, destination)
            chosen = None
            for candidate in candidates:
                # Savepoint per attempt: a failed reservation (someone else took
                # the stock between our SELECT and UPDATE) is rolled back cleanly
                # and we fall through to the next-nearest warehouse.
                savepoint = await session.begin_nested()
                if await inventory.try_reserve(session, candidate.id, items):
                    await savepoint.commit()
                    chosen = candidate
                    break
                await savepoint.rollback()
            if chosen is None:
                raise NoWarehouseCanFulfill(sorted(items, key=str))

            total_cents = sum(products[pid].unit_price_cents * qty for pid, qty in items.items())
            order = Order(
                id=uuid4(),
                customer_id=customer.id,
                warehouse_id=chosen.id,
                status=OrderStatus.PENDING_PAYMENT,
                currency=currency,
                total_cents=total_cents,
                shipping_line1=request.shipping_address.line1,
                shipping_line2=request.shipping_address.line2,
                shipping_city=request.shipping_address.city,
                shipping_region=request.shipping_address.region,
                shipping_postal_code=request.shipping_address.postal_code,
                shipping_country=request.shipping_address.country,
                shipping_latitude=destination.latitude,
                shipping_longitude=destination.longitude,
            )
            session.add(order)
            session.add_all(
                OrderItem(
                    order_id=order.id,
                    product_id=pid,
                    quantity=qty,
                    unit_price_cents=products[pid].unit_price_cents,
                )
                for pid, qty in items.items()
            )
            card_number = request.payment.card_number.get_secret_value()
            payment = Payment(
                id=uuid4(),
                order_id=order.id,
                status=PaymentStatus.PENDING,
                amount_cents=total_cents,
                currency=currency,
                card_last4=card_number[-4:],
                gateway_idempotency_key=f"order-{order.id}-attempt-1",
            )
            session.add(payment)

            charge_request = ChargeRequest(
                card_number=card_number,
                amount_cents=total_cents,
                currency=currency,
                description=f"Order {order.id}",
                idempotency_key=payment.gateway_idempotency_key,
            )
            return order.id, chosen.id, charge_request

    async def _charge(self, request: ChargeRequest) -> ChargeResult | None:
        """Call the gateway. Returns None when the outcome is unknown."""
        try:
            async with asyncio.timeout(self._payment_timeout):
                return await self._payments.charge(request)
        except TimeoutError:
            log.warning("payment_timeout", idempotency_key=request.idempotency_key)
        except PaymentGatewayError as exc:
            log.warning(
                "payment_gateway_error", idempotency_key=request.idempotency_key, error=str(exc)
            )
        return None

    async def _settle(
        self,
        order_id: UUID,
        warehouse_id: UUID,
        items: dict[UUID, int],
        charge: ChargeResult | None,
    ) -> Order:
        async with self._sessions() as session, session.begin():
            order = await _load_order(session, order_id, for_update=True)
            payment = order.payments[-1]

            if charge is None:
                order.status = OrderStatus.PAYMENT_UNKNOWN
                payment.status = PaymentStatus.UNKNOWN
                payment.failure_reason = "gateway_unavailable"
            elif charge.approved:
                order.status = OrderStatus.PAID
                payment.status = PaymentStatus.SUCCEEDED
                payment.provider_reference = charge.provider_reference
            else:
                order.status = OrderStatus.PAYMENT_FAILED
                payment.status = PaymentStatus.FAILED
                payment.failure_reason = charge.decline_reason
                await inventory.release(session, warehouse_id, items)
            await session.flush()
            await session.refresh(order, attribute_names=["updated_at"])
        return order


async def _load_order(session: AsyncSession, order_id: UUID, *, for_update: bool = False) -> Order:
    stmt = (
        select(Order)
        .where(Order.id == order_id)
        .options(
            selectinload(Order.items).selectinload(OrderItem.product),
            selectinload(Order.payments),
            selectinload(Order.warehouse),
        )
    )
    if for_update:
        stmt = stmt.with_for_update(of=Order)
    order = await session.scalar(stmt)
    if order is None:
        raise OrderNotFound(order_id)
    return order


def _to_response(order: Order) -> OrderResponse:
    destination = Coordinates(order.shipping_latitude, order.shipping_longitude)
    warehouse = order.warehouse
    payment = order.payments[-1] if order.payments else None
    return OrderResponse(
        id=order.id,
        status=order.status,
        customer_id=order.customer_id,
        currency=order.currency,
        total_cents=order.total_cents,
        shipping_address=AddressOut(
            line1=order.shipping_line1,
            line2=order.shipping_line2,
            city=order.shipping_city,
            region=order.shipping_region,
            postal_code=order.shipping_postal_code,
            country=order.shipping_country,
            latitude=order.shipping_latitude,
            longitude=order.shipping_longitude,
        ),
        warehouse=WarehouseOut(
            id=warehouse.id,
            code=warehouse.code,
            name=warehouse.name,
            distance_km=round(
                haversine_km(destination, Coordinates(warehouse.latitude, warehouse.longitude)), 1
            ),
        ),
        items=[
            OrderItemOut(
                product_id=item.product_id,
                sku=item.product.sku,
                name=item.product.name,
                quantity=item.quantity,
                unit_price_cents=item.unit_price_cents,
                line_total_cents=item.unit_price_cents * item.quantity,
            )
            for item in sorted(order.items, key=lambda i: i.product.sku)
        ],
        payment=PaymentOut(
            id=payment.id,
            status=payment.status,
            amount_cents=payment.amount_cents,
            currency=payment.currency,
            card_last4=payment.card_last4,
            provider_reference=payment.provider_reference,
            failure_reason=payment.failure_reason,
        )
        if payment
        else None,
        created_at=order.created_at,
        updated_at=order.updated_at,
    )


def build_order_service(settings: Settings, session_factory: SessionFactory) -> OrderService:
    from app.integrations.geocoding import CachedGeocoder, MockGeocoder
    from app.integrations.payments import MockPaymentGateway

    geocoder: Geocoder = CachedGeocoder(MockGeocoder(), session_factory)
    gateway: PaymentGateway = MockPaymentGateway(settings.payment_mock_latency_seconds)
    return OrderService(session_factory, geocoder, gateway, settings)
