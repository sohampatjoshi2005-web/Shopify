"""Regression tests for the fixes in this revision (one test per bug that was found in review)."""
import hashlib, hmac, json, time
from datetime import timedelta
import jwt
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.config import settings
from app.models import Coupon, Order, now
from app.notify import OUTBOX
from app.ext import accounts, ledger, returns as rt, shipping as ship
from app.ext.models import LineSplit, ReturnRequest, Shipment
from app.models import WebhookEvent
from app.security import create_token, hash_password
from app.services import orders as osvc
from app.support.chatbot import classify_intent
from . import factory as f
from .conftest import auth
from .test_ext_money import deliver, world


@pytest.fixture()
def ac(db):
    """Client with the extensions, support desk, admin + auth + payments routers on the in-memory DB."""
    from app.db import get_db
    from app.ext import install
    from app.support import install as install_support
    from app.routers import admin, auth as auth_router, payments
    app = FastAPI()
    for r in (auth_router.router, payments.router, admin.router):
        app.include_router(r)
    install(app)
    install_support(app)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


# ------------------------------------------------------------------ auth
def test_password_reset_logs_out_existing_sessions(db, ac):
    OUTBOX.clear()
    u = f.user(db, "sess@example.com", pw="oldpassword")
    old = jwt.encode({"sub": str(u.id), "role": "customer", "iat": int(time.time()) - 120, "exp": int(time.time()) + 3600}, settings.jwt_secret, algorithm="HS256")
    hdr_old = {"Authorization": f"Bearer {old}"}
    assert ac.get("/auth/me", headers=hdr_old).status_code == 200
    ac.post("/auth/password/forgot", json={"email": u.email})
    tok = OUTBOX[-1]["body"].split("Or paste this code in the app: ")[1].split()[0]
    assert ac.post("/auth/password/reset", json={"token": tok, "password": "brandnewpass"}).status_code == 200
    assert ac.get("/auth/me", headers=hdr_old).status_code == 401              # the stolen/forgotten session is dead
    new = ac.post("/auth/login", json={"email": u.email, "password": "brandnewpass"}).json()["access_token"]
    assert ac.get("/auth/me", headers={"Authorization": f"Bearer {new}"}).status_code == 200


def test_login_throttle_is_per_email_and_client(db, ac):
    from app.db import get_db
    f.user(db, "victim@example.com", pw="password1")
    attacker = TestClient(ac.app, client=("6.6.6.6", 1000))
    victim = TestClient(ac.app, client=("7.7.7.7", 1000))
    for _ in range(5):
        assert attacker.post("/auth/login", json={"email": "victim@example.com", "password": "wrongwrong"}).status_code == 401
    assert attacker.post("/auth/login", json={"email": "victim@example.com", "password": "password1"}).status_code == 429
    assert victim.post("/auth/login", json={"email": "victim@example.com", "password": "password1"}).status_code == 200   # owner is not locked out


# ------------------------------------------------------------------ money
def test_double_refund_is_blocked_while_provider_call_is_in_flight(db, monkeypatch):
    admin, buyer, (ua, sa), (ub, sb), house, pa, pb = world(db)
    o = f.paid_order(db, buyer, [(pa, 1)])
    deliver(db, o)
    r = rt.create_return(db, buyer, o.number, [{"order_item_id": o.items[0].id, "qty": 1}], "x")
    rt.approve(db, r.id); rt.mark_received(db, r.id)
    calls = []

    def provider(order, cents, key):
        calls.append(cents)
        assert db.get(ReturnRequest, r.id).status == "refunding"              # claimed BEFORE the provider was called
        with pytest.raises(rt.ReturnError) as e:                               # a concurrent click now loses
            rt.refund(db, r.id)
        assert e.value.status == 409
        return "re_1"
    monkeypatch.setattr(rt, "refund_partial", provider)
    assert rt.refund(db, r.id).status == "refunded" and calls == [30000]


