from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Response, status
from fastapi.responses import JSONResponse

from app.api.deps import OrderServiceDep
from app.schemas.orders import CreateOrderRequest, ErrorResponse, OrderResponse

router = APIRouter(prefix="/orders", tags=["orders"])

IdempotencyKeyHeader = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=255,
        description=(
            "Optional client-generated key (UUID recommended). Retrying with the same key "
            "and body returns the original response instead of creating a second order."
        ),
    ),
]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=OrderResponse,
    summary="Place an order",
    description=(
        "Reserves stock at the nearest warehouse that can fill the whole order, charges the "
        "card, and returns the order. The response status reflects the payment outcome."
    ),
    responses={
        201: {"description": "Order placed and paid."},
        402: {
            "model": OrderResponse,
            "description": "Card declined. Order kept as payment_failed.",
        },
        404: {"model": ErrorResponse, "description": "Unknown customer or product."},
        409: {
            "model": ErrorResponse,
            "description": "No single warehouse can fill the order, or a request with this "
            "Idempotency-Key is still in progress.",
        },
        422: {"model": ErrorResponse, "description": "Validation error."},
        502: {
            "model": OrderResponse,
            "description": "Payment gateway unavailable; outcome unknown. Order kept as "
            "payment_unknown with stock reserved pending reconciliation.",
        },
    },
)
async def create_order(
    payload: CreateOrderRequest,
    service: OrderServiceDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Response:
    outcome = await service.create_order(payload, idempotency_key)
    return JSONResponse(
        status_code=outcome.status_code,
        content=outcome.order.model_dump(mode="json"),
        headers={"Location": f"/orders/{outcome.order.id}"},
    )


@router.get(
    "/{order_id}",
    response_model=OrderResponse,
    summary="Fetch an order",
    responses={404: {"model": ErrorResponse}},
)
async def get_order(order_id: UUID, service: OrderServiceDep) -> OrderResponse:
    return await service.get_order(order_id)
