# Orders API

A small, production-shaped order management service for an e-commerce platform.

`POST /orders` takes a customer, a shipping address, a list of items and a card. It finds the
nearest warehouse that can fill the *entire* order, reserves the stock, charges the card and
returns the order. Geocoding and payments are behind interfaces with mock implementations.

**Stack:** Python 3.13 · FastAPI · SQLAlchemy 2 (async) · asyncpg · Alembic · Pydantic v2 ·
PostgreSQL 16 · structlog · uv · Docker · Cloud Run + Cloud SQL

---

## Quickstart

```bash
docker compose up --build
```

That starts Postgres, applies migrations, loads seed data and serves the API on
<http://localhost:8000> (interactive docs at `/docs`).

Place an order from San Francisco. Both products are in stock in Los Angeles, the nearest
warehouse, so that is where it is filled:

```bash
curl -s -X POST localhost:8000/orders \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{
    "customer_id": "00000000-0000-4000-8000-000000000001",
    "shipping_address": {
      "line1": "1 Market St", "city": "San Francisco", "region": "CA",
      "postal_code": "94105", "country": "US"
    },
    "items": [
      {"product_id": "10000000-0000-4000-8000-000000000001", "quantity": 1},
      {"product_id": "10000000-0000-4000-8000-000000000002", "quantity": 2}
    ],
    "payment": {"card_number": "4242 4242 4242 4242"}
  }' | jq
```

### Seed data

| Customers | id |
|---|---|
| Ada Lovelace | `00000000-0000-4000-8000-000000000001` |
| Grace Hopper | `00000000-0000-4000-8000-000000000002` |

| Products | id | price |
|---|---|---|
| Mechanical Keyboard | `10000000-0000-4000-8000-000000000001` | $89.99 |
| Wireless Mouse | `10000000-0000-4000-8000-000000000002` | $39.99 |
| 27" 4K Monitor | `10000000-0000-4000-8000-000000000003` | $299.99 |
| Noise-cancelling Headset | `10000000-0000-4000-8000-000000000004` | $199.99 |
| 4K Webcam | `10000000-0000-4000-8000-000000000005` | $129.99 |
| Desk Mat XL | `10000000-0000-4000-8000-000000000006` | $24.99 |

| Warehouse | Keyboard | Mouse | Monitor | Headset | Webcam | Desk Mat |
|---|---|---|---|---|---|---|
| LAX Los Angeles | 50 | 100 | 10 | – | 25 | 200 |
| DFW Dallas | 20 | 40 | 5 | 15 | – | 80 |
| ORD Chicago | 30 | 60 | 8 | 20 | 10 | 150 |
| EWR Newark | 10 | 30 | – | 25 | 5 | – |

The gaps are deliberate. Keyboard + Headset from San Francisco skips LAX (no headsets) and goes to
DFW. Monitor + Headset + Webcam can only come from ORD. Eleven monitors cannot be filled anywhere
and returns `409`.

### Test cards (mock gateway)

| Card | Outcome | HTTP |
|---|---|---|
| `4242424242424242` (any other valid number) | approved | `201` order `paid` |
| `4000000000000002` | declined | `402` order `payment_failed`, stock released |
| `4000000000009995` | declined, insufficient funds | `402` |
| `4000000000000119` | gateway timeout | `502` order `payment_unknown`, stock kept |

The mock geocoder knows ~30 major cities by `(city, country)`; anything else maps to a stable
pseudo-location so results are deterministic.

---

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/orders` | Place an order. Optional `Idempotency-Key` header. |
| `GET` | `/orders/{id}` | Fetch an order (also returned in `Location` after `POST`). |
| `GET` | `/healthz` | Liveness. |
| `GET` | `/readyz` | Readiness; runs `SELECT 1`. |
| `GET` | `/docs` | OpenAPI UI. |

`POST /orders` response codes:

| Code | Meaning |
|---|---|
| `201` | Paid. Body is the order. |
| `402` | Card declined. Order persisted as `payment_failed`; stock released. Body is the order. |
| `502` | Gateway did not answer. Order persisted as `payment_unknown`; stock held for reconciliation. Body is the order. |
| `404` | Unknown customer or product (`details.product_ids` lists the missing ones). |
| `409` | No single warehouse can fill the order, or a request with this `Idempotency-Key` is still in flight. |
| `422` | Validation error (bad card checksum, unknown field, non-ISO country...) or `Idempotency-Key` reused with a different body. |

Errors share one envelope: `{"error": {"code", "message", "details"}}`. Validation errors never
echo submitted values, since the body contains a card number.

---

## Design

### Order flow: two transactions around the payment call

The one thing never to do is hold a database transaction open while waiting on a third party.
So creation runs as:

```
tx1   validate customer and products
      rank warehouses that stock every item, nearest first
      atomically reserve stock at the first one that still has it
      insert order (pending_payment), items, payment (pending)
