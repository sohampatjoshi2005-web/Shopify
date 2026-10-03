from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from .models import Channel, Priority, ResolutionType, TicketStatus


class WorklogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    actor: str
    action: str
    note: Optional[str] = None
    created_at: datetime


class TicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    user_id: Optional[int] = None
    customer_email: str
    order_number: Optional[str] = None
    channel: Channel
    subject: str
    description: Optional[str] = None
    status: TicketStatus
    priority: Priority
    resolution_type: Optional[ResolutionType] = None
    assigned_to: Optional[str] = None
    sla_due_at: datetime
    resolved_at: Optional[datetime] = None
    sla_breached: bool
    created_at: datetime
    worklogs: list[WorklogOut] = []


class TicketCreate(BaseModel):
    subject: str = Field(min_length=3, max_length=255)
    description: Optional[str] = Field(default=None, max_length=5000)
    channel: Channel = Channel.web_form
    order_number: Optional[str] = Field(default=None, max_length=32)
    priority: Priority = Priority.medium          # honoured for admins only
    customer_email: Optional[EmailStr] = None     # admins raising a ticket on a customer's behalf
    customer_name: Optional[str] = None


class TicketUpdate(BaseModel):
    status: Optional[TicketStatus] = None
    priority: Optional[Priority] = None
    assigned_to: Optional[str] = Field(default=None, max_length=255)
    note: Optional[str] = Field(default=None, max_length=5000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    channel: Channel = Channel.chat
    order_number: Optional[str] = Field(default=None, max_length=32)
    customer_email: Optional[EmailStr] = None     # only used by logged-out visitors, for password reset
    customer_name: Optional[str] = None


class ChatResponse(BaseModel):
    intent: str
    resolved: bool
    reply: str
    ticket: Optional[TicketOut] = None


class RagIndexStatus(BaseModel):
    documents_indexed: int
    built_at: Optional[datetime] = None
    by_type: dict[str, int] = {}


class RagSearchResult(BaseModel):
    doc_id: str
    doc_type: str
    title: str
    snippet: str
    score: float
    metadata: dict = {}


class CagSummary(BaseModel):
    document_count: int
    by_type: dict[str, int] = {}
    built_at: Optional[datetime] = None
    stale: bool
    context_size_chars: Optional[int] = None


class CagContextOut(BaseModel):
    context: str
    document_count: int
    by_type: dict[str, int] = {}
    built_at: Optional[datetime] = None
    stale: bool
