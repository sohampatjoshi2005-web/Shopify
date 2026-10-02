"""AI SDR tables. JSON columns hold agent payloads; lifecycle state lives in typed columns."""
from datetime import datetime
from typing import Optional
from sqlalchemy import String, Integer, Float, Boolean, DateTime, ForeignKey, JSON, UniqueConstraint, Text
from sqlalchemy.orm import Mapped, mapped_column
from ..db import Base
from ..models import now


class SdrIcp(Base):
    __tablename__ = "sdr_icps"
    __table_args__ = (UniqueConstraint("group_id", "version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    name: Mapped[str] = mapped_column(String(128))
    data: Mapped[dict] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SdrAccount(Base):
    __tablename__ = "sdr_accounts"
    __table_args__ = (UniqueConstraint("icp_id", "domain"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    icp_id: Mapped[int] = mapped_column(ForeignKey("sdr_icps.id", ondelete="CASCADE"), index=True)
    domain: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(32))
    fit_score: Mapped[float] = mapped_column(Float, default=0)
    data: Mapped[dict] = mapped_column(JSON)
    enrichment: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SdrContact(Base):
    __tablename__ = "sdr_contacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("sdr_accounts.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(255), default="")
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    persona_match: Mapped[float] = mapped_column(Float, default=0)
    suppressed: Mapped[bool] = mapped_column(Boolean, default=False)
    intel: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    qualification: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)


class SdrCampaign(Base):
    __tablename__ = "sdr_campaigns"
    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("sdr_contacts.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending_approval", index=True)
    snapshot_key: Mapped[str] = mapped_column(String(40))
    messages: Mapped[list] = mapped_column(JSON)
    provider: Mapped[str] = mapped_column(String(16), default="dry_run")
    provider_message_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    events: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class SdrTouch(Base):
    __tablename__ = "sdr_touches"
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("sdr_campaigns.id", ondelete="CASCADE"), index=True)
    step: Mapped[int] = mapped_column(Integer)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending_approval", index=True)  # pending_approval|approved|sent|stopped
    subject: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    stop_reason: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class SdrThread(Base):
    __tablename__ = "sdr_threads"
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("sdr_campaigns.id", ondelete="CASCADE"), unique=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("sdr_contacts.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="open")
    intent: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    messages: Mapped[list] = mapped_column(JSON, default=list)
    reply_draft: Mapped[str] = mapped_column(Text, default="")
    reply_status: Mapped[str] = mapped_column(String(16), default="none")  # none|draft|approved|sent
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SdrMeeting(Base):
    __tablename__ = "sdr_meetings"
    id: Mapped[int] = mapped_column(primary_key=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey("sdr_threads.id", ondelete="CASCADE"), index=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("sdr_contacts.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16), default="requested", index=True)  # requested|approved|booked|cancelled|failed
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_min: Mapped[int] = mapped_column(Integer, default=30)
    provider_event_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    meet_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    reminders: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SdrCrmSync(Base):
    __tablename__ = "sdr_crm_syncs"
    id: Mapped[int] = mapped_column(primary_key=True)
    meeting_id: Mapped[int] = mapped_column(ForeignKey("sdr_meetings.id", ondelete="CASCADE"), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|synced|failed
    provider: Mapped[str] = mapped_column(String(16), default="dry_run")
    records: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SdrOutcome(Base):
    """Labels for future propensity training: reply | unsubscribe | meeting_booked."""
    __tablename__ = "sdr_outcomes"
    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("sdr_contacts.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
