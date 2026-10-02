"""Shopify connector: signed webhooks, paginated product import, stock push-back."""
import base64
import hashlib
import hmac
import html
import logging
import re
from decimal import Decimal
from urllib.parse import urlparse
import requests
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..config import settings
from ..models import Product, ProductImage, Variant
from ..models_market import ProductSeller, ShopifyConnection, ShopifyLink
from .slugs import unique_slug

log = logging.getLogger("store")
_DOMAIN = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")
MAX_PRODUCTS = 5000


def valid_domain(d: str) -> bool:
    return bool(_DOMAIN.match(d or ""))


def verify_webhook(raw: bytes, header: str | None) -> bool:
    if not settings.shopify_api_secret or not header:
        return False
    want = base64.b64encode(hmac.new(settings.shopify_api_secret.encode(), raw, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(want, header)


def _base(c: ShopifyConnection) -> str:
    return f"https://{c.shop_domain}/admin/api/{settings.shopify_api_version}"


def _headers(c: ShopifyConnection) -> dict:
    return {"X-Shopify-Access-Token": c.access_token}


def fetch_products(c: ShopifyConnection) -> list[dict]:
    """All products, following Link-header pagination but only while it stays on the connected shop host."""
    url, out = f"{_base(c)}/products.json?limit=250", []
    while url and len(out) < MAX_PRODUCTS:
        r = requests.get(url, headers=_headers(c), timeout=20)
        r.raise_for_status()
        out += r.json().get("products", [])
        nxt = r.links.get("next", {}).get("url")
        u = urlparse(nxt) if nxt else None
        url = nxt if u and u.scheme == "https" and u.hostname == c.shop_domain else None
    return out


def _cents(price) -> int:
    return int((Decimal(str(price or "0")) * 100).to_integral_value())


def _text(body_html: str | None) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", body_html or "")).strip()


def upsert_product(db: Session, seller_id: int, c: ShopifyConnection, sp: dict) -> int:
    """Create or update the local product for a Shopify product payload. Returns variants synced."""
    spid = str(sp["id"])
    link = db.scalars(select(ShopifyLink).where(ShopifyLink.connection_id == c.id, ShopifyLink.shopify_product_id == spid)).first()
    product = db.get(Variant, link.variant_id).product if link else None
    title, active = sp.get("title") or "Untitled", sp.get("status", "active") == "active"
    if not product:
        product = Product(title=title, slug=unique_slug(db, Product, title))
        db.add(product)
        db.flush()
        db.add(ProductSeller(product_id=product.id, seller_id=seller_id))
    product.title, product.brand, product.description, product.is_active = title, sp.get("vendor") or None, _text(sp.get("body_html")), active
    product.images = [ProductImage(url=i["src"], position=n) for n, i in enumerate(sp.get("images") or []) if i.get("src")]
    seen = set()
    for v in sp.get("variants") or []:
        vid = str(v["id"])
        seen.add(vid)
        vl = db.scalars(select(ShopifyLink).where(ShopifyLink.connection_id == c.id, ShopifyLink.shopify_variant_id == vid)).first()
        vt = v.get("title") or "Default"
        vt = "Default" if vt == "Default Title" else vt
        stock = max(0, int(v.get("inventory_quantity") or 0))
        if vl:
            var = db.get(Variant, vl.variant_id)
            var.title, var.price_cents, var.stock, var.is_active = vt, _cents(v.get("price")), stock, True
            vl.inventory_item_id = str(v.get("inventory_item_id") or vl.inventory_item_id)
        else:
            sku = (v.get("sku") or "").strip() or f"SH{c.id}-{vid}"
            if db.scalars(select(Variant.id).where(Variant.sku == sku)).first():
                sku = f"SH{c.id}-{vid}"
            var = Variant(product_id=product.id, sku=sku, title=vt, price_cents=_cents(v.get("price")), stock=stock)
            db.add(var)
            db.flush()
            db.add(ShopifyLink(connection_id=c.id, variant_id=var.id, shopify_product_id=spid, shopify_variant_id=vid,
                               inventory_item_id=str(v.get("inventory_item_id") or "")))
    if sp.get("variants"):  # variants deleted in Shopify stop being sold here
        for vl in db.scalars(select(ShopifyLink).where(ShopifyLink.connection_id == c.id, ShopifyLink.shopify_product_id == spid)):
            if vl.shopify_variant_id not in seen:
                db.get(Variant, vl.variant_id).is_active = False
    db.flush()
    return len(seen)


def sync_order_stock(db: Session, order) -> None:
    """After a sale, push the new stock level to Shopify. Never raises: a Shopify outage must not fail a payment."""
    try:
        for it in order.items:
            if not it.variant_id:
                continue
            link = db.scalars(select(ShopifyLink).where(ShopifyLink.variant_id == it.variant_id)).first()
            conn = db.get(ShopifyConnection, link.connection_id) if link else None
            if not conn or not conn.location_id or not link.inventory_item_id:
                continue
            db.refresh(db.get(Variant, it.variant_id))
            stock = db.get(Variant, it.variant_id).stock
            r = requests.post(f"{_base(conn)}/inventory_levels/set.json", headers=_headers(conn), timeout=15,
                              json={"location_id": int(conn.location_id), "inventory_item_id": int(link.inventory_item_id), "available": stock})
            if r.status_code >= 400:
                log.error("Shopify stock push failed for %s: %s", it.sku, r.status_code)
    except Exception:
        log.exception("Shopify stock sync failed for order %s", getattr(order, "number", "?"))
