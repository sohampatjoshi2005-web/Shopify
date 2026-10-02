import logging, secrets
from datetime import timedelta
from fastapi import HTTPException
from sqlalchemy import select, update, delete, or_
from sqlalchemy.orm import Session
from ..config import settings
from ..models import Address, Cart, CartItem, Coupon, Order, OrderItem, Variant, now
from ..notify import send_email
from . import payments, pricing, shopify

log = logging.getLogger("store")


def _new_number(db: Session) -> str:
    while True:
        n = f"ORD-{secrets.token_hex(4).upper()}"
        if not db.scalars(select(Order.id).where(Order.number == n)).first():
            return n


def create_order(db: Session, user, address_id: int, coupon_code: str | None, idem_key: str | None) -> Order:
    if idem_key:  # same key => same order, so a double-click / retry never double-charges
        existing = db.scalars(select(Order).where(Order.user_id == user.id, Order.idempotency_key == idem_key)).first()
        if existing:
            return existing
    cart = db.scalars(select(Cart).where(Cart.user_id == user.id)).first()
    if not cart or not cart.items:
        raise HTTPException(400, "Cart is empty")
    addr = db.get(Address, address_id)
    if not addr or addr.user_id != user.id:
        raise HTTPException(404, "Address not found")

    lines = []
    for ci in cart.items:
        v = ci.variant
        if not v.is_active or not v.product.is_active:
            raise HTTPException(409, f"'{v.product.title}' ({v.sku}) is no longer available")
        lines.append((v, ci.qty))
    subtotal = sum(v.price_cents * q for v, q in lines)  # prices always come from the DB, never the client
    coupon = pricing.find_coupon(db, coupon_code, subtotal) if coupon_code else None
    q = pricing.quote(subtotal, coupon)

    # Reserve stock atomically: the WHERE clause makes overselling impossible even under concurrency.
    for v, qty in lines:
        res = db.execute(update(Variant).where(Variant.id == v.id, Variant.stock >= qty).values(stock=Variant.stock - qty))
        if res.rowcount != 1:
            db.rollback()
            raise HTTPException(409, f"Not enough stock for '{v.product.title}' ({v.title})")
    if coupon:
        res = db.execute(update(Coupon).where(Coupon.id == coupon.id, or_(Coupon.max_uses.is_(None), Coupon.used_count < Coupon.max_uses))
                         .values(used_count=Coupon.used_count + 1))
        if res.rowcount != 1:
            db.rollback()
            raise HTTPException(409, "Coupon fully redeemed")

    order = Order(number=_new_number(db), user_id=user.id, currency=settings.currency, coupon_id=coupon.id if coupon else None,
                  coupon_code=coupon.code if coupon else None, payment_provider=settings.payment_provider, idempotency_key=idem_key,
                  shipping_address={k: getattr(addr, k) for k in ("full_name", "phone", "line1", "line2", "city", "state", "postal_code", "country")},
                  **{k: q[k] for k in ("subtotal_cents", "discount_cents", "shipping_cents", "tax_cents", "total_cents")})
    order.items = [OrderItem(variant_id=v.id, product_id=v.product_id, sku=v.sku,
                             title=f"{v.product.title} - {v.title}" if v.title != "Default" else v.product.title,
                             unit_price_cents=v.price_cents, qty=qty) for v, qty in lines]
    db.add(order)
    db.execute(delete(CartItem).where(CartItem.cart_id == cart.id))
    db.commit()

    try:
        order.payment_ref, order.payment_client = payments.create_payment(order)
        db.commit()
    except Exception:
        log.exception("payment init failed for %s", order.number)
        cancel(db, order, allowed=("pending_payment",))
        raise HTTPException(502, "Payment provider unavailable, please try again")
    return order


def _restock(db: Session, order: Order):
    for it in order.items:
        if it.variant_id:
            db.execute(update(Variant).where(Variant.id == it.variant_id).values(stock=Variant.stock + it.qty))
    if order.coupon_id:
        db.execute(update(Coupon).where(Coupon.id == order.coupon_id, Coupon.used_count > 0).values(used_count=Coupon.used_count - 1))


def cancel(db: Session, order: Order, allowed=("pending_payment", "paid"), new_status="cancelled") -> bool:
    """Atomic status flip (so a concurrent payment webhook can't race us), then release stock."""
    res = db.execute(update(Order).where(Order.id == order.id, Order.status.in_(allowed)).values(status=new_status))
    if res.rowcount != 1:
        db.rollback()
        return False
    _restock(db, order)
    db.commit()
    order.status = new_status
    return True


def mark_paid(db: Session, order: Order, provider_payment_id: str, amount_minor: int) -> bool:
    if amount_minor != order.total_cents:
        log.error("AMOUNT MISMATCH order=%s expected=%s got=%s", order.number, order.total_cents, amount_minor)
        return False
    res = db.execute(update(Order).where(Order.id == order.id, Order.status == "pending_payment")
                     .values(status="paid", paid_at=now(), provider_payment_id=provider_payment_id))
    db.commit()
    if res.rowcount == 1:
        order.status, order.provider_payment_id = "paid", provider_payment_id
        send_email(order.user.email, f"Order {order.number} confirmed", f"Thanks! We received your payment of {order.total_cents / 100:.2f}.")
        shopify.sync_order_stock(db, order)  # push new stock back to Shopify (never raises)
        return True
    if order.status == "cancelled":  # customer paid after we expired the order: give the money back
        order.provider_payment_id = provider_payment_id
        log.error("PAID_AFTER_CANCEL order=%s, refunding", order.number)
        if payments.refund(order):
            db.execute(update(Order).where(Order.id == order.id).values(status="refunded", provider_payment_id=provider_payment_id))
            db.commit()
    return False


def customer_cancel(db: Session, order: Order):
    if order.status == "paid":
        if not payments.refund(order):
            raise HTTPException(502, "Refund failed, order not cancelled. Please contact support.")
        ok = cancel(db, order, allowed=("paid",), new_status="refunded")
    elif order.status == "pending_payment":
        ok = cancel(db, order, allowed=("pending_payment",))
    else:
        raise HTTPException(409, f"Order can't be cancelled once it is {order.status}")
    if not ok:
        raise HTTPException(409, "Order status changed, please refresh")


def expire_unpaid(db: Session) -> int:
    cutoff = now() - timedelta(minutes=settings.unpaid_expiry_minutes)
    stale = db.scalars(select(Order).where(Order.status == "pending_payment", Order.created_at < cutoff)).all()
    return sum(1 for o in stale if cancel(db, o, allowed=("pending_payment",)))
