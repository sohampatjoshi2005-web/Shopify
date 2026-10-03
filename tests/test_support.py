from datetime import timedelta
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.models import Order, Variant, now
from app.notify import OUTBOX
from app.support import sla
from app.support.models import Escalation, Priority, Ticket, TicketStatus
from . import factory as f
from .conftest import auth


@pytest.fixture()
def sc(db):
    """Test client with the extensions AND the support desk installed on the in-memory test DB."""
    from app.db import get_db
    from app.ext import install
    from app.support import install as install_support
    app = FastAPI()
    install(app)
    install_support(app)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def say(sc, who, text, **kw):
    r = sc.post("/chat/message", json={"message": text, **kw}, headers=auth(who) if who else {})
    assert r.status_code == 200, r.text
    return r.json()


def world(db):
    admin, buyer, other = f.user(db, "admin@x.test", "admin"), f.user(db, "buyer@x.test"), f.user(db, "other@x.test")
    p = f.product(db, "Blue Mug", 30000)
    return admin, buyer, other, p


def test_order_status_is_resolved_autonomously_and_tracked(db, sc):
    admin, buyer, other, p = world(db)
    o = f.paid_order(db, buyer, [(p, 1)])
    r = say(sc, buyer, f"Where is my order {o.number}?")
    assert r["intent"] == "order_status" and r["resolved"] and "'paid'" in r["reply"]
    t = db.scalars(select(Ticket)).one()
    assert t.status == TicketStatus.resolved and t.assigned_to == "autonomous_agent" and t.order_number == o.number
    assert r["ticket"]["worklogs"] == []                                     # customers never see internal worklogs
    assert sc.get("/tickets", headers=auth(admin)).json()[0]["worklogs"][0]["action"] == "resolved"


def test_cannot_look_up_someone_elses_order_and_it_escalates(db, sc):
    admin, buyer, other, p = world(db)
    o = f.paid_order(db, buyer, [(p, 1)])
    OUTBOX.clear()
    r = say(sc, other, f"order status {o.number}")
    assert not r["resolved"] and "couldn't find" in r["reply"]
    t = db.scalars(select(Ticket)).one()
    assert t.status == TicketStatus.escalated and t.user_id == other.id
    assert db.scalars(select(Escalation)).one().team == "engineering"
    assert any("[Escalation]" in m["subject"] for m in OUTBOX)               # engineering was emailed
    assert "paid" not in r["reply"]                                          # nothing about the real order leaked


def test_refund_on_paid_order_cancels_and_restocks(db, sc):
    admin, buyer, other, p = world(db)
    o = f.paid_order(db, buyer, [(p, 2)])
    vid, stock = p.variants[0].id, db.get(Variant, p.variants[0].id).stock
    r = say(sc, buyer, f"I want a refund for {o.number}")
    assert r["intent"] == "refund_request" and r["resolved"]
    db.expire_all()
    assert db.get(Order, o.id).status == "refunded" and db.get(Variant, vid).stock == stock + 2


def test_refund_on_shipped_order_goes_to_a_human(db, sc):
    admin, buyer, other, p = world(db)
    o = f.paid_order(db, buyer, [(p, 1)])
    db.query(Order).filter_by(id=o.id).update({"status": "shipped"}); db.commit()
    r = say(sc, buyer, f"refund {o.number} please")
    assert not r["resolved"] and r["ticket"]["status"] == "escalated"
    db.expire_all()
    assert db.get(Order, o.id).status == "shipped"                          # untouched


def test_invoice_and_shipping_intents(db, sc):
    admin, buyer, other, p = world(db)
    o = f.paid_order(db, buyer, [(p, 1)])
    r = say(sc, buyer, f"send me the invoice for {o.number}")
    assert r["resolved"] and "/invoices/" in r["reply"]
    r = say(sc, buyer, "tracking?", order_number=o.number)
    assert r["intent"] == "shipping_status" and r["resolved"] and "not shipped yet" in r["reply"]
    assert say(sc, buyer, "where is my order")["reply"].startswith("Could you share your order number")
    unpaid = f.paid_order(db, buyer, [(p, 1)], pay=False)
    assert "no invoice" in say(sc, buyer, f"invoice for {unpaid.number}")["reply"]


