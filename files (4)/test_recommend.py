from datetime import timedelta
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.models import Cart, CartItem, Order, now
from app.ext.models import WishlistItem
from app.recommend import models as _rec_models  # noqa: F401  (register rec_* tables before the db fixture creates them)
from app.recommend import leads
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
    """b2 and b3 both bought mug+tee (b3 also a cap); b1 only bought the mug."""
    b1, b2, b3 = (f.user(db, f"b{i}@x.test") for i in (1, 2, 3))
    mug, tee, cap = f.product(db, "Blue Mug", 30000), f.product(db, "Green Tee", 50000), f.product(db, "Black Cap", 20000)
    gone = f.product(db, "Out Lamp", 10000, stock=0)
    old = f.product(db, "Old Hat", 15000)
    old.is_active = False
    db.commit()
    f.paid_order(db, b2, [(mug, 1), (tee, 1)])
    f.paid_order(db, b3, [(mug, 1), (tee, 1), (cap, 1)])
    f.paid_order(db, b1, [(mug, 1)])
    return b1, mug, tee, cap, gone, old


def ids(r): return [i["id"] for i in r["items"]]


def test_for_you_requires_login(rc):
    assert rc.get("/recommend/for-you").status_code == 401


def test_for_you_uses_copurchase_and_excludes_unsellable_and_owned(db, rc):
    b1, mug, tee, cap, gone, old = shop(db)
    r = rc.get("/recommend/for-you", headers=auth(b1)).json()
    assert r["strategy"] == "personalized"
    assert ids(r)[0] == tee.id and "bought" in r["items"][0]["reason"]      # bought together twice beats once
    assert mug.id not in ids(r)                                              # already bought
    assert gone.id not in ids(r) and old.id not in ids(r)                    # out of stock / archived


def test_cart_items_are_not_recommended_and_wishlist_is_explained(db, rc):
    b1, mug, tee, cap, gone, old = shop(db)
    c = db.query(Cart).filter_by(user_id=b1.id).first()             # the factory already created one
    db.add(CartItem(cart_id=c.id, variant_id=tee.variants[0].id, qty=1))
    db.add(WishlistItem(user_id=b1.id, product_id=cap.id))
    db.commit()
    r = rc.get("/recommend/for-you", headers=auth(b1)).json()
    assert tee.id not in ids(r)
    assert next(i for i in r["items"] if i["id"] == cap.id)["reason"] == "On your wishlist"


def test_cold_start_falls_back_to_popular(db, rc):
    shop(db)
    newbie = f.user(db, "newbie@x.test")
    r = rc.get("/recommend/for-you", headers=auth(newbie)).json()
    assert r["strategy"] == "popular" and r["items"]


def test_similar_is_public_and_scoped_to_the_product(db, rc):
    b1, mug, tee, cap, gone, old = shop(db)
    r = rc.get("/recommend/similar/blue-mug").json()
    assert mug.id not in ids(r) and ids(r)[0] == tee.id and r["items"][0]["reason"] == "Frequently bought with Blue Mug"
    assert rc.get("/recommend/similar/nope").status_code == 404


def test_classify():
    t = now()
    assert leads.classify(0, 0, None, t, False, t)[0] == "new"
    assert leads.classify(0, 0, None, t, True, t)[0] == "cart_waiting"
    assert leads.classify(1, 1000, t, t, False, t)[0] == "first_time"
    assert leads.classify(3, 1000, t, t, False, t)[0] == "repeat"
    assert leads.classify(3, 10**9, t, t, False, t)[0] == "high_value"
    assert leads.classify(2, 10**9, t - timedelta(days=200), t, False, t)[0] == "lapsed"
    assert 0 <= leads.classify(9, 10**9, t, t, True, t)[1] <= 100


def test_lead_sync_segments_idempotent_and_preserves_edits(db, rc):
    b1, mug, tee, cap, gone, old = shop(db)
    admin = f.user(db, "admin@x.test", "admin")
    visitor = f.user(db, "visitor@x.test")
    ha = auth(admin)
    assert rc.post("/admin/recommend/leads/sync", headers=ha).json()["created"] == 4          # b1..b3 + visitor; admin excluded
    seg = {l["email"]: l for l in rc.get("/admin/recommend/leads", headers=ha).json()["items"]}
    assert "admin@x.test" not in seg and seg["b1@x.test"]["segment"] == "first_time" and seg["visitor@x.test"]["segment"] == "new"
    assert seg["b1@x.test"]["order_count"] == 1 and seg["b1@x.test"]["lifetime_cents"] > 0
    lid = seg["visitor@x.test"]["id"]
    assert rc.patch(f"/admin/recommend/leads/{lid}", json={"status": "contacted", "owner": "Asha", "notes": "called"}, headers=ha).status_code == 200
    assert rc.post("/admin/recommend/leads/sync", headers=ha).json()["created"] == 0          # idempotent
    l = db.get(Lead, lid)
    assert (l.status, l.owner, l.notes) == ("contacted", "Asha", "called")                    # edits survive a sync
    f.paid_order(db, visitor, [(tee, 1)])
    assert rc.post("/admin/recommend/leads/sync", headers=ha).json()["converted"] == 1
    db.expire_all()
    assert db.get(Lead, lid).status == "converted" and db.get(Lead, lid).segment == "first_time"


def test_lapsed_segment(db, rc):
    b1, *_ = shop(db)
    db.query(Order).filter_by(user_id=b1.id).update({"created_at": now() - timedelta(days=120)})
    db.commit()
    leads.sync(db)
    assert db.scalars(select(Lead).where(Lead.user_id == b1.id)).one().segment == "lapsed"


def test_lead_endpoints_are_admin_only_and_manual_leads_work(db, rc):
    b1, *_ = shop(db)
    admin = f.user(db, "admin@x.test", "admin")
    assert rc.get("/admin/recommend/leads").status_code == 401
    assert rc.get("/admin/recommend/leads", headers=auth(b1)).status_code == 403
    assert rc.post("/admin/recommend/leads/sync", headers=auth(b1)).status_code == 403
    ha = auth(admin)
    r = rc.post("/admin/recommend/leads", json={"email": "Prospect@Example.com", "name": "P"}, headers=ha)
    assert r.status_code == 201 and r.json()["segment"] == "manual" and r.json()["user_id"] is None
    assert rc.post("/admin/recommend/leads", json={"email": "prospect@example.com"}, headers=ha).status_code == 409
    assert rc.patch(f"/admin/recommend/leads/{r.json()['id']}", json={"opted_out": True}, headers=ha).json()["opted_out"] is True
    assert rc.get("/admin/recommend/leads?opted_out=true", headers=ha).json()["total"] == 1
    assert rc.get("/admin/recommend/leads/summary", headers=ha).json()["by_segment"] == {"manual": 1}


def test_lead_recommendations_personalised_for_accounts_popular_for_manual(db, rc):
    b1, mug, tee, *_ = shop(db)
    admin = f.user(db, "admin@x.test", "admin")
    ha = auth(admin)
    leads.sync(db)
    lid = db.scalars(select(Lead).where(Lead.user_id == b1.id)).one().id
    r = rc.get(f"/admin/recommend/leads/{lid}/recommendations", headers=ha).json()
    assert r["strategy"] == "personalized" and ids(r)[0] == tee.id
    m = rc.post("/admin/recommend/leads", json={"email": "p@example.com"}, headers=ha).json()
    assert rc.get(f"/admin/recommend/leads/{m['id']}/recommendations", headers=ha).json()["strategy"] == "popular"
    assert rc.get("/admin/recommend/leads/9999/recommendations", headers=ha).status_code == 404
