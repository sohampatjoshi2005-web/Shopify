import json
import logging
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ..config import settings
from ..db import get_db
from ..models import Order, User, WebhookEvent
from ..security import current_user
from ..services import orders as svc, payments as pay

router = APIRouter(prefix="/payments", tags=["payments"])
log = logging.getLogger("store")


@router.post("/mock/{number}/confirm")
def mock_confirm(number: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if settings.payment_provider != "mock":
        raise HTTPException(404, "Not found")
    o = db.scalars(select(Order).where(Order.number == number, Order.user_id == user.id)).first()
    if not o:
        raise HTTPException(404, "Order not found")
    if o.status != "pending_payment":
        raise HTTPException(409, f"Order is {o.status}")
    if not svc.mark_paid(db, o, f"mock_pay_{o.number}", o.total_cents):
        raise HTTPException(409, "Payment could not be applied")
    return {"status": "paid", "number": o.number}


def _once(db: Session, event_id: str, provider: str) -> bool:
    """True the first time an event id is seen."""
    if db.get(WebhookEvent, event_id):
        return False
    db.add(WebhookEvent(id=event_id, provider=provider))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return False
    return True


@router.post("/webhook/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db), stripe_signature: str | None = Header(None)):
    raw = await request.body()
    if not pay.verify_stripe(raw, stripe_signature):
        raise HTTPException(400, "Bad signature")
    ev = json.loads(raw)
    if ev.get("type") != "payment_intent.succeeded" or not _once(db, f"stripe:{ev.get('id')}", "stripe"):
        return {"status": "ignored"}
    pi = ev["data"]["object"]
    o = db.scalars(select(Order).where(Order.payment_ref == pi["id"])).first()
    if o:
        svc.mark_paid(db, o, pi["id"], int(pi.get("amount_received") or pi.get("amount") or 0))
    return {"status": "ok"}


@router.post("/webhook/razorpay")
async def razorpay_webhook(request: Request, db: Session = Depends(get_db), x_razorpay_signature: str | None = Header(None),
                           x_razorpay_event_id: str | None = Header(None)):
    raw = await request.body()
    if not pay.verify_razorpay(raw, x_razorpay_signature):
        raise HTTPException(400, "Bad signature")
    ev = json.loads(raw)
    if ev.get("event") != "payment.captured" or (x_razorpay_event_id and not _once(db, f"razorpay:{x_razorpay_event_id}", "razorpay")):
        return {"status": "ignored"}
    p = ev["payload"]["payment"]["entity"]
    o = db.scalars(select(Order).where(Order.payment_ref == p.get("order_id"))).first()
    if o:
        svc.mark_paid(db, o, p["id"], int(p.get("amount") or 0))
    return {"status": "ok"}
