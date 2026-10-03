"""Marketplace tables the extension builds on: a Seller (one per user) and the product -> seller link.
A product with no ProductSeller row is sold by the house."""
from datetime import datetime
from typing import Optional
from sqlalchemy import DateTime, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base
from .models import now


class Seller(Base):
    __tablename__ = "sellers"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(280), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)   # pending | approved | suspended
    commission_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)     # NULL -> EXT_DEFAULT_COMMISSION_PCT
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ProductSeller(Base):
    __tablename__ = "product_sellers"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), unique=True)
    seller_id: Mapped[int] = mapped_column(ForeignKey("sellers.id", ondelete="CASCADE"), index=True)
