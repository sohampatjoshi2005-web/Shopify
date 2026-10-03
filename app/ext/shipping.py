"""Shipping: carrier adapters (mock + Shiprocket), rate quotes per seller, per-seller shipments, tracking -> order status.

Adapters follow the same idea as your payments module: `mock` is complete and needs nothing; `shiprocket` talks to the
Shiprocket REST API (written from its public docs, UNTESTED against a live account)."""
import math
import secrets
import time
from dataclasses import dataclass
import requests
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ..models import Order, now
from .config import cfg
from .ledger import seller_id_for_product
from .models import SellerProfile, Shipment, VariantShipping


class ShippingError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status, self.msg = status, msg


@dataclass
class Rate:
    carrier: str
    service: str
    cost_cents: int
    eta_days: int
    courier_id: str = ""

    def dict(self):
        return self.__dict__.copy()


def zone(origin: str, dest: str) -> str:
    if origin[:3] == dest[:3]:
        return "local"
    return "regional" if origin[:1] == dest[:1] else "national"


class MockProvider:
    name = "mock"
    TABLE = {"local": (4000, 1500, 2), "regional": (6000, 2500, 4), "national": (8000, 3500, 6)}

    def rates(self, origin: str, dest: str, weight_g: int) -> list[Rate]:
        base, step, eta = self.TABLE[zone(origin, dest)]
        cost = base + step * (math.ceil(max(weight_g, 1) / 500) - 1)
        return [Rate("MockExpress", "Standard", cost, eta, "mock-std"), Rate("MockAir", "Priority", int(cost * 1.6), max(1, eta - 1), "mock-air")]

    def create(self, order: Order, origin: str, weight_g: int, rate: Rate) -> dict:
        awb = "MOCK" + secrets.token_hex(5).upper()      # unique per parcel (an order can have several)
        return {"awb": awb, "provider_ref": f"mock-{awb}", "carrier": rate.carrier, "label_url": f"https://label.example/{awb}.pdf"}

    def track(self, awb: str) -> dict:
        return {"status": "in_transit", "detail": "Mock carrier: use the admin button or webhook to move this shipment forward"}


class ShiprocketProvider:
    name = "shiprocket"
    BASE = "https://apiv2.shiprocket.in/v1/external"
    _tok: tuple[str, float] = ("", 0.0)

    def _h(self) -> dict:
        t, exp = ShiprocketProvider._tok
        if not t or exp < time.time():
            r = requests.post(f"{self.BASE}/auth/login", json={"email": cfg.shiprocket_email, "password": cfg.shiprocket_password}, timeout=20)
            r.raise_for_status()
            t = r.json()["token"]
            ShiprocketProvider._tok = (t, time.time() + 8 * 24 * 3600)
        return {"Authorization": f"Bearer {t}", "Content-Type": "application/json"}

    def rates(self, origin: str, dest: str, weight_g: int) -> list[Rate]:
        r = requests.get(f"{self.BASE}/courier/serviceability/", headers=self._h(), timeout=20,
                         params={"pickup_postcode": origin, "delivery_postcode": dest, "weight": max(weight_g, 1) / 1000, "cod": 0})
        r.raise_for_status()
        cs = (r.json().get("data") or {}).get("available_courier_companies") or []
        return sorted((Rate(c.get("courier_name", ""), "Standard", int(round(float(c.get("rate", 0)) * 100)), int(float(c.get("estimated_delivery_days") or 0) or 0),
                            str(c.get("courier_company_id", ""))) for c in cs), key=lambda x: x.cost_cents)

    def create(self, order: Order, origin: str, weight_g: int, rate: Rate) -> dict:
        a, h = order.shipping_address, self._h()
        body = {"order_id": order.number, "order_date": now().strftime("%Y-%m-%d %H:%M"), "pickup_location": "Primary",
                "billing_customer_name": a.get("full_name", ""), "billing_last_name": "", "billing_address": a.get("line1", ""), "billing_address_2": a.get("line2") or "",
                "billing_city": a.get("city", ""), "billing_pincode": a.get("postal_code", ""), "billing_state": a.get("state", ""), "billing_country": "India",
                "billing_email": order.user.email if order.user else "", "billing_phone": a.get("phone", ""), "shipping_is_billing": True,
                "order_items": [{"name": i.title, "sku": i.sku, "units": i.qty, "selling_price": i.unit_price_cents / 100} for i in order.items],
                "payment_method": "Prepaid", "sub_total": order.subtotal_cents / 100, "length": 10, "breadth": 10, "height": 10, "weight": max(weight_g, 1) / 1000}
        r = requests.post(f"{self.BASE}/orders/create/adhoc", json=body, headers=h, timeout=30)
        r.raise_for_status()
        sid = r.json()["shipment_id"]
        aw = requests.post(f"{self.BASE}/courier/assign/awb", json={"shipment_id": sid, "courier_id": rate.courier_id or None}, headers=h, timeout=30)
        aw.raise_for_status()
        resp = (aw.json().get("response") or {}).get("data") or {}
        return {"awb": str(resp.get("awb_code", "")), "provider_ref": str(sid), "carrier": resp.get("courier_name", rate.carrier), "label_url": ""}

    def track(self, awb: str) -> dict:
        r = requests.get(f"{self.BASE}/courier/track/awb/{awb}", headers=self._h(), timeout=20)
        r.raise_for_status()
        t = (r.json().get("tracking_data") or {})
        return {"status": map_status(str(t.get("shipment_status_id", "")) or str((t.get("shipment_track") or [{}])[0].get("current_status", ""))),
                "detail": (t.get("shipment_track") or [{}])[0].get("current_status", "")}


