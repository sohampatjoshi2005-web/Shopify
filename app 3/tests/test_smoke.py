import os, base64, hashlib, hmac, json
os.environ.update(ADMIN_EMAIL="a@x.com", ADMIN_PASSWORD="pw", WIDGET_API_KEY="k", SHOPIFY_WEBHOOK_SECRET="s",
                  DATABASE_URL="sqlite:///./test.db", JWT_SECRET="t")
from fastapi.testclient import TestClient
from backend.main import app
from backend import seed  # noqa: F401  (seeds #1001 alice, #1002 bob)

c = TestClient(app)
K = {"X-API-Key": "k"}

def token():
    return {"Authorization": "Bearer " + c.post("/auth/login", json={"email": "a@x.com", "password": "pw"}).json()["access_token"]}

def test_auth_required():
    assert c.get("/tickets").status_code == 401
    assert c.post("/chat/message", json={"email": "a", "message": "hi"}).status_code == 401

def test_order_status_owner_ok():
    r = c.post("/chat/message", headers=K, json={"email": "alice@example.com", "message": "status of order #1001"}).json()
    assert r["resolved"] and "fulfilled" in r["reply"]

def test_wrong_owner_escalates_without_leaking():
    r = c.post("/chat/message", headers=K, json={"email": "mallory@example.com", "message": "invoice for #1001"}).json()
    assert not r["resolved"] and "fulfilled" not in r["reply"]

def test_refund_eligible_then_ineligible():
    ok = c.post("/chat/message", headers=K, json={"email": "alice@example.com", "message": "refund #1001"}).json()
    assert ok["resolved"]
    late = c.post("/chat/message", headers=K, json={"email": "bob@example.com", "message": "refund #1002"}).json()
    assert not late["resolved"]

def test_intent_word_boundaries():
    from backend.modules.support import classify
    assert classify("I won a billion dollars") == "unknown"
    assert classify("what is the capital of France") == "unknown"

def test_webhook_hmac():
    body = json.dumps({"id": 9, "name": "#2001", "email": "z@x.com", "total_price": "10"}).encode()
    sig = base64.b64encode(hmac.new(b"s", body, hashlib.sha256).digest()).decode()
    h = {"X-Shopify-Hmac-Sha256": sig, "X-Shopify-Topic": "orders/create", "X-Shopify-Webhook-Id": "w1"}
    assert c.post("/webhooks/shopify", content=body, headers=h).json()["status"] == "ok"
    assert c.post("/webhooks/shopify", content=body, headers=h).json()["status"] == "duplicate"
    assert c.post("/webhooks/shopify", content=body, headers={**h, "X-Shopify-Hmac-Sha256": "bad"}).status_code == 401
    assert c.get("/marketing/leads", headers=token()).json()[0]["segment"] in ("Bronze", "Silver")