----  charge the card: bounded by a timeout, with a gateway idempotency key
tx2   approved  -> order paid,            payment succeeded
      declined  -> order payment_failed,  payment failed,  stock released
      unknown   -> order payment_unknown, payment unknown, stock kept
```

Why persist failed orders? A decline is a real business event (the customer tried to buy, the
card said no); it belongs in the order history and the client gets an id to show. An *unknown*
outcome is the important one: the gateway may have charged the card, so stock must stay reserved
and the order must be visible to a reconciliation process that queries the gateway by our
idempotency key and settles it. That job is out of scope here but the data model and statuses
are designed for it. A crash between tx1 and tx2 leaves an order in `pending_payment` and is
handled by the same process.

### Warehouse selection

One SQL query joins the requested `(product, quantity)` pairs against `inventory` and keeps
warehouses that satisfy all of them (`GROUP BY warehouse HAVING COUNT(*) = n`). The handful of
survivors are ranked in Python by haversine distance to the geocoded address, tie-broken by code
so the choice is deterministic.

With thousands of warehouses the ranking would move into Postgres too (PostGIS `geography` and a
KNN index). With four, that would be ceremony.

### Inventory under concurrency

Reservation is a conditional update per product:

```sql
UPDATE inventory SET quantity = quantity - :n
WHERE warehouse_id = :w AND product_id = :p AND quantity >= :n
```

Under `READ COMMITTED` Postgres re-checks the predicate after waiting on the row lock, so two
orders racing for the last unit cannot both see `rowcount = 1`. Products are always updated in
sorted id order so overlapping orders acquire locks in the same sequence and cannot deadlock.
Each warehouse attempt runs inside a savepoint: if a nearer warehouse was drained between our
`SELECT` and `UPDATE`, the partial decrements roll back and we try the next-nearest one. The
`CHECK (quantity >= 0)` constraint is the last line of defence. `tests/test_concurrency.py`
fires 12 concurrent orders at 3 remaining units and asserts exactly 3 succeed.

### Idempotency

Clients should send an `Idempotency-Key` (a UUID) and reuse it on retries. The key is inserted
with `ON CONFLICT DO NOTHING`; the primary key is the arbiter, so exactly one of N concurrent
identical requests does the work. The others get the stored response once it exists, or `409`
while it is in flight. Same key with a different body is `422`.

Any outcome that persisted an order (`201`, `402`, `502`) is stored and replayed, which is what
stops a retried timeout from creating a second order. Outcomes that persisted nothing (`404`,
`409` no stock) release the key so the client can retry. The fingerprint is a SHA-256 of the
canonical body with the card number replaced by its last four digits. Keys expire after
`IDEMPOTENCY_KEY_TTL_HOURS`; a scheduled `DELETE ... WHERE expires_at < now()` keeps the table
bounded.

### Money and prices

Integer minor units (`*_cents`) plus an ISO-4217 `currency` column. No floats, no `NUMERIC`
arithmetic in the application. `order_items.unit_price_cents` snapshots the price at purchase
time so later price changes do not rewrite history. Mixed-currency carts are rejected.

### Card data

The PAN is a Pydantic `SecretStr`, validated (12-19 digits, Luhn) and passed straight to the
gateway. Only `card_last4` is persisted. Request bodies are never logged, validation errors do not
echo input, and `tests/test_create_order.py::test_card_number_never_persisted` dumps the relevant
tables and asserts the PAN is absent.

### External services

`Geocoder` and `PaymentGateway` are `Protocol`s. The mocks are the only implementations shipped,
but they are swappable by configuration and the service code does not know which one it has. The
geocoder is wrapped in a Postgres-backed cache (`geocode_cache`), since real geocoding is slow,
rate-limited and billed per call. The payment call is bounded by `asyncio.timeout` and carries a
per-attempt idempotency key (`payments.gateway_idempotency_key`, unique) so a real gateway could
never double-charge on a retry.

### Why not Redis

Nothing here is worth caching. Idempotency keys and geocode results both fit naturally in
Postgres with full transactional semantics and no extra infrastructure to run, secure and
monitor. Redis would earn its place for rate limiting or hot-path caching at a traffic level
this exercise does not model.

### Data model

```
customers        id, email (unique), full_name
products         id, sku (unique), name, unit_price_cents, currency
warehouses       id, code (unique), name, latitude, longitude
inventory        (warehouse_id, product_id) PK, quantity CHECK >= 0
orders           id, customer_id, warehouse_id, status, currency, total_cents,
                 shipping_* address snapshot + lat/lng, created_at, updated_at
