"""HTTP API for the extension modules. Every route is new; no existing route is changed."""
from typing import Literal
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import Order, Product, User, Variant
from ..models_market import ProductSeller, Seller
from ..schemas import public_product
from ..security import admin_user, current_user
from . import accounts, gst, ledger, returns as rt, search as srch, shipping as ship
from .config import cfg
from .models import (Invoice, LedgerEntry, LineSplit, Payout, ProductTax, ReturnLine, ReturnRequest, SellerProfile, Shipment, VariantShipping, WishlistItem)

# ============================================================== accounts: verification + password reset
auth = APIRouter(prefix="/auth", tags=["accounts"])


class TokenIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)


class ForgotIn(BaseModel):
    email: str = Field(max_length=255)


class ResetIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    password: str = Field(min_length=8, max_length=128)


@auth.get("/email/status")
def email_status(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return {"email": user.email, "verified": accounts.is_verified(db, user.id)}


@auth.post("/email/send-verification")
def send_verification(user: User = Depends(current_user), db: Session = Depends(get_db)):
    if accounts.is_verified(db, user.id):
        return {"sent": False, "verified": True}
    if not accounts.send_verification(db, user):
        raise HTTPException(429, "Too many requests. Check your inbox or try again in an hour.")
    return {"sent": True, "verified": False}


@auth.post("/email/verify")
def verify_email(body: TokenIn, db: Session = Depends(get_db)):
    if not accounts.verify(db, body.token):
        raise HTTPException(400, "This link is invalid or has expired")
    return {"verified": True}


@auth.post("/password/forgot")
def forgot(body: ForgotIn, db: Session = Depends(get_db)):
    accounts.start_reset(db, body.email)
    return {"ok": True, "message": "If that email has an account, a reset link is on its way."}


@auth.post("/password/reset")
def reset(body: ResetIn, db: Session = Depends(get_db)):
    if not accounts.finish_reset(db, body.token, body.password):
        raise HTTPException(400, "This reset link is invalid or has expired")
    return {"ok": True}


def require_verified_email(user: User = Depends(current_user), db: Session = Depends(get_db)) -> User:
    """Optional dependency: add to any route (e.g. checkout) that must only be used by verified buyers."""
    if not accounts.is_verified(db, user.id):
        raise HTTPException(403, "Please verify your email address first")
    return user


# ============================================================== wishlist
wish = APIRouter(prefix="/wishlist", tags=["wishlist"])


@wish.get("")
def wishlist(user: User = Depends(current_user), db: Session = Depends(get_db)):
    ids = list(db.scalars(select(WishlistItem.product_id).where(WishlistItem.user_id == user.id).order_by(WishlistItem.id.desc())))
    ps = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_(ids)))} if ids else {}
    return {"items": [public_product(ps[i]) for i in ids if i in ps and ps[i].is_active]}


