"""Support-desk tables (all NEW, prefixed sup_). Customers are the store's own users, so there is no separate customers/orders
mirror: tickets point at users.id (nullable for tickets raised by an unknown email) and at orders by order number."""
import enum
import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from ..db import Base
from ..models import now


def gen_uuid() -> str:
    return str(uuid.uuid4())


def _enum(e):
    return Enum(e, native_enum=False, length=16, create_constraint=False, validate_strings=True)


class Channel(str, enum.Enum):
    email = "email"
    chat = "chat"
    voice = "voice"
    social = "social"
    whatsapp = "whatsapp"
    web_form = "web_form"
    slack = "slack"


class Priority(str, enum.Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class TicketStatus(str, enum.Enum):
    open = "open"
    in_progress = "in_progress"
    escalated = "escalated"
    resolved = "resolved"
    closed = "closed"


class ResolutionType(str, enum.Enum):
    autonomous = "autonomous"
    agent = "agent"
    engineering = "engineering"


class Ticket(Base):
    __tablename__ = "sup_tickets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    customer_email: Mapped[str] = mapped_column(String(255), index=True)
    customer_name: Mapped[str] = mapped_column(String(255), default="")
    order_number: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    channel: Mapped[Channel] = mapped_column(_enum(Channel))
    subject: Mapped[str] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[TicketStatus] = mapped_column(_enum(TicketStatus), default=TicketStatus.open, index=True)
    priority: Mapped[Priority] = mapped_column(_enum(Priority), default=Priority.medium)
    resolution_type: Mapped[Optional[ResolutionType]] = mapped_column(_enum(ResolutionType), nullable=True)
    assigned_to: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    sla_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    sla_breached: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    worklogs: Mapped[list["Worklog"]] = relationship(back_populates="ticket", cascade="all, delete-orphan", lazy="selectin", order_by="Worklog.created_at")
    escalations: Mapped[list["Escalation"]] = relationship(back_populates="ticket", cascade="all, delete-orphan", lazy="selectin")


class Worklog(Base):
    """Append-only audit trail of everything that happened on a ticket."""
    __tablename__ = "sup_worklogs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    ticket_id: Mapped[str] = mapped_column(ForeignKey("sup_tickets.id", ondelete="CASCADE"), index=True)
    actor: Mapped[str] = mapped_column(String(255))          # "autonomous_agent", agent name, "system"
    action: Mapped[str] = mapped_column(String(64))
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    ticket: Mapped[Ticket] = relationship(back_populates="worklogs")


class Escalation(Base):
    __tablename__ = "sup_escalations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    ticket_id: Mapped[str] = mapped_column(ForeignKey("sup_tickets.id", ondelete="CASCADE"), index=True)
    team: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    notified_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    ticket: Mapped[Ticket] = relationship(back_populates="escalations")
