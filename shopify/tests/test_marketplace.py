"""Sellers + Shopify. Every Shopify HTTP call is monkeypatched."""
import base64, hashlib, hmac, json
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.services import shopify as S

h = lambda t: {"Authorization": f"Bearer {t}"}
SP = {"id": 111, "title": "Linen Shirt", "vendor": "Lumen", "status": "active", "body_html": "<p>Soft &amp; light</p>",
      "images": [{"src": "https://cdn.example/shirt.png"}],
      "variants": [{"id": 1, "title": "S", "price": "1299.50", "sku": "LS-S", "inventory_quantity": 7, "inventory_item_id": 901},
                   {"id": 2, "title": "M", "price": "1299.50", "sku": "LS-M", "inventory_quantity": 3, "inventory_item_id": 902}]}


@pytest.fixture(scope="module")
def c():
    with TestClient(app) as cl:
        yield cl


@pytest.fixture(scope="module")
def adm(c):
    return h(c.post("/auth/login", json={"email": "admin@shop.dev", "password": "adminpass123"}).json()["access_token"])


@pytest.fixture(scope="module")
def seller(c):
    return h(c.post("/auth/register", json={"email": "seller@shop.dev", "password": "password123"}).json()["access_token"])


def sign(raw: bytes, secret="shpss_test") -> str:
    return base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()


def test_seller_lifecycle(c, adm, seller):
    assert c.post("/sellers", json={"name": "Lumen Goods"}, headers=seller).status_code == 201
    assert c.post("/sellers", json={"name": "Again"}, headers=seller).status_code == 409
    assert c.put("/sellers/me/shopify", json={"shop_domain": "lumen.myshopify.com", "access_token": "shpat_1234567890"}, headers=seller).status_code == 403  # pending
    sid = c.get("/admin/sellers", headers=adm).json()[0]["id"]
    assert c.get("/sellers/lumen-goods/products").status_code == 404                    # hidden until approved
    assert c.post(f"/admin/sellers/{sid}/status", params={"status": "approved"}, headers=adm).json()["status"] == "approved"
    bad = c.put("/sellers/me/shopify", json={"shop_domain": "evil.example.com", "access_token": "shpat_1234567890"}, headers=seller)
    assert bad.status_code == 400
    ok = c.put("/sellers/me/shopify", json={"shop_domain": "Lumen.myshopify.com", "access_token": "shpat_1234567890", "location_id": "5550"}, headers=seller)
    assert ok.json()["connected"] == "lumen.myshopify.com"


def test_import_is_idempotent_and_prices_exact(c, seller, monkeypatch):
    monkeypatch.setattr(S, "fetch_products", lambda conn: [SP])
    assert c.post("/sellers/me/shopify/import", headers=seller).json() == {"products": 1, "variants_synced": 2}
    assert c.post("/sellers/me/shopify/import", headers=seller).json()["variants_synced"] == 2
    items = c.get("/sellers/lumen-goods/products").json()["items"]
    assert len(items) == 1                                                              # re-import updates, never duplicates
    p = items[0]
    assert p["title"] == "Linen Shirt" and p["description"] == "Soft & light" and [v["price_cents"] for v in p["variants"]] == [129950, 129950]
    assert [v["stock"] for v in p["variants"]] == [7, 3]
    assert c.get("/products", params={"q": "linen"}).json()["total"] == 1               # visible in the main shop too


def test_webhooks(c, seller, monkeypatch):
    def hook(topic, payload, secret="shpss_test", wid="w1"):
        raw = json.dumps(payload).encode()
        return c.post("/webhooks/shopify", content=raw, headers={"X-Shopify-Hmac-Sha256": sign(raw, secret), "X-Shopify-Topic": topic,
                                                                   "X-Shopify-Shop-Domain": "lumen.myshopify.com", "X-Shopify-Webhook-Id": wid})
    assert hook("inventory_levels/update", {}, secret="wrong").status_code == 401
    inv = {"inventory_item_id": 901, "location_id": 5550, "available": 2}
    assert hook("inventory_levels/update", inv, wid="w-inv").json()["status"] == "ok"
    assert hook("inventory_levels/update", {**inv, "available": 99}, wid="w-inv").json()["status"] == "duplicate"   # retry ignored
    assert hook("inventory_levels/update", {**inv, "location_id": 1, "available": 50}, wid="w-other").json()["status"] == "ok"  # other location ignored
    stock = lambda: c.get("/sellers/lumen-goods/products").json()["items"][0]["variants"][0]["stock"]
    assert stock() == 2
    upd = {**SP, "title": "Linen Shirt v2", "variants": SP["variants"][:1]}               # variant M removed in Shopify
    assert hook("products/update", upd, wid="w-prod").json()["status"] == "ok"
    p = c.get("/sellers/lumen-goods/products").json()["items"][0]
    assert p["title"] == "Linen Shirt v2" and [v["title"] for v in p["variants"]] == ["S"]


def test_stock_pushed_back_after_payment(c, seller, monkeypatch):
    calls = []
    class R:
        status_code = 200
    monkeypatch.setattr(S.requests, "post", lambda url, **k: calls.append((url, k["json"])) or R())
    v = c.get("/sellers/lumen-goods/products").json()["items"][0]["variants"][0]
    addr = c.post("/addresses", json=dict(full_name="S", phone="9999999999", line1="x", city="y", state="z", postal_code="682001"), headers=seller).json()
    c.post("/cart/items", json={"variant_id": v["id"], "qty": 1}, headers=seller)
    o = c.post("/checkout", json={"address_id": addr["id"]}, headers=seller).json()
    c.post(o["payment"]["confirm_url"], headers=seller)
    assert calls and calls[0][0].endswith("/inventory_levels/set.json")
    assert calls[0][1] == {"location_id": 5550, "inventory_item_id": 901, "available": 6}  # products/update set stock to 7, one sold


def test_pagination_never_leaves_the_shop_host(monkeypatch):
    class Conn: shop_domain, access_token = "lumen.myshopify.com", "t"
    class Resp:
        def __init__(s, nxt): s.links, s.status_code = ({"next": {"url": nxt}} if nxt else {}), 200
        def raise_for_status(s): pass
        def json(s): return {"products": [{"id": 1}]}
    seen = []
    def fake_get(url, **k):
        seen.append(url)
        return Resp("https://attacker.example/steal" if len(seen) == 1 else None)
    monkeypatch.setattr(S.requests, "get", fake_get)
    assert len(S.fetch_products(Conn())) == 1 and len(seen) == 1
