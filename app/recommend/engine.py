"""Recommendation engine on the store's real data. No ML service, no new dependencies: transparent scoring you can explain to a customer.

Signals for a user   : products they bought (paid/shipped/delivered orders), reviewed 4-5 stars, have in the cart, or wishlisted.
Candidates           : what OTHER customers bought together with those products, plus the same category / brand, plus what is selling.
Score (0-1)          : 0.45 co-purchase  + 0.20 category + 0.10 brand + 0.15 recent popularity + 0.10 rating.
Never recommended    : inactive or out-of-stock products, and (for "for you") things already bought or in the cart.
New users (no signal): popular + well-rated products, so the endpoint is never empty."""
from collections import defaultdict
from datetime import timedelta
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased, selectinload
from ..models import Cart, CartItem, Order, OrderItem, Product, Review, Variant, now
from ..schemas import public_product
from ..ext.models import WishlistItem
from .config import cfg

SALE_STATES = ("paid", "shipped", "delivered")
WEIGHTS = {"co": 0.45, "cat": 0.20, "brand": 0.10, "pop": 0.15, "rating": 0.10}
SEED_WEIGHT = {"bought": 1.0, "liked": 1.0, "carted": 0.8, "wished": 0.6}


# ------------------------------------------------------------------ signals
def user_signals(db: Session, user_id: int) -> dict:
    """{'seeds': {product_id: weight}, 'exclude': {product_id}} for one user."""
    bought = set(db.scalars(select(OrderItem.product_id).join(Order, Order.id == OrderItem.order_id)
                            .where(Order.user_id == user_id, Order.status.in_(SALE_STATES), OrderItem.product_id.is_not(None))))
    liked = set(db.scalars(select(Review.product_id).where(Review.user_id == user_id, Review.rating >= 4)))
    carted = set(db.scalars(select(Variant.product_id).join(CartItem, CartItem.variant_id == Variant.id).join(Cart, Cart.id == CartItem.cart_id).where(Cart.user_id == user_id)))
    wished = set(db.scalars(select(WishlistItem.product_id).where(WishlistItem.user_id == user_id)))
    seeds: dict[int, float] = {}
    for kind, ids in (("wished", wished), ("carted", carted), ("liked", liked), ("bought", bought)):     # strongest signal wins
        for pid in ids:
            seeds[pid] = max(seeds.get(pid, 0.0), SEED_WEIGHT[kind])
    return {"seeds": seeds, "exclude": bought | carted | wished}


# ------------------------------------------------------------------ building blocks
def sellable_ids(db: Session) -> set[int]:
    return set(db.scalars(select(Variant.product_id).join(Product, Product.id == Variant.product_id)
                          .where(Product.is_active.is_(True), Variant.is_active.is_(True), Variant.stock > 0).distinct()))


def popularity(db: Session) -> dict[int, int]:
    """Units sold per product in the last REC_POPULAR_DAYS days."""
    since = now() - timedelta(days=cfg.popular_days)
    rows = db.execute(select(OrderItem.product_id, func.sum(OrderItem.qty)).join(Order, Order.id == OrderItem.order_id)
                      .where(Order.status.in_(SALE_STATES), Order.created_at >= since, OrderItem.product_id.is_not(None)).group_by(OrderItem.product_id)).all()
    return {pid: int(n) for pid, n in rows}


def co_purchases(db: Session, seeds: dict[int, float], own_user: int | None) -> tuple[dict[int, float], dict[int, tuple[int, float]]]:
    """Other customers' baskets: for each product bought together with a seed, a weighted count of distinct buyers.
    Returns (score per product, best (seed, strength) per product for the explanation). The user's own orders are excluded."""
    if not seeds:
        return {}, {}
    a, b = aliased(OrderItem), aliased(OrderItem)
    q = (select(a.product_id, b.product_id, func.count(func.distinct(Order.user_id)))
         .select_from(a).join(b, b.order_id == a.order_id).join(Order, Order.id == a.order_id)
         .where(a.product_id.in_(list(seeds)), b.product_id.is_not(None), b.product_id != a.product_id, Order.status.in_(SALE_STATES))
         .group_by(a.product_id, b.product_id))
    if own_user is not None:
        q = q.where(Order.user_id != own_user)
    score: dict[int, float] = defaultdict(float)
    why: dict[int, tuple[int, float]] = {}
    for seed, other, buyers in db.execute(q):
        s = seeds[seed] * buyers
        score[other] += s
        if other not in why or s > why[other][1]:
            why[other] = (seed, s)
    return dict(score), why


def _rank(db: Session, seeds: dict[int, float], exclude: set[int], limit: int, own_user: int | None = None) -> list[dict]:
    ok = sellable_ids(db)
    pop = popularity(db)
    co, why = co_purchases(db, seeds, own_user)
    seed_rows = list(db.execute(select(Product.id, Product.category_id, Product.brand, Product.title).where(Product.id.in_(list(seeds))))) if seeds else []
    cats = {c for _, c, _, _ in seed_rows if c}
    brands = {b.strip().lower() for _, _, b, _ in seed_rows if b}
    titles = {pid: t for pid, _, _, t in seed_rows}

    pool: set[int] = set(co)
    if cats:
        pool |= set(db.scalars(select(Product.id).where(Product.category_id.in_(cats), Product.is_active.is_(True)).limit(cfg.pool_size)))
    if brands:
        pool |= set(db.scalars(select(Product.id).where(func.lower(Product.brand).in_(brands), Product.is_active.is_(True)).limit(cfg.pool_size)))
    pool |= {pid for pid, _ in sorted(pop.items(), key=lambda x: -x[1])[:50]}
    pool |= set(db.scalars(select(Product.id).where(Product.is_active.is_(True)).order_by(Product.id.desc()).limit(50)))   # brand-new catalogue: newest first
    pool = (pool & ok) - exclude
    if not pool:
        return []

    products = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_(pool)).options(selectinload(Product.variants), selectinload(Product.images), selectinload(Product.category)))}
    max_co = max(co.values(), default=0) or 1
    max_pop = max((pop.get(i, 0) for i in pool), default=0) or 1
    scored = []
    for pid, p in products.items():
        parts, reasons = {}, []
        if pid in co:
            parts["co"] = co[pid] / max_co
            reasons.append(f"Customers who bought “{titles.get(why[pid][0], 'an item you like')}” also bought this")
        if p.category_id in cats:
            parts["cat"] = 1.0
            reasons.append(f"More from {p.category.name}" if p.category else "Similar category")
        if p.brand and p.brand.strip().lower() in brands:
            parts["brand"] = 1.0
            reasons.append(f"From {p.brand}, a brand you like")
        if pop.get(pid):
            parts["pop"] = pop[pid] / max_pop
            reasons.append("Popular right now")
        if p.rating_count >= cfg.min_ratings and p.rating_avg >= 4:
            parts["rating"] = p.rating_avg / 5
            reasons.append(f"Highly rated ({p.rating_avg:.1f}★)")
        scored.append((sum(WEIGHTS[k] * v for k, v in parts.items()), pid, reasons))
    scored.sort(key=lambda x: (-x[0], -x[1]))

    out, per_cat = [], defaultdict(int)
    cap = max(2, limit // 2)                       # variety: no more than half the list from one category
    for s, pid, reasons in scored:
        p = products[pid]
        if per_cat[p.category_id] >= cap and len(scored) > limit:
            continue
        per_cat[p.category_id] += 1
        out.append(_item(p, s, reasons or ["New in the shop"]))
        if len(out) == limit:
            break
    return out


def _item(p: Product, score: float, reasons: list[str]) -> dict:
    d = public_product(p).model_dump()
    d["score"], d["reasons"] = round(score, 4), reasons[:3]
    return d


# ------------------------------------------------------------------ public API of the engine
def recommend_for_user(db: Session, user_id: int, limit: int = 8) -> dict:
    sig = user_signals(db, user_id)
    items = _rank(db, sig["seeds"], sig["exclude"], limit, own_user=user_id)
    return {"strategy": "personalised" if sig["seeds"] else "popular", "items": items}


def similar_products(db: Session, product_id: int, limit: int = 6) -> list[dict]:
    return _rank(db, {product_id: 1.0}, {product_id}, limit)


def popular_products(db: Session, limit: int = 8) -> list[dict]:
    return _rank(db, {}, set(), limit)
