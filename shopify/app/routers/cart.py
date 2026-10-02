from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import Cart, CartItem, User, Variant
from ..security import current_user
from ..services import pricing

router = APIRouter(prefix="/cart", tags=["cart"])


class AddIn(BaseModel):
    variant_id: int
    qty: int = Field(default=1, ge=1, le=99)


class QtyIn(BaseModel):
    qty: int = Field(ge=0, le=99)


def _cart(db: Session, user: User) -> Cart:
    c = db.scalars(select(Cart).where(Cart.user_id == user.id)).first()
    if not c:
        c = Cart(user_id=user.id)
        db.add(c)
        db.commit()
    return c


def _sellable(db: Session, variant_id: int) -> Variant:
    v = db.get(Variant, variant_id)
    if not v or not v.is_active or not v.product.is_active:
        raise HTTPException(404, "Variant not found")
    return v


@router.get("")
def view(coupon: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    cart, items, subtotal = _cart(db, user), [], 0
    for ci in cart.items:
        v = ci.variant
        live = v.is_active and v.product.is_active
        line = v.price_cents * ci.qty
        subtotal += line
        items.append({"variant_id": v.id, "product_id": v.product_id, "title": v.product.title, "variant_title": v.title,
                      "image": v.product.images[0].url if v.product.images else None, "unit_price_cents": v.price_cents, "qty": ci.qty,
                      "line_total_cents": line, "available": v.stock, "ok": live and v.stock >= ci.qty})
    c = pricing.find_coupon(db, coupon, subtotal) if coupon else None
    return {"items": items, "pricing": pricing.quote(subtotal, c)}


@router.post("/items", status_code=201)
def add(body: AddIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    v, cart = _sellable(db, body.variant_id), _cart(db, user)
    ci = next((i for i in cart.items if i.variant_id == v.id), None)
    want = (ci.qty if ci else 0) + body.qty
    if want > v.stock:
        raise HTTPException(409, f"Only {v.stock} in stock")
    if ci:
        ci.qty = want
    else:
        db.add(CartItem(cart_id=cart.id, variant_id=v.id, qty=want))
    db.commit()
    return {"ok": True}


@router.patch("/items/{variant_id}")
def set_qty(variant_id: int, body: QtyIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    cart = _cart(db, user)
    ci = next((i for i in cart.items if i.variant_id == variant_id), None)
    if not ci:
        raise HTTPException(404, "Not in cart")
    if body.qty == 0:
        db.delete(ci)
    else:
        if body.qty > ci.variant.stock:
            raise HTTPException(409, f"Only {ci.variant.stock} in stock")
        ci.qty = body.qty
    db.commit()
    return {"ok": True}


@router.delete("/items/{variant_id}")
def remove(variant_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return set_qty(variant_id, QtyIn(qty=0), user, db)
