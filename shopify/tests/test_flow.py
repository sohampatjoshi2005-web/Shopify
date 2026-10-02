"""Buyer + admin flow on the mock payment provider."""
import hashlib, hmac, json, time
import pytest
from fastapi.testclient import TestClient
from app.main import app

h = lambda t: {"Authorization": f"Bearer {t}"}


@pytest.fixture(scope="module")
def c():
    with TestClient(app) as cl:
        yield cl


@pytest.fixture(scope="module")
def adm(c):
    return h(c.post("/auth/login", json={"email": "admin@shop.dev", "password": "adminpass123"}).json()["access_token"])


@pytest.fixture(scope="module")
def buyer(c):
    return h(c.post("/auth/register", json={"email": "buyer@shop.dev", "password": "password123", "full_name": "Bea Buyer"}).json()["access_token"])


@pytest.fixture(scope="module")
def catalog(c, adm):
    from app.seed import seed
    seed()
    items = c.get("/products", params={"page_size": 50}).json()["items"]
    assert len(items) == 4
    return items


ADDR = dict(full_name="Bea Buyer", phone="9999999999", line1="1 MG Road", city="Kochi", state="KL", postal_code="682001")


def test_auth_rules(c, buyer):
    assert c.post("/auth/register", json={"email": "buyer@shop.dev", "password": "password123"}).status_code == 409
    assert c.post("/auth/register", json={"email": "nope", "password": "password123"}).status_code == 422
    assert c.post("/auth/login", json={"email": "buyer@shop.dev", "password": "wrong-pass"}).status_code == 401
    assert c.get("/cart").status_code == 401
    assert c.get("/admin/stats", headers=buyer).status_code == 403


def test_catalog(c, catalog):
    assert c.get("/products", params={"q": "mug"}).json()["total"] == 1
    assert c.get("/products", params={"category": "apparel"}).json()["total"] == 2
    prices = [p["price_from_cents"] for p in c.get("/products", params={"sort": "price_asc"}).json()["items"]]
    assert prices == sorted(prices)
    assert [x["slug"] for x in c.get("/categories").json()] == ["apparel", "drinkware", "stickers"]


def test_checkout_pay_ship_deliver(c, buyer, adm, catalog):
    mug = next(p for p in catalog if p["title"] == "Ceramic Mug")
    white = next(v for v in mug["variants"] if v["title"] == "White")
    assert c.post("/cart/items", json={"variant_id": white["id"], "qty": 2}, headers=buyer).status_code == 201
    assert c.post("/cart/items", json={"variant_id": white["id"], "qty": 90}, headers=buyer).status_code == 409   # more than the 80 in stock
    cart = c.get("/cart", params={"coupon": "WELCOME10"}, headers=buyer).json()
    assert cart["pricing"]["subtotal_cents"] == 69800 and cart["pricing"]["discount_cents"] == 6980 and cart["pricing"]["shipping_cents"] == 4900
    assert c.get("/cart", params={"coupon": "NOPE"}, headers=buyer).status_code == 400
    addr = c.post("/addresses", json=ADDR, headers=buyer).json()
    k = {**buyer, "Idempotency-Key": "abc123"}
    o = c.post("/checkout", json={"address_id": addr["id"], "coupon_code": "WELCOME10"}, headers=k).json()
    assert o["status"] == "pending_payment" and o["total_cents"] == 69800 - 6980 + 4900 and o["payment"]["provider"] == "mock"
    again = c.post("/checkout", json={"address_id": addr["id"]}, headers=k).json()
    assert again["number"] == o["number"]                                         # same key, same order
    assert c.get("/cart", headers=buyer).json()["items"] == []                      # cart emptied
    stock = lambda: next(v for v in c.get(f"/products/{mug['id']}").json()["variants"] if v["id"] == white["id"])["stock"]
    assert stock() == 78                                                          # 80 - 2 reserved
    assert c.post(o["payment"]["confirm_url"], headers=buyer).json()["status"] == "paid"
    assert c.post(o["payment"]["confirm_url"], headers=buyer).status_code == 409   # can't pay twice
    assert c.post(f"/orders/{o['number']}/cancel", headers=buyer).json()["status"] == "refunded"
    assert stock() == 80                                                          # refund + cancel releases stock

    # ship / deliver path on a fresh order
    c.post("/cart/items", json={"variant_id": white["id"], "qty": 1}, headers=buyer)
    o2 = c.post("/checkout", json={"address_id": addr["id"]}, headers=buyer).json()
    assert c.post(f"/admin/orders/{o2['number']}/ship", json={"tracking_number": "T1", "carrier": "DHL"}, headers=adm).status_code == 409
    c.post(o2["payment"]["confirm_url"], headers=buyer)
    assert c.post(f"/admin/orders/{o2['number']}/ship", json={"tracking_number": "T1", "carrier": "DHL"}, headers=adm).json()["status"] == "shipped"
    assert c.post(f"/admin/orders/{o2['number']}/deliver", headers=adm).json()["status"] == "delivered"
    mine = c.get(f"/orders/{o2['number']}", headers=buyer).json()
    assert mine["tracking_number"] == "T1" and mine["items"][0]["line_total_cents"] == 34900
    s = c.get("/admin/stats", headers=adm).json()
    assert s["revenue_cents"] == o2["total_cents"] and s["orders_by_status"]["delivered"] == 1
    assert any(r["sku"] == "MUG-BLK" for r in s["low_stock"])                      # stock 4 is flagged
    assert c.post(f"/admin/orders/{o2['number']}/refund", params={"restock": True}, headers=adm).json()["restocked"] is True
    assert stock() == 80


