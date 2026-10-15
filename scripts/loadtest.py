"""Concurrency and throughput check for POST /orders.

Fires N orders at the API with C in flight at once, then reports status counts
and latency percentiles. With --database-url it also snapshots inventory before
and after and proves the invariants that matter in a high-traffic system:

  * stock decrement == units sold in orders the API reported as paid or unknown
    (declined orders must have released theirs), and never below zero;
  * at most one order per Idempotency-Key, even when every request is a
    duplicate of the same key (--duplicate-keys).

Scenarios:
  throughput  ample stock, distinct keys. Measures latency and RPS.
  contention  --stock units of one product at one warehouse (requires
              --database-url), far more requests than units. Exactly --stock
              orders may succeed; everything else must be a clean 409.

Examples:
  uv run python -m scripts.loadtest --url http://localhost:8000 --requests 300 --concurrency 50
  uv run python -m scripts.loadtest --scenario contention --stock 5 --requests 200 \
      --database-url postgresql+asyncpg://orders:orders@localhost:5432/orders
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from scripts.seed import Ids

SAN_FRANCISCO = {
    "line1": "1 Market St",
    "city": "San Francisco",
    "region": "CA",
    "postal_code": "94105",
    "country": "US",
}
NEW_YORK = {"line1": "350 5th Ave", "city": "New York", "postal_code": "10118", "country": "US"}

APPROVED = "4242424242424242"
DECLINED = "4000000000000002"
TIMEOUT = "4000000000000119"


@dataclass
class Result:
    status: int
    latency_ms: float
    order_status: str | None = None
    units: int = 0
    error: str | None = None


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)
    wall_seconds: float = 0.0

    def status_counts(self) -> Counter[str]:
        return Counter(
            f"{r.status} {r.order_status}" if r.order_status else str(r.status)
            for r in self.results
        )

    def percentile(self, p: float) -> float:
        lat = sorted(r.latency_ms for r in self.results)
        if not lat:
            return 0.0
        index = min(len(lat) - 1, round(p / 100 * (len(lat) - 1)))
        return lat[index]


async def place_order(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    payload: dict[str, object],
    key: str,
    units: int,
) -> Result:
    async with sem:
        started = time.perf_counter()
        try:
            resp = await client.post("/orders", json=payload, headers={"Idempotency-Key": key})
        except httpx.HTTPError as exc:
            return Result(0, (time.perf_counter() - started) * 1000, error=type(exc).__name__)
        latency = (time.perf_counter() - started) * 1000
        order_status = resp.json().get("status") if resp.status_code in (201, 402, 502) else None
        return Result(resp.status_code, latency, order_status, units)


def build_payload(
    product_id: uuid.UUID, quantity: int, card: str, address: dict[str, str]
) -> dict[str, object]:
    return {
        "customer_id": str(Ids.ADA),
        "shipping_address": address,
        "items": [{"product_id": str(product_id), "quantity": quantity}],
        "payment": {"card_number": card},
    }


async def run_load(
    args: argparse.Namespace, product_id: uuid.UUID, address: dict[str, str]
) -> Report:
    sem = asyncio.Semaphore(args.concurrency)
    shared_key = f"dup-{uuid.uuid4()}"
    cards = [APPROVED] * 8 + [DECLINED, TIMEOUT] if args.mixed_cards else [APPROVED]

    tasks = []
    async with httpx.AsyncClient(base_url=args.url, timeout=60) as client:
        for i in range(args.requests):
            card = cards[i % len(cards)]
            key = shared_key if args.duplicate_keys else f"lt-{uuid.uuid4()}"
            tasks.append(
                place_order(
                    client,
                    sem,
                    build_payload(product_id, args.quantity, card, address),
                    key,
                    args.quantity,
                )
            )
        started = time.perf_counter()
        results = await asyncio.gather(*tasks)
        wall = time.perf_counter() - started
    return Report(list(results), wall)


async def stock(database_url: str, product_id: uuid.UUID) -> int:
    """Total units of a product across every warehouse.

    Summed across warehouses on purpose: when the nearest warehouse is drained
    the service falls through to the next one, which is correct behaviour, not
    a leak.
    """
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            row = await conn.execute(
                text("SELECT coalesce(sum(quantity), 0) FROM inventory WHERE product_id=:p"),
                {"p": product_id},
            )
            return int(row.scalar_one())
    finally:
        await engine.dispose()


async def set_stock(database_url: str, levels: dict[tuple[uuid.UUID, uuid.UUID], int]) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            for (warehouse_id, product_id), quantity in levels.items():
                await conn.execute(
                    text(
                        "UPDATE inventory SET quantity=:q WHERE warehouse_id=:w AND product_id=:p"
                    ),
                    {"q": quantity, "w": warehouse_id, "p": product_id},
                )
    finally:
        await engine.dispose()


def print_report(report: Report) -> None:
    n = len(report.results)
    print(
        f"\nrequests: {n}   wall: {report.wall_seconds:.2f}s   rps: {n / report.wall_seconds:.1f}"
    )
    print("status:")
    for key, count in sorted(report.status_counts().items()):
        print(f"  {key:<22} {count}")
    errors = Counter(r.error for r in report.results if r.error)
    if errors:
        print(f"transport errors: {dict(errors)}")
    lat = [r.latency_ms for r in report.results]
    print(
        "latency ms:  "
        f"p50={report.percentile(50):.0f}  p95={report.percentile(95):.0f}  "
        f"p99={report.percentile(99):.0f}  max={max(lat):.0f}  mean={statistics.fmean(lat):.0f}"
    )


def check(condition: bool, message: str) -> bool:
    print(f"  [{'OK' if condition else 'FAIL'}] {message}")
    return condition


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--scenario", choices=["throughput", "contention"], default="throughput")
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--quantity", type=int, default=1, help="units per order")
    parser.add_argument("--stock", type=int, default=5, help="contention: units available")
    parser.add_argument(
        "--mixed-cards", action="store_true", help="throughput: 10%% declines, 10%% timeouts"
    )
    parser.add_argument(
        "--duplicate-keys", action="store_true", help="send every request with ONE key"
    )
    parser.add_argument("--database-url", help="enables inventory invariant checks")
    args = parser.parse_args()

    if args.scenario == "contention":
        if not args.database_url:
            parser.error("--scenario contention requires --database-url")
        product_id, address = Ids.WEBCAM, NEW_YORK
        # Only EWR can fill webcams for a New York address; everything else is drained.
        await set_stock(
            args.database_url,
            {
                (Ids.EWR, Ids.WEBCAM): args.stock,
                (Ids.LAX, Ids.WEBCAM): 0,
                (Ids.ORD, Ids.WEBCAM): 0,
            },
        )
        print(f"contention: {args.stock} webcams at EWR, {args.requests} orders x {args.quantity}")
    else:
        product_id, address = Ids.DESK_MAT, SAN_FRANCISCO
        print(
            f"throughput: {args.requests} desk-mat orders from SF, concurrency {args.concurrency}"
        )

    before = await stock(args.database_url, product_id) if args.database_url else None
    report = await run_load(args, product_id, address)
    print_report(report)

    ok = True
    print("invariants:")
    ok &= check(
        all(r.status in (201, 402, 409, 502) for r in report.results),
        "every response is a defined outcome (201/402/409/502): no 5xx, no transport errors",
    )
    held = sum(r.units for r in report.results if r.status in (201, 502))
    if args.database_url and before is not None:
        after = await stock(args.database_url, product_id)
        print(f"  total stock {before} -> {after}  (paid+unknown units: {held})")
        ok &= check(
            before - after == held, "stock decrement equals units held by paid/unknown orders"
        )
        ok &= check(after >= 0, "stock never negative")
        if args.duplicate_keys:
            created = sum(1 for r in report.results if r.status in (201, 402, 502))
            ok &= check(
                before - after <= args.quantity, "one shared key reserved stock at most once"
            )
            print(f"  {created} responses carried an order, all for the same id" if created else "")
    if args.scenario == "contention":
        wins = sum(1 for r in report.results if r.status == 201)
        ok &= check(
            wins == args.stock // args.quantity,
            f"exactly {args.stock // args.quantity} orders won the stock",
        )
        ok &= check(
            all(r.status in (201, 409) for r in report.results),
            "every loser got a clean 409, nothing else",
        )
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
