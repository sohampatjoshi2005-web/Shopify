"""Bridge between the support desk and the REAL store: orders, shipments, invoices and refunds all come from the store's own tables
and services (no mirror copy). Every lookup is scoped to the customer, so a customer can only ever see their own orders."""
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import Order
from ..services import orders as store_orders
from ..ext import gst
from ..ext.models import Shipment


class OrderNotFound(Exception):
    pass


def get_order(db: Session, user, order_number: str) -> Order:
    o = db.scalars(select(Order).where(Order.number == (order_number or "").strip().upper(), Order.user_id == user.id)).first()
    if not o:
        raise OrderNotFound(order_number)
    return o


def get_order_status(db: Session, user, order_number: str) -> dict:
    o = get_order(db, user, order_number)
    return {"order_number": o.number, "order_status": o.status, "shipping_status": shipping(db, o)["summary"]}


def shipping(db: Session, o: Order) -> dict:
    parcels = [{"carrier": s.carrier, "awb": s.awb, "status": s.status}
               for s in db.scalars(select(Shipment).where(Shipment.order_number == o.number, Shipment.status != "cancelled"))]
    if parcels:
        summary = "; ".join(f"{p['carrier'] or 'carrier'} {p['awb']}: {p['status']}" for p in parcels)
    elif o.tracking_number:
        summary = f"{o.carrier or 'carrier'} {o.tracking_number}"
    else:
        summary = "not shipped yet" if o.status in ("pending_payment", "paid") else o.status
    return {"order_number": o.number, "parcels": parcels, "summary": summary}


def get_shipping_status(db: Session, user, order_number: str) -> dict:
    return shipping(db, get_order(db, user, order_number))


def get_invoice(db: Session, user, order_number: str) -> dict:
    o = get_order(db, user, order_number)
    try:
        invs = gst.invoices_for_order(db, o)
    except ValueError as e:          # order not paid yet / cancelled
        return {"order_number": o.number, "invoices": [], "note": str(e)}
    return {"order_number": o.number, "amount": o.total_cents / 100,
            "invoices": [{"id": i.id, "number": i.number, "pdf": f"/invoices/{i.id}/pdf"} for i in invs]}


def initiate_refund(db: Session, user, order_number: str, reason: str) -> dict:
    """Autonomous refunds are limited to what the store already lets a customer do alone: cancel before payment, or cancel-and-refund
    while the order is still 'paid' (not shipped). Anything later needs a human (return request), so it returns success=False."""
    o = get_order(db, user, order_number)
    if o.status in ("pending_payment", "paid"):
        try:
            store_orders.customer_cancel(db, o)
        except HTTPException as e:
            return {"success": False, "reason": str(e.detail)}
        return {"success": True, "order_number": o.number, "status": o.status}
    if o.status in ("shipped", "delivered"):
        return {"success": False, "reason": f"Order is already {o.status}; refunds at this stage go through a return request that our team reviews."}
    return {"success": False, "reason": f"Order is {o.status}; nothing to refund."}
