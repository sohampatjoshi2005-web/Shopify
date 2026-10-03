import json
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ..config import settings
from ..db import get_db
from ..models import Order, WebhookEvent
from ..schemas import order_out
from ..security import current_user
from ..services import orders as svc, payments

router = APIRouter(prefix="/payments", tags=["payments"])


def _seen(db: Session, event_id: str) -> bool:
    """Read-only check. The dedup row is written AFTER the work (see _record), so a crash mid-way lets the provider's retry redo it.
    Reprocessing is safe: mark_paid only flips an order that is still pending_payment."""
    return bool(event_id) and db.get(WebhookEvent, event_id) is not None


def _record(db: Session, provider: str, event_id: str) -> None:
    if event_id:
        db.add(WebhookEvent(id=event_id, provider=provider))


def _by_ref(db: Session, ref: str) -> Order | None:
    return db.scalars(select(Order).where(Order.payment_ref == ref)).first()


@router.post("/webhook/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db), stripe_signature: str | None = Header(None)):
    raw = await request.body()
    if not payments.verify_stripe(raw, stripe_signature):
        raise HTTPException(400, "Bad signature")
    ev = json.loads(raw)
    try:
        if _seen(db, ev.get("id", "")):
            return {"status": "duplicate"}
        obj = ev["data"]["object"]
        if ev["type"] == "payment_intent.succeeded":
            o = _by_ref(db, obj["id"])
            if o:
                svc.mark_paid(db, o, obj["id"], obj.get("amount_received", 0))
        _record(db, "stripe", ev.get("id", ""))
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"status": "duplicate"}
    return {"status": "ok"}


@router.post("/webhook/razorpay")
async def razorpay_webhook(request: Request, db: Session = Depends(get_db), x_razorpay_signature: str | None = Header(None),
                           x_razorpay_event_id: str = Header("")):
    raw = await request.body()
    if not payments.verify_razorpay(raw, x_razorpay_signature):
        raise HTTPException(400, "Bad signature")
    ev = json.loads(raw)
    try:
        if _seen(db, x_razorpay_event_id):
            return {"status": "duplicate"}
        if ev.get("event") in ("payment.captured", "order.paid"):
            pay = ev["payload"]["payment"]["entity"]
            o = _by_ref(db, pay.get("order_id", ""))
            if o:
                svc.mark_paid(db, o, pay["id"], pay.get("amount", 0))
        _record(db, "razorpay", x_razorpay_event_id)
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"status": "duplicate"}
    return {"status": "ok"}


@router.post("/mock/{number}/confirm")
def mock_confirm(number: str, user=Depends(current_user), db: Session = Depends(get_db)):
    """Dev-only stand-in for a real payment. Disabled in production and when a real provider is configured."""
    if settings.env == "production" or settings.payment_provider != "mock":
        raise HTTPException(404, "Not found")
    o = db.scalars(select(Order).where(Order.number == number, Order.user_id == user.id)).first()
    if not o:
        raise HTTPException(404, "Order not found")
    svc.mark_paid(db, o, f"mockpay_{number}", o.total_cents)
    return order_out(o)
