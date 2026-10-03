import os, tempfile, time, hmac, hashlib, json
_db = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ.update(DATABASE_URL=f"sqlite:///{_db}", APP_ENV="test", RUN_EXPIRY_LOOP="false", PAYMENT_PROVIDER="mock",
                  ADMIN_EMAIL="admin@shop.dev", ADMIN_PASSWORD="adminpass123", STRIPE_WEBHOOK_SECRET="whsec_test",
                  JWT_SECRET="test-secret")
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.main import app
from app.db import SessionLocal
from app.models import Order, Variant, Coupon

ADDR = dict(full_name="A B", phone="9999999999", line1="1 Main St", city="Kochi", state="KL", postal_code="682001")


@pytest.fixture(scope="module")
def c():
    with TestClient(app) as client:
        yield client


def hdr(tok): return {"Authorization": f"Bearer {tok}"}


def signup(c, email):
    r = c.post("/auth/register", json={"email": email, "password": "password123", "full_name": "Test User"})
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def admin(c):
    return c.post("/auth/login", json={"email": "admin@shop.dev", "password": "adminpass123"}).json()["access_token"]


@pytest.fixture(scope="module")
def product(c, admin):
    c.post("/admin/categories", json={"name": "Apparel"}, headers=hdr(admin))
    r = c.post("/admin/products", headers=hdr(admin), json={
        "title": "Test Hoodie", "brand": "B", "category_slug": "apparel", "image_urls": ["https://x/y.jpg"],
        "variants": [{"sku": "HOOD-M", "title": "M", "price_cents": 100000, "stock": 5},
                     {"sku": "HOOD-L", "title": "L", "price_cents": 100000, "stock": 1}]})
    assert r.status_code == 201, r.text
    c.post("/admin/coupons", headers=hdr(admin), json={"code": "save10", "percent_off": 10})
    return r.json()


def test_catalog_and_admin_guard(c, admin, product):
    tok = signup(c, "guard@shop.dev")
    assert c.post("/admin/products", headers=hdr(tok), json={}).status_code == 403
    assert c.get("/admin/orders").status_code == 401
    r = c.get("/products", params={"q": "hoodie", "category": "apparel", "sort": "price_asc"}).json()
    assert r["total"] == 1 and r["items"][0]["price_from_cents"] == 100000 and r["items"][0]["in_stock"]
    assert c.get(f"/products/{product['slug']}").status_code == 200


def test_full_purchase_flow(c, admin, product):
    tok = signup(c, "buyer@shop.dev"); h = hdr(tok)
    vid = product["variants"][0]["id"]
    assert c.post("/cart/items", json={"variant_id": vid, "qty": 99}, headers=h).status_code == 409  # > stock
    cart = c.post("/cart/items", json={"variant_id": vid, "qty": 2}, headers=h).json()
    assert cart["pricing"]["subtotal_cents"] == 200000
    aid = c.post("/addresses", json=ADDR, headers=h).json()["id"]
    r = c.post("/checkout", json={"address_id": aid, "coupon_code": "SAVE10"}, headers={**h, "Idempotency-Key": "k1"})
    assert r.status_code == 201, r.text
    o = r.json()
    assert o["status"] == "pending_payment" and o["discount_cents"] == 20000 and o["shipping_cents"] == 0
    assert o["total_cents"] == 180000 and "confirm_url" in o["payment"]
    # retry with same key must NOT create a second order or reserve stock twice
    assert c.post("/checkout", json={"address_id": aid, "coupon_code": "SAVE10"}, headers={**h, "Idempotency-Key": "k1"}).json()["number"] == o["number"]
    with SessionLocal() as db:
        assert db.get(Variant, vid).stock == 3
    assert c.get("/cart", headers=h).json()["items"] == []
    paid = c.post(o["payment"]["confirm_url"], headers=h).json()
    assert paid["status"] == "paid"
    # another customer can't see it
    other = hdr(signup(c, "snoop@shop.dev"))
    assert c.get(f"/orders/{o['number']}", headers=other).status_code == 404
    # review requires purchase
    assert c.post(f"/products/{product['slug']}/reviews", json={"rating": 5, "comment": "great"}, headers=h).status_code == 201
    assert c.post(f"/products/{product['slug']}/reviews", json={"rating": 5}, headers=other).status_code == 403
    assert c.get(f"/products/{product['slug']}").json()["rating_avg"] == 5
    # admin ships -> delivers; can't skip steps
    a = hdr(admin)
    assert c.post(f"/admin/orders/{o['number']}/deliver", headers=a).status_code == 409
    assert c.post(f"/admin/orders/{o['number']}/ship", json={"tracking_number": "T1", "carrier": "DHL"}, headers=a).json()["status"] == "shipped"
    assert c.post(f"/orders/{o['number']}/cancel", headers=h).status_code == 409  # too late to self-cancel
    assert c.post(f"/admin/orders/{o['number']}/deliver", headers=a).json()["status"] == "delivered"
    assert c.get("/admin/stats", headers=a).json()["revenue_cents"] >= 180000


