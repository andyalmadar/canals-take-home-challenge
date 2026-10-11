"""Payment gateway port and a mock adapter.

The real adapter would be an httpx client with a request timeout, a
provider-side idempotency key, and retries only on connection errors. The mock
reproduces the gateway's observable behaviour, including the two outcomes that
matter most for correctness: a decline, and a timeout where the outcome is
unknown.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from typing import Protocol

from app.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ChargeRequest:
    card_number: str
    amount_cents: int
    currency: str
    description: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ChargeResult:
    approved: bool
    provider_reference: str | None = None
    decline_reason: str | None = None


class PaymentGatewayError(Exception):
    """The gateway failed in a way where the outcome of the charge is unknown."""


class PaymentGatewayTimeout(PaymentGatewayError):
    pass


class PaymentGateway(Protocol):
    async def charge(self, request: ChargeRequest) -> ChargeResult: ...


class MockPaymentGateway:
    """Behaves like a sandbox gateway. Outcome is decided by the card's last digits.

    These mirror well-known sandbox card numbers:
      * ...0002  -> declined (generic)
      * ...9995  -> declined (insufficient funds)
      * ...0119  -> gateway timeout (outcome unknown)
      * anything else -> approved
    """

    DECLINE_RULES: dict[str, str] = {
        "0002": "card_declined",
        "9995": "insufficient_funds",
    }
    TIMEOUT_SUFFIX = "0119"

    def __init__(self, latency_seconds: float = 0.0) -> None:
        self._latency = latency_seconds

    async def charge(self, request: ChargeRequest) -> ChargeResult:
        if self._latency:
            await asyncio.sleep(self._latency)
        last4 = request.card_number[-4:]
        if last4 == self.TIMEOUT_SUFFIX:
            raise PaymentGatewayTimeout("mock gateway timed out")
        reason = self.DECLINE_RULES.get(last4)
        if reason is not None:
            return ChargeResult(approved=False, decline_reason=reason)
        return ChargeResult(approved=True, provider_reference=f"mock_ch_{secrets.token_hex(12)}")
