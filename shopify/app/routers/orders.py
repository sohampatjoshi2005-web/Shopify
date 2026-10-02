from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import Address, Order, User
from ..schemas import order_out
from ..security import current_user
from ..services import orders as svc

router = APIRouter(tags=["orders"])


class AddressIn(BaseModel):
    full_name: str = Field(min_length=1, max_length=255)
    phone: str = Field(min_length=5, max_length=32)
    line1: str = Field(min_length=1, max_length=255)
    line2: str | None = Field(default=None, max_length=255)
    city: str = Field(min_length=1, max_length=128)
    state: str = Field(min_length=1, max_length=128)
    postal_code: str = Field(min_length=3, max_length=16)
    country: str = Field(default="IN", min_length=2, max_length=2)


class CheckoutIn(BaseModel):
    address_id: int
    coupon_code: str | None = None


def _addr(a: Address) -> dict:
    return {"id": a.id, **{k: getattr(a, k) for k in ("full_name", "phone", "line1", "line2", "city", "state", "postal_code", "country")}}


@router.get("/addresses")
def addresses(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return [_addr(a) for a in db.scalars(select(Address).where(Address.user_id == user.id).order_by(Address.id))]


@router.post("/addresses", status_code=201)
def add_address(body: AddressIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    a = Address(user_id=user.id, **body.model_dump())
    db.add(a)
    db.commit()
    return _addr(a)


@router.post("/checkout", status_code=201)
def checkout(body: CheckoutIn, user: User = Depends(current_user), db: Session = Depends(get_db),
             idempotency_key: str | None = Header(None, max_length=64)):
    return order_out(svc.create_order(db, user, body.address_id, (body.coupon_code or "").strip() or None, idempotency_key))


def _mine(db: Session, user: User, number: str) -> Order:
    o = db.scalars(select(Order).where(Order.number == number, Order.user_id == user.id)).first()
    if not o:
        raise HTTPException(404, "Order not found")
    return o


@router.get("/orders")
def my_orders(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return [order_out(o) for o in db.scalars(select(Order).where(Order.user_id == user.id).order_by(Order.id.desc()))]


@router.get("/orders/{number}")
def my_order(number: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return order_out(_mine(db, user, number))


@router.post("/orders/{number}/cancel")
def cancel_order(number: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    o = _mine(db, user, number)
    svc.customer_cancel(db, o)
    db.refresh(o)
    return order_out(o)
