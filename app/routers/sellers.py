"""Minimal seller marketplace API: apply, view own account, admin approve/suspend, assign products to a seller.
(Shopify import and everything else seller-related lives in app/ext/api.py or is not part of this build.)"""
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from ..db import get_db
from ..models import Product, User
from ..models_market import ProductSeller, Seller
from ..security import admin_user, current_user
from .catalog import unique_slug

router = APIRouter(tags=["sellers"])


class SellerIn(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    description: str = Field(default="", max_length=2000)


def _out(s: Seller) -> dict:
    return {"id": s.id, "name": s.name, "slug": s.slug, "status": s.status, "description": s.description,
            "commission_pct": s.commission_pct}


@router.post("/sellers", status_code=201)
def apply(body: SellerIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if db.scalars(select(Seller).where(Seller.user_id == user.id)).first():
        raise HTTPException(409, "You already have a seller account")
    s = Seller(user_id=user.id, name=body.name.strip(), slug=unique_slug(db, Seller, body.name), description=body.description)
    db.add(s)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "You already have a seller account")
    return _out(s)


@router.get("/sellers/me")
def my_seller(user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = db.scalars(select(Seller).where(Seller.user_id == user.id)).first()
    if not s:
        raise HTTPException(404, "You are not a seller yet")
    return _out(s)


@router.get("/admin/sellers", dependencies=[Depends(admin_user)])
def list_sellers(db: Session = Depends(get_db)):
    rows = db.execute(select(Seller, User.email).join(User, User.id == Seller.user_id).order_by(Seller.id)).all()
    return [{**_out(s), "owner_email": email} for s, email in rows]


@router.post("/admin/sellers/{sid}/status", dependencies=[Depends(admin_user)])
def set_status(sid: int, status: Literal["pending", "approved", "suspended"], db: Session = Depends(get_db)):
    s = db.get(Seller, sid)
    if not s:
        raise HTTPException(404, "Seller not found")
    s.status = status
    db.commit()
    return _out(s)


@router.put("/admin/products/{pid}/seller", dependencies=[Depends(admin_user)])
def assign_product(pid: int, seller_id: int | None = None, db: Session = Depends(get_db)):
    """Attach a product to a seller (seller_id omitted -> back to house-sold)."""
    if not db.get(Product, pid):
        raise HTTPException(404, "Product not found")
    link = db.scalars(select(ProductSeller).where(ProductSeller.product_id == pid)).first()
    if seller_id is None:
        if link:
            db.delete(link)
    else:
        if not db.get(Seller, seller_id):
            raise HTTPException(404, "Seller not found")
        if link:
            link.seller_id = seller_id
        else:
            db.add(ProductSeller(product_id=pid, seller_id=seller_id))
    db.commit()
    return {"product_id": pid, "seller_id": seller_id}
