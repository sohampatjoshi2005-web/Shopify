from datetime import timedelta
from sqlalchemy import select
from app.models import Order, Variant, now
from app.ext import ledger, returns as rt, gst
from app.ext.models import Invoice, LedgerEntry, LineSplit, ReturnLine, SellerProfile
from . import factory as f
from .conftest import auth


def world(db):
    admin = f.user(db, "admin@x.test", "admin")
    buyer = f.user(db, "buyer@x.test")
    ua, sa = f.seller(db, "Alpha", 10)
    ub, sb = f.seller(db, "Beta", 20)
    house = f.product(db, "House Tee", 50000)
    pa = f.product(db, "Alpha Mug", 30000, sa.id)
    pb = f.product(db, "Beta Cap", 20000, sb.id)
    db.add_all([SellerProfile(seller_id=sa.id, gstin="29ABCDE1234F1Z5", state_code="29", upi_id="alpha@upi", pickup_pincode="560001"),
                SellerProfile(seller_id=sb.id, gstin="27AAAAA0000A1Z5", state_code="27", bank_account_no="123456789012", bank_ifsc="HDFC0000001")])
    db.commit()
    return admin, buyer, (ua, sa), (ub, sb), house, pa, pb


def test_split_commission_and_balances(db):
    admin, buyer, (ua, sa), (ub, sb), house, pa, pb = world(db)
    o = f.paid_order(db, buyer, [(house, 1), (pa, 2), (pb, 1)])
    assert ledger.sync(db) == {"lines_accrued": 3, "lines_reversed": 0}
    assert ledger.sync(db)["lines_accrued"] == 0                               # idempotent
    sp = {s.seller_id: s for s in db.scalars(select(LineSplit))}
    assert sp[sa.id].gross_cents == 60000 and sp[sa.id].commission_cents == 6000 and sp[sa.id].net_cents == 54000
    assert sp[sb.id].commission_cents == 4000 and sp[sb.id].net_cents == 16000
    assert sp[None].net_cents == 50000 and sp[None].commission_cents == 0       # house sale: no ledger entry
    assert not db.scalars(select(LedgerEntry).where(LedgerEntry.seller_id.is_(None))).all()   # house sale writes no seller ledger row
    assert ledger.balances(db, sa.id)["available_cents"] == 54000


def test_hold_period_and_payout_cycle(db, monkeypatch):
    monkeypatch.setenv("EXT_PAYOUT_HOLD_DAYS", "7")
    admin, buyer, (ua, sa), (ub, sb), house, pa, pb = world(db)
    f.paid_order(db, buyer, [(pa, 1)])
    ledger.sync(db)
    b = ledger.balances(db, sa.id)
    assert b["pending_cents"] == 27000 and b["available_cents"] == 0
    assert ledger.run_payouts(db)["created"] == []                              # still held
    monkeypatch.setenv("EXT_PAYOUT_HOLD_DAYS", "0")
    for e in db.scalars(select(LedgerEntry)):
        e.available_at = now() - timedelta(seconds=1)
    db.commit()
    res = ledger.run_payouts(db)
    assert [p["amount_cents"] for p in res["created"]] == [27000]
    assert ledger.run_payouts(db)["created"] == []                              # nothing left: no double payout
    p = db.get(ledger.Payout, res["created"][0]["payout_id"])
    ledger.mark_paid(db, p, "UTR123456")
    assert ledger.balances(db, sa.id)["paid_out_cents"] == 27000


def test_payout_skips_seller_without_bank_details(db):
    admin, buyer, (ua, sa), (ub, sb), house, pa, pb = world(db)
    db.query(SellerProfile).filter_by(seller_id=sa.id).update({"upi_id": ""})
    db.commit()
    f.paid_order(db, buyer, [(pa, 1)])
    r = ledger.run_payouts(db)
    assert r["created"] == [] and r["skipped"][0]["seller_id"] == sa.id


def test_failed_payout_releases_money(db):
    admin, buyer, (ua, sa), (ub, sb), house, pa, pb = world(db)
    f.paid_order(db, buyer, [(pa, 1)])
    r = ledger.run_payouts(db)
    p = db.get(ledger.Payout, r["created"][0]["payout_id"])
    assert ledger.balances(db, sa.id)["available_cents"] == 0
    ledger.mark_failed(db, p, "bank rejected")
    assert ledger.balances(db, sa.id)["available_cents"] == 27000


def test_full_refund_reverses_seller_credit(db):
    admin, buyer, (ua, sa), (ub, sb), house, pa, pb = world(db)
    o = f.paid_order(db, buyer, [(pa, 1)])
    ledger.sync(db)
    db.query(Order).filter_by(id=o.id).update({"status": "refunded"})
    db.commit()
    assert ledger.sync(db)["lines_reversed"] == 1
    assert ledger.balances(db, sa.id)["available_cents"] == 0
    assert ledger.sync(db)["lines_reversed"] == 0


def deliver(db, o):
    db.query(Order).filter_by(id=o.id).update({"status": "delivered"})
    db.commit()


