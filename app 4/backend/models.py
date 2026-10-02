from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import String, Float, Boolean, DateTime, Text, Integer
from sqlalchemy.orm import Mapped, mapped_column
from backend.core.db import Base

def now():
    return datetime.now(timezone.utc)

class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    shopify_order_id: Mapped[Optional[str]] = mapped_column(String(64), unique=True, nullable=True)
    name: Mapped[str] = mapped_column(String(32), index=True)
    customer_email: Mapped[str] = mapped_column(String(255), index=True)
    financial_status: Mapped[str] = mapped_column(String(32), default="paid")
    fulfillment_status: Mapped[str] = mapped_column(String(32), default="unfulfilled")
    tracking_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    total: Mapped[float] = mapped_column(Float, default=0)
    refunded: Mapped[bool] = mapped_column(Boolean, default=False)
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class Ticket(Base):
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_email: Mapped[str] = mapped_column(String(255), index=True)
    channel: Mapped[str] = mapped_column(String(32), default="web")
    message: Mapped[str] = mapped_column(Text)
    intent: Mapped[str] = mapped_column(String(32))
    priority: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))  # open | escalated | closed
    resolution: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    sla_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

class Lead(Base):
    __tablename__ = "leads"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="web")
    score: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    type: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class WebhookLog(Base):
    __tablename__ = "webhook_log"
    webhook_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    topic: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
