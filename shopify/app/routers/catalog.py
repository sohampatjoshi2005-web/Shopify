from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload
from ..db import get_db
from ..models import Category, Product, Review, User, Variant
from ..schemas import public_product
from ..security import current_user
from ..services.slugs import unique_slug  # noqa: F401  (re-exported: the marketplace router imports it from here)

router = APIRouter(tags=["catalog"])


@router.get("/categories")
def categories(db: Session = Depends(get_db)):
    return [{"id": c.id, "name": c.name, "slug": c.slug} for c in db.scalars(select(Category).order_by(Category.name))]


@router.get("/products")
def products(q: str | None = None, category: str | None = None, sort: str = Query("new", pattern="^(new|price_asc|price_desc|rating)$"),
             page: int = Query(1, ge=1), page_size: int = Query(12, ge=1, le=50), db: Session = Depends(get_db)):
    price = (select(Variant.product_id.label("pid"), func.min(Variant.price_cents).label("p")).where(Variant.is_active.is_(True))
             .group_by(Variant.product_id).subquery())  # only products with at least one sellable variant appear
    base = select(Product).join(price, price.c.pid == Product.id).where(Product.is_active.is_(True))
    if q:
        like = f"%{q.strip().lower()}%"
        base = base.where(or_(func.lower(Product.title).like(like), func.lower(func.coalesce(Product.brand, "")).like(like),
                              func.lower(Product.description).like(like)))
    if category:
        base = base.join(Category, Category.id == Product.category_id).where(Category.slug == category)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    order = {"new": [Product.id.desc()], "price_asc": [price.c.p, Product.id], "price_desc": [price.c.p.desc(), Product.id],
             "rating": [Product.rating_avg.desc(), Product.rating_count.desc(), Product.id]}[sort]
    rows = db.scalars(base.options(selectinload(Product.variants), selectinload(Product.images), selectinload(Product.category))
                      .order_by(*order).offset((page - 1) * page_size).limit(page_size)).all()
    return {"items": [public_product(p) for p in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/products/{pid}")
def product(pid: int, db: Session = Depends(get_db)):
    p = db.get(Product, pid)
    if not p or not p.is_active:
        raise HTTPException(404, "Product not found")
    return public_product(p)


class ReviewIn(BaseModel):
    rating: int = Field(ge=1, le=5)
    body: str = Field(default="", max_length=2000)


@router.get("/products/{pid}/reviews")
def reviews(pid: int, db: Session = Depends(get_db)):
    rows = db.execute(select(Review, User.full_name).join(User, User.id == Review.user_id).where(Review.product_id == pid).order_by(Review.id.desc()))
    return [{"rating": r.rating, "body": r.body, "author": n or "Customer", "created_at": r.created_at.isoformat()} for r, n in rows]


@router.post("/products/{pid}/reviews", status_code=201)
def add_review(pid: int, body: ReviewIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = db.get(Product, pid)
    if not p or not p.is_active:
        raise HTTPException(404, "Product not found")
    r = db.scalars(select(Review).where(Review.product_id == pid, Review.user_id == user.id)).first()
    if r:
        r.rating, r.body = body.rating, body.body
    else:
        db.add(Review(product_id=pid, user_id=user.id, rating=body.rating, body=body.body))
    db.flush()
    avg, n = db.execute(select(func.avg(Review.rating), func.count()).where(Review.product_id == pid)).one()
    p.rating_avg, p.rating_count = round(float(avg or 0), 2), n
    db.commit()
    return {"rating_avg": p.rating_avg, "rating_count": p.rating_count}