order_items      (order_id, product_id) PK, quantity, unit_price_cents
payments         id, order_id, status, amount_cents, currency, card_last4,
                 gateway_idempotency_key (unique), provider_reference, failure_reason
idempotency_keys key PK, request_hash, status, response_status, response_body, expires_at
geocode_cache    address_key PK, normalized_address, latitude, longitude
```

Statuses are strings guarded by `CHECK` constraints rather than Postgres enums: adding a value is
a non-locking migration. Every constraint and index has a deterministic name via SQLAlchemy's
naming convention, so migrations stay reviewable and reversible. `alembic check` runs in CI to
fail the build if models and migrations drift.

---

## Operations

**Configuration** is environment variables (see `.env.example`), parsed by `pydantic-settings`.

**Logging** is structured JSON (`LOG_JSON=true`). Every request gets an `X-Request-Id` (honoured
if the client sends one), bound into every log line emitted while handling it, plus one access log
line with status and duration.

**Connection pooling.** Each instance keeps `DB_POOL_SIZE + DB_MAX_OVERFLOW` connections. Keep
`max_instances × that` below the database's `max_connections` (about 25 on the smallest Cloud SQL
tier; the deploy script uses 4 instances × 4 connections). `pool_pre_ping` and `pool_recycle`
handle dropped connections.

**Scaling.** One uvicorn worker per container, async I/O for concurrency within it, horizontal
scaling across containers.

**Periodic jobs** (not implemented, documented for completeness): purge expired
`idempotency_keys`; reconcile `pending_payment` / `payment_unknown` orders against the gateway.

---

## Deploy to Cloud Run

Requires the `gcloud` CLI authenticated against a project with billing enabled.

```bash
PROJECT_ID=your-project ./deploy/cloudrun.sh
```

The script is idempotent and creates: an Artifact Registry repo, a Cloud SQL Postgres 16
instance (`db-f1-micro`, roughly USD 10/month, the only real cost), a Secret Manager secret
holding the database URL, a least-privilege service account, a Cloud Run **job** that runs
migrations and seeds, and the Cloud Run **service** (scale-to-zero, so idle cost is nil). The
service reaches Cloud SQL over the built-in Auth Proxy unix socket; no public database IP is
used by the application.

Re-running the script builds a new image from `HEAD`, runs migrations, and redeploys.
`PROJECT_ID=your-project ./deploy/teardown.sh` deletes everything, database included.

---

## Development

```bash
uv sync                      # install
docker compose up db -d      # or any Postgres on localhost:5432
make migrate seed            # schema + reference data
make run                     # uvicorn --reload on :8000
make check                   # ruff, mypy --strict, pytest
```

Tests run against a real Postgres (`TEST_DATABASE_URL`, default
`postgresql+asyncpg://orders:orders@localhost:5432/orders_test`); create that database once with
`createdb orders_test` or via compose. Migrations are applied once per session and every test
starts from freshly seeded data. CI (GitHub Actions) runs lint, type-check, `alembic check`,
the tests against a Postgres service container, and a Docker image build.

### Layout

```
app/
  main.py              app factory, lifespan (engine, services), routers
  config.py            settings
  logging.py           structlog setup
  errors.py            domain exceptions
  api/                 routes, error handlers, request-id middleware
  schemas/             Pydantic request/response models
  services/
    orders.py          the two-transaction create flow
    warehouse_selection.py
    inventory.py       atomic reserve / release
    idempotency.py     Idempotency-Key store
    geo.py, cards.py   haversine, Luhn
  integrations/        Geocoder / PaymentGateway ports + mocks
  db/                  models, session factory
alembic/               migrations (0001 creates the schema)
scripts/seed.py        idempotent reference data
tests/                 integration tests against Postgres
deploy/                Cloud Run provisioning
```

---

## Scope and next steps

Deliberately out of scope, per the brief: auth, CRUD for customers/products/warehouses, a UI.

What I would do next in a real system, in order:

1. **Reconciliation worker** for `pending_payment` and `payment_unknown` orders, querying the
   gateway by `gateway_idempotency_key`, then settling or releasing stock.
2. **Outbox + events** (`order.paid`) for fulfilment, email, analytics, instead of callers
   polling `GET /orders/{id}`.
3. **Real geocoder and gateway adapters** (httpx, retries on connection errors only, circuit
   breaker), selected by `GEOCODER` / `PAYMENT_GATEWAY` settings.
4. **Partial fulfilment / split shipments** if the business wants it; the single-warehouse rule
   is a product decision this service enforces, not a technical limit.
5. **Observability:** OpenTelemetry traces and metrics (payment latency, reservation
   retries, no-stock rate by product).
