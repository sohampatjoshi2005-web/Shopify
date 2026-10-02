import re
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import Category, Product, ProductImage, Review, Variant
from ..schemas import (CategoryIn, ProductIn, ProductOut, ProductPatch, VariantIn, VariantOut, VariantPatch,
                       ReviewIn, public_product)
from ..security import admin_user, current_user

router = APIRouter(tags=["catalog"])


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "item"


def unique_slug(db: Session, model, base: str) -> str:
    slug, n = slugify(base), 1
    while db.scalars(select(model.id).where(model.slug == slug)).first():
        n += 1
        slug = f"{slugify(base)}-{n}"
    return slug


def _category_by_slug(db: Session, slug: str | None) -> Category | None:
    if not slug:
        return None
    c = db.scalars(select(Category).where(Category.slug == slug)).first()
    if not c:
        raise HTTPException(400, f"Unknown category '{slug}'")
    return c


# ---------- public ----------
@router.get("/categories")
def categories(db: Session = Depends(get_db)):
    return [{"id": c.id, "name": c.name, "slug": c.slug, "parent_id": c.parent_id} for c in db.scalars(select(Category).order_by(Category.name))]


@router.get("/products")
def list_products(q: str | None = None, category: str | None = None, min_price: int | None = Query(None, ge=0),
                  max_price: int | None = Query(None, ge=0), in_stock: bool = False,
                  sort: Literal["new", "price_asc", "price_desc", "rating"] = "new",
                  page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=50), db: Session = Depends(get_db)):
    pf = (select(Variant.product_id, func.min(Variant.price_cents).label("minp"), func.sum(Variant.stock).label("stock"))
          .where(Variant.is_active.is_(True)).group_by(Variant.product_id).subquery())
    stmt = select(Product).join(pf, pf.c.product_id == Product.id).where(Product.is_active.is_(True))
    if q and q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Product.title.ilike(like), Product.brand.ilike(like), Product.description.ilike(like)))
    if category:
        cat = _category_by_slug(db, category)
        ids = [cat.id] + list(db.scalars(select(Category.id).where(Category.parent_id == cat.id)))
        stmt = stmt.where(Product.category_id.in_(ids))
    if min_price is not None:
        stmt = stmt.where(pf.c.minp >= min_price)
    if max_price is not None:
        stmt = stmt.where(pf.c.minp <= max_price)
    if in_stock:
        stmt = stmt.where(pf.c.stock > 0)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    order = {"new": Product.id.desc(), "price_asc": pf.c.minp.asc(), "price_desc": pf.c.minp.desc(),
             "rating": Product.rating_avg.desc()}[sort]
    rows = db.scalars(stmt.order_by(order, Product.id).offset((page - 1) * page_size).limit(page_size)).unique().all()
    return {"items": [public_product(p) for p in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/products/{slug}", response_model=ProductOut)
def product_detail(slug: str, db: Session = Depends(get_db)):
    p = db.scalars(select(Product).where(Product.slug == slug, Product.is_active.is_(True))).first()
    if not p:
        raise HTTPException(404, "Product not found")
    return public_product(p)


@router.get("/products/{slug}/reviews")
def product_reviews(slug: str, db: Session = Depends(get_db)):
    p = db.scalars(select(Product).where(Product.slug == slug)).first()
    if not p:
        raise HTTPException(404, "Product not found")
    rows = db.scalars(select(Review).where(Review.product_id == p.id).order_by(Review.id.desc()).limit(100)).all()
    return [{"rating": r.rating, "comment": r.comment, "author": (r.user.full_name or "Customer").split(" ")[0],
             "created_at": r.created_at.isoformat()} for r in rows]


@router.post("/products/{slug}/reviews", status_code=201)
def add_review(slug: str, body: ReviewIn, user=Depends(current_user), db: Session = Depends(get_db)):
    from ..models import Order, OrderItem
    p = db.scalars(select(Product).where(Product.slug == slug)).first()
    if not p:
        raise HTTPException(404, "Product not found")
    bought = db.scalars(select(OrderItem.id).join(Order, Order.id == OrderItem.order_id).where(
        Order.user_id == user.id, Order.status.in_(("paid", "shipped", "delivered")), OrderItem.product_id == p.id)).first()
    if not bought:
        raise HTTPException(403, "Only verified buyers can review this product")
    db.add(Review(user_id=user.id, product_id=p.id, rating=body.rating, comment=body.comment))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "You already reviewed this product")
    avg, cnt = db.execute(select(func.avg(Review.rating), func.count()).where(Review.product_id == p.id)).one()
    p.rating_avg, p.rating_count = round(float(avg), 2), cnt
    db.commit()
    return {"ok": True}


# ---------- admin ----------
@router.post("/admin/categories", status_code=201, dependencies=[Depends(admin_user)])
def create_category(body: CategoryIn, db: Session = Depends(get_db)):
    parent = _category_by_slug(db, body.parent_slug)
    c = Category(name=body.name, slug=unique_slug(db, Category, body.name), parent_id=parent.id if parent else None)
    db.add(c)
    db.commit()
    return {"id": c.id, "name": c.name, "slug": c.slug}


@router.post("/admin/products", status_code=201, response_model=ProductOut, dependencies=[Depends(admin_user)])
def create_product(body: ProductIn, db: Session = Depends(get_db)):
    cat = _category_by_slug(db, body.category_slug)
    skus = [v.sku for v in body.variants]
    if len(set(skus)) != len(skus) or db.scalars(select(Variant.id).where(Variant.sku.in_(skus))).first():
        raise HTTPException(409, "Duplicate SKU")
    p = Product(title=body.title, slug=unique_slug(db, Product, body.title), description=body.description,
                brand=body.brand, category_id=cat.id if cat else None)
    p.variants = [Variant(**v.model_dump()) for v in body.variants]
    p.images = [ProductImage(url=u, alt=body.title, position=i) for i, u in enumerate(body.image_urls)]
    db.add(p)
    db.commit()
    return p


@router.patch("/admin/products/{pid}", response_model=ProductOut, dependencies=[Depends(admin_user)])
def update_product(pid: int, body: ProductPatch, db: Session = Depends(get_db)):
    p = db.get(Product, pid)
    if not p:
        raise HTTPException(404, "Not found")
    data = body.model_dump(exclude_unset=True)
    if "category_slug" in data:
        cat = _category_by_slug(db, data.pop("category_slug"))
        p.category_id = cat.id if cat else None
    for k, v in data.items():
        setattr(p, k, v)
    db.commit()
    return p


@router.delete("/admin/products/{pid}", status_code=204, dependencies=[Depends(admin_user)])
def archive_product(pid: int, db: Session = Depends(get_db)):
    """Soft delete: past orders keep referencing the product."""
    p = db.get(Product, pid)
    if not p:
        raise HTTPException(404, "Not found")
    p.is_active = False
    db.commit()


@router.post("/admin/products/{pid}/variants", status_code=201, response_model=VariantOut, dependencies=[Depends(admin_user)])
def add_variant(pid: int, body: VariantIn, db: Session = Depends(get_db)):
    if not db.get(Product, pid):
        raise HTTPException(404, "Not found")
    if db.scalars(select(Variant.id).where(Variant.sku == body.sku)).first():
        raise HTTPException(409, "Duplicate SKU")
    v = Variant(product_id=pid, **body.model_dump())
    db.add(v)
    db.commit()
    return v


@router.patch("/admin/variants/{vid}", response_model=VariantOut, dependencies=[Depends(admin_user)])
def update_variant(vid: int, body: VariantPatch, db: Session = Depends(get_db)):
    v = db.get(Variant, vid)
    if not v:
        raise HTTPException(404, "Not found")
    for k, val in body.model_dump(exclude_unset=True).items():
        setattr(v, k, val)
    db.commit()
    return v