def test_provider_failure_releases_the_return_for_a_retry(db, monkeypatch):
    admin, buyer, (ua, sa), (ub, sb), house, pa, pb = world(db)
    o = f.paid_order(db, buyer, [(pa, 1)])
    deliver(db, o)
    r = rt.create_return(db, buyer, o.number, [{"order_item_id": o.items[0].id, "qty": 1}], "x")
    rt.approve(db, r.id); rt.mark_received(db, r.id)
    monkeypatch.setattr(rt, "refund_partial", lambda *a: None)
    with pytest.raises(rt.ReturnError) as e:
        rt.refund(db, r.id)
    assert e.value.status == 502
    db.expire_all()
    assert db.get(ReturnRequest, r.id).status == "received" and db.get(ReturnRequest, r.id).refund_cents == 0
    monkeypatch.undo()
    assert rt.refund(db, r.id).status == "refunded"


def test_admin_refund_after_partial_return_only_refunds_the_rest(db, ac, monkeypatch):
    admin, buyer, (ua, sa), (ub, sb), house, pa, pb = world(db)
    o = f.paid_order(db, buyer, [(pa, 2)])                                      # 60000 + 4900 shipping
    deliver(db, o)
    it = o.items[0]
    r = rt.create_return(db, buyer, o.number, [{"order_item_id": it.id, "qty": 1}], "x")
    rt.approve(db, r.id); rt.mark_received(db, r.id); rt.refund(db, r.id)       # 30000 already back to the buyer
    sent = []
    monkeypatch.setattr(rt, "refund_partial", lambda order, cents, key: sent.append(cents) or "re_rest")
    monkeypatch.setattr("app.services.payments.refund", lambda order: pytest.fail("a full refund would double-refund the returned item"))
    res = ac.post(f"/admin/orders/{o.number}/refund", headers=auth(admin))
    assert res.status_code == 200 and sent == [o.total_cents - 30000] and res.json()["refunded_cents"] == o.total_cents - 30000
    assert ac.post(f"/admin/orders/{o.number}/refund", headers=auth(admin)).status_code == 409   # already refunded


def test_coupon_funding_is_an_explicit_policy(db, monkeypatch):
    admin, buyer, (ua, sa), (ub, sb), house, pa, pb = world(db)
    db.add(Coupon(code="C10", percent_off=10)); db.commit()
    f.paid_order(db, buyer, [(house, 1), (pa, 1)], coupon="C10")               # 80000 - 8000 discount; Alpha's share is 3000
    ledger.sync(db)
    sp = db.scalars(select(LineSplit).where(LineSplit.seller_id == sa.id)).one()
    assert (sp.gross_cents, sp.net_cents) == (30000, 27000)                      # default: platform absorbs the coupon
    monkeypatch.setenv("EXT_COUPON_FUNDED_BY", "seller")
    buyer2 = f.user(db, "b2@x.test")
    f.paid_order(db, buyer2, [(house, 1), (pa, 1)], coupon="C10")
    ledger.sync(db)
    sp2 = db.scalars(select(LineSplit).where(LineSplit.seller_id == sa.id).order_by(LineSplit.id.desc())).first()
    assert (sp2.gross_cents, sp2.commission_cents, sp2.net_cents) == (27000, 2700, 24300)


def test_idempotency_key_of_a_cancelled_checkout_is_not_handed_back(db):
    admin, buyer, (ua, sa), (ub, sb), house, pa, pb = world(db)
    o = f.paid_order(db, buyer, [(pa, 1)], pay=False, idem="k-1")
    assert osvc.create_order(db, buyer, o.shipping_address and db.scalars(select(f.Address)).first().id, None, "k-1").number == o.number   # live order: same one
    osvc.customer_cancel(db, o)
    with pytest.raises(HTTPException) as e:
        osvc.create_order(db, buyer, db.scalars(select(f.Address)).first().id, None, "k-1")
    assert e.value.status_code == 409


