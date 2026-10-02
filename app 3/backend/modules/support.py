import re
from datetime import timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.core.auth import require_user, widget_key
from backend.core.config import settings
from backend.core.db import get_db
from backend.core.email import send_email
from backend.integrations import shopify
from backend.models import Ticket, now

router = APIRouter(tags=["support"])

# Word-boundary rules (fixes the substring bugs flagged in the design doc). Order = priority.
INTENTS = [
    ("refund", r"\brefunds?\b|\bmoney back\b|\breturn(s|ed)?\b"),
    ("invoice", r"\binvoices?\b|\breceipts?\b|\bbill(ing)?\b"),
    ("password_reset", r"\bpassword\b|\bforgot\b"),
    ("shipping", r"\bship(ping|ped|ment)?\b|\btrack(ing)?\b|\bdeliver(y|ed)?\b"),
    ("order_status", r"\borders?\b|\bstatus\b"),
]
SLA_HOURS = {"critical": 2, "high": 8, "medium": 72, "low": 72}

def classify(text: str) -> str:
    t = text.lower()
    return next((name for name, rx in INTENTS if re.search(rx, t)), "unknown")

def extract_order_name(text: str, explicit: str | None) -> str | None:
    src = explicit or text
    m = re.search(r"#?\b(\d{3,10})\b", src)
    return f"#{m.group(1)}" if m else None

def _ticket(db, email, channel, message, intent, priority, status, resolution):
    t = Ticket(customer_email=email, channel=channel, message=message, intent=intent, priority=priority,
               status=status, resolution=resolution, sla_due_at=now() + timedelta(hours=SLA_HOURS[priority]))
    db.add(t); db.commit(); db.refresh(t)
    return t

def _escalate(db, email, channel, message, intent, priority="medium"):
    t = _ticket(db, email, channel, message, intent, priority, "escalated", None)
    send_email(settings.engineering_email, f"[{priority.upper()}] Ticket #{t.id}: {intent}",
               f"Customer: {email}\nChannel: {channel}\nMessage: {message}\nSLA due: {t.sla_due_at}")
    send_email(email, f"We received your request (#{t.id})", "Our team will get back to you shortly.")
    return {"reply": f"Thanks - we've passed this to our team (ticket #{t.id}) and you'll get an email update.",
            "intent": intent, "resolved": False, "ticket_id": t.id}

def _done(db, email, channel, message, intent, reply):
    t = _ticket(db, email, channel, message, intent, "low", "closed", "resolved by autonomous_agent")
    return {"reply": reply, "intent": intent, "resolved": True, "ticket_id": t.id}

def resolve(db: Session, email: str, message: str, channel="web", order_number=None) -> dict:
    email = email.strip().lower()
    intent = classify(message)
    if intent == "password_reset":
        shopify.recover_customer(email)  # Shopify sends the email; no token is handled here
        return _done(db, email, channel, message, intent,
                     "If an account exists for that email, a password reset link is on its way.")
    if intent == "unknown":
        return _escalate(db, email, channel, message, intent)
    name = extract_order_name(message, order_number)
    if not name:
        return {"reply": "Could you share your order number (for example #1001)?", "intent": intent,
                "resolved": False, "ticket_id": None}
    order = shopify.get_order(db, name)
    # Ownership check: never reveal whether an order exists for someone else.
    if not order or order["email"].lower() != email:
        return _escalate(db, email, channel, message, intent)
    if intent == "order_status":
        return _done(db, email, channel, message, intent,
                     f"Order {name}: payment {order['financial_status']}, fulfilment {order['fulfillment_status']}.")
    if intent == "shipping":
        if order["tracking_url"]:
            return _done(db, email, channel, message, intent, f"Track order {name} here: {order['tracking_url']}")
        return _done(db, email, channel, message, intent, f"Order {name} hasn't shipped yet; you'll get a tracking email once it does.")
    if intent == "invoice":
        link = order.get("status_url")
        return _done(db, email, channel, message, intent,
                     f"Your order details for {name}: {link}" if link else f"Your invoice for {name} will be emailed to you.")
    if intent == "refund":
        placed = order["placed_at"]
        placed = placed.replace(tzinfo=timezone.utc) if placed.tzinfo is None else placed
        eligible = (not order["refunded"] and order["financial_status"] in ("paid", "partially_paid")
                    and (now() - placed).days <= settings.refund_window_days)
        if eligible and shopify.issue_refund(db, order):
            return _done(db, email, channel, message, intent, f"Your refund for {name} has been issued.")
        return _escalate(db, email, channel, message, intent)
    return _escalate(db, email, channel, message, intent)

class ChatIn(BaseModel):
    email: str
    message: str
    name: str | None = None
    channel: str = "web"
    order_number: str | None = None

@router.post("/chat/message", dependencies=[Depends(widget_key)])
def chat(body: ChatIn, db: Session = Depends(get_db)):
    return resolve(db, body.email, body.message, body.channel, body.order_number)

@router.get("/tickets", dependencies=[Depends(require_user)])
def tickets(db: Session = Depends(get_db)):
    rows = db.scalars(select(Ticket).order_by(Ticket.id.desc()).limit(200)).all()
    n = now()
    out = []
    for t in rows:
        due = t.sla_due_at.replace(tzinfo=timezone.utc) if t.sla_due_at.tzinfo is None else t.sla_due_at
        out.append({"id": t.id, "email": t.customer_email, "intent": t.intent, "priority": t.priority,
                    "status": t.status, "message": t.message, "sla_due_at": due.isoformat(),
                    "sla_breached": t.status != "closed" and n > due})
    return out

@router.post("/tickets/{tid}/close", dependencies=[Depends(require_user)])
def close_ticket(tid: int, db: Session = Depends(get_db)):
    t = db.get(Ticket, tid)
    if not t:
        raise HTTPException(404, "Not found")
    t.status = "closed"; db.commit()
    return {"ok": True}
