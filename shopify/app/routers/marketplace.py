from datetime import timezone
from typing import Literal
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
import json, requests
from ..db import get_db
from ..models import Product, User, Variant, WebhookEvent, now
from ..models_market import ProductSeller, Seller, ShopifyConnection, ShopifyLink
from ..schemas import public_product
from ..security import admin_user, current_user
from ..services import shopify
from .catalog import unique_slug

router = APIRouter(tags=["marketplace"])


class SellerIn(BaseModel):
    name: str = Field(min_length=2, max_length=128)


class ShopifyIn(BaseModel):
    shop_domain: str
    access_token: str = Field(min_length=10, max_length=255)
    location_id: str | None = Field(default=None, pattern=r"^\d+$")


def _seller(db: Session, user, approved=False) -> Seller:
    s = db.scalars(select(Seller).where(Seller.user_id == user.id)).first()
    if not s:
        raise HTTPException(404, "You are not a seller yet")
    if approved and s.status != "approved":
        raise HTTPException(403, f"Seller account is {s.status}")
    return s


@router.post("/sellers", status_code=201)
def become_seller(body: SellerIn, user=Depends(current_user), db: Session = Depends(get_db)):
    if db.scalars(select(Seller.id).where(Seller.user_id == user.id)).first():
        raise HTTPException(409, "Already a seller")
    s = Seller(user_id=user.id, name=body.name, slug=unique_slug(db, Seller, body.name))
    db.add(s)
    db.commit()
    return {"id": s.id, "slug": s.slug, "status": s.status}


@router.get("/sellers/me")
def my_seller(user=Depends(current_user), db: Session = Depends(get_db)):
    s = _seller(db, user)
    c = db.scalars(select(ShopifyConnection).where(ShopifyConnection.seller_id == s.id)).first()
    return {"id": s.id, "name": s.name, "slug": s.slug, "status": s.status, "commission_pct": s.commission_pct,
            "shopify": {"shop_domain": c.shop_domain, "last_import_at": c.last_import_at} if c else None}


@router.post("/admin/sellers/{sid}/status", dependencies=[Depends(admin_user)])
def set_seller_status(sid: int, status: Literal["approved", "suspended"], db: Session = Depends(get_db)):
    s = db.get(Seller, sid)
    if not s:
        raise HTTPException(404, "Not found")
    s.status = status
    db.commit()
    return {"id": s.id, "status": s.status}


@router.get("/sellers/{slug}/products")
def seller_products(slug: str, db: Session = Depends(get_db)):
    s = db.scalars(select(Seller).where(Seller.slug == slug, Seller.status == "approved")).first()
    if not s:
        raise HTTPException(404, "Seller not found")
    rows = db.scalars(select(Product).join(ProductSeller, ProductSeller.product_id == Product.id)
                      .where(ProductSeller.seller_id == s.id, Product.is_active.is_(True)).order_by(Product.id.desc()).limit(50)).all()
    return {"seller": s.name, "items": [public_product(p) for p in rows]}


@router.put("/sellers/me/shopify")
def connect_shopify(body: ShopifyIn, user=Depends(current_user), db: Session = Depends(get_db)):
    s = _seller(db, user, approved=True)
    domain = body.shop_domain.strip().lower()
    if not shopify.valid_domain(domain):
        raise HTTPException(400, "shop_domain must look like your-store.myshopify.com")
    c = db.scalars(select(ShopifyConnection).where(ShopifyConnection.seller_id == s.id)).first()
    taken = db.scalars(select(ShopifyConnection).where(ShopifyConnection.shop_domain == domain)).first()
    if taken and taken.seller_id != s.id:
        raise HTTPException(409, "That Shopify store is connected to another seller")
    if not c:
        c = ShopifyConnection(seller_id=s.id, shop_domain=domain, access_token=body.access_token)
        db.add(c)
    c.shop_domain, c.access_token, c.location_id = domain, body.access_token, body.location_id
    db.commit()
    return {"connected": domain}


@router.post("/sellers/me/shopify/import")
def import_shopify(user=Depends(current_user), db: Session = Depends(get_db)):
    s = _seller(db, user, approved=True)
    c = db.scalars(select(ShopifyConnection).where(ShopifyConnection.seller_id == s.id)).first()
    if not c:
        raise HTTPException(404, "Connect a Shopify store first")
    try:
        products = shopify.fetch_products(c)
    except requests.RequestException as e:
        raise HTTPException(502, f"Shopify request failed: {getattr(e.response, 'status_code', 'network error')}")
    variants = sum(shopify.upsert_product(db, s.id, c, sp) for sp in products)
    c.last_import_at = now()
    db.commit()
    return {"products": len(products), "variants_synced": variants}


@router.post("/webhooks/shopify")
async def shopify_webhook(request: Request, db: Session = Depends(get_db),
                          x_shopify_hmac_sha256: str | None = Header(None), x_shopify_topic: str = Header(""),
                          x_shopify_shop_domain: str = Header(""), x_shopify_webhook_id: str = Header("")):
    raw = await request.body()
    if not shopify.verify_webhook(raw, x_shopify_hmac_sha256):
        raise HTTPException(401, "Bad signature")
    c = db.scalars(select(ShopifyConnection).where(ShopifyConnection.shop_domain == x_shopify_shop_domain.lower())).first()
    if not c:
        return {"status": "ignored"}
    try:
        if x_shopify_webhook_id:
            eid = f"shopify:{x_shopify_webhook_id}"
            if db.get(WebhookEvent, eid):
                return {"status": "duplicate"}
            db.add(WebhookEvent(id=eid, provider="shopify"))
        data = json.loads(raw)
        if x_shopify_topic == "inventory_levels/update":
            if c.location_id is None or str(data.get("location_id")) == c.location_id:
                link = db.scalars(select(ShopifyLink).where(ShopifyLink.connection_id == c.id,
                                                            ShopifyLink.inventory_item_id == str(data.get("inventory_item_id")))).first()
                if link and data.get("available") is not None:
                    db.get(Variant, link.variant_id).stock = max(0, int(data["available"]))
        elif x_shopify_topic in ("products/create", "products/update"):
            shopify.upsert_product(db, c.seller_id, c, data)
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"status": "duplicate"}
    return {"status": "ok"}


@router.get("/admin/sellers", dependencies=[Depends(admin_user)])
def list_sellers(db: Session = Depends(get_db)):
    q = select(Seller, User).join(User, User.id == Seller.user_id).order_by(Seller.id.desc())
    return [{"id": s.id, "name": s.name, "slug": s.slug, "status": s.status, "owner_email": u.email} for s, u in db.execute(q)]
