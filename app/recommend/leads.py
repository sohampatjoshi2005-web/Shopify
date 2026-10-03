"""Lead list fed from the store. A lead is a person we may want to reach; registered customers are synced from `users`.

segment : prospect (registered, never bought) | new_customer (1 order) | repeat (2+ orders) | lapsed (last order older than REC_LAPSED_DAYS)
score   : 0-100 = 10 registered + 8 per wishlist item (max 4) + 25 items in cart + 10 verified email + 15 per order (max 3) + 10 ordered in last 30 days
status  : new -> contacted -> converted | lost. A prospect who buys is moved to `converted` automatically."""
from datetime import timedelta
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from ..models import Cart, CartItem, Order, User, aware, now
from ..ext.models import UserFlag, WishlistItem
from .config import cfg
from .engine import SALE_STATES

STATUSES = ("new", "contacted", "converted", "lost")
SEGMENTS = ("prospect", "new_customer", "repeat", "lapsed")


def segment_of(orders: int, last_order, t=None) -> str:
    t = t or now()
    if not orders:
        return "prospect"
    if last_order and aware(last_order) < t - timedelta(days=cfg.lapsed_days):
        return "lapsed"
    return "repeat" if orders >= 2 else "new_customer"


def score_of(orders: int, last_order, wishlist: int, cart_items: int, verified: bool, t=None) -> int:
    t = t or now()
    s = 10 + 8 * min(wishlist, 4) + (25 if cart_items else 0) + (10 if verified else 0) + 15 * min(orders, 3)
    if last_order and aware(last_order) >= t - timedelta(days=30):
        s += 10
    return min(100, s)


def _stats(db: Session):
    """One query per signal (no per-user loop)."""
    orders = {uid: (n, last) for uid, n, last in db.execute(
        select(Order.user_id, func.count(), func.max(func.coalesce(Order.paid_at, Order.created_at))).where(Order.status.in_(SALE_STATES)).group_by(Order.user_id))}
    wish = dict(db.execute(select(WishlistItem.user_id, func.count()).group_by(WishlistItem.user_id)).all())
    cart = dict(db.execute(select(Cart.user_id, func.coalesce(func.sum(CartItem.qty), 0)).join(CartItem, CartItem.cart_id == Cart.id).group_by(Cart.user_id)).all())
    verified = set(db.scalars(select(UserFlag.user_id).where(UserFlag.email_verified_at.is_not(None))))
    return orders, wish, cart, verified


def sync(db: Session, only_user_id: int | None = None) -> dict:
    """Create / refresh a lead for every active customer (or just `only_user_id`), and link manual leads whose email now has an account. Idempotent."""
    from .models import Lead
    orders, wish, cart, verified = _stats(db)
    existing = {l.email: l for l in db.scalars(select(Lead))}
    created = updated = converted = 0
    t = now()
    users = select(User).where(User.role == "customer", User.is_active.is_(True))
    if only_user_id is not None:
        users = users.where(User.id == only_user_id)
    for u in db.scalars(users):
        n, last = orders.get(u.id, (0, None))
        seg, sc = segment_of(n, last, t), score_of(n, last, wish.get(u.id, 0), int(cart.get(u.id, 0)), u.id in verified, t)
        lead = existing.get(u.email.lower())
        if not lead:
            lead = Lead(email=u.email.lower(), source="store", status="new")
            db.add(lead)
            created += 1
        else:
            updated += 1
        lead.user_id, lead.name = u.id, lead.name or u.full_name or ""
        lead.segment, lead.score, lead.orders_count = seg, sc, n
        lead.wishlist_count, lead.cart_items, lead.last_order_at = wish.get(u.id, 0), int(cart.get(u.id, 0)), last
        if seg != "prospect" and lead.status in ("new", "contacted"):
            lead.status = "converted"
            converted += 1
    db.commit()
    return {"created": created, "updated": updated, "converted": converted}


def summary(db: Session) -> dict:
    from .models import Lead
    by = lambda col: dict(db.execute(select(col, func.count()).group_by(col)).all())
    return {"total": db.scalar(select(func.count()).select_from(Lead)) or 0, "by_segment": by(Lead.segment), "by_status": by(Lead.status),
            "avg_score": round(float(db.scalar(select(func.coalesce(func.avg(Lead.score), 0))) or 0), 1)}
