from datetime import timedelta
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.models import Cart, CartItem, Category, Order, Review, now
from app.ext.models import WishlistItem
from app.recommend import engine, leads as ld
from app.recommend.models import Lead
from . import factory as f
from .conftest import auth


@pytest.fixture()
def rc(db):
    from app.db import get_db
    from app.ext import install
    from app.recommend import install as install_rec
    app = FastAPI()
    install(app)
    install_rec(app)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def shop(db):
    """A small catalogue and four customers. Mug+Cap are bought together by u2 and u3; u1 only bought the Mug."""
    mugs, hats, tees = (Category(name=n, slug=n.lower()) for n in ("Mugs", "Hats", "Tees"))
    db.add_all([mugs, hats, tees]); db.commit()
    mug = f.product(db, "Blue Mug", 30000, cat=mugs); mug.brand = "Acme"
    cap = f.product(db, "Red Cap", 20000, cat=hats)
    tee = f.product(db, "Green Tee", 25000, cat=tees)
    mug2 = f.product(db, "Red Mug", 28000, cat=mugs)
    gone = f.product(db, "Sold Out Hat", 10000, cat=hats, stock=0)
    dead = f.product(db, "Archived Tee", 10000, cat=tees); dead.is_active = False
    db.commit()
    u1, u2, u3, u4 = (f.user(db, f"u{i}@example.com") for i in range(1, 5))
    f.paid_order(db, u1, [(mug, 1)])
    f.paid_order(db, u2, [(mug, 1), (cap, 1)])
    f.paid_order(db, u3, [(mug, 1), (cap, 1), (tee, 1)])
    return dict(mug=mug, cap=cap, tee=tee, mug2=mug2, gone=gone, dead=dead, u1=u1, u2=u2, u3=u3, u4=u4)


def ids(res): return [i["id"] for i in res["items"]]


def cart_of(db, user):
    c = db.query(Cart).filter_by(user_id=user.id).first()
    if not c:
        c = Cart(user_id=user.id); db.add(c); db.commit()
    return c


def test_copurchase_drives_recommendation_and_explains_why(db):
    w = shop(db)
    res = engine.recommend_for_user(db, w["u1"].id, 8)
    assert res["strategy"] == "personalised"
    assert ids(res)[0] == w["cap"].id                                              # what u2 and u3 bought with the mug
    top = res["items"][0]
    assert any("also bought" in r and "Blue Mug" in r for r in top["reasons"]) and top["score"] > 0
    assert top["price_from_cents"] == 20000 and "variants" in top                  # same product shape as the catalogue API


def test_never_recommends_owned_unsellable_or_carted_products(db):
    w = shop(db)
    got = ids(engine.recommend_for_user(db, w["u1"].id, 24))
    assert w["mug"].id not in got                                                  # already bought
    assert w["gone"].id not in got and w["dead"].id not in got                     # out of stock / archived
    c = cart_of(db, w["u1"])
    db.add(CartItem(cart_id=c.id, variant_id=w["cap"].variants[0].id, qty=1)); db.commit()
    assert w["cap"].id not in ids(engine.recommend_for_user(db, w["u1"].id, 24))   # already in the cart
    db.add(WishlistItem(user_id=w["u1"].id, product_id=w["tee"].id)); db.commit()
    assert w["tee"].id not in ids(engine.recommend_for_user(db, w["u1"].id, 24))   # already wishlisted


def test_wishlist_and_reviews_count_as_signals(db):
    w = shop(db)
    db.add(WishlistItem(user_id=w["u4"].id, product_id=w["mug"].id)); db.commit()
    res = engine.recommend_for_user(db, w["u4"].id, 8)
    assert res["strategy"] == "personalised" and ids(res)[0] == w["cap"].id        # wishlisted mug -> cap
    u5 = f.user(db, "u5@example.com")
    db.add(Review(user_id=u5.id, product_id=w["mug"].id, rating=5)); db.commit()
    assert ids(engine.recommend_for_user(db, u5.id, 8))[0] == w["cap"].id
    u6 = f.user(db, "u6@example.com")
    db.add(Review(user_id=u6.id, product_id=w["mug"].id, rating=1)); db.commit()   # a 1-star review is not a liking
    assert engine.recommend_for_user(db, u6.id, 8)["strategy"] == "popular"


