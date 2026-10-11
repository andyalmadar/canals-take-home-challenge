"""initial schema

Revision ID: 0001
Revises: none
Create Date: 2026-10-11 00:40:44.121255
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customers")),
        sa.UniqueConstraint("email", name=op.f("uq_customers_email")),
    )
    op.create_table(
        "geocode_cache",
        sa.Column("address_key", sa.String(length=64), nullable=False),
        sa.Column("normalized_address", sa.Text(), nullable=False),
        sa.Column("latitude", sa.Double(), nullable=False),
        sa.Column("longitude", sa.Double(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("address_key", name=op.f("pk_geocode_cache")),
    )
    op.create_table(
        "idempotency_keys",
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('in_progress', 'completed')",
            name=op.f("ck_idempotency_keys_status_allowed"),
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_idempotency_keys")),
    )
    op.create_index(
        "ix_idempotency_keys_expires_at", "idempotency_keys", ["expires_at"], unique=False
    )
    op.create_table(
        "products",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("unit_price_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("unit_price_cents > 0", name=op.f("ck_products_unit_price_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_products")),
        sa.UniqueConstraint("sku", name=op.f("uq_products_sku")),
    )
    op.create_table(
        "warehouses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("latitude", sa.Double(), nullable=False),
        sa.Column("longitude", sa.Double(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "latitude BETWEEN -90 AND 90", name=op.f("ck_warehouses_latitude_range")
        ),
        sa.CheckConstraint(
            "longitude BETWEEN -180 AND 180", name=op.f("ck_warehouses_longitude_range")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_warehouses")),
        sa.UniqueConstraint("code", name=op.f("uq_warehouses_code")),
    )
    op.create_table(
        "inventory",
        sa.Column("warehouse_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("quantity >= 0", name=op.f("ck_inventory_quantity_non_negative")),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_inventory_product_id_products"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_inventory_warehouse_id_warehouses"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("warehouse_id", "product_id", name=op.f("pk_inventory")),
    )
    op.create_index(
        "ix_inventory_product_id_warehouse_id",
        "inventory",
        ["product_id", "warehouse_id"],
        unique=False,
    )
    op.create_table(
        "orders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("warehouse_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("total_cents", sa.BigInteger(), nullable=False),
        sa.Column("shipping_line1", sa.String(length=200), nullable=False),
        sa.Column("shipping_line2", sa.String(length=200), nullable=True),
        sa.Column("shipping_city", sa.String(length=100), nullable=False),
        sa.Column("shipping_region", sa.String(length=100), nullable=True),
        sa.Column("shipping_postal_code", sa.String(length=20), nullable=False),
        sa.Column("shipping_country", sa.String(length=2), nullable=False),
        sa.Column("shipping_latitude", sa.Double(), nullable=False),
        sa.Column("shipping_longitude", sa.Double(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending_payment', 'paid', 'payment_failed', 'payment_unknown')",
            name=op.f("ck_orders_status_allowed"),
        ),
        sa.CheckConstraint(
            "shipping_latitude BETWEEN -90 AND 90 AND shipping_longitude BETWEEN -180 AND 180",
            name=op.f("ck_orders_shipping_coordinates_range"),
        ),
        sa.CheckConstraint("total_cents >= 0", name=op.f("ck_orders_total_non_negative")),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_orders_customer_id_customers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_orders_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orders")),
    )
    op.create_index(
        "ix_orders_customer_id_created_at", "orders", ["customer_id", "created_at"], unique=False
    )
    op.create_index("ix_orders_status_created_at", "orders", ["status", "created_at"], unique=False)
    op.create_table(
        "order_items",
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price_cents", sa.Integer(), nullable=False),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_order_items_quantity_positive")),
        sa.CheckConstraint("unit_price_cents > 0", name=op.f("ck_order_items_unit_price_positive")),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_order_items_order_id_orders"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_order_items_product_id_products"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("order_id", "product_id", name=op.f("pk_order_items")),
    )
    op.create_table(
        "payments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("card_last4", sa.String(length=4), nullable=False),
        sa.Column("gateway_idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("provider_reference", sa.String(length=128), nullable=True),
        sa.Column("failure_reason", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'succeeded', 'failed', 'unknown')",
            name=op.f("ck_payments_status_allowed"),
        ),
        sa.CheckConstraint("amount_cents >= 0", name=op.f("ck_payments_amount_non_negative")),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_payments_order_id_orders"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payments")),
        sa.UniqueConstraint(
            "gateway_idempotency_key", name=op.f("uq_payments_gateway_idempotency_key")
        ),
    )
    op.create_index("ix_payments_order_id", "payments", ["order_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_payments_order_id", table_name="payments")
    op.drop_table("payments")
    op.drop_table("order_items")
    op.drop_index("ix_orders_status_created_at", table_name="orders")
    op.drop_index("ix_orders_customer_id_created_at", table_name="orders")
    op.drop_table("orders")
    op.drop_index("ix_inventory_product_id_warehouse_id", table_name="inventory")
    op.drop_table("inventory")
    op.drop_table("warehouses")
    op.drop_table("products")
    op.drop_index("ix_idempotency_keys_expires_at", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")
    op.drop_table("geocode_cache")
    op.drop_table("customers")
