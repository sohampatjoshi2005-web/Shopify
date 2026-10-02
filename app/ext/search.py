"""Product search. SEARCH_PROVIDER=sql (default, no extra service) or meilisearch (typo-tolerant, facets, fast).

If Meilisearch is down the API silently falls back to SQL, so search never takes the shop down. The index is kept fresh by
SQLAlchemy session hooks (no edits to your existing routers) plus an admin reindex."""
import logging
import requests
from sqlalchemy import event, func, or_, select
from sqlalchemy.orm import Session, selectinload
from ..models import Category, Product, ProductImage, Review, Variant
from ..models_market import ProductSeller
from ..schemas import public_product
from .config import cfg

log = logging.getLogger("store")
SORTS = {"new": ["id:desc"], "price_asc": ["price_from_cents:asc"], "price_desc": ["price_from_cents:desc"], "rating": ["rating_avg:desc", "rating_count:desc"]}


# ------------------------------------------------------------------ Meilisearch REST
def _m(method: str, path: str, **kw):
    h = {"Content-Type": "application/json"}
    if cfg.meili_key:
        h["Authorization"] = f"Bearer {cfg.meili_key}"
    r = requests.request(method, f"{cfg.meili_url}{path}", headers=h, timeout=10, **kw)
    r.raise_for_status()
    return r.json() if r.content else {}


def _idx(path: str = "") -> str:
    return f"/indexes/{cfg.meili_index}{path}"


def ensure_index() -> None:
    try:
        _m("POST", "/indexes", json={"uid": cfg.meili_index, "primaryKey": "id"})
    except requests.HTTPError as e:
        if e.response is None or e.response.status_code != 409:
            raise
    _m("PATCH", _idx("/settings"), json={"searchableAttributes": ["title", "brand", "category", "seller", "description"],
                                          "filterableAttributes": ["category_slug", "seller_id", "in_stock", "price_from_cents"],
                                          "sortableAttributes": ["price_from_cents", "rating_avg", "rating_count", "id"]})


def doc(db: Session, p: Product) -> dict | None:
    """Index document, or None when the product must not be searchable (inactive / nothing sellable)."""
    vs = [v for v in p.variants if v.is_active]
    if not p.is_active or not vs:
        return None
    sid = db.scalar(select(ProductSeller.seller_id).where(ProductSeller.product_id == p.id))
    return {"id": p.id, "title": p.title, "brand": p.brand or "", "description": (p.description or "")[:2000], "category": p.category.name if p.category else "",
            "category_slug": p.category.slug if p.category else "", "seller_id": sid or 0, "price_from_cents": min(v.price_cents for v in vs),
            "in_stock": any(v.stock > 0 for v in vs), "rating_avg": p.rating_avg or 0, "rating_count": p.rating_count or 0}


def push(db: Session, product_ids: set[int]) -> None:
    docs, gone = [], []
    for pid in product_ids:
        p = db.get(Product, pid)
        d = doc(db, p) if p else None
        (docs.append(d) if d else gone.append(pid))
    if docs:
        _m("POST", _idx("/documents"), json=docs)
    if gone:
        _m("POST", _idx("/documents/delete-batch"), json=gone)


def reindex_all(db: Session) -> dict:
    if cfg.search_provider != "meilisearch":
        return {"provider": "sql", "indexed": 0, "note": "SEARCH_PROVIDER is not meilisearch; nothing to index"}
    ensure_index()
    ids = list(db.scalars(select(Product.id)))
    for i in range(0, len(ids), 500):
        push(db, set(ids[i:i + 500]))
    return {"provider": "meilisearch", "indexed": len(ids)}


