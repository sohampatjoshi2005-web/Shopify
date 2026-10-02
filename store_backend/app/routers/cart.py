from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import Cart, CartItem, Variant
from ..schemas import CartAdd, CartSet
from ..security import current_user
from ..services import pricing

router = APIRouter(prefix="/cart", tags=["cart"])


def _cart(db: Session, user) -> Cart:
    c = db.scalars(select(Cart).where(Cart.user_id == user.id)).first()
    if not c:
        c = Cart(user_id=user.id)
        db.add(c)
        db.commit()
    return c


def _view(db: Session, cart: Cart, coupon_code: str | None = None):
    items, subtotal = [], 0
    for ci in cart.items:
        v = ci.variant
        subtotal += v.price_cents * ci.qty
        items.append({"variant_id": v.id, "sku": v.sku, "product_slug": v.product.slug, "title": v.product.title,
                      "variant_title": v.title, "image": v.product.images[0].url if v.product.images else None,
                      "unit_price_cents": v.price_cents, "qty": ci.qty, "line_total_cents": v.price_cents * ci.qty,
                      "available": v.stock, "ok": v.is_active and v.product.is_active and v.stock >= ci.qty})
    coupon = pricing.find_coupon(db, coupon_code, subtotal) if coupon_code else None
    return {"items": items, "pricing": pricing.quote(subtotal, coupon)}


@router.get("")
def get_cart(coupon: str | None = None, user=Depends(current_user), db: Session = Depends(get_db)):
    return _view(db, _cart(db, user), coupon)


@router.post("/items")
def add_item(body: CartAdd, user=Depends(current_user), db: Session = Depends(get_db)):
    v = db.get(Variant, body.variant_id)
    if not v or not v.is_active or not v.product.is_active:
        raise HTTPException(404, "Product not available")
    cart = _cart(db, user)
    ci = next((i for i in cart.items if i.variant_id == v.id), None)
    new_qty = (ci.qty if ci else 0) + body.qty
    if new_qty > v.stock:
        raise HTTPException(409, f"Only {v.stock} in stock")
    if ci:
        ci.qty = new_qty
    else:
        cart.items.append(CartItem(variant_id=v.id, qty=new_qty))
    db.commit()
    db.refresh(cart)
    return _view(db, cart)


@router.patch("/items/{variant_id}")
def set_qty(variant_id: int, body: CartSet, user=Depends(current_user), db: Session = Depends(get_db)):
    cart = _cart(db, user)
    ci = next((i for i in cart.items if i.variant_id == variant_id), None)
    if not ci:
        raise HTTPException(404, "Not in cart")
    if body.qty == 0:
        cart.items.remove(ci)
    elif body.qty > ci.variant.stock:
        raise HTTPException(409, f"Only {ci.variant.stock} in stock")
    else:
        ci.qty = body.qty
    db.commit()
    db.refresh(cart)
    return _view(db, cart)


@router.delete("/items/{variant_id}")
def remove_item(variant_id: int, user=Depends(current_user), db: Session = Depends(get_db)):
    return set_qty(variant_id, CartSet(qty=0), user, db)


@router.delete("", status_code=204)
def clear_cart(user=Depends(current_user), db: Session = Depends(get_db)):
    cart = _cart(db, user)
    cart.items.clear()
    db.commit()