def test_category_and_brand_affinity_without_any_sales(db):
    w = shop(db)
    res = engine.recommend_for_user(db, w["u1"].id, 24)
    red_mug = next(i for i in res["items"] if i["id"] == w["mug2"].id)
    assert "More from Mugs" in red_mug["reasons"]                                  # no one bought it, but u1 likes mugs


def test_new_user_gets_popular_items_not_an_empty_list(db):
    w = shop(db)
    res = engine.recommend_for_user(db, w["u4"].id, 3)
    assert res["strategy"] == "popular" and len(res["items"]) == 3
    assert ids(res)[0] in (w["mug"].id, w["cap"].id)                               # best sellers first
    assert all("Popular right now" in i["reasons"] or i["reasons"] for i in res["items"])
    assert len(engine.recommend_for_user(db, w["u4"].id, 1)["items"]) == 1        # limit respected


def test_empty_catalogue_is_not_an_error(db):
    u = f.user(db, "lonely@example.com")
    assert engine.recommend_for_user(db, u.id, 5) == {"strategy": "popular", "items": []}


def test_similar_products(db):
    w = shop(db)
    got = ids({"items": engine.similar_products(db, w["mug"].id, 5)})
    assert w["mug"].id not in got and got[0] == w["cap"].id and w["gone"].id not in got


def test_http_endpoints_and_access(db, rc):
    w = shop(db)
    assert rc.get("/recommend").status_code == 401                                 # personal data needs a login
    r = rc.get("/recommend?limit=2", headers=auth(w["u1"])).json()
    assert r["strategy"] == "personalised" and len(r["items"]) <= 2
    assert rc.get("/recommend?limit=0", headers=auth(w["u1"])).status_code == 422
    assert [i["id"] for i in rc.get(f"/recommend/similar/{w['mug'].id}").json()["items"]][0] == w["cap"].id
    assert rc.get(f"/recommend/similar/{w['dead'].id}").status_code == 404
    assert rc.get("/recommend/similar/99999").status_code == 404
    assert rc.get("/recommend/popular").json()["strategy"] == "popular"
    # one customer never sees another's shelf
    assert rc.get("/recommend", headers=auth(w["u4"])).json()["strategy"] == "popular"


# ------------------------------------------------------------------ leads
def test_lead_sync_segments_scores_and_idempotency(db):
    w = shop(db)
    admin = f.user(db, "boss@example.com", "admin")
    db.add_all([WishlistItem(user_id=w["u4"].id, product_id=w["tee"].id), WishlistItem(user_id=w["u4"].id, product_id=w["cap"].id)])
    c = cart_of(db, w["u4"])
    db.add(CartItem(cart_id=c.id, variant_id=w["mug2"].variants[0].id, qty=1)); db.commit()
    f.paid_order(db, w["u1"], [(w["tee"], 1)])                                       # u1 now has 2 orders -> repeat
    lapsed = f.paid_order(db, f.user(db, "old@example.com"), [(w["tee"], 1)])
    db.query(Order).filter_by(id=lapsed.id).update({"paid_at": now() - timedelta(days=120), "created_at": now() - timedelta(days=120)}); db.commit()

    assert ld.sync(db)["created"] == 5 and not db.scalars(select(Lead).where(Lead.email == admin.email)).first()   # admins are not leads
    L = {l.email: l for l in db.scalars(select(Lead))}
    assert (L["u4@example.com"].segment, L["u4@example.com"].score) == ("prospect", 10 + 16 + 25)                    # registered + 2 wishlist + cart
    assert (L["u2@example.com"].segment, L["u2@example.com"].orders_count) == ("new_customer", 1)
    assert L["u1@example.com"].segment == "repeat" and L["u1@example.com"].score == 10 + 30 + 10
    assert L["old@example.com"].segment == "lapsed" and L["old@example.com"].score == 25                           # no recency bonus
    assert L["u2@example.com"].status == "converted" and L["u4@example.com"].status == "new"
    again = ld.sync(db)
    assert again["created"] == 0 and db.query(Lead).count() == 5                                                    # idempotent


