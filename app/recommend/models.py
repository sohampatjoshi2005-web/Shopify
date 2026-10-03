"""Recommendation tables (all NEW, prefixed rec_). References to users are plain integers, like the other extensions."""
from datetime import datetime
from typing import Optional
from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from ..db import Base
from ..models import now


class Lead(Base):
    """A person the shop may want to reach: a registered customer (user_id set, synced from the store) or someone added by hand."""
    __tablename__ = "rec_leads"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(16), default="store")                 # store | manual
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)       # new | contacted | converted | lost
    segment: Mapped[str] = mapped_column(String(16), default="prospect", index=True)  # prospect | new_customer | repeat | lapsed
    score: Mapped[int] = mapped_column(Integer, default=0, index=True)               # 0-100 purchase-intent / engagement
    orders_count: Mapped[int] = mapped_column(Integer, default=0)
    wishlist_count: Mapped[int] = mapped_column(Integer, default=0)
    cart_items: Mapped[int] = mapped_column(Integer, default=0)
    last_order_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
