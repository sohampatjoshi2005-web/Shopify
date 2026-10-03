from sqlalchemy import select
from app.models import Order
from app.ext import gst, shipping as ship
from app.ext.models import Invoice, Shipment, VariantShipping, ProductTax
from . import factory as f
from .conftest import auth
from .test_ext_money import world, deliver


def test_state_codes():
    assert gst.state_code("Karnataka") == "29" and gst.state_code(" new delhi ") == "07" and gst.state_code("Orissa") == "21" and gst.state_code("Narnia") == ""
    assert gst.state_code("27") == "27"


def test_invoices_split_by_supplier_with_correct_gst(db):
    admin, buyer, (ua, sa), (ub, sb), house, pa, pb = world(db)
    o = f.paid_order(db, buyer, [(house, 1), (pa, 2), (pb, 1)], state="Karnataka")   # buyer in Karnataka(29)
    invs = {i.seller_id: i for i in gst.invoices_for_order(db, o)}
    assert set(invs) == {None, sa.id, sb.id}
    a = invs[sa.id].data
    assert a["supply_type"] == "intra-state" and a["place_of_supply"] == "29"
    l = a["lines"][0]                                     # 60000 paid incl. 18% -> taxable 50847, tax 9153
    assert (l["taxable_cents"], l["tax"], l["cgst"], l["sgst"], l["igst"]) == (50847, 9153, 4576, 4577, 0)
    b = invs[sb.id].data["lines"][0]                      # Beta is Maharashtra -> inter-state IGST
    assert b["igst"] == b["tax"] > 0 and b["cgst"] == 0
    assert invs[None].data["supply_type"] == "inter-state"
    assert sum(i.total_cents for i in invs.values()) == o.total_cents      # invoices add up to what the buyer paid


def test_invoice_numbers_sequential_unique_idempotent_and_short(db):
    admin, buyer, (ua, sa), (ub, sb), house, pa, pb = world(db)
    o1 = f.paid_order(db, buyer, [(pa, 1)])
    o2 = f.paid_order(db, buyer, [(pa, 1)])
    n1 = gst.invoices_for_order(db, o1)[0].number
    n2 = gst.invoices_for_order(db, o2)[0].number
    assert gst.invoices_for_order(db, o1)[0].number == n1          # re-request: same invoice, no new number
    assert n1.endswith("000001") and n2.endswith("000002") and len(n2) <= 16
    assert db.scalars(select(Invoice)).all().__len__() == 2


def test_per_product_rate_and_hsn(db):
    admin, buyer, (ua, sa), (ub, sb), house, pa, pb = world(db)
    db.add(ProductTax(product_id=pa.id, hsn="6911", gst_rate_pct=12))
    db.commit()
    o = f.paid_order(db, buyer, [(pa, 1)])
    l = gst.invoices_for_order(db, o)[0].data["lines"][0]
    assert l["hsn"] == "6911" and l["rate_pct"] == 12 and l["taxable_cents"] == 26786 and l["tax"] == 3214


def test_shipping_charge_and_discount_allocation_sum_up(db):
    admin, buyer, (ua, sa), (ub, sb), house, pa, pb = world(db)
    o = f.paid_order(db, buyer, [(house, 1)])                       # 50000 < free-shipping threshold -> shipping charged
    assert o.shipping_cents > 0
    inv = gst.invoices_for_order(db, o)[0]
    assert inv.total_cents == o.total_cents


def test_exclusive_mode_and_missing_gstin_warn(db, monkeypatch):
    admin, buyer, (ua, sa), (ub, sb), house, pa, pb = world(db)
    monkeypatch.setenv("GST_MODE", "exclusive")
    monkeypatch.setenv("GST_PLATFORM_GSTIN", "")
    o = f.paid_order(db, buyer, [(house, 1)])
    inv = gst.invoices_for_order(db, o)[0]
    assert any("exclusive" in w for w in inv.data["warnings"]) and any("GSTIN" in w for w in inv.data["warnings"])


def test_pdf_and_html_render_and_access_control(db, client):
    admin, buyer, (ua, sa), (ub, sb), house, pa, pb = world(db)
    o = f.paid_order(db, buyer, [(pa, 1)])
    r = client.get(f"/orders/{o.number}/invoices", headers=auth(buyer))
    assert r.status_code == 200
    iid = r.json()[0]["id"]
    pdf = client.get(f"/invoices/{iid}/pdf", headers=auth(buyer))
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    assert "Tax Invoice" in client.get(f"/invoices/{iid}/html", headers=auth(ua)).text      # the supplying seller can see it
    assert client.get(f"/invoices/{iid}/pdf", headers=auth(ub)).status_code == 404            # another seller cannot
    assert client.get(f"/invoices/{iid}", headers=auth(f.user(db, "nosy@x.test"))).status_code == 404
    unpaid = f.paid_order(db, buyer, [(pa, 1)], pay=False)
    assert client.get(f"/orders/{unpaid.number}/invoices", headers=auth(buyer)).status_code == 409


