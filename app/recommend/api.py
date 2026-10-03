"""Recommendation API.
Customers : GET /recommend (for me), GET /recommend/similar/{product_id}, GET /recommend/popular. Everything is scoped to the caller.
Admins    : lead list, sync from the store, per-lead recommendations, summary, preview for any customer email."""
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import Product, User
from ..security import admin_user, current_user
from . import engine, leads as ld
from .models import Lead

rec = APIRouter(prefix="/recommend", tags=["recommendations"])
adm = APIRouter(prefix="/admin/recommend", tags=["recommendations (admin)"], dependencies=[Depends(admin_user)])


# ------------------------------------------------------------------ customers
@rec.get("")
def for_me(limit: int = Query(8, ge=1, le=24), user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Recommended for the logged-in customer (purchases, reviews, cart, wishlist; popular items if there is no history yet)."""
    return engine.recommend_for_user(db, user.id, limit)


@rec.get("/popular")
def popular(limit: int = Query(8, ge=1, le=24), db: Session = Depends(get_db)):
    return {"strategy": "popular", "items": engine.popular_products(db, limit)}


@rec.get("/similar/{product_id}")
def similar(product_id: int, limit: int = Query(6, ge=1, le=24), db: Session = Depends(get_db)):
    p = db.get(Product, product_id)
    if not p or not p.is_active:
        raise HTTPException(404, "Product not found")
    return {"product_id": product_id, "items": engine.similar_products(db, product_id, limit)}


# ------------------------------------------------------------------ admin: leads
class LeadIn(BaseModel):
    email: EmailStr
    name: str = Field(default="", max_length=255)
    notes: str = Field(default="", max_length=2000)


class LeadPatch(BaseModel):
    status: Literal["new", "contacted", "converted", "lost"] | None = None
    notes: str | None = Field(default=None, max_length=2000)


def _lead_out(l: Lead) -> dict:
    return {"id": l.id, "email": l.email, "name": l.name, "user_id": l.user_id, "source": l.source, "status": l.status, "segment": l.segment, "score": l.score,
            "orders_count": l.orders_count, "wishlist_count": l.wishlist_count, "cart_items": l.cart_items, "notes": l.notes,
            "last_order_at": l.last_order_at.isoformat() if l.last_order_at else None, "created_at": l.created_at.isoformat()}


def _lead(db: Session, lid: int) -> Lead:
    l = db.get(Lead, lid)
    if not l:
        raise HTTPException(404, "Lead not found")
    return l


@adm.get("/summary")
def summary(db: Session = Depends(get_db)):
    return {**ld.summary(db), "popular": [{"id": i["id"], "title": i["title"], "score": i["score"]} for i in engine.popular_products(db, 5)]}


@adm.post("/leads/sync")
def sync(db: Session = Depends(get_db)):
    return ld.sync(db)


@adm.get("/leads")
def list_leads(status: str | None = None, segment: str | None = None, q: str | None = Query(None, max_length=100),
               page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)):
    stmt = select(Lead).order_by(Lead.score.desc(), Lead.id)
    if status:
        stmt = stmt.where(Lead.status == status)
    if segment:
        stmt = stmt.where(Lead.segment == segment)
    if q and q.strip():
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(Lead.email.like(like), Lead.name.ilike(like)))
    return [_lead_out(l) for l in db.scalars(stmt.offset((page - 1) * page_size).limit(page_size))]


@adm.post("/leads", status_code=201)
def add_lead(body: LeadIn, db: Session = Depends(get_db)):
    email = str(body.email).lower()
    u = db.scalars(select(User).where(User.email == email)).first()
    l = Lead(email=email, name=body.name or (u.full_name if u else ""), user_id=u.id if u else None, source="manual", notes=body.notes)
    db.add(l)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A lead with this email already exists")
    if u:
        ld.sync(db, only_user_id=u.id)      # fills in segment / score for this customer only
        db.refresh(l)
    return _lead_out(l)


@adm.patch("/leads/{lid}")
def update_lead(lid: int, body: LeadPatch, db: Session = Depends(get_db)):
    l = _lead(db, lid)
    if body.status:
        l.status = body.status
    if body.notes is not None:
        l.notes = body.notes
    db.commit()
    return _lead_out(l)


@adm.get("/leads/{lid}/products")
def lead_products(lid: int, limit: int = Query(6, ge=1, le=24), db: Session = Depends(get_db)):
    """What to put in front of this lead. Leads without an account get the shop's popular items."""
    l = _lead(db, lid)
    res = engine.recommend_for_user(db, l.user_id, limit) if l.user_id else {"strategy": "popular", "items": engine.popular_products(db, limit)}
    return {"lead": _lead_out(l), **res}


@adm.get("/preview")
def preview(email: EmailStr, limit: int = Query(8, ge=1, le=24), db: Session = Depends(get_db)):
    """Admin check: what would this customer see on their 'Recommended for you' shelf?"""
    u = db.scalars(select(User).where(User.email == str(email).lower())).first()
    if not u:
        raise HTTPException(404, "No account with that email")
    return {"user_id": u.id, **engine.recommend_for_user(db, u.id, limit)}


ROUTERS = [rec, adm]
