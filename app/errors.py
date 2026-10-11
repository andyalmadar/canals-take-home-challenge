"""Domain errors. The API layer maps these to HTTP responses in one place."""

from __future__ import annotations

from typing import Any
from uuid import UUID


class DomainError(Exception):
    code: str = "domain_error"
    http_status: int = 400

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class CustomerNotFound(DomainError):
    code = "customer_not_found"
    http_status = 404

    def __init__(self, customer_id: UUID) -> None:
        super().__init__("Customer does not exist.", {"customer_id": str(customer_id)})


class ProductNotFound(DomainError):
    code = "product_not_found"
    http_status = 404

    def __init__(self, product_ids: list[UUID]) -> None:
        super().__init__(
            "One or more products do not exist.",
            {"product_ids": [str(p) for p in product_ids]},
        )


class MixedCurrencies(DomainError):
    code = "mixed_currencies"
    http_status = 422

    def __init__(self, currencies: set[str]) -> None:
        super().__init__(
            "All items in an order must be priced in the same currency.",
            {"currencies": sorted(currencies)},
        )


class NoWarehouseCanFulfill(DomainError):
    code = "no_warehouse_can_fulfill"
    http_status = 409

    def __init__(self, product_ids: list[UUID]) -> None:
        super().__init__(
            "No single warehouse has sufficient stock for every item in the order.",
            {"product_ids": [str(p) for p in product_ids]},
        )


class OrderNotFound(DomainError):
    code = "order_not_found"
    http_status = 404

    def __init__(self, order_id: UUID) -> None:
        super().__init__("Order does not exist.", {"order_id": str(order_id)})


class IdempotencyKeyReused(DomainError):
    code = "idempotency_key_reused"
    http_status = 422

    def __init__(self) -> None:
        super().__init__(
            "This Idempotency-Key was already used with a different request body.",
        )


class IdempotentRequestInProgress(DomainError):
    code = "idempotent_request_in_progress"
    http_status = 409

    def __init__(self) -> None:
        super().__init__(
            "A request with this Idempotency-Key is still being processed. Retry shortly.",
        )
