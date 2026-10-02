from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import Category, Coupon, Order, Product, ProductImage, User, Variant, now
from ..security import admin_user
from ..services import orders as svc, payments as pay
from ..services.slugs import unique_slug

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(admin_user)])


@router.get("/stats")
def stats(db: Session = Depends(get_db)):
    by = dict(db.execute(select(Order.status, func.count()).group_by(Order.status)).all())
    revenue = db.scalar(select(func.coalesce(func.sum(Order.total_cents), 0)).where(Order.status.in_(("paid", "shipped", "delivered")))) or 0
    low = db.execute(select(Variant.sku, Product.title, Variant.title, Variant.stock).join(Product, Product.id == Variant.product_id)
                     .where(Variant.stock <= 5, Variant.is_active.is_(True), Product.is_active.is_(True)).order_by(Variant.stock, Variant.id).limit(50)).all()
    return {"revenue_cents": int(revenue), "orders_by_status": by,
            "low_stock": [{"sku": s, "product": p, "variant": v, "stock": n} for s, p, v, n in low]}


@router.get("/orders")
def orders(status: str | None = None, db: Session = Depends(get_db)):
    q = select(Order, User.email).join(User, User.id == Order.user_id).order_by(Order.id.desc()).limit(200)
    return [{"number": o.number, "customer_email": e, "status": o.status, "total_cents": o.total_cents, "created_at": o.created_at.isoformat()}
            for o, e in db.execute(q.where(Order.status == status) if status else q)]


def _order(db: Session, number: str) -> Order:
    o = db.scalars(select(Order).where(Order.number == number)).first()
    if not o:
        raise HTTPException(404, "Order not found")
    return o


def _flip(db: Session, o: Order, frm: tuple, to: str, **extra) -> None:
    res = db.execute(update(Order).where(Order.id == o.id, Order.status.in_(frm)).values(status=to, **extra))
    db.commit()
    if res.rowcount != 1:
        raise HTTPException(409, f"Order is {o.status}, expected one of {', '.join(frm)}")


class ShipIn(BaseModel):
    tracking_number: str = Field(min_length=1, max_length=64)
    carrier: str = Field(default="", max_length=64)


@router.post("/orders/{number}/ship")
def ship(number: str, body: ShipIn, db: Session = Depends(get_db)):
    o = _order(db, number)
    _flip(db, o, ("paid",), "shipped", tracking_number=body.tracking_number, carrier=body.carrier or None)
    return {"number": number, "status": "shipped"}


@router.post("/orders/{number}/deliver")
def deliver(number: str, db: Session = Depends(get_db)):
    _flip(db, _order(db, number), ("shipped",), "delivered")
    return {"number": number, "status": "delivered"}


@router.post("/orders/{number}/refund")
def refund(number: str, restock: bool = False, db: Session = Depends(get_db)):
    o = _order(db, number)
    if o.status not in ("paid", "shipped", "delivered"):
        raise HTTPException(409, f"Order is {o.status}, nothing to refund")
    if not pay.refund(o):
        raise HTTPException(502, "The payment provider did not confirm the refund")
    _flip(db, o, ("paid", "shipped", "delivered"), "refunded")
    if restock:
        svc._restock(db, o)
        db.commit()
    return {"number": number, "status": "refunded", "restocked": restock}


class VariantIn(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    title: str = Field(default="Default", max_length=128)
    price_cents: int = Field(ge=0)
    stock: int = Field(default=0, ge=0)


class ProductIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    brand: str | None = Field(default=None, max_length=128)
    category_slug: str | None = None
    description: str = ""
    variants: list[VariantIn] = Field(min_length=1)
    image_urls: list[str] = []


@router.post("/products", status_code=201)
def create_product(body: ProductIn, db: Session = Depends(get_db)):
    cat = None
    if body.category_slug:
        cat = db.scalars(select(Category).where(Category.slug == body.category_slug)).first()
        if not cat:
            raise HTTPException(404, "Category not found")
    skus = [v.sku for v in body.variants]
    if len(set(skus)) != len(skus) or db.scalars(select(Variant.id).where(Variant.sku.in_(skus))).first():
        raise HTTPException(409, "SKU already exists")
    p = Product(title=body.title, slug=unique_slug(db, Product, body.title), brand=body.brand, description=body.description,
                category_id=cat.id if cat else None)
    p.variants = [Variant(**v.model_dump()) for v in body.variants]
    p.images = [ProductImage(url=u, position=n) for n, u in enumerate(body.image_urls)]
    db.add(p)
    db.commit()
    return {"id": p.id, "slug": p.slug}


@router.delete("/products/{pid}")
def archive_product(pid: int, db: Session = Depends(get_db)):
    p = db.get(Product, pid)
    if not p:
        raise HTTPException(404, "Product not found")
    p.is_active = False  # archived, not deleted: past orders keep their history
    db.commit()
    return {"id": pid, "archived": True}


class VariantPatch(BaseModel):
    title: str | None = Field(default=None, max_length=128)
    price_cents: int | None = Field(default=None, ge=0)
    stock: int | None = Field(default=None, ge=0)


@router.patch("/variants/{vid}")
def patch_variant(vid: int, body: VariantPatch, db: Session = Depends(get_db)):
    v = db.get(Variant, vid)
    if not v:
        raise HTTPException(404, "Variant not found")
    for k, val in body.model_dump(exclude_none=True).items():
        setattr(v, k, val)
    db.commit()
    return {"id": v.id, "title": v.title, "price_cents": v.price_cents, "stock": v.stock}


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)


@router.post("/categories", status_code=201)
def create_category(body: CategoryIn, db: Session = Depends(get_db)):
    c = Category(name=body.name.strip(), slug=unique_slug(db, Category, body.name))
    db.add(c)
    db.commit()
    return {"id": c.id, "slug": c.slug}


class CouponIn(BaseModel):
    code: str = Field(min_length=3, max_length=32)
    percent_off: int | None = Field(default=None, ge=1, le=100)
    amount_off_cents: int | None = Field(default=None, ge=1)
    min_subtotal_cents: int = Field(default=0, ge=0)
    max_uses: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _one_kind(self):
        if (self.percent_off is None) == (self.amount_off_cents is None):
            raise ValueError("give either percent_off or amount_off_cents")
        return self


@router.get("/coupons")
def coupons(db: Session = Depends(get_db)):
    return [{"id": c.id, "code": c.code, "percent_off": c.percent_off, "amount_off_cents": c.amount_off_cents,
             "min_subtotal_cents": c.min_subtotal_cents, "max_uses": c.max_uses, "used_count": c.used_count, "is_active": c.is_active}
            for c in db.scalars(select(Coupon).order_by(Coupon.id.desc()))]


@router.post("/coupons", status_code=201)
def create_coupon(body: CouponIn, db: Session = Depends(get_db)):
    c = Coupon(**{**body.model_dump(), "code": body.code.strip().upper()})
    db.add(c)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Coupon code already exists")
    return {"id": c.id, "code": c.code}