def test_partial_return_full_flow(db):
    admin, buyer, (ua, sa), (ub, sb), house, pa, pb = world(db)
    o = f.paid_order(db, buyer, [(pa, 2), (pb, 1)])
    ledger.sync(db)
    deliver(db, o)
    item_a = next(i for i in o.items if i.product_id == pa.id)
    stock_before = db.get(Variant, item_a.variant_id).stock
    r = rt.create_return(db, buyer, o.number, [{"order_item_id": item_a.id, "qty": 1}], "Handle broke")
    assert r.status == "requested"
    rt.approve(db, r.id, "ok")
    rt.mark_received(db, r.id)
    r = rt.refund(db, r.id)
    assert r.status == "refunded" and r.refund_cents == 30000                   # one mug, no discount/tax
    assert db.get(Variant, item_a.variant_id).stock == stock_before + 1         # restocked
    assert ledger.balances(db, sa.id)["available_cents"] == 27000               # 54000 - 27000 reversal
    assert db.get(Order, o.id).status == "delivered"                            # not everything returned yet
    cn = db.scalars(select(Invoice).where(Invoice.kind == "credit_note")).all()
    assert len(cn) == 1 and cn[0].data["lines"][0]["qty"] == 1 and cn[0].number.startswith("CS")


def test_cannot_over_return_or_return_undelivered(db):
    admin, buyer, (ua, sa), (ub, sb), house, pa, pb = world(db)
    o = f.paid_order(db, buyer, [(pa, 1)])
    it = o.items[0]
    try:
        rt.create_return(db, buyer, o.number, [{"order_item_id": it.id, "qty": 1}], "x")
        assert False
    except rt.ReturnError as e:
        assert e.status == 409                                                  # not delivered yet
    deliver(db, o)
    rt.create_return(db, buyer, o.number, [{"order_item_id": it.id, "qty": 1}], "x")
    try:
        rt.create_return(db, buyer, o.number, [{"order_item_id": it.id, "qty": 1}], "again")
        assert False
    except rt.ReturnError as e:
        assert e.status == 409                                                  # already requested


def test_return_window_and_full_return_marks_order_refunded(db, monkeypatch):
    admin, buyer, (ua, sa), (ub, sb), house, pa, pb = world(db)
    o = f.paid_order(db, buyer, [(pa, 1)])
    deliver(db, o)
    db.query(Order).filter_by(id=o.id).update({"paid_at": now() - timedelta(days=30)})
    db.commit()
    try:
        rt.create_return(db, buyer, o.number, [{"order_item_id": o.items[0].id, "qty": 1}], "late")
        assert False
    except rt.ReturnError as e:
        assert "window" in e.msg
    monkeypatch.setenv("EXT_RETURN_WINDOW_DAYS", "60")
    r = rt.create_return(db, buyer, o.number, [{"order_item_id": o.items[0].id, "qty": 1}], "ok now")
    rt.approve(db, r.id); rt.mark_received(db, r.id); rt.refund(db, r.id)
    assert db.get(Order, o.id).status == "refunded"


def test_double_refund_click_is_blocked(db):
    admin, buyer, (ua, sa), (ub, sb), house, pa, pb = world(db)
    o = f.paid_order(db, buyer, [(pa, 1)])
    deliver(db, o)
    r = rt.create_return(db, buyer, o.number, [{"order_item_id": o.items[0].id, "qty": 1}], "x")
    rt.approve(db, r.id); rt.mark_received(db, r.id); rt.refund(db, r.id)
    try:
        rt.refund(db, r.id)
        assert False
    except rt.ReturnError as e:
        assert e.status == 409


def test_refund_amount_includes_discount_share_and_tax():
    class It:  # minimal stand-ins
        def __init__(s, i, p, q): s.id, s.unit_price_cents, s.qty = i, p, q
    class O: items = [It(1, 10000, 1), It(2, 30000, 1)]; discount_cents = 4000; tax_cents = 7200
    # item 2 is 3/4 of the subtotal: 30000 - 3000 discount + 5400 tax
    assert rt.refund_amount(O, {2: 1}) == 32400


def test_http_return_flow_and_permissions(db, client):
    admin, buyer, (ua, sa), (ub, sb), house, pa, pb = world(db)
    o = f.paid_order(db, buyer, [(pa, 1)])
    deliver(db, o)
    it = o.items[0]
    h = auth(buyer)
    r = client.post(f"/orders/{o.number}/returns", json={"lines": [{"order_item_id": it.id, "qty": 1}], "reason": "Wrong size"}, headers=h)
    assert r.status_code == 201
    rid = r.json()["id"]
    assert client.post(f"/admin/ext/returns/{rid}/approve", headers=h).status_code == 403   # customers can't approve
    ha = auth(admin)
    for step in ("approve", "receive", "refund"):
        assert client.post(f"/admin/ext/returns/{rid}/{step}", headers=ha).status_code == 200
    assert client.get("/returns", headers=h).json()[0]["status"] == "refunded"
    assert client.get("/returns", headers=auth(f.user(db, "other@x.test"))).json() == []
