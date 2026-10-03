from datetime import timedelta
from sqlalchemy import select, update
from app.models import Category, Product, User, Variant, now
from app.notify import OUTBOX
from app.security import verify_password
from app.ext import accounts, search as srch
from app.ext.models import AuthToken, WishlistItem
from . import factory as f
from .conftest import auth


def token_from_mail(i=-1):
    body = OUTBOX[i]["body"]
    return body.split("Or paste this code in the app: ")[1].split()[0]


# ------------------------------------------------------------------ verification + reset
def test_email_verification_flow(db, client):
    OUTBOX.clear()
    u = f.user(db, "v@x.test")
    h = auth(u)
    assert client.get("/auth/email/status", headers=h).json()["verified"] is False
    assert client.post("/auth/email/send-verification", headers=h).json()["sent"] is True
    tok = token_from_mail()
    assert client.post("/auth/email/verify", json={"token": "x" * 20}).status_code == 400
    assert client.post("/auth/email/verify", json={"token": tok}).json() == {"verified": True}
    assert client.post("/auth/email/verify", json={"token": tok}).status_code == 400            # single use
    assert client.get("/auth/email/status", headers=h).json()["verified"] is True
    assert client.post("/auth/email/send-verification", headers=h).json()["verified"] is True    # no more mail once verified


def test_verification_rate_limit(db, client):
    OUTBOX.clear()
    h = auth(f.user(db, "rl@x.test"))
    codes = [client.post("/auth/email/send-verification", headers=h).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]


def test_password_reset_flow(db, client):
    OUTBOX.clear()
    u = f.user(db, "p@x.test", pw="oldpassword")
    r1 = client.post("/auth/password/forgot", json={"email": "P@x.test"})
    r2 = client.post("/auth/password/forgot", json={"email": "ghost@x.test"})
    assert r1.status_code == r2.status_code == 200 and r1.json() == r2.json()                    # no account enumeration
    assert len(OUTBOX) == 1 and OUTBOX[0]["to"] == "p@x.test"
    tok = token_from_mail()
    assert client.post("/auth/password/reset", json={"token": tok, "password": "short"}).status_code == 422
    assert client.post("/auth/password/reset", json={"token": tok, "password": "brandnewpass"}).status_code == 200
    db.refresh(u)
    assert verify_password("brandnewpass", u.password_hash) and not verify_password("oldpassword", u.password_hash)
    assert client.post("/auth/password/reset", json={"token": tok, "password": "another-pass1"}).status_code == 400   # reuse blocked
    assert accounts.is_verified(db, u.id)                                                         # mailbox ownership proven


def test_new_reset_link_invalidates_older_ones_after_use_and_expiry(db, client):
    OUTBOX.clear()
    u = f.user(db, "e@x.test")
    client.post("/auth/password/forgot", json={"email": u.email})
    t1 = token_from_mail()
    client.post("/auth/password/forgot", json={"email": u.email})
    t2 = token_from_mail()
    assert client.post("/auth/password/reset", json={"token": t2, "password": "password-two"}).status_code == 200
    assert client.post("/auth/password/reset", json={"token": t1, "password": "password-three"}).status_code == 400   # sibling link died
    client.post("/auth/password/forgot", json={"email": u.email})
    t3 = token_from_mail()
    db.execute(update(AuthToken).where(AuthToken.used_at.is_(None)).values(expires_at=now() - timedelta(minutes=1)))
    db.commit()
    assert client.post("/auth/password/reset", json={"token": t3, "password": "password-four"}).status_code == 400    # expired


def test_tokens_are_stored_hashed(db, client):
    OUTBOX.clear()
    u = f.user(db, "h@x.test")
    client.post("/auth/password/forgot", json={"email": u.email})
    tok = token_from_mail()
    assert tok not in [t.token_hash for t in db.scalars(select(AuthToken))]


def test_require_verified_dependency(db):
    from fastapi import FastAPI, Depends
    from fastapi.testclient import TestClient
    from app.db import get_db
    from app.ext.api import require_verified_email
    app = FastAPI()
    app.get("/secure")(lambda u=Depends(require_verified_email): {"ok": True})
    app.dependency_overrides[get_db] = lambda: db
    c, u = TestClient(app), f.user(db, "rv@x.test")
    assert c.get("/secure", headers=auth(u)).status_code == 403
    accounts.send_verification(db, u)
    accounts.verify(db, token_from_mail())
    assert c.get("/secure", headers=auth(u)).status_code == 200


# ------------------------------------------------------------------ wishlist
def test_wishlist(db, client):
    u = f.user(db, "w@x.test")
    p = f.product(db, "Wish Lamp", 9900)
    h = auth(u)
    assert client.put(f"/wishlist/{p.id}", headers=h).status_code == 201
    assert client.put(f"/wishlist/{p.id}", headers=h).status_code == 201                         # idempotent
    assert db.query(WishlistItem).count() == 1
    assert [i["title"] for i in client.get("/wishlist", headers=h).json()["items"]] == ["Wish Lamp"]
    assert client.put("/wishlist/9999", headers=h).status_code == 404
    p.is_active = False; db.commit()
    assert client.get("/wishlist", headers=h).json()["items"] == []                              # archived products vanish
    assert client.delete(f"/wishlist/{p.id}", headers=h).json()["wishlisted"] is False
    assert client.get("/wishlist").status_code == 401


