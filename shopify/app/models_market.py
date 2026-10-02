"""Marketplace tables: sellers, product ownership, Shopify connections and variant links."""
from datetime import datetime
from typing import Optional
from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base
from .models import now


class Seller(Base):
    __tablename__ = "sellers"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    slug: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending|approved|suspended
    commission_pct: Mapped[float] = mapped_column(Float, default=10.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ProductSeller(Base):
    __tablename__ = "product_sellers"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), unique=True)
    seller_id: Mapped[int] = mapped_column(ForeignKey("sellers.id", ondelete="CASCADE"), index=True)


class ShopifyConnection(Base):
    __tablename__ = "shopify_connections"
    id: Mapped[int] = mapped_column(primary_key=True)
    seller_id: Mapped[int] = mapped_column(ForeignKey("sellers.id", ondelete="CASCADE"), unique=True)
    shop_domain: Mapped[str] = mapped_column(String(255), unique=True)
    access_token: Mapped[str] = mapped_column(String(255))  # plaintext today; encrypt before production
    location_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    last_import_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class ShopifyLink(Base):
    __tablename__ = "shopify_links"
    __table_args__ = (UniqueConstraint("connection_id", "shopify_variant_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("shopify_connections.id", ondelete="CASCADE"), index=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("variants.id", ondelete="CASCADE"), unique=True)
    shopify_product_id: Mapped[str] = mapped_column(String(32), index=True)
    shopify_variant_id: Mapped[str] = mapped_column(String(32))
    inventory_item_id: Mapped[str] = mapped_column(String(32), index=True)