def test_logged_out_visitors_can_only_reset_password(db, sc):
    admin, buyer, other, p = world(db)
    real = f.user(db, "real@example.com")
    OUTBOX.clear()
    r = say(sc, None, "I forgot my password", customer_email="real@example.com")
    assert r["resolved"] and "real@example.com" in r["reply"] and r["ticket"] is None
    assert OUTBOX[-1]["to"] == "real@example.com" and "reset_token=" in OUTBOX[-1]["body"]
    n = len(OUTBOX)
    ghost = say(sc, None, "reset password", customer_email="ghost@example.com")
    assert ghost["resolved"] and len(OUTBOX) == n                           # same answer, no mail, no account enumeration
    o = f.paid_order(db, buyer, [(p, 1)])
    r = say(sc, None, f"status of {o.number}")
    assert not r["resolved"] and "log in" in r["reply"] and db.scalars(select(Ticket).where(Ticket.subject.like("%Order%"))).first() is None


def test_ticket_permissions_and_admin_workflow(db, sc):
    admin, buyer, other, p = world(db)
    hb, ho, ha = auth(buyer), auth(other), auth(admin)
    r = sc.post("/tickets", json={"subject": "Box was damaged", "priority": "critical", "description": "crushed"}, headers=hb)
    assert r.status_code == 201 and r.json()["priority"] == "medium"        # customers can't pick their own priority
    tid = r.json()["id"]
    assert [t["id"] for t in sc.get("/tickets", headers=hb).json()] == [tid] and sc.get("/tickets", headers=ho).json() == []
    assert sc.get(f"/tickets/{tid}", headers=ho).status_code == 404
    assert sc.patch(f"/tickets/{tid}", json={"status": "resolved"}, headers=hb).status_code == 403
    assert sc.get("/tickets").status_code == 401
    r = sc.patch(f"/tickets/{tid}", json={"priority": "critical", "assigned_to": "Asha", "note": "calling customer"}, headers=ha).json()
    assert r["priority"] == "critical" and r["assigned_to"] == "Asha" and {w["action"] for w in r["worklogs"]} >= {"created", "priority_changed", "assigned", "note"}
    due = db.get(Ticket, tid).sla_due_at
    assert sc.patch(f"/tickets/{tid}", json={"status": "resolved"}, headers=ha).json()["sla_breached"] is False
    assert sc.post(f"/tickets/{tid}/escalate?team=billing&reason=double+charge", headers=ha).json()["status"] == "escalated"
    assert [t["id"] for t in sc.get("/tickets?status=escalated&customer_email=buyer@x.test", headers=ha).json()] == [tid]
    assert sc.post(f"/tickets/{tid}/escalate", headers=hb).status_code == 403


def test_sla_hours_and_breach(monkeypatch):
    t0 = now()
    assert sla.compute_sla_due(Priority.critical, t0) == t0 + timedelta(hours=2)
    assert sla.compute_sla_due(Priority.high, t0) == t0 + timedelta(hours=8) and sla.compute_sla_due(Priority.low, t0) == t0 + timedelta(hours=72)
    monkeypatch.setenv("SLA_CRITICAL_HOURS", "1")
    assert sla.compute_sla_due(Priority.critical, t0) == t0 + timedelta(hours=1)
    assert sla.is_breached(t0 - timedelta(hours=1)) and not sla.is_breached(t0 + timedelta(hours=1))
    assert sla.is_breached(t0, t0 + timedelta(minutes=1))


def test_rag_and_cag_are_admin_only_and_work(db, sc):
    pytest.importorskip("sklearn")
    admin, buyer, other, p = world(db)
    o = f.paid_order(db, buyer, [(p, 1)])
    say(sc, buyer, f"order status {o.number}")
    for path in ("/rag/status", "/cag/summary"):
        assert sc.get(path, headers=auth(buyer)).status_code == 403 and sc.get(path).status_code == 401
    ha = auth(admin)
    assert sc.post("/rag/collect", headers=ha).json()["by_type"].keys() >= {"order", "ticket", "worklog"}
    hits = sc.get("/rag/search", params={"q": "blue mug", "top_k": 3}, headers=ha).json()
    assert hits and hits[0]["doc_type"] == "order" and hits[0]["metadata"]["order_number"] == o.number
    assert sc.post("/cag/refresh", headers=ha).json()["document_count"] >= 3
    c = sc.get("/cag/context", params={"max_chars": 40}, headers=ha).json()
    assert len(c["context"]) <= 40 and c["stale"] is False