def provider():
    return ShiprocketProvider() if cfg.shipping_provider == "shiprocket" else MockProvider()


_STATUS = {"delivered": "delivered", "7": "delivered", "rto": "rto", "rto delivered": "rto", "9": "rto", "cancelled": "cancelled", "8": "cancelled",
           "in_transit": "in_transit", "in transit": "in_transit", "shipped": "in_transit", "out for delivery": "in_transit", "picked up": "in_transit", "6": "in_transit", "18": "in_transit", "42": "in_transit"}


def map_status(raw: str) -> str:
    return _STATUS.get((raw or "").strip().lower(), "in_transit" if raw else "created")


# ------------------------------------------------------------------ helpers
def weight_of(db: Session, lines: list[tuple[int, int]]) -> int:
    """lines = [(variant_id, qty)] -> grams"""
    w = 0
    for vid, q in lines:
        vs = db.scalars(select(VariantShipping).where(VariantShipping.variant_id == vid)).first()
        w += (vs.weight_g if vs else cfg.default_weight_g) * q
    return w


def pickup_pin(db: Session, seller_id: int | None) -> str:
    p = db.scalars(select(SellerProfile).where(SellerProfile.seller_id == seller_id)).first() if seller_id else None
    return (p.pickup_pincode if p and p.pickup_pincode else cfg.default_pickup_pincode)


def groups(db: Session, order: Order) -> dict[int | None, list]:
    g: dict[int | None, list] = {}
    for it in order.items:
        g.setdefault(seller_id_for_product(db, it.product_id), []).append(it)
    return g


def quote_lines(db: Session, lines: list[tuple[int, int, int | None]], dest_pin: str) -> list[dict]:
    """lines = [(variant_id, qty, product_id)] -> one entry per seller shipment with its rate options."""
    by: dict[int | None, list] = {}
    for vid, q, pid in lines:
        by.setdefault(seller_id_for_product(db, pid), []).append((vid, q))
    out = []
    for sid, ls in by.items():
        origin, w = pickup_pin(db, sid), weight_of(db, ls)
        try:
            rates = provider().rates(origin, dest_pin, w)
        except requests.RequestException as e:
            raise ShippingError(502, f"Carrier rate lookup failed: {getattr(e.response, 'status_code', 'network error')}")
        out.append({"seller_id": sid, "origin_pincode": origin, "weight_g": w, "rates": [r.dict() for r in rates]})
    return out