def test_no_oversell_and_expiry(c, buyer, catalog):
    from app.db import SessionLocal
    from app.models import Order, now
    from app.services.orders import expire_unpaid
    from datetime import timedelta
    blk = next(v for p in catalog if p["title"] == "Ceramic Mug" for v in p["variants"] if v["title"] == "Black")
    addr = c.get("/addresses", headers=buyer).json()[0]
    c.post("/cart/items", json={"variant_id": blk["id"], "qty": 4}, headers=buyer)
    o = c.post("/checkout", json={"address_id": addr["id"]}, headers=buyer).json()
    assert c.post("/cart/items", json={"variant_id": blk["id"], "qty": 1}, headers=buyer).status_code == 409   # all 4 reserved
    with SessionLocal() as db:                                                    # backdate so it counts as abandoned
        db.get(Order, db.query(Order.id).filter(Order.number == o["number"]).scalar()).created_at = now() - timedelta(hours=2)
        db.commit()
        assert expire_unpaid(db) == 1
    assert c.get(f"/orders/{o['number']}", headers=buyer).json()["status"] == "cancelled"
    assert c.post("/cart/items", json={"variant_id": blk["id"], "qty": 4}, headers=buyer).status_code == 201   # stock released
    c.patch(f"/cart/items/{blk['id']}", json={"qty": 0}, headers=buyer)


def test_stripe_webhook_rules(c, buyer, catalog):
    """Signature, amount check and de-duplication, independent of the provider used to create the order."""
    from app.db import SessionLocal
    from app.models import Order
    stk = next(v for p in catalog if p["title"] == "Sticker Pack" for v in p["variants"])
    addr = c.get("/addresses", headers=buyer).json()[0]
    c.post("/cart/items", json={"variant_id": stk["id"], "qty": 1}, headers=buyer)
    o = c.post("/checkout", json={"address_id": addr["id"]}, headers=buyer).json()
    with SessionLocal() as db:
        row = db.query(Order).filter(Order.number == o["number"]).one()
        row.payment_ref = "pi_test_1"
        db.commit()

    def post(event_id, amount, sig_secret="whsec_test", t=None):
        raw = json.dumps({"id": event_id, "type": "payment_intent.succeeded", "data": {"object": {"id": "pi_test_1", "amount_received": amount}}}).encode()
        t = t or int(time.time())
        sig = hmac.new(sig_secret.encode(), f"{t}.".encode() + raw, hashlib.sha256).hexdigest()
        return c.post("/payments/webhook/stripe", content=raw, headers={"Stripe-Signature": f"t={t},v1={sig}"})

    assert post("evt_1", o["total_cents"], sig_secret="wrong").status_code == 400
    assert post("evt_1", o["total_cents"], t=int(time.time()) - 3600).status_code == 400          # stale timestamp
    post("evt_2", o["total_cents"] - 1)
    assert c.get(f"/orders/{o['number']}", headers=buyer).json()["status"] == "pending_payment"   # amount mismatch is refused
    assert post("evt_3", o["total_cents"]).json()["status"] == "ok"
    assert c.get(f"/orders/{o['number']}", headers=buyer).json()["status"] == "paid"
    assert post("evt_3", o["total_cents"]).json()["status"] == "ignored"                          # duplicate delivery
