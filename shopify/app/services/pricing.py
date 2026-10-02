from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..config import settings
from ..models import Coupon, aware, now


def find_coupon(db: Session, code: str, subtotal: int) -> Coupon:
    c = db.scalars(select(Coupon).where(Coupon.code == code.strip().upper(), Coupon.is_active.is_(True))).first()
    if not c or (c.expires_at and aware(c.expires_at) < now()):
        raise HTTPException(400, "Invalid or expired coupon")
    if c.max_uses is not None and c.used_count >= c.max_uses:
        raise HTTPException(400, "Coupon fully redeemed")
    if subtotal < c.min_subtotal_cents:
        raise HTTPException(400, f"Coupon needs a minimum order of {c.min_subtotal_cents / 100:.2f}")
    return c


def quote(subtotal: int, coupon: Coupon | None) -> dict:
    discount = 0
    if coupon:
        discount = subtotal * coupon.percent_off // 100 if coupon.percent_off else (coupon.amount_off_cents or 0)
        discount = min(discount, subtotal)
    taxable = subtotal - discount
    shipping = 0 if taxable == 0 or taxable >= settings.free_shipping_over_cents else settings.shipping_flat_cents
    tax = round(taxable * settings.tax_rate_pct / 100)
    return {"subtotal_cents": subtotal, "discount_cents": discount, "shipping_cents": shipping, "tax_cents": tax,
            "total_cents": taxable + shipping + tax}
