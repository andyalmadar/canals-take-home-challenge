from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
)

from app.services.cards import luhn_valid

NonBlankStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class StrictModel(BaseModel):
    """Request models reject unknown fields so client typos surface as 422s."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AddressIn(StrictModel):
    line1: Annotated[NonBlankStr, StringConstraints(max_length=200)]
    line2: Annotated[str, StringConstraints(max_length=200)] | None = None
    city: Annotated[NonBlankStr, StringConstraints(max_length=100)]
    region: Annotated[str, StringConstraints(max_length=100)] | None = Field(
        default=None, description="State, province or region."
    )
    postal_code: Annotated[NonBlankStr, StringConstraints(max_length=20)]
    country: Annotated[str, StringConstraints(to_upper=True, pattern=r"^[A-Za-z]{2}$")] = Field(
        description="ISO 3166-1 alpha-2 country code."
    )


class OrderItemIn(StrictModel):
    product_id: UUID
    quantity: int = Field(gt=0, le=10_000)


class PaymentIn(StrictModel):
    card_number: SecretStr = Field(
        description="Card number (PAN). Passed through to the payment gateway, never stored."
    )

    @field_validator("card_number")
    @classmethod
    def _validate_card_number(cls, value: SecretStr) -> SecretStr:
        digits = value.get_secret_value().replace(" ", "").replace("-", "")
        if not digits.isdigit() or not 12 <= len(digits) <= 19:
            raise ValueError("card_number must contain 12 to 19 digits")
        if not luhn_valid(digits):
            raise ValueError("card_number failed checksum validation")
        return SecretStr(digits)


class CreateOrderRequest(StrictModel):
    customer_id: UUID
    shipping_address: AddressIn
    items: list[OrderItemIn] = Field(min_length=1, max_length=100)
    payment: PaymentIn

    def canonical_payload(self) -> dict[str, Any]:
        """Deterministic representation used for idempotency fingerprinting.

        The PAN is replaced by its last four digits so the fingerprint never
        derives from the full card number.
        """
        payload = self.model_dump(mode="json")
        payload["payment"]["card_number"] = self.payment.card_number.get_secret_value()[-4:]
        return payload


class AddressOut(BaseModel):
    line1: str
    line2: str | None
    city: str
    region: str | None
    postal_code: str
    country: str
    latitude: float
    longitude: float


class WarehouseOut(BaseModel):
    id: UUID
    code: str
    name: str
    distance_km: float = Field(description="Great-circle distance to the shipping address.")


class OrderItemOut(BaseModel):
    product_id: UUID
    sku: str
    name: str
    quantity: int
    unit_price_cents: int
    line_total_cents: int


class PaymentOut(BaseModel):
    id: UUID
    status: str
    amount_cents: int
    currency: str
    card_last4: str
    provider_reference: str | None
    failure_reason: str | None


class OrderResponse(BaseModel):
    id: UUID
    status: str
    customer_id: UUID
    currency: str
    total_cents: int
    shipping_address: AddressOut
    warehouse: WarehouseOut
    items: list[OrderItemOut]
    payment: PaymentOut | None
    created_at: datetime
    updated_at: datetime


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorDetail
