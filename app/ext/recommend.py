"""'Recommended for you' built from the store's real data: purchases, wishlist, 4-5 star reviews and carts.

Item-item cosine similarity over a user x product signal matrix, blended with category affinity and popularity. New customers
get popular items. Only products that can be bought today are returned (active, in stock, seller approved).
In-process cache (EXT_RECO_CACHE_SECONDS, default 600); fine for thousands of products, move it to a job/Redis beyond that."""
import math
import os
import time
from collections import Counter, defaultdict
from datetime import timedelta
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import Cart, CartItem, Order, OrderItem, Product, Review, Variant, aware, now
from ..models_market import ProductSeller, Seller
from .models import WishlistItem

WEIGHT = {"purchase": 3.0, "wishlist": 2.0, "review": 2.0, "cart": 1.0}
SOLD = ("paid", "shipped", "delivered")        # refunded / cancelled orders are not a signal
VERB = {"purchase": "you bought", "wishlist": "you wishlisted", "review": "you rated highly", "cart": "is in your cart"}
_cache: dict = {"at": 0.0, "model": None}


class Model:
    def __init__(self, signals, pop: Counter, cat: dict, title: dict):
        self.signals, self.pop, self.cat, self.title = signals, pop, cat, title
        cooc: dict = defaultdict(lambda: defaultdict(float))
        norm: Counter = Counter()
        for items in signals.values():
            for i, (wi, _) in items.items():
                norm[i] += wi * wi
                for j, (wj, _) in items.items():
                    if i != j:
                        cooc[i][j] += wi * wj
        self.sim = {i: {j: v / math.sqrt(norm[i] * norm[j]) for j, v in row.items()} for i, row in cooc.items()}


def invalidate() -> None:
    _cache["model"] = None


def build(db: Session) -> Model:
    sig: dict = defaultdict(dict)

    def put(uid, pid, kind):
        if pid is not None and (pid not in sig[uid] or sig[uid][pid][0] < WEIGHT[kind]):
            sig[uid][pid] = (WEIGHT[kind], kind)

    pop: Counter = Counter()
    recent = now() - timedelta(days=90)
    for uid, pid, qty, at in db.execute(select(Order.user_id, OrderItem.product_id, OrderItem.qty, Order.created_at)
                                        .join(Order, Order.id == OrderItem.order_id).where(Order.status.in_(SOLD))):
        put(uid, pid, "purchase")
        if pid is not None:
            pop[pid] += qty * (2 if at and aware(at) >= recent else 1)
    for uid, pid in db.execute(select(WishlistItem.user_id, WishlistItem.product_id)):
        put(uid, pid, "wishlist")
    for uid, pid in db.execute(select(Review.user_id, Review.product_id).where(Review.rating >= 4)):
        put(uid, pid, "review")
    for uid, pid in db.execute(select(Cart.user_id, Variant.product_id).join(CartItem, CartItem.cart_id == Cart.id)
                               .join(Variant, Variant.id == CartItem.variant_id)):
        put(uid, pid, "cart")
    rows = db.execute(select(Product.id, Product.category_id, Product.title)).all()
    return Model(sig, pop, {r[0]: r[1] for r in rows}, {r[0]: r[2] for r in rows})


def model(db: Session) -> Model:
    if _cache["model"] is None or time.time() - _cache["at"] > float(os.getenv("EXT_RECO_CACHE_SECONDS", "600")):
        _cache.update(model=build(db), at=time.time())
    return _cache["model"]


def _sellable(db: Session, ids: list) -> dict:
    """Products someone can buy right now. Products of pending/suspended sellers are hidden."""
    if not ids:
        return {}
    hidden = set(db.scalars(select(ProductSeller.product_id).join(Seller, Seller.id == ProductSeller.seller_id).where(Seller.status != "approved")))
    ps = db.scalars(select(Product).where(Product.id.in_([i for i in ids if i not in hidden]), Product.is_active.is_(True))).all()
    return {p.id: p for p in ps if any(v.is_active and v.stock > 0 for v in p.variants)}


def _card(p: Product, reason: str, score: float) -> dict:
    vs = [v for v in p.variants if v.is_active]
    return {"id": p.id, "slug": p.slug, "title": p.title, "brand": p.brand, "category_id": p.category_id,
            "image": p.images[0].url if p.images else None, "price_from_cents": min((v.price_cents for v in vs), default=0),
            "rating_avg": p.rating_avg, "rating_count": p.rating_count, "reason": reason, "score": round(score, 4)}


def popular(db: Session, m: Model, exclude: set, limit: int, category_id=None) -> list:
    ranked = [pid for pid, _ in m.pop.most_common() if pid not in exclude and (category_id is None or m.cat.get(pid) == category_id)]
    live = _sellable(db, ranked[: limit * 4])
    out = [_card(live[i], "Popular right now", float(m.pop[i])) for i in ranked if i in live][:limit]
    if len(out) < limit:   # brand-new shop: best rated, then newest
        have = {c["id"] for c in out} | exclude
        more = db.scalars(select(Product).where(Product.is_active.is_(True)).order_by(Product.rating_avg.desc(), Product.id.desc()).limit(limit * 6)).all()
        live = _sellable(db, [p.id for p in more if p.id not in have])
        out += [_card(p, "Top rated" if p.rating_count else "New arrival", 0.0) for p in live.values()][: limit - len(out)]
    return out


def for_user(db: Session, user_id: int, limit: int = 8) -> list:
    m = model(db)
    mine = m.signals.get(user_id, {})
    if not mine:
        return popular(db, m, set(), limit)
    bought = {p for p, (_, k) in mine.items() if k == "purchase"}
    scores: Counter = Counter()
    best: dict = {}                                   # candidate -> (contribution, source product)
    for i, (wi, _) in mine.items():
        for j, s in m.sim.get(i, {}).items():
            if j in bought:
                continue
            c = wi * s
            scores[j] += c
            if c > best.get(j, (0, None))[0]:
                best[j] = (c, i)
    cats = Counter(m.cat.get(p) for p in mine if m.cat.get(p) is not None)
    total, top = sum(cats.values()) or 1, max(m.pop.values(), default=1)
    for j in list(scores):
        scores[j] += 0.3 * cats.get(m.cat.get(j), 0) / total + 0.1 * math.log1p(m.pop[j]) / math.log1p(top)
    ranked = [j for j, _ in scores.most_common(limit * 4)]
    live = _sellable(db, ranked)
    out = []
    for j in ranked:
        if j in live:
            src = best[j][1]
            out.append(_card(live[j], f"Because {m.title.get(src, 'an item')} {VERB[mine[src][1]]}", scores[j]))
        if len(out) == limit:
            break
    if len(out) < limit:                              # top up: favourite category first, then overall popularity
        have = {c["id"] for c in out} | bought
        fav = cats.most_common(1)[0][0] if cats else None
        out += popular(db, m, have, limit - len(out), fav)
        if len(out) < limit:
            out += popular(db, m, have | {c["id"] for c in out}, limit - len(out))
    return out[:limit]


def for_product(db: Session, pid: int, limit: int = 6) -> list:
    m = model(db)
    ranked = [j for j, _ in sorted(m.sim.get(pid, {}).items(), key=lambda kv: -kv[1])[: limit * 4]]
    live = _sellable(db, ranked)
    out = [_card(live[j], "Customers also liked", m.sim[pid][j]) for j in ranked if j in live][:limit]
    if len(out) < limit:
        out += popular(db, m, {c["id"] for c in out} | {pid}, limit - len(out), m.cat.get(pid))
    return out[:limit]
