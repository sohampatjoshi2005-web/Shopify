"""Response shapes shared by several routers."""
from .models import Order, Product


def public_product(p: Product) -> dict:
    vs = [v for v in p.variants if v.is_active]
    return {"id": p.id, "slug": p.slug, "title": p.title, "brand": p.brand, "description": p.description,
            "category": p.category.slug if p.category else None,
            "images": [{"url": i.url} for i in p.images],
            "variants": [{"id": v.id, "sku": v.sku, "title": v.title, "price_cents": v.price_cents, "stock": v.stock} for v in vs],
            "price_from_cents": min((v.price_cents for v in vs), default=0),
            "rating_avg": p.rating_avg, "rating_count": p.rating_count}


def order_out(o: Order) -> dict:
    return {"number": o.number, "status": o.status, "currency": o.currency, "coupon_code": o.coupon_code,
            "subtotal_cents": o.subtotal_cents, "discount_cents": o.discount_cents, "shipping_cents": o.shipping_cents,
            "tax_cents": o.tax_cents, "total_cents": o.total_cents, "created_at": o.created_at.isoformat(),
            "paid_at": o.paid_at.isoformat() if o.paid_at else None, "shipping_address": o.shipping_address,
            "tracking_number": o.tracking_number, "carrier": o.carrier,
            "items": [{"sku": i.sku, "title": i.title, "qty": i.qty, "unit_price_cents": i.unit_price_cents,
                       "line_total_cents": i.unit_price_cents * i.qty} for i in o.items],
            "payment": o.payment_client if o.status == "pending_payment" else None}