def test_prospect_who_buys_is_converted_and_manual_status_is_kept(db):
    w = shop(db)
    ld.sync(db)
    lead = db.scalars(select(Lead).where(Lead.email == "u4@example.com")).one()
    lead.status = "contacted"; db.commit()
    f.paid_order(db, w["u4"], [(w["tee"], 1)])
    ld.sync(db); db.refresh(lead)
    assert lead.status == "converted" and lead.segment == "new_customer"
    lead.status = "lost"; db.commit()
    ld.sync(db); db.refresh(lead)
    assert lead.status == "lost"                                                    # sync never overrides a human decision


def test_admin_lead_endpoints_and_permissions(db, rc):
    w = shop(db)
    admin = f.user(db, "boss@example.com", "admin")
    ha, hc = auth(admin), auth(w["u1"])
    for path in ("/admin/recommend/summary", "/admin/recommend/leads"):
        assert rc.get(path, headers=hc).status_code == 403 and rc.get(path).status_code == 401
    assert rc.post("/admin/recommend/leads/sync", headers=hc).status_code == 403
    # a manual lead whose email already has an account is linked and scored, WITHOUT creating anyone else's lead
    r2 = rc.post("/admin/recommend/leads", json={"email": "u2@example.com"}, headers=ha).json()
    assert r2["user_id"] == w["u2"].id and r2["segment"] == "new_customer" and r2["source"] == "manual" and db.query(Lead).count() == 1
    assert rc.post("/admin/recommend/leads/sync", headers=ha).json()["created"] == 3          # u1, u3, u4 (u2 already has one)
    rows = rc.get("/admin/recommend/leads", headers=ha).json()
    assert [r["score"] for r in rows] == sorted((r["score"] for r in rows), reverse=True)
    assert {r["email"] for r in rc.get("/admin/recommend/leads?segment=prospect", headers=ha).json()} == {"u4@example.com"}
    assert [r["email"] for r in rc.get("/admin/recommend/leads?q=u3", headers=ha).json()] == ["u3@example.com"]
    # manual lead: unique, can be edited, gets popular items because it has no account
    r = rc.post("/admin/recommend/leads", json={"email": "Walkin@Example.com", "name": "Walk In"}, headers=ha)
    assert r.status_code == 201 and r.json()["email"] == "walkin@example.com" and r.json()["source"] == "manual" and r.json()["user_id"] is None
    assert rc.post("/admin/recommend/leads", json={"email": "walkin@example.com"}, headers=ha).status_code == 409
    lid = r.json()["id"]
    assert rc.patch(f"/admin/recommend/leads/{lid}", json={"status": "contacted", "notes": "met at fair"}, headers=ha).json()["status"] == "contacted"
    assert rc.patch(f"/admin/recommend/leads/{lid}", json={"status": "bogus"}, headers=ha).status_code == 422
    assert rc.get(f"/admin/recommend/leads/{lid}/products", headers=ha).json()["strategy"] == "popular"
    assert rc.get("/admin/recommend/leads/9999/products", headers=ha).status_code == 404
    # per-customer shelf, and a preview by email
    top = rc.get(f"/admin/recommend/leads/{next(r['id'] for r in rows if r['email'] == 'u1@example.com')}/products", headers=ha).json()
    assert top["strategy"] == "personalised" and top["items"][0]["id"] == w["cap"].id
    assert rc.get("/admin/recommend/preview?email=u1@example.com", headers=ha).json()["items"][0]["id"] == w["cap"].id
    assert rc.get("/admin/recommend/preview?email=ghost@example.com", headers=ha).status_code == 404
    s = rc.get("/admin/recommend/summary", headers=ha).json()
    assert s["total"] == 5 and s["by_segment"]["prospect"] >= 1 and s["popular"]


def test_segment_and_score_functions():
    t = now()
    assert ld.segment_of(0, None, t) == "prospect" and ld.segment_of(1, t, t) == "new_customer" and ld.segment_of(5, t, t) == "repeat"
    assert ld.segment_of(3, t - timedelta(days=200), t) == "lapsed"
    assert ld.score_of(0, None, 0, 0, False, t) == 10
    assert ld.score_of(9, t, 99, 9, True, t) == 100                               # capped