# ------------------------------------------------------------------ search
def catalog(db):
    mugs = Category(name="Mugs", slug="mugs"); tees = Category(name="Tees", slug="tees")
    db.add_all([mugs, tees]); db.commit()
    _, s = f.seller(db, "Alpha", 10)
    a = f.product(db, "Blue Mug", 29900, s.id, cat=mugs)
    b = f.product(db, "Red Mug", 49900, cat=mugs)
    c = f.product(db, "Green Tee", 79900, cat=tees, stock=0)
    return s, a, b, c


def titles(r): return [i["title"] for i in r["items"]]


def test_sql_search_filters_sorts_and_paginates(db, client):
    s, a, b, c = catalog(db)
    assert titles(client.get("/search?q=mug&sort=price_desc").json()) == ["Red Mug", "Blue Mug"]
    assert titles(client.get("/search?category=tees").json()) == ["Green Tee"]
    assert titles(client.get(f"/search?seller_id={s.id}").json()) == ["Blue Mug"]
    assert titles(client.get("/search?min_price=30000&max_price=60000").json()) == ["Red Mug"]
    assert "Green Tee" not in titles(client.get("/search?in_stock=true").json())
    r = client.get("/search?page_size=2&page=2&sort=price_asc").json()
    assert titles(r) == ["Green Tee"] and r["total"] == 3 and r["engine"] == "sql"
    assert client.get("/search?sort=nope").status_code == 422
    assert client.get("/search/suggest?q=gr").json()["suggestions"] == ["Green Tee"]
    assert client.get("/search/suggest?q=g").json()["suggestions"] == []


class FakeMeili:
    def __init__(self): self.calls, self.docs, self.down = [], {}, False
    def __call__(self, method, path, **kw):
        import requests
        if self.down:
            raise requests.ConnectionError("down")
        self.calls.append((method, path, kw.get("json")))
        j = kw.get("json")
        if path.endswith("/documents") and method == "POST":
            for d in j: self.docs[d["id"]] = d
        if path.endswith("/delete-batch"):
            for i in j: self.docs.pop(i, None)
        if path.endswith("/search"):
            hits = [d for d in self.docs.values() if (not j["q"] or j["q"].lower() in d["title"].lower())]
            return {"hits": hits[: j["limit"]], "estimatedTotalHits": len(hits), "facetDistribution": {"category_slug": {"mugs": 2}}}
        return {}


def test_meilisearch_path_reindex_filters_and_fallback(db, client, monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDER", "meilisearch")
    fake = FakeMeili(); monkeypatch.setattr(srch, "_m", fake)
    s, a, b, c = catalog(db)
    admin = f.user(db, "adm@x.test", "admin")
    assert client.post("/admin/ext/search/reindex", headers=auth(f.user(db, "n@x.test"))).status_code == 403
    assert client.post("/admin/ext/search/reindex", headers=auth(admin)).json() == {"provider": "meilisearch", "indexed": 3}
    assert set(fake.docs) == {a.id, b.id, c.id} and fake.docs[a.id]["seller_id"] == s.id and fake.docs[c.id]["in_stock"] is False
    r = client.get("/search?q=mug&category=mugs&min_price=100&in_stock=true").json()
    assert r["engine"] == "meilisearch" and r["facets"] == {"category_slug": {"mugs": 2}}
    body = [x for x in fake.calls if x[1].endswith("/search")][-1][2]
    assert body["filter"] == ['category_slug = "mugs"', "price_from_cents >= 100", "in_stock = true"]
    assert client.get('/search?category=x" OR 1=1').json()["engine"] == "meilisearch"              # injection chars never reach the filter
    assert not any('OR' in str(x[2].get("filter")) for x in fake.calls if x[1].endswith("/search"))
    fake.down = True
    r = client.get("/search?q=mug").json()
    assert r["engine"] == "sql" and len(r["items"]) == 2                                          # engine down -> SQL answers


def test_index_hooks_push_changes_after_commit(db, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    import app.db as appdb
    monkeypatch.setenv("SEARCH_PROVIDER", "meilisearch")
    fake = FakeMeili(); monkeypatch.setattr(srch, "_m", fake)
    srch.install_hooks()
    monkeypatch.setattr(appdb, "SessionLocal", sessionmaker(db.get_bind(), expire_on_commit=False))   # hook opens its own session
    p = f.product(db, "Hooked Mug", 10000)
    assert p.id in fake.docs
    v = p.variants[0]; v.price_cents = 12345; db.commit()
    assert fake.docs[p.id]["price_from_cents"] == 12345
    p.is_active = False; db.commit()
    assert p.id not in fake.docs                                                                  # archived -> removed from the index
    n = len(fake.calls)
    db.add(Product(title="Rolled back", slug="rb")); db.flush(); db.rollback()
    assert len(fake.calls) == n                                                                   # nothing pushed for rolled-back work
    fake.down = True
    q = f.product(db, "Survives Outage", 500)                                                     # index down must not break the write
    assert db.get(Product, q.id) is not None