# ------------------------------------------------------------------ query
def _sql(db: Session, q, category, seller_id, min_price, max_price, in_stock, sort, page, size) -> tuple[list[int], int]:
    price = select(Variant.product_id.label("pid"), func.min(Variant.price_cents).label("p"), func.sum(Variant.stock).label("s")).where(Variant.is_active.is_(True)).group_by(Variant.product_id).subquery()
    base = select(Product.id).join(price, price.c.pid == Product.id).where(Product.is_active.is_(True))
    if q:
        like = f"%{q.strip().lower()}%"
        base = base.where(or_(func.lower(Product.title).like(like), func.lower(func.coalesce(Product.brand, "")).like(like), func.lower(Product.description).like(like)))
    if category:
        base = base.join(Category, Category.id == Product.category_id).where(Category.slug == category)
    if seller_id:
        base = base.join(ProductSeller, ProductSeller.product_id == Product.id).where(ProductSeller.seller_id == seller_id)
    if min_price is not None:
        base = base.where(price.c.p >= min_price)
    if max_price is not None:
        base = base.where(price.c.p <= max_price)
    if in_stock:
        base = base.where(price.c.s > 0)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    order = {"new": [Product.id.desc()], "price_asc": [price.c.p, Product.id], "price_desc": [price.c.p.desc(), Product.id], "rating": [Product.rating_avg.desc(), Product.rating_count.desc(), Product.id]}[sort]
    return list(db.scalars(base.order_by(*order).offset((page - 1) * size).limit(size))), total


def search(db: Session, q=None, category=None, seller_id=None, min_price=None, max_price=None, in_stock=False, sort="new", page=1, page_size=12) -> dict:
    engine, facets, ids, total = "sql", None, None, 0
    if cfg.search_provider == "meilisearch":
        flt = [f'category_slug = "{category}"'] if category and category.replace("-", "").isalnum() else []
        if seller_id:
            flt.append(f"seller_id = {int(seller_id)}")
        if min_price is not None:
            flt.append(f"price_from_cents >= {int(min_price)}")
        if max_price is not None:
            flt.append(f"price_from_cents <= {int(max_price)}")
        if in_stock:
            flt.append("in_stock = true")
        try:
            body = {"q": q or "", "filter": flt, "offset": (page - 1) * page_size, "limit": page_size, "facets": ["category_slug", "in_stock"]}
            if not q:
                body["sort"] = SORTS[sort]
            elif sort != "new":
                body["sort"] = SORTS[sort]
            r = _m("POST", _idx("/search"), json=body)
            ids, total, facets, engine = [h["id"] for h in r["hits"]], r.get("estimatedTotalHits", r.get("totalHits", 0)), r.get("facetDistribution"), "meilisearch"
        except (requests.RequestException, KeyError, ValueError):
            log.warning("Meilisearch unavailable, falling back to SQL search")
    if ids is None:
        ids, total = _sql(db, q, category, seller_id, min_price, max_price, in_stock, sort, page, page_size)
    rows = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_(ids)).options(selectinload(Product.variants), selectinload(Product.images), selectinload(Product.category)))} if ids else {}
    out = {"items": [public_product(rows[i]) for i in ids if i in rows and rows[i].is_active], "total": total, "page": page, "page_size": page_size, "engine": engine}
    if facets:
        out["facets"] = facets
    return out


def suggest(db: Session, q: str, limit: int = 6) -> list[str]:
    q = (q or "").strip()
    if len(q) < 2:
        return []
    return [i["title"] for i in search(db, q=q, page_size=limit)["items"]]


# ------------------------------------------------------------------ keep the index fresh
_HOOKED = False


def _collect(session: Session, ctx) -> None:
    dirty: set[int] = session.info.setdefault("x_search_dirty", set())
    for o in list(session.new) + list(session.dirty) + list(session.deleted):
        if isinstance(o, Product):
            dirty.add(o.id)
        elif isinstance(o, (Variant, ProductImage, ProductSeller, Review)) and getattr(o, "product_id", None):
            dirty.add(o.product_id)


def _after_commit(session: Session) -> None:
    ids = session.info.pop("x_search_dirty", None)
    if not ids or cfg.search_provider != "meilisearch":
        return
    try:
        from ..db import SessionLocal
        with SessionLocal() as s:           # a fresh session: the committing one can't run SQL inside after_commit
            push(s, {i for i in ids if i})
    except Exception:
        log.exception("search index update failed (run Admin -> Search -> Reindex to repair)")


def install_hooks() -> None:
    global _HOOKED
    if _HOOKED:
        return
    event.listen(Session, "after_flush", _collect)
    event.listen(Session, "after_commit", _after_commit)
    event.listen(Session, "after_rollback", lambda s: s.info.pop("x_search_dirty", None))
    _HOOKED = True