# ------------------------------------------------------------------ shipping
def test_quote_per_seller_parcel(db, client):
    admin, buyer, (ua, sa), (ub, sb), house, pa, pb = world(db)
    from app.models import Cart, CartItem
    c = Cart(user_id=buyer.id); db.add(c); db.commit()
    db.add_all([CartItem(cart_id=c.id, variant_id=pa.variants[0].id, qty=3), CartItem(cart_id=c.id, variant_id=house.variants[0].id, qty=1)])
    db.add(VariantShipping(variant_id=pa.variants[0].id, weight_g=800)); db.commit()
    db.refresh(c)
    r = client.post("/shipping/quote", json={"postal_code": "560034"}, headers=auth(buyer))
    assert r.status_code == 200
    ps = {p["seller_id"]: p for p in r.json()["shipments"]}
    assert ps[sa.id]["weight_g"] == 2400 and ps[sa.id]["origin_pincode"] == "560001"
    assert ps[None]["weight_g"] == 500 and ps[None]["origin_pincode"] == "110001"
    assert ps[sa.id]["rates"][0]["cost_cents"] < ps[sa.id]["rates"][1]["cost_cents"]
    assert client.post("/shipping/quote", json={"postal_code": "abc"}, headers=auth(buyer)).status_code == 422


def test_multi_seller_shipping_to_delivery_updates_order(db, client):
    admin, buyer, (ua, sa), (ub, sb), house, pa, pb = world(db)
    o = f.paid_order(db, buyer, [(pa, 1), (pb, 1)])
    ha, hb = auth(ua), auth(ub)
    r = client.post(f"/sellers/me/orders/{o.number}/ship", headers=ha)
    assert r.status_code == 200 and r.json()["awb"]
    assert db.get(Order, o.id).status == "paid"                       # Beta's parcel not booked yet
    assert client.post(f"/sellers/me/orders/{o.number}/ship", headers=ha).status_code == 409   # no duplicate parcel
    assert client.post(f"/sellers/me/orders/{o.number}/ship", headers=hb).status_code == 200
    db.expire_all()
    assert db.get(Order, o.id).status == "shipped" and db.get(Order, o.id).tracking_number
    sh = {s.seller_id: s for s in db.scalars(select(Shipment))}
    ah = auth(admin)
    assert client.post(f"/admin/ext/shipments/{sh[sa.id].id}/status", json={"status": "delivered"}, headers=ah).status_code == 200
    db.expire_all()
    assert db.get(Order, o.id).status == "shipped"                    # one parcel still on the road
    r = client.post("/shipping/webhook", json={"awb": sh[sb.id].awb, "current_status": "Delivered"}, headers={"X-Webhook-Token": "hook-secret"})
    assert r.json()["status"] == "ok"
    db.expire_all()
    assert db.get(Order, o.id).status == "delivered"
    assert client.post("/shipping/webhook", json={"awb": sh[sb.id].awb, "status": "delivered"}, headers={"X-Webhook-Token": "hook-secret"}).json()["status"] == "duplicate"


def test_ship_permissions_and_webhook_auth(db, client):
    admin, buyer, (ua, sa), (ub, sb), house, pa, pb = world(db)
    o = f.paid_order(db, buyer, [(pa, 1)])
    assert client.post(f"/sellers/me/orders/{o.number}/ship", headers=auth(ub)).status_code == 404   # Beta has no items here
    assert client.post(f"/sellers/me/orders/{o.number}/ship", headers=auth(buyer)).status_code == 404 # not a seller
    assert client.post("/shipping/webhook", json={"awb": "x", "status": "delivered"}).status_code == 401
    assert client.post("/shipping/webhook", json={"awb": "x", "status": "delivered"}, headers={"X-Webhook-Token": "wrong"}).status_code == 401
    pending = f.paid_order(db, buyer, [(pa, 1)], pay=False)
    assert client.post(f"/admin/ext/orders/{pending.number}/shipments?seller_id={sa.id}", headers=auth(admin)).status_code == 409


def test_returns_use_shipment_delivery_date(db):
    from datetime import timedelta
    from app.models import now
    from app.ext import returns as rt
    admin, buyer, (ua, sa), (ub, sb), house, pa, pb = world(db)
    o = f.paid_order(db, buyer, [(pa, 1)])
    s = ship.create_shipment(db, o, sa.id)
    ship.apply_tracking(db, s, "delivered")
    db.query(Order).filter_by(id=o.id).update({"paid_at": now() - timedelta(days=90)})   # paid long ago, but only just delivered
    db.commit()
    assert rt.create_return(db, buyer, o.number, [{"order_item_id": o.items[0].id, "qty": 1}], "x").status == "requested"