def test_webhook_dedup_row_is_not_written_if_processing_crashes(db, ac, monkeypatch):
    admin, buyer, (ua, sa), (ub, sb), house, pa, pb = world(db)
    o = f.paid_order(db, buyer, [(pa, 1)], pay=False)
    o.payment_ref = "pi_x"; db.commit()

    def send(eid):
        raw = json.dumps({"id": eid, "type": "payment_intent.succeeded", "data": {"object": {"id": "pi_x", "amount_received": o.total_cents}}}).encode()
        t = str(int(time.time()))
        sig = hmac.new(b"whsec_test", f"{t}.".encode() + raw, hashlib.sha256).hexdigest()
        return TestClient(ac.app, raise_server_exceptions=False).post("/payments/webhook/stripe", content=raw, headers={"Stripe-Signature": f"t={t},v1={sig}"})

    real = osvc.mark_paid
    monkeypatch.setattr(osvc, "mark_paid", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert send("evt_crash").status_code == 500
    assert db.get(WebhookEvent, "evt_crash") is None                              # so the provider's retry is NOT swallowed as "duplicate"
    monkeypatch.setattr(osvc, "mark_paid", real)
    assert send("evt_crash").json()["status"] == "ok"
    db.expire_all()
    assert db.get(Order, o.id).status == "paid" and send("evt_crash").json()["status"] == "duplicate"


# ------------------------------------------------------------------ shipping
def test_database_refuses_a_second_live_parcel(db):
    admin, buyer, (ua, sa), (ub, sb), house, pa, pb = world(db)
    o = f.paid_order(db, buyer, [(pa, 1), (house, 1)])
    first = ship.create_shipment(db, o, sa.id)
    db.add(Shipment(order_number=o.number, seller_id=sa.id, awb="DUP1", status="created"))      # what a racing request would do
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.add(Shipment(order_number=o.number, seller_id=None, awb="HOUSE1", status="created")); db.commit()   # another seller's parcel is fine
    db.add(Shipment(order_number=o.number, seller_id=None, awb="HOUSE2", status="created"))
    with pytest.raises(IntegrityError):                                          # house parcels (NULL seller) are covered too
        db.commit()
    db.rollback()
    ship.apply_tracking(db, db.get(Shipment, first.id), "rto")                   # a returned-to-origin parcel frees the slot
    db.add(Shipment(order_number=o.number, seller_id=sa.id, awb="RESHIP", status="created")); db.commit()


# ------------------------------------------------------------------ support desk
def test_intents_use_whole_words():
    assert classify_intent("What is 5 billion divided by 2?") == "unknown"
    assert classify_intent("my capital is Delhi") == "unknown"
    assert classify_intent("I forgot my password") == "password_reset"
    assert classify_intent("can I return my order?") == "refund_request"
    assert classify_intent("status of ORD-1A2B3C4D") == "order_status"
    assert classify_intent("send the invoice") == "invoice_lookup"
    assert classify_intent("tracking?") == "shipping_status"
    assert classify_intent("how do I use the API?") == "api_usage_clarification"


def test_stale_token_is_treated_as_logged_out_in_chat(db, ac):
    OUTBOX.clear()
    f.user(db, "stale@example.com")
    r = ac.post("/chat/message", json={"message": "forgot my password", "customer_email": "stale@example.com"}, headers={"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 200 and r.json()["resolved"] and OUTBOX and OUTBOX[-1]["to"] == "stale@example.com"


def test_admin_can_raise_a_ticket_for_an_unknown_email_and_reopen_one(db, ac):
    admin = f.user(db, "adm@x.test", "admin")
    ha = auth(admin)
    r = ac.post("/tickets", json={"subject": "Phone call follow-up", "customer_email": "New.Person@Example.com", "priority": "high"}, headers=ha)
    assert r.status_code == 201 and r.json()["user_id"] is None and r.json()["customer_email"] == "new.person@example.com" and r.json()["priority"] == "high"
    tid = r.json()["id"]
    assert ac.patch(f"/tickets/{tid}", json={"status": "resolved"}, headers=ha).json()["resolved_at"] is not None
    back = ac.patch(f"/tickets/{tid}", json={"status": "in_progress"}, headers=ha).json()
    assert back["resolved_at"] is None and back["status"] == "in_progress"