@wish.put("/{pid}", status_code=201)
def wish_add(pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = db.get(Product, pid)
    if not p or not p.is_active:
        raise HTTPException(404, "Product not found")
    if not db.scalars(select(WishlistItem.id).where(WishlistItem.user_id == user.id, WishlistItem.product_id == pid)).first():
        db.add(WishlistItem(user_id=user.id, product_id=pid))
        db.commit()
    return {"product_id": pid, "wishlisted": True}


@wish.delete("/{pid}")
def wish_remove(pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    for w in db.scalars(select(WishlistItem).where(WishlistItem.user_id == user.id, WishlistItem.product_id == pid)):
        db.delete(w)
    db.commit()
    return {"product_id": pid, "wishlisted": False}


# ============================================================== search
sr = APIRouter(tags=["search"])


@sr.get("/search")
def search(q: str | None = Query(None, max_length=100), category: str | None = None, seller_id: int | None = None, min_price: int | None = Query(None, ge=0),
           max_price: int | None = Query(None, ge=0), in_stock: bool = False, sort: str = Query("new", pattern="^(new|price_asc|price_desc|rating)$"),
           page: int = Query(1, ge=1), page_size: int = Query(12, ge=1, le=50), db: Session = Depends(get_db)):
    return srch.search(db, q, category, seller_id, min_price, max_price, in_stock, sort, page, page_size)


@sr.get("/search/suggest")
def suggest(q: str = Query("", max_length=100), db: Session = Depends(get_db)):
    return {"suggestions": srch.suggest(db, q)}


# ============================================================== seller self-service
sel = APIRouter(prefix="/sellers/me", tags=["seller-ext"])


def _my_seller(db: Session, user: User, approved: bool = False) -> Seller:
    s = db.scalars(select(Seller).where(Seller.user_id == user.id)).first()
    if not s:
        raise HTTPException(404, "You are not a seller yet")
    if approved and s.status != "approved":
        raise HTTPException(403, f"Seller account is {s.status}")
    return s


class ProfileIn(BaseModel):
    legal_name: str = Field(default="", max_length=255)
    gstin: str = Field(default="", max_length=15, pattern=r"^([0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z])?$")
    pan: str = Field(default="", max_length=10)
    address: str = Field(default="", max_length=500)
    pickup_pincode: str = Field(default="", max_length=16, pattern=r"^([0-9]{6})?$")
    upi_id: str = Field(default="", max_length=128)
    bank_account_name: str = Field(default="", max_length=255)
    bank_account_no: str = Field(default="", max_length=34)
    bank_ifsc: str = Field(default="", max_length=11)


def _profile_out(p: SellerProfile | None) -> dict:
    if not p:
        return {}
    acct = p.bank_account_no
    return {"legal_name": p.legal_name, "gstin": p.gstin, "state_code": p.state_code, "pan": p.pan, "address": p.address, "pickup_pincode": p.pickup_pincode, "upi_id": p.upi_id,
            "bank_account_name": p.bank_account_name, "bank_account_masked": ("*" * max(len(acct) - 4, 0) + acct[-4:]) if acct else "", "bank_ifsc": p.bank_ifsc}


@sel.get("/profile")
def my_profile(user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = _my_seller(db, user)
    return _profile_out(db.scalars(select(SellerProfile).where(SellerProfile.seller_id == s.id)).first())


@sel.put("/profile")
def put_profile(body: ProfileIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = _my_seller(db, user)
    p = db.scalars(select(SellerProfile).where(SellerProfile.seller_id == s.id)).first() or SellerProfile(seller_id=s.id)
    data = body.model_dump()
    if not data["bank_account_no"] and p.bank_account_no:       # the masked value is all the UI ever sees; blank = keep existing
        data["bank_account_no"] = p.bank_account_no
    for k, v in data.items():
        setattr(p, k, v.strip().upper() if k in ("gstin", "pan", "bank_ifsc") else v.strip())
    p.state_code = gst.state_code(p.gstin[:2]) if p.gstin else p.state_code
    db.add(p)
    db.commit()
    return _profile_out(p)


@sel.get("/ledger")
def my_ledger(user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = _my_seller(db, user)
    ledger.sync(db)
    rows = db.scalars(select(LedgerEntry).where(LedgerEntry.seller_id == s.id).order_by(LedgerEntry.id.desc()).limit(200)).all()
    return {"balance": ledger.balances(db, s.id), "commission_pct": s.commission_pct, "hold_days": cfg.payout_hold_days,
            "entries": [{"id": e.id, "kind": e.kind, "amount_cents": e.amount_cents, "order": e.order_number, "note": e.note, "available_at": e.available_at.isoformat(),
                         "paid_out": e.payout_id is not None, "created_at": e.created_at.isoformat()} for e in rows]}


@sel.get("/payouts")
def my_payouts(user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = _my_seller(db, user)
    return [_payout(p) for p in db.scalars(select(Payout).where(Payout.seller_id == s.id).order_by(Payout.id.desc()))]


@sel.get("/orders")
def my_sales(user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = _my_seller(db, user)
    ledger.sync(db)
    rows = db.execute(select(LineSplit, Order.status, Order.created_at).join(Order, Order.id == LineSplit.order_id).where(LineSplit.seller_id == s.id).order_by(LineSplit.id.desc()).limit(200)).all()
    return [{"order": l.order_number, "status": st, "qty": l.qty, "gross_cents": l.gross_cents, "commission_cents": l.commission_cents, "net_cents": l.net_cents,
             "reversed_cents": l.reversed_cents, "placed": c.isoformat()} for l, st, c in rows]


@sel.post("/orders/{number}/ship")
def seller_ship(number: str, courier_id: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = _my_seller(db, user, approved=True)
    o = _order(db, number)
    return _shipment(_ship_or_http(db, o, s.id, courier_id))


@sel.get("/shipments")
def my_shipments(user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = _my_seller(db, user)
    return [_shipment(x) for x in db.scalars(select(Shipment).where(Shipment.seller_id == s.id).order_by(Shipment.id.desc()).limit(100))]


def _payout(p: Payout) -> dict:
    return {"id": p.id, "seller_id": p.seller_id, "amount_cents": p.amount_cents, "status": p.status, "method": p.method, "reference": p.reference, "created_at": p.created_at.isoformat(),
            "paid_at": p.paid_at.isoformat() if p.paid_at else None}


# ============================================================== returns (customer)
ret = APIRouter(tags=["returns"])


class ReturnLineIn(BaseModel):
    order_item_id: int
    qty: int = Field(ge=1, le=99)


class ReturnIn(BaseModel):
    lines: list[ReturnLineIn] = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=255)
    note: str = Field(default="", max_length=2000)


def _ret_out(db: Session, r: ReturnRequest) -> dict:
    return {"id": r.id, "order": r.order_number, "status": r.status, "reason": r.reason, "note": r.note, "admin_note": r.admin_note, "refund_cents": r.refund_cents,
            "restock": r.restock, "created_at": r.created_at.isoformat(), "lines": [{"order_item_id": l.order_item_id, "qty": l.qty} for l in rt.lines_of(db, r)]}


def _rt(fn, *a, **k):
    try:
        return fn(*a, **k)
    except rt.ReturnError as e:
        raise HTTPException(e.status, e.msg)


@ret.post("/orders/{number}/returns", status_code=201)
def request_return(number: str, body: ReturnIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _ret_out(db, _rt(rt.create_return, db, user, number, [l.model_dump() for l in body.lines], body.reason, body.note))


@ret.get("/returns")
def my_returns(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return [_ret_out(db, r) for r in db.scalars(select(ReturnRequest).where(ReturnRequest.user_id == user.id).order_by(ReturnRequest.id.desc()))]


@ret.post("/returns/{rid}/cancel")
def cancel_return(rid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _rt(rt.cancel_by_customer, db, rid, user)
    return {"id": rid, "status": "cancelled"}


# ============================================================== shipping + invoices (buyer facing)
buyer = APIRouter(tags=["shipping-invoices"])


class QuoteIn(BaseModel):
    postal_code: str = Field(pattern=r"^[0-9]{6}$")


def _order(db: Session, number: str) -> Order:
    o = db.scalars(select(Order).where(Order.number == number)).first()
    if not o:
        raise HTTPException(404, "Order not found")
    return o


def _ship_or_http(db, o, sid, courier_id=None):
    try:
        return ship.create_shipment(db, o, sid, courier_id)
    except ship.ShippingError as e:
        raise HTTPException(e.status, e.msg)


def _shipment(s: Shipment) -> dict:
    return {"id": s.id, "order": s.order_number, "seller_id": s.seller_id, "carrier": s.carrier, "awb": s.awb, "status": s.status, "cost_cents": s.cost_cents,
            "label_url": s.label_url, "events": s.events, "delivered_at": s.delivered_at.isoformat() if s.delivered_at else None}


@buyer.post("/shipping/quote")
def shipping_quote(body: QuoteIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Carrier options for what is in the buyer's cart, one entry per seller parcel. (Checkout still charges your flat shipping fee.)"""
    from ..models import Cart
    cart = db.scalars(select(Cart).where(Cart.user_id == user.id)).first()
    if not cart or not cart.items:
        raise HTTPException(400, "Cart is empty")
    try:
        return {"shipments": ship.quote_lines(db, [(ci.variant_id, ci.qty, ci.variant.product_id) for ci in cart.items], body.postal_code)}
    except ship.ShippingError as e:
        raise HTTPException(e.status, e.msg)


@buyer.get("/orders/{number}/items")
def order_items(number: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Line ids + how many units are still returnable (used by the return form)."""
    o = _order(db, number)
    if o.user_id != user.id:
        raise HTTPException(404, "Order not found")
    return [{"id": i.id, "title": i.title, "qty": i.qty, "unit_price_cents": i.unit_price_cents, "returnable_qty": i.qty - rt._returned_qty(db, i.id)} for i in o.items]


@buyer.get("/orders/{number}/shipments")
def order_shipments(number: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    o = _order(db, number)
    if o.user_id != user.id and user.role != "admin":
        raise HTTPException(404, "Order not found")
    return [_shipment(s) for s in db.scalars(select(Shipment).where(Shipment.order_number == number).order_by(Shipment.id))]


@buyer.post("/shipping/webhook")
async def shipping_webhook(payload: dict, db: Session = Depends(get_db), x_webhook_token: str | None = Header(None)):
    """Carrier -> us. Send header X-Webhook-Token = SHIPPING_WEBHOOK_TOKEN. Body: {awb, status|current_status, detail?}."""
    import hmac
    if not cfg.shipping_webhook_token or not x_webhook_token or not hmac.compare_digest(cfg.shipping_webhook_token, x_webhook_token):
        raise HTTPException(401, "Bad token")
    awb = str(payload.get("awb") or payload.get("awb_code") or "")
    s = db.scalars(select(Shipment).where(Shipment.awb == awb)).first() if awb else None
    if not s:
        return {"status": "ignored"}
    changed = ship.apply_tracking(db, s, str(payload.get("current_status") or payload.get("status") or ""), str(payload.get("detail") or payload.get("location") or ""))
    return {"status": "ok" if changed else "duplicate"}


def _invoice_row(i: Invoice) -> dict:
    return {"id": i.id, "number": i.number, "kind": i.kind, "order": i.order_number, "seller_id": i.seller_id, "total_cents": i.total_cents, "issued_at": i.issued_at.isoformat()}


@buyer.get("/orders/{number}/invoices")
def order_invoices(number: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    o = _order(db, number)
    if o.user_id != user.id and user.role != "admin":
        raise HTTPException(404, "Order not found")
    try:
        invs = gst.invoices_for_order(db, o)
    except ValueError as e:
        raise HTTPException(409, str(e))
    cn = db.scalars(select(Invoice).where(Invoice.order_number == number, Invoice.kind == "credit_note")).all()
    return [_invoice_row(i) for i in [*invs, *cn]]


def _can_see(db: Session, user: User, inv: Invoice) -> bool:
    if user.role == "admin":
        return True
    o = db.scalars(select(Order).where(Order.number == inv.order_number)).first()
    if o and o.user_id == user.id:
        return True
    s = db.scalars(select(Seller).where(Seller.user_id == user.id)).first()
    return bool(s and inv.seller_id == s.id)


@buyer.get("/invoices/{iid}")
def invoice_json(iid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    inv = db.get(Invoice, iid)
    if not inv or not _can_see(db, user, inv):
        raise HTTPException(404, "Invoice not found")
    return {**_invoice_row(inv), "data": inv.data}


@buyer.get("/invoices/{iid}/pdf")
def invoice_pdf(iid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    inv = db.get(Invoice, iid)
    if not inv or not _can_see(db, user, inv):
        raise HTTPException(404, "Invoice not found")
    fname = inv.number.replace("/", "-")
    try:
        return Response(gst.render_pdf(inv), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{fname}.pdf"'})
    except ImportError:
        raise HTTPException(501, "PDF support needs `pip install reportlab`; use /invoices/{id}/html instead")


@buyer.get("/invoices/{iid}/html")
def invoice_html(iid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    inv = db.get(Invoice, iid)
    if not inv or not _can_see(db, user, inv):
        raise HTTPException(404, "Invoice not found")
    return Response(gst.render_html(inv), media_type="text/html")


# ============================================================== admin
adm = APIRouter(prefix="/admin/ext", tags=["admin-ext"], dependencies=[Depends(admin_user)])


@adm.get("/dashboard")
def dashboard(db: Session = Depends(get_db)):
    ledger.sync(db)
    sums = db.execute(select(func.coalesce(func.sum(LineSplit.gross_cents), 0), func.coalesce(func.sum(LineSplit.commission_cents), 0), func.coalesce(func.sum(LineSplit.reversed_cents), 0))).one()
    owed = db.scalar(select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0)).where(LedgerEntry.payout_id.is_(None))) or 0
    by = lambda col, st=None: dict(db.execute(select(col, func.count()).group_by(col)).all())
    return {"marketplace_gmv_cents": int(sums[0]), "commission_earned_cents": int(sums[1]), "reversed_to_sellers_cents": int(sums[2]), "owed_to_sellers_cents": int(owed),
            "payouts_by_status": by(Payout.status), "returns_by_status": by(ReturnRequest.status), "shipments_by_status": by(Shipment.status),
            "sellers_by_status": by(Seller.status), "search_provider": cfg.search_provider, "shipping_provider": cfg.shipping_provider}


@adm.post("/ledger/sync")
def ledger_sync(db: Session = Depends(get_db)):
    return ledger.sync(db)


@adm.get("/ledger/sellers")
def seller_balances(db: Session = Depends(get_db)):
    ledger.sync(db)
    out = []
    for s in db.scalars(select(Seller).order_by(Seller.id)):
        out.append({"seller_id": s.id, "name": s.name, "status": s.status, "commission_pct": s.commission_pct, "payout_details": ledger.payout_details_ok(db, s.id), **ledger.balances(db, s.id)})
    return out


class CommissionIn(BaseModel):
    commission_pct: float = Field(ge=0, le=100)


@adm.put("/sellers/{sid}/commission")
def set_commission(sid: int, body: CommissionIn, db: Session = Depends(get_db)):
    s = db.get(Seller, sid)
    if not s:
        raise HTTPException(404, "Seller not found")
    s.commission_pct = body.commission_pct       # applies to future orders; accrued lines keep their snapshot
    db.commit()
    return {"seller_id": sid, "commission_pct": s.commission_pct}


@adm.get("/sellers/{sid}/profile")
def admin_seller_profile(sid: int, db: Session = Depends(get_db)):
    return _profile_out(db.scalars(select(SellerProfile).where(SellerProfile.seller_id == sid)).first())


@adm.post("/payouts/run")
def run_payouts(db: Session = Depends(get_db)):
    return ledger.run_payouts(db)


@adm.get("/payouts")
def payouts(status: str | None = None, db: Session = Depends(get_db)):
    q = select(Payout).order_by(Payout.id.desc()).limit(200)
    return [_payout(p) for p in db.scalars(q.where(Payout.status == status) if status else q)]


class PaidIn(BaseModel):
    reference: str = Field(min_length=3, max_length=64)


def _payout_or_404(db, pid):
    p = db.get(Payout, pid)
    if not p:
        raise HTTPException(404, "Payout not found")
    return p


@adm.post("/payouts/{pid}/paid")
def payout_paid(pid: int, body: PaidIn, db: Session = Depends(get_db)):
    try:
        ledger.mark_paid(db, _payout_or_404(db, pid), body.reference)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"id": pid, "status": "paid"}


@adm.post("/payouts/{pid}/fail")
def payout_fail(pid: int, note: str = "", db: Session = Depends(get_db)):
    try:
        ledger.mark_failed(db, _payout_or_404(db, pid), note)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"id": pid, "status": "failed"}


class TaxIn(BaseModel):
    hsn: str = Field(default="", max_length=8, pattern=r"^[0-9]*$")
    gst_rate_pct: float = Field(ge=0, le=40)


@adm.put("/products/{pid}/tax")
def product_tax(pid: int, body: TaxIn, db: Session = Depends(get_db)):
    if not db.get(Product, pid):
        raise HTTPException(404, "Product not found")
    t = db.scalars(select(ProductTax).where(ProductTax.product_id == pid)).first() or ProductTax(product_id=pid)
    t.hsn, t.gst_rate_pct = body.hsn, body.gst_rate_pct
    db.add(t)
    db.commit()
    return {"product_id": pid, "hsn": t.hsn, "gst_rate_pct": t.gst_rate_pct}


class WeightIn(BaseModel):
    weight_g: int = Field(ge=1, le=100000)


@adm.put("/variants/{vid}/shipping")
def variant_weight(vid: int, body: WeightIn, db: Session = Depends(get_db)):
    if not db.get(Variant, vid):
        raise HTTPException(404, "Variant not found")
    v = db.scalars(select(VariantShipping).where(VariantShipping.variant_id == vid)).first() or VariantShipping(variant_id=vid)
    v.weight_g = body.weight_g
    db.add(v)
    db.commit()
    return {"variant_id": vid, "weight_g": v.weight_g}


@adm.get("/returns")
def admin_returns(status: str | None = None, db: Session = Depends(get_db)):
    q = select(ReturnRequest).order_by(ReturnRequest.id.desc()).limit(200)
    return [_ret_out(db, r) for r in db.scalars(q.where(ReturnRequest.status == status) if status else q)]


class NoteIn(BaseModel):
    admin_note: str = Field(default="", max_length=500)


@adm.post("/returns/{rid}/approve")
def ret_approve(rid: int, body: NoteIn = NoteIn(), db: Session = Depends(get_db)):
    _rt(rt.approve, db, rid, body.admin_note)
    return {"id": rid, "status": "approved"}


@adm.post("/returns/{rid}/reject")
def ret_reject(rid: int, body: NoteIn = NoteIn(), db: Session = Depends(get_db)):
    _rt(rt.reject, db, rid, body.admin_note)
    return {"id": rid, "status": "rejected"}


@adm.post("/returns/{rid}/receive")
def ret_receive(rid: int, db: Session = Depends(get_db)):
    _rt(rt.mark_received, db, rid)
    return {"id": rid, "status": "received"}


@adm.post("/returns/{rid}/refund")
def ret_refund(rid: int, db: Session = Depends(get_db)):
    r = _rt(rt.refund, db, rid)
    return {"id": rid, "status": r.status, "refund_cents": r.refund_cents}


@adm.get("/shipments")
def admin_shipments(status: str | None = None, db: Session = Depends(get_db)):
    q = select(Shipment).order_by(Shipment.id.desc()).limit(200)
    return [_shipment(s) for s in db.scalars(q.where(Shipment.status == status) if status else q)]


@adm.post("/orders/{number}/shipments")
def admin_create_shipment(number: str, seller_id: int | None = None, courier_id: str | None = None, db: Session = Depends(get_db)):
    return _shipment(_ship_or_http(db, _order(db, number), seller_id or None, courier_id))


@adm.post("/shipments/{sid}/refresh")
def admin_refresh(sid: int, db: Session = Depends(get_db)):
    s = db.get(Shipment, sid)
    if not s:
        raise HTTPException(404, "Shipment not found")
    try:
        return _shipment(ship.refresh(db, s))
    except ship.ShippingError as e:
        raise HTTPException(e.status, e.msg)


class TrackIn(BaseModel):
    status: Literal["in_transit", "delivered", "rto", "cancelled"]
    detail: str = Field(default="", max_length=255)


@adm.post("/shipments/{sid}/status")
def admin_set_status(sid: int, body: TrackIn, db: Session = Depends(get_db)):
    """Manual tracking update (also how you move mock shipments forward)."""
    s = db.get(Shipment, sid)
    if not s:
        raise HTTPException(404, "Shipment not found")
    ship.apply_tracking(db, s, body.status, body.detail or "Updated by admin")
    return _shipment(s)


@adm.get("/invoices")
def admin_invoices(order: str | None = None, db: Session = Depends(get_db)):
    q = select(Invoice).order_by(Invoice.id.desc()).limit(200)
    return [_invoice_row(i) for i in db.scalars(q.where(Invoice.order_number == order) if order else q)]


@adm.post("/search/reindex")
def reindex(db: Session = Depends(get_db)):
    try:
        return srch.reindex_all(db)
    except Exception as e:
        raise HTTPException(502, f"Meilisearch reindex failed: {e.__class__.__name__}")


ROUTERS = [auth, wish, sr, sel, ret, buyer, adm]
