from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import Order
from ..schemas import CheckoutIn, order_out
from ..security import current_user
from ..services import orders as svc

router = APIRouter(tags=["orders"])


def _mine(db: Session, user, number: str) -> Order:
    o = db.scalars(select(Order).where(Order.number == number, Order.user_id == user.id)).first()
    if not o:
        raise HTTPException(404, "Order not found")  # same answer for "not yours" and "doesn't exist"
    return o


@router.post("/checkout", status_code=201)
def checkout(body: CheckoutIn, idempotency_key: str | None = Header(None, max_length=80),
             user=Depends(current_user), db: Session = Depends(get_db)):
    """Turns the cart into an order, reserves stock, and returns what the frontend needs to take payment."""
    return order_out(svc.create_order(db, user, body.address_id, body.coupon_code, idempotency_key))


@router.get("/orders")
def my_orders(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=50),
              user=Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Order).where(Order.user_id == user.id).order_by(Order.id.desc())
                      .offset((page - 1) * page_size).limit(page_size)).all()
    return [order_out(o) for o in rows]


@router.get("/orders/{number}")
def order_detail(number: str, user=Depends(current_user), db: Session = Depends(get_db)):
    return order_out(_mine(db, user, number))


@router.post("/orders/{number}/cancel")
def cancel_order(number: str, user=Depends(current_user), db: Session = Depends(get_db)):
    o = _mine(db, user, number)
    svc.customer_cancel(db, o)
    return order_out(o)
