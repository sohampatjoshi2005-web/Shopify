from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import Coupon, Order, Variant, now
from ..notify import send_email
from ..schemas import CouponIn, ShipIn, order_out
from ..security import admin_user
from ..services import orders as svc, payments

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(admin_user)])


def _order(db: Session, number: str) -> Order:
    o = db.scalars(select(Order).where(Order.number == number)).first()
    if not o:
        raise HTTPException(404, "Order not found")
    return o


@router.get("/orders")
def list_orders(status: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                db: Session = Depends(get_db)):
    stmt = select(Order).order_by(Order.id.desc())
    if status:
        stmt = stmt.where(Order.status == status)
    return [{**order_out(o), "customer_email": o.user.email} for o in db.scalars(stmt.offset((page - 1) * page_size).limit(page_size))]


def _advance(db: Session, o: Order, allowed: tuple, new: str, **fields):
    from sqlalchemy import update
    res = db.execute(update(Order).where(Order.id == o.id, Order.status.in_(allowed)).values(status=new, **fields))
    db.commit()
    if res.rowcount != 1:
        raise HTTPException(409, f"Can't move an order from '{o.status}' to '{new}'")
    db.refresh(o)


@router.post("/orders/{number}/ship")
def ship(number: str, body: ShipIn, db: Session = Depends(get_db)):
    o = _order(db, number)
    _advance(db, o, ("paid",), "shipped", tracking_number=body.tracking_number, carrier=body.carrier, shipped_at=now())
    send_email(o.user.email, f"Order {o.number} shipped", f"Tracking: {o.carrier} {o.tracking_number}")
    return order_out(o)


@router.post("/orders/{number}/deliver")
def deliver(number: str, db: Session = Depends(get_db)):
    o = _order(db, number)
    _advance(db, o, ("shipped",), "delivered")
    return order_out(o)


@router.post("/orders/{number}/refund")
def refund(number: str, restock: bool = False, db: Session = Depends(get_db)):
    o = _order(db, number)
    if o.status not in ("paid", "shipped", "delivered"):
        raise HTTPException(409, f"Can't refund an order that is '{o.status}'")
    if not payments.refund(o):
        raise HTTPException(502, "Provider refused the refund, check the payment dashboard")
    if restock:
        svc.cancel(db, o, allowed=("paid", "shipped", "delivered"), new_status="refunded")
    else:
        _advance(db, o, ("paid", "shipped", "delivered"), "refunded")
    return order_out(o)


@router.post("/maintenance/expire-unpaid")
def expire(db: Session = Depends(get_db)):
    return {"cancelled": svc.expire_unpaid(db)}


@router.get("/stats")
def stats(db: Session = Depends(get_db)):
    by_status = dict(db.execute(select(Order.status, func.count()).group_by(Order.status)).all())
    revenue = db.scalar(select(func.coalesce(func.sum(Order.total_cents), 0)).where(Order.status.in_(("paid", "shipped", "delivered"))))
    low = db.scalars(select(Variant).where(Variant.is_active.is_(True), Variant.stock <= 5).order_by(Variant.stock).limit(50)).all()
    return {"orders_by_status": by_status, "revenue_cents": revenue,
            "low_stock": [{"sku": v.sku, "product": v.product.title, "stock": v.stock} for v in low]}


@router.post("/coupons", status_code=201)
def create_coupon(body: CouponIn, db: Session = Depends(get_db)):
    if bool(body.percent_off) == bool(body.amount_off_cents):
        raise HTTPException(400, "Set exactly one of percent_off or amount_off_cents")
    c = Coupon(**{**body.model_dump(), "code": body.code.strip().upper()})
    db.add(c)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Coupon code exists")
    return {"id": c.id, "code": c.code}


@router.get("/coupons")
def list_coupons(db: Session = Depends(get_db)):
    return [{c.name: getattr(x, c.name) for c in Coupon.__table__.columns} for x in db.scalars(select(Coupon).order_by(Coupon.id.desc()))]