def create_shipment(db: Session, order: Order, seller_id: int | None, courier_id: str | None = None) -> Shipment:
    if order.status not in ("paid", "shipped"):
        raise ShippingError(409, f"Order is {order.status}; only paid orders can be shipped")
    grp = groups(db, order)
    if seller_id not in grp:
        raise ShippingError(404, "That seller has no items in this order")
    if db.scalars(select(Shipment).where(Shipment.order_number == order.number, Shipment.seller_id == seller_id, Shipment.status.in_(("created", "in_transit", "delivered")))).first():
        raise ShippingError(409, "A shipment already exists for this seller on this order")
    origin, w = pickup_pin(db, seller_id), weight_of(db, [(i.variant_id, i.qty) for i in grp[seller_id] if i.variant_id])
    prov = provider()
    try:
        rates = prov.rates(origin, order.shipping_address.get("postal_code", ""), w)
        if not rates:
            raise ShippingError(422, "No carrier serves this pincode")
        rate = next((r for r in rates if r.courier_id == courier_id), None) if courier_id else min(rates, key=lambda r: r.cost_cents)
        if not rate:
            raise ShippingError(400, "Unknown courier_id")
        made = prov.create(order, origin, w, rate)
    except requests.RequestException as e:
        raise ShippingError(502, f"Carrier request failed: {getattr(e.response, 'status_code', 'network error')}")
    s = Shipment(order_number=order.number, seller_id=seller_id, provider=prov.name, carrier=made["carrier"], awb=made["awb"], provider_ref=made["provider_ref"],
                 cost_cents=rate.cost_cents, weight_g=w, label_url=made["label_url"], events=[{"at": now().isoformat(), "status": "created", "detail": "Shipment created"}])
    db.add(s)
    try:
        db.flush()
    except IntegrityError:                  # a concurrent request booked this seller's parcel first
        db.rollback()
        raise ShippingError(409, "A shipment already exists for this seller on this order")
    if not order.tracking_number:
        db.execute(update(Order).where(Order.id == order.id).values(tracking_number=s.awb, carrier=s.carrier))
    if set(grp) <= {x.seller_id for x in db.scalars(select(Shipment).where(Shipment.order_number == order.number, Shipment.status != "cancelled"))}:
        db.execute(update(Order).where(Order.id == order.id, Order.status == "paid").values(status="shipped"))   # all parcels booked
    db.commit()
    return s


def apply_tracking(db: Session, s: Shipment, raw_status: str, detail: str = "") -> bool:
    """Record a tracking update. Returns True if it changed anything. Delivered shipments are final."""
    new = map_status(raw_status)
    if s.status == "delivered" or (new == s.status and not detail):
        return False
    ev = list(s.events or [])
    if ev and ev[-1].get("status") == new and ev[-1].get("detail") == detail:
        return False
    ev.append({"at": now().isoformat(), "status": new, "detail": detail})
    s.events, s.status = ev, new
    if new == "delivered":
        s.delivered_at = now()
    db.flush()
    o = db.scalars(select(Order).where(Order.number == s.order_number)).first()
    if o and new == "delivered":
        live = db.scalars(select(Shipment).where(Shipment.order_number == s.order_number, Shipment.status != "cancelled")).all()
        if live and all(x.status == "delivered" for x in live) and set(groups(db, o)) <= {x.seller_id for x in live}:
            db.execute(update(Order).where(Order.id == o.id, Order.status.in_(("shipped", "paid"))).values(status="delivered"))
    db.commit()
    return True


def refresh(db: Session, s: Shipment) -> Shipment:
    try:
        t = provider().track(s.awb)
    except requests.RequestException:
        raise ShippingError(502, "Carrier tracking lookup failed")
    apply_tracking(db, s, t["status"], t.get("detail", ""))
    return s
