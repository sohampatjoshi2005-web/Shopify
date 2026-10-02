"""Extension tables. All of them are NEW tables (no column is added to your existing ones), so `Base.metadata.create_all`
creates them without a migration. Cross-table references are plain indexed integers instead of ForeignKeys so the
package does not depend on the exact table names in your models.py."""
from datetime import datetime
from typing import Optional
from sqlalchemy import Boolean, DateTime, Float, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from ..db import Base
from ..models import now


def _dt(**k):
    return mapped_column(DateTime(timezone=True), **k)


class SellerProfile(Base):
    __tablename__ = "x_seller_profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    seller_id: Mapped[int] = mapped_column(Integer, unique=True)
    legal_name: Mapped[str] = mapped_column(String(255), default="")
    gstin: Mapped[str] = mapped_column(String(15), default="")
    state_code: Mapped[str] = mapped_column(String(2), default="")
    pan: Mapped[str] = mapped_column(String(10), default="")
    address: Mapped[str] = mapped_column(String(500), default="")
    pickup_pincode: Mapped[str] = mapped_column(String(16), default="")
    upi_id: Mapped[str] = mapped_column(String(128), default="")
    bank_account_name: Mapped[str] = mapped_column(String(255), default="")
    bank_account_no: Mapped[str] = mapped_column(String(34), default="")  # encrypt at rest in production
    bank_ifsc: Mapped[str] = mapped_column(String(11), default="")
    updated_at: Mapped[datetime] = _dt(default=now, onupdate=now)


class ProductTax(Base):
    __tablename__ = "x_product_tax"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(Integer, unique=True)
    hsn: Mapped[str] = mapped_column(String(8), default="")
    gst_rate_pct: Mapped[float] = mapped_column(Float, default=18.0)


class VariantShipping(Base):
    __tablename__ = "x_variant_shipping"
    id: Mapped[int] = mapped_column(primary_key=True)
    variant_id: Mapped[int] = mapped_column(Integer, unique=True)
    weight_g: Mapped[int] = mapped_column(Integer, default=500)


class LineSplit(Base):
    """One row per order line: who sold it and how the money splits. Snapshot at accrual time (commission can change later)."""
    __tablename__ = "x_line_splits"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_item_id: Mapped[int] = mapped_column(Integer, unique=True)
    order_id: Mapped[int] = mapped_column(Integer, index=True)
    order_number: Mapped[str] = mapped_column(String(32), index=True)
    product_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    seller_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)  # NULL = sold by the house
    qty: Mapped[int] = mapped_column(Integer)
    gross_cents: Mapped[int] = mapped_column(Integer)
    commission_pct: Mapped[float] = mapped_column(Float, default=0)
    commission_cents: Mapped[int] = mapped_column(Integer, default=0)
    net_cents: Mapped[int] = mapped_column(Integer, default=0)
    reversed_cents: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _dt(default=now)


class LedgerEntry(Base):
    """Append-only seller money ledger. Positive = we owe the seller, negative = reversal. payout_id set once paid out."""
    __tablename__ = "x_ledger"
    __table_args__ = (UniqueConstraint("seller_id", "kind", "ref"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    seller_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(24))          # sale | reversal | adjustment
    amount_cents: Mapped[int] = mapped_column(Integer)
    ref: Mapped[str] = mapped_column(String(64))
    order_number: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    note: Mapped[str] = mapped_column(String(255), default="")
    available_at: Mapped[datetime] = _dt(default=now)
    payout_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = _dt(default=now)


class Payout(Base):
    __tablename__ = "x_payouts"
    id: Mapped[int] = mapped_column(primary_key=True)
    seller_id: Mapped[int] = mapped_column(Integer, index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="created", index=True)   # created | paid | failed
    method: Mapped[str] = mapped_column(String(16), default="manual")
    reference: Mapped[str] = mapped_column(String(64), default="")                  # bank UTR / UPI ref
    note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = _dt(default=now)
    paid_at: Mapped[Optional[datetime]] = _dt(nullable=True)


class ReturnRequest(Base):
    __tablename__ = "x_returns"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_number: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(16), default="requested", index=True)
    # requested -> approved -> received -> refunded ; or rejected / cancelled
    reason: Mapped[str] = mapped_column(String(255), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    admin_note: Mapped[str] = mapped_column(String(500), default="")
    restock: Mapped[bool] = mapped_column(Boolean, default=True)
    refund_cents: Mapped[int] = mapped_column(Integer, default=0)
    refund_ref: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = _dt(default=now)
    updated_at: Mapped[datetime] = _dt(default=now, onupdate=now)


class ReturnLine(Base):
    __tablename__ = "x_return_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    return_id: Mapped[int] = mapped_column(Integer, index=True)
    order_item_id: Mapped[int] = mapped_column(Integer)
    qty: Mapped[int] = mapped_column(Integer)


class Shipment(Base):
    __tablename__ = "x_shipments"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_number: Mapped[str] = mapped_column(String(32), index=True)
    seller_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    provider: Mapped[str] = mapped_column(String(16), default="mock")
    carrier: Mapped[str] = mapped_column(String(64), default="")
    awb: Mapped[str] = mapped_column(String(64), default="", index=True)
    provider_ref: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="created", index=True)  # created|in_transit|delivered|rto|cancelled
    cost_cents: Mapped[int] = mapped_column(Integer, default=0)
    weight_g: Mapped[int] = mapped_column(Integer, default=0)
    label_url: Mapped[str] = mapped_column(String(500), default="")
    events: Mapped[list] = mapped_column(JSON, default=list)
    delivered_at: Mapped[Optional[datetime]] = _dt(nullable=True)
    created_at: Mapped[datetime] = _dt(default=now)


class Invoice(Base):
    __tablename__ = "x_invoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(96), unique=True)   # kind:order:seller:return  (makes issuing idempotent)
    number: Mapped[str] = mapped_column(String(20), unique=True)
    kind: Mapped[str] = mapped_column(String(12))               # invoice | credit_note
    order_number: Mapped[str] = mapped_column(String(32), index=True)
    seller_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    return_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_cents: Mapped[int] = mapped_column(Integer, default=0)
    data: Mapped[dict] = mapped_column(JSON)
    issued_at: Mapped[datetime] = _dt(default=now)


class InvoiceCounter(Base):
    __tablename__ = "x_invoice_counters"
    key: Mapped[str] = mapped_column(String(48), primary_key=True)
    last: Mapped[int] = mapped_column(Integer, default=0)


class AuthToken(Base):
    __tablename__ = "x_auth_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    purpose: Mapped[str] = mapped_column(String(12))            # verify | reset
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = _dt()
    used_at: Mapped[Optional[datetime]] = _dt(nullable=True)
    created_at: Mapped[datetime] = _dt(default=now)


class UserFlag(Base):
    __tablename__ = "x_user_flags"
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    email_verified_at: Mapped[Optional[datetime]] = _dt(nullable=True)
    password_changed_at: Mapped[Optional[datetime]] = _dt(nullable=True)


class WishlistItem(Base):
    __tablename__ = "x_wishlist"
    __table_args__ = (UniqueConstraint("user_id", "product_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    product_id: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = _dt(default=now)
