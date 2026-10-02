"""Store tables. Money is integer minor units (paise/cents)."""
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .db import Base


def now() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite returns naive datetimes; everything we store is UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str] = mapped_column(String(16), default="customer")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Address(Base):
    __tablename__ = "addresses"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    full_name: Mapped[str] = mapped_column(String(255))
    phone: Mapped[str] = mapped_column(String(32))
    line1: Mapped[str] = mapped_column(String(255))
    line2: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    city: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(128))
    postal_code: Mapped[str] = mapped_column(String(16))
    country: Mapped[str] = mapped_column(String(2), default="IN")


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    slug: Mapped[str] = mapped_column(String(160), unique=True, index=True)


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255))
    brand: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    category_id: Mapped[Optional[int]] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    rating_avg: Mapped[float] = mapped_column(Float, default=0)
    rating_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    category: Mapped[Optional[Category]] = relationship()
    variants: Mapped[list["Variant"]] = relationship(back_populates="product", order_by="Variant.id", cascade="all, delete-orphan")
    images: Mapped[list["ProductImage"]] = relationship(order_by="ProductImage.position", cascade="all, delete-orphan")


class ProductImage(Base):
    __tablename__ = "product_images"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(String(1000))
    position: Mapped[int] = mapped_column(Integer, default=0)


class Variant(Base):
    __tablename__ = "variants"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    sku: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(String(128), default="Default")
    price_cents: Mapped[int] = mapped_column(Integer)
    stock: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    product: Mapped[Product] = relationship(back_populates="variants")


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (UniqueConstraint("product_id", "user_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    rating: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Cart(Base):
    __tablename__ = "carts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    items: Mapped[list["CartItem"]] = relationship(order_by="CartItem.id", cascade="all, delete-orphan")


class CartItem(Base):
    __tablename__ = "cart_items"
    __table_args__ = (UniqueConstraint("cart_id", "variant_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    cart_id: Mapped[int] = mapped_column(ForeignKey("carts.id", ondelete="CASCADE"), index=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("variants.id", ondelete="CASCADE"))
    qty: Mapped[int] = mapped_column(Integer, default=1)
    variant: Mapped[Variant] = relationship()


class Coupon(Base):
    __tablename__ = "coupons"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    percent_off: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    amount_off_cents: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    min_subtotal_cents: Mapped[int] = mapped_column(Integer, default=0)
    max_uses: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("user_id", "idempotency_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending_payment", index=True)
    currency: Mapped[str] = mapped_column(String(3))
    coupon_id: Mapped[Optional[int]] = mapped_column(ForeignKey("coupons.id", ondelete="SET NULL"), nullable=True)
    coupon_code: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    payment_provider: Mapped[str] = mapped_column(String(16), default="mock")
    payment_ref: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    payment_client: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    provider_payment_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    shipping_address: Mapped[dict] = mapped_column(JSON)
    subtotal_cents: Mapped[int] = mapped_column(Integer)
    discount_cents: Mapped[int] = mapped_column(Integer, default=0)
    shipping_cents: Mapped[int] = mapped_column(Integer, default=0)
    tax_cents: Mapped[int] = mapped_column(Integer, default=0)
    total_cents: Mapped[int] = mapped_column(Integer)
    tracking_number: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    carrier: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    user: Mapped[User] = relationship()
    items: Mapped[list["OrderItem"]] = relationship(order_by="OrderItem.id", cascade="all, delete-orphan")


class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    variant_id: Mapped[Optional[int]] = mapped_column(ForeignKey("variants.id", ondelete="SET NULL"), nullable=True)
    product_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sku: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(255))
    unit_price_cents: Mapped[int] = mapped_column(Integer)
    qty: Mapped[int] = mapped_column(Integer)


class WebhookEvent(Base):
    """De-duplicates provider webhook retries (Stripe, Razorpay, Shopify)."""
    __tablename__ = "webhook_events"
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    provider: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
