import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.models import Category, Review, Variant
from app.ext import recommend as reco
from app.ext.models import WishlistItem
from . import factory as f
from .conftest import auth


@pytest.fixture()
def rc(db):
    from app.db import get_db
    from app.ext.recommend_api import router
    reco.invalidate()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def shop(db):
    cat = Category(name="Home", slug="home")
    db.add(cat); db.commit()
    return [f.product(db, f"Item{i}", 10000 + i * 100, cat=cat) for i in range(1, 7)]   # distinct titles/prices -> distinct SKUs


def ids(r): return [i["id"] for i in r.json()["items"]]


def test_co_purchase_ranking_reason_and_no_repeat_of_owned(db, rc):
    p = shop(db)
    me = f.user(db, "me@x.test")
    for n in range(2):
        f.paid_order(db, f.user(db, f"b{n}@x.test"), [(p[0], 1), (p[1], 1)])        # P1+P2 bought together twice
    f.paid_order(db, f.user(db, "c@x.test"), [(p[0], 1), (p[2], 1)])                 # P1+P3 once
    f.paid_order(db, me, [(p[0], 1)])
    reco.invalidate()
    r = rc.get("/recommend/me", headers=auth(me))
    got = ids(r)
    assert p[0].id not in got and got[0] == p[1].id and got.index(p[1].id) < got.index(p[2].id)
    assert r.json()["items"][0]["reason"] == "Because Item1 you bought" and len(got) == len(set(got))
    assert rc.get(f"/recommend/product/{p[0].id}").json()["items"][0]["id"] == p[1].id


def test_new_customer_gets_popular_items(db, rc):
    p = shop(db)
    f.paid_order(db, f.user(db, "a@x.test"), [(p[4], 3)])
    new = f.user(db, "new@x.test")
    r = rc.get("/recommend/me?limit=3", headers=auth(new))
    assert len(r.json()["items"]) == 3 and ids(r)[0] == p[4].id and r.json()["items"][0]["reason"] == "Popular right now"
    assert ids(rc.get("/recommend/popular?limit=2"))[0] == p[4].id


def test_wishlist_and_review_signals(db, rc):
    p = shop(db)
    for n in range(2):
        f.paid_order(db, f.user(db, f"b{n}@x.test"), [(p[0], 1), (p[1], 1)])
    me = f.user(db, "w@x.test")
    db.add(WishlistItem(user_id=me.id, product_id=p[0].id)); db.commit()
    reco.invalidate()
    top = rc.get("/recommend/me", headers=auth(me)).json()["items"][0]
    assert top["id"] == p[1].id and top["reason"] == "Because Item1 you wishlisted"
    me2 = f.user(db, "r@x.test")
    db.add(Review(user_id=me2.id, product_id=p[0].id, rating=5)); db.commit()
    reco.invalidate()
    assert rc.get("/recommend/me", headers=auth(me2)).json()["items"][0]["reason"] == "Because Item1 you rated highly"


def test_refunded_orders_and_unsellable_products_are_ignored(db, rc):
    p = shop(db)
    for n in range(2):
        f.paid_order(db, f.user(db, f"b{n}@x.test"), [(p[0], 1), (p[1], 1), (p[2], 1)])
    me = f.user(db, "me@x.test")
    f.paid_order(db, me, [(p[0], 1)])
    p[1].variants[0].stock = 0                       # out of stock
    p[2].is_active = False                           # archived
    db.commit(); reco.invalidate()
    got = ids(rc.get("/recommend/me?limit=10", headers=auth(me)))
    assert p[1].id not in got and p[2].id not in got and p[0].id not in got
    o = f.paid_order(db, f.user(db, "ref@x.test"), [(p[5], 1)])
    o.status = "refunded"; db.commit(); reco.invalidate()
    assert reco.model(db).signals.get(o.user_id, {}) == {}              # a refunded order is not a signal


def test_products_of_suspended_sellers_are_hidden(db, rc):
    _, s = f.seller(db, "Gamma", 10)
    p = shop(db)
    mine = f.product(db, "Gamma Lamp", 55500, s.id)
    for n in range(2):
        f.paid_order(db, f.user(db, f"b{n}@x.test"), [(p[0], 1), (mine, 1)])
    me = f.user(db, "me@x.test")
    f.paid_order(db, me, [(p[0], 1)]); reco.invalidate()
    assert mine.id in ids(rc.get("/recommend/me", headers=auth(me)))
    s.status = "suspended"; db.commit(); reco.invalidate()
    assert mine.id not in ids(rc.get("/recommend/me?limit=20", headers=auth(me)))


def test_auth_rules(db, rc):
    shop(db)
    assert rc.get("/recommend/me").status_code == 401
    assert rc.post("/recommend/refresh", headers=auth(f.user(db, "n@x.test"))).status_code == 403
    assert rc.post("/recommend/refresh", headers=auth(f.user(db, "adm@x.test", "admin"))).json() == {"ok": True}
    assert rc.get("/recommend/me?limit=999", headers=auth(f.user(db, "z@x.test"))).status_code == 422