def test_no_overselling(c, product):
    vid = product["variants"][1]["id"]  # stock = 1
    a, b = hdr(signup(c, "r1@shop.dev")), hdr(signup(c, "r2@shop.dev"))
    for h in (a, b):
        c.post("/cart/items", json={"variant_id": vid}, headers=h)
    addr = {h["Authorization"]: c.post("/addresses", json=ADDR, headers=h).json()["id"] for h in (a, b)}
    assert c.post("/checkout", json={"address_id": addr[a["Authorization"]]}, headers=a).status_code == 201
    r = c.post("/checkout", json={"address_id": addr[b["Authorization"]]}, headers=b)
    assert r.status_code == 409  # second buyer loses, stock never negative


def test_cancel_restocks_and_expiry(c, product):
    vid = product["variants"][0]["id"]
    h = hdr(signup(c, "cx@shop.dev"))
    with SessionLocal() as db:
        before = db.get(Variant, vid).stock
    c.post("/cart/items", json={"variant_id": vid}, headers=h)
    aid = c.post("/addresses", json=ADDR, headers=h).json()["id"]
    num = c.post("/checkout", json={"address_id": aid}, headers=h).json()["number"]
    assert c.post(f"/orders/{num}/cancel", headers=h).json()["status"] == "cancelled"
    with SessionLocal() as db:
        assert db.get(Variant, vid).stock == before
    # expiry job
    c.post("/cart/items", json={"variant_id": vid}, headers=h)
    num2 = c.post("/checkout", json={"address_id": aid}, headers=h).json()["number"]
    from app.services.orders import expire_unpaid
    from app.config import settings
    settings.unpaid_expiry_minutes = -1
    with SessionLocal() as db:
        assert expire_unpaid(db) >= 1
    settings.unpaid_expiry_minutes = 30
    assert c.get(f"/orders/{num2}", headers=h).json()["status"] == "cancelled"


def test_stripe_webhook_signature_and_amount(c, product):
    vid = product["variants"][0]["id"]
    h = hdr(signup(c, "wh@shop.dev"))
    c.post("/cart/items", json={"variant_id": vid}, headers=h)
    aid = c.post("/addresses", json=ADDR, headers=h).json()["id"]
    o = c.post("/checkout", json={"address_id": aid}, headers=h).json()
    with SessionLocal() as db:
        row = db.scalars(select(Order).where(Order.number == o["number"])).one()
        row.payment_ref = "pi_123"; db.commit()

    def send(event, sig_ok=True):
        raw = json.dumps(event).encode(); t = str(int(time.time()))
        sig = hmac.new(b"whsec_test", f"{t}.".encode() + raw, hashlib.sha256).hexdigest()
        if not sig_ok: sig = "0" * 64
        return c.post("/payments/webhook/stripe", content=raw, headers={"Stripe-Signature": f"t={t},v1={sig}"})

    ev = lambda amt, eid: {"id": eid, "type": "payment_intent.succeeded", "data": {"object": {"id": "pi_123", "amount_received": amt}}}
    assert send(ev(o["total_cents"], "evt_1"), sig_ok=False).status_code == 400   # forged
    send(ev(1, "evt_2"))                                                          # wrong amount
    assert c.get(f"/orders/{o['number']}", headers=h).json()["status"] == "pending_payment"
    assert send(ev(o["total_cents"], "evt_3")).json()["status"] == "ok"
    assert c.get(f"/orders/{o['number']}", headers=h).json()["status"] == "paid"
    assert send(ev(o["total_cents"], "evt_3")).json()["status"] == "duplicate"   # retry is harmless


def test_coupon_limits_and_login_throttle(c, admin, product):
    a = hdr(admin)
    assert c.post("/admin/coupons", json={"code": "bad", "percent_off": 5, "amount_off_cents": 5}, headers=a).status_code == 400
    h = hdr(signup(c, "cp@shop.dev"))
    c.post("/cart/items", json={"variant_id": product["variants"][0]["id"]}, headers=h)
    assert c.get("/cart", params={"coupon": "NOPE"}, headers=h).status_code == 400
    for _ in range(5):
        assert c.post("/auth/login", json={"email": "cp@shop.dev", "password": "wrongwrong"}).status_code == 401
    assert c.post("/auth/login", json={"email": "cp@shop.dev", "password": "password123"}).status_code == 429
