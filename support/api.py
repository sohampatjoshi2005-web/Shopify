"""Support-desk API. Customers: chat with the autonomous agent, raise and read their OWN tickets. Admins: queue, update, escalate, RAG/CAG tools."""
from typing import Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import User, now
from ..notify import send_email
from ..security import admin_user, current_user
from . import cag_service, rag_service, resolver, sla
from .config import cfg
from .models import Escalation, Priority, ResolutionType, Ticket, TicketStatus, Worklog
from .schemas import (CagContextOut, CagSummary, ChatRequest, ChatResponse, RagIndexStatus, RagSearchResult, TicketCreate, TicketOut, TicketUpdate)

chat = APIRouter(prefix="/chat", tags=["support / chat"])
tix = APIRouter(prefix="/tickets", tags=["support / tickets"])
rag = APIRouter(tags=["support / rag + cag"], dependencies=[Depends(admin_user)])


def optional_user(authorization: str | None = Header(None), db: Session = Depends(get_db)) -> User | None:
    return current_user(authorization, db) if authorization else None


def _out(t: Ticket, viewer: User) -> TicketOut:
    o = TicketOut.model_validate(t)
    if viewer.role != "admin":
        o.worklogs = []          # internal notes stay internal
    return o


def _ticket_for(db: Session, user: User, tid: str) -> Ticket:
    t = db.get(Ticket, tid)
    if not t or (user.role != "admin" and t.user_id != user.id):
        raise HTTPException(404, "Ticket not found")      # same answer for "not yours" and "doesn't exist"
    return t


# ------------------------------------------------------------------ chat
@chat.post("/message", response_model=ChatResponse)
def message(body: ChatRequest, user: User | None = Depends(optional_user), db: Session = Depends(get_db)):
    intent, resolved, reply, ticket = resolver.resolve(db, user, body.message, body.channel, body.order_number, str(body.customer_email) if body.customer_email else None)
    return ChatResponse(intent=intent, resolved=resolved, reply=reply, ticket=_out(ticket, user) if ticket and user else None)


# ------------------------------------------------------------------ tickets
@tix.post("", response_model=TicketOut, status_code=201)
def create_ticket(body: TicketCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    owner, priority = user, Priority.medium
    if user.role == "admin":
        priority = body.priority
        if body.customer_email:
            owner = db.scalars(select(User).where(User.email == str(body.customer_email).lower())).first() or User(email=str(body.customer_email).lower(), full_name=body.customer_name or "")
    t = Ticket(user_id=owner.id, customer_email=owner.email, customer_name=owner.full_name or "", channel=body.channel, subject=body.subject,
               description=body.description, priority=priority, status=TicketStatus.open, order_number=(body.order_number or "").strip().upper() or None,
               sla_due_at=sla.compute_sla_due(priority))
    db.add(t)
    db.flush()
    db.add(Worklog(ticket_id=t.id, actor=user.email if user.role == "admin" else "customer", action="created", note="Ticket created."))
    db.commit()
    return _out(t, user)


@tix.get("", response_model=list[TicketOut])
def list_tickets(status: Optional[TicketStatus] = None, priority: Optional[Priority] = None, customer_email: Optional[str] = None,
                 order_number: Optional[str] = None, user: User = Depends(current_user), db: Session = Depends(get_db),
                 page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200)):
    """Admins see the whole queue (filterable); customers only their own tickets."""
    q = select(Ticket).order_by(Ticket.created_at.desc())
    if user.role != "admin":
        q = q.where(Ticket.user_id == user.id)
    elif customer_email:
        q = q.where(Ticket.customer_email == customer_email.lower())
    if status:
        q = q.where(Ticket.status == status)
    if priority:
        q = q.where(Ticket.priority == priority)
    if order_number:
        q = q.where(Ticket.order_number == order_number.strip().upper())
    return [_out(t, user) for t in db.scalars(q.offset((page - 1) * page_size).limit(page_size))]


@tix.get("/{ticket_id}", response_model=TicketOut)
def get_ticket(ticket_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _out(_ticket_for(db, user, ticket_id), user)


@tix.patch("/{ticket_id}", response_model=TicketOut)
def update_ticket(ticket_id: str, body: TicketUpdate, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    """Every change is written to the worklog audit trail."""
    t = db.get(Ticket, ticket_id)
    if not t:
        raise HTTPException(404, "Ticket not found")
    who = admin.email
    if body.priority and body.priority != t.priority:
        t.priority = body.priority
        t.sla_due_at = sla.compute_sla_due(body.priority, from_time=t.created_at)
        db.add(Worklog(ticket_id=t.id, actor=who, action="priority_changed", note=f"Priority changed to {body.priority.value}; SLA recalculated."))
    if body.assigned_to:
        t.assigned_to = body.assigned_to
        db.add(Worklog(ticket_id=t.id, actor=who, action="assigned", note=f"Assigned to {body.assigned_to}."))
    if body.status and body.status != t.status:
        t.status = body.status
        if body.status == TicketStatus.resolved:
            t.resolved_at = now()
            t.sla_breached = sla.is_breached(t.sla_due_at, t.resolved_at)
            t.resolution_type = t.resolution_type or ResolutionType.agent
        db.add(Worklog(ticket_id=t.id, actor=who, action="status_changed", note=f"Status changed to {body.status.value}."))
    if body.note:
        db.add(Worklog(ticket_id=t.id, actor=who, action="note", note=body.note))
    db.commit()
    db.refresh(t)
    return _out(t, admin)


@tix.post("/{ticket_id}/escalate", response_model=TicketOut)
def escalate_ticket(ticket_id: str, team: str = "engineering", reason: str = "Manual escalation", admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    t = db.get(Ticket, ticket_id)
    if not t:
        raise HTTPException(404, "Ticket not found")
    t.status = TicketStatus.escalated
    db.add_all([Escalation(ticket_id=t.id, team=team, reason=reason, notified_email=cfg.escalation_email),
                Worklog(ticket_id=t.id, actor=admin.email, action="escalated", note=reason)])
    db.commit()
    send_email(cfg.escalation_email, f"[Escalation] Ticket {t.id}", f"Reason: {reason}\nTeam: {team}")
    db.refresh(t)
    return _out(t, admin)


# ------------------------------------------------------------------ RAG / CAG (admin only: they expose all orders and tickets)
def _rag_guard(fn, *a, **k):
    try:
        return fn(*a, **k)
    except rag_service.RagUnavailable as e:
        raise HTTPException(503, str(e))


@rag.post("/rag/collect", response_model=RagIndexStatus)
def rag_collect(db: Session = Depends(get_db)):
    return _rag_guard(rag_service.build_index, db)


@rag.get("/rag/status", response_model=RagIndexStatus)
def rag_status():
    return rag_service.index_status()


@rag.get("/rag/search", response_model=list[RagSearchResult])
def rag_search(q: str = Query(..., min_length=1, max_length=300), top_k: int = Query(5, ge=1, le=50)):
    return _rag_guard(rag_service.search, q, top_k)


@rag.post("/cag/refresh", response_model=CagSummary)
def cag_refresh(db: Session = Depends(get_db)):
    return cag_service.refresh_cache(db)


@rag.get("/cag/summary", response_model=CagSummary)
def cag_summary():
    return cag_service.summary()


@rag.get("/cag/context", response_model=CagContextOut)
def cag_context(max_chars: int | None = Query(None, ge=0)):
    return cag_service.get_context(max_chars)


ROUTERS = [chat, tix, rag]
