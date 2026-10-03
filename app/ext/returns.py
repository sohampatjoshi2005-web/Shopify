"""Returns: requested -> approved -> received -> refunded (or rejected / cancelled). Supports partial returns and partial refunds,
restocking, seller ledger reversal and GST credit notes."""
import logging
import requests
from datetime import timedelta
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from ..config import settings
from ..models import Order, OrderItem, Variant, aware, now
from . import gst
from .config import cfg
from .ledger import reverse_split
from .models import LineSplit, ReturnLine, ReturnRequest, Shipment

log = logging.getLogger("store")
OPEN = ("requested", "approved", "received", "refunded")      # states whose quantities count as "already returned/pending"


class ReturnError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status, self.msg = status, msg


def _flip(db: Session, rid: int, frm: tuple, to: str, **extra) -> None:
    res = db.execute(update(ReturnRequest).where(ReturnRequest.id == rid, ReturnRequest.status.in_(frm)).values(status=to, updated_at=now(), **extra))
    db.commit()
    if res.rowcount != 1:
        raise ReturnError(409, f"Return is not in a state that allows this (expected {', '.join(frm)})")


def _returned_qty(db: Session, order_item_id: int, states=OPEN) -> int:
    return int(db.scalar(select(func.coalesce(func.sum(ReturnLine.qty), 0)).join(ReturnRequest, ReturnRequest.id == ReturnLine.return_id)
                         .where(ReturnLine.order_item_id == order_item_id, ReturnRequest.status.in_(states))) or 0)


def _delivered_at(db: Session, order: Order):
    ts = [s.delivered_at for s in db.scalars(select(Shipment).where(Shipment.order_number == order.number, Shipment.status == "delivered")) if s.delivered_at]
    return aware(max(ts)) if ts else aware(order.paid_at or order.created_at)


def create_return(db: Session, user, order_number: str, lines: list[dict], reason: str, note: str = "", restock: bool = True) -> ReturnRequest:
    o = db.scalars(select(Order).where(Order.number == order_number, Order.user_id == user.id)).first()
    if not o:
        raise ReturnError(404, "Order not found")
    if o.status != "delivered":
        raise ReturnError(409, f"Only delivered orders can be returned (this one is {o.status})")
    if now() > _delivered_at(db, o) + timedelta(days=cfg.return_window_days):
        raise ReturnError(409, f"The {cfg.return_window_days}-day return window has passed")
    if not lines:
        raise ReturnError(400, "Pick at least one item")
    items, picked = {i.id: i for i in o.items}, {}
    for ln in lines:
        picked[ln["order_item_id"]] = picked.get(ln["order_item_id"], 0) + int(ln["qty"])
    for iid, q in picked.items():
        it = items.get(iid)
        if not it:
            raise ReturnError(400, f"Item {iid} is not part of this order")
        if q < 1 or q + _returned_qty(db, iid) > it.qty:
            raise ReturnError(409, f"Cannot return {q} of '{it.title}' (bought {it.qty}, already requested/returned {_returned_qty(db, iid)})")
    r = ReturnRequest(order_number=o.number, user_id=user.id, reason=reason[:255], note=note, restock=restock)
    db.add(r)
    db.flush()
    db.add_all([ReturnLine(return_id=r.id, order_item_id=i, qty=q) for i, q in picked.items()])
    db.commit()
    return r


def lines_of(db: Session, ret: ReturnRequest) -> list[ReturnLine]:
    return list(db.scalars(select(ReturnLine).where(ReturnLine.return_id == ret.id)))


def refund_amount(order: Order, qty_by_item: dict[int, int]) -> int:
    """Items value, minus their share of the order discount, plus their share of tax charged. Shipping is not refunded."""
    sub = sum(i.unit_price_cents * i.qty for i in order.items) or 1
    total = 0
    for it in order.items:
        q = qty_by_item.get(it.id, 0)
        if not q:
            continue
        line = it.unit_price_cents * q
        total += line - order.discount_cents * line // sub + order.tax_cents * line // sub
    return total


def refund_partial(order: Order, cents: int, key: str) -> str | None:
    """Provider refund of `cents`. Returns the provider's refund id, or None if it was not confirmed."""
    try:
        if order.payment_provider == "mock":
            return f"mock_refund_{key}"
        pid = order.provider_payment_id
        if not pid:
            return None
        if order.payment_provider == "stripe":
            r = requests.post("https://api.stripe.com/v1/refunds", auth=(settings.stripe_secret, ""), timeout=20,
                              data={"payment_intent": pid, "amount": cents}, headers={"Idempotency-Key": f"rf-{key}"})
        else:
            r = requests.post(f"https://api.razorpay.com/v1/payments/{pid}/refund", timeout=20, json={"amount": cents, "receipt": key[:40]},
                              auth=(settings.razorpay_key_id, settings.razorpay_key_secret))
        return (r.json().get("id") or key) if r.status_code < 300 else None
    except requests.RequestException:
        log.exception("partial refund failed for %s", order.number)
        return None


def approve(db: Session, rid: int, admin_note: str = "") -> None:
    _flip(db, rid, ("requested",), "approved", admin_note=admin_note[:500])


def reject(db: Session, rid: int, admin_note: str = "") -> None:
    _flip(db, rid, ("requested", "approved"), "rejected", admin_note=admin_note[:500])


def cancel_by_customer(db: Session, rid: int, user) -> None:
    r = db.get(ReturnRequest, rid)
    if not r or r.user_id != user.id:
        raise ReturnError(404, "Return not found")
    _flip(db, rid, ("requested",), "cancelled")


def mark_received(db: Session, rid: int) -> None:
    _flip(db, rid, ("approved",), "received")


def refund(db: Session, rid: int) -> ReturnRequest:
    r = db.get(ReturnRequest, rid)
    if not r:
        raise ReturnError(404, "Return not found")
    if r.status != "received":
        raise ReturnError(409, f"Return is {r.status}; goods must be received before refunding")
    o = db.scalars(select(Order).where(Order.number == r.order_number)).first()
    ls = lines_of(db, r)
    amount = refund_amount(o, {l.order_item_id: l.qty for l in ls})
    ref = refund_partial(o, amount, f"{o.number}-ret{r.id}")
    if not ref:
        raise ReturnError(502, "The payment provider did not confirm the refund")
    # claim the transition first so a double click can't refund twice
    _flip(db, r.id, ("received",), "refunded", refund_cents=amount, refund_ref=ref)
    items = {i.id: i for i in o.items}
    for l in ls:
        it = items[l.order_item_id]
        if r.restock and it.variant_id:
            db.execute(update(Variant).where(Variant.id == it.variant_id).values(stock=Variant.stock + l.qty))
        sp = db.scalars(select(LineSplit).where(LineSplit.order_item_id == it.id)).first()
        if sp:
            reverse_split(db, sp, sp.net_cents * l.qty // max(sp.qty, 1), f"ret{r.id}-item{it.id}", f"Return #{r.id}")
    if all(_returned_qty(db, i.id, ("refunded",)) >= i.qty for i in o.items):
        db.execute(update(Order).where(Order.id == o.id, Order.status.in_(("delivered", "shipped", "paid"))).values(status="refunded"))
    db.commit()
    try:
        gst.issue_credit_notes(db, r)
    except Exception:
        db.rollback()
        log.exception("credit note failed for return %s", r.id)
    db.refresh(r)
    return r
