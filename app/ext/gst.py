"""GST tax invoices and credit notes, one per supplier (each seller, or the house) per order.

Modes (GST_MODE): `inclusive` (default; shelf price already contains GST, as is usual for Indian retail) or `exclusive`
(GST is added on top; only consistent with your order totals if the amount charged at checkout matches, see `warnings`)."""
import html
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ..models import Order, now
from ..models_market import Seller
from .config import cfg
from .ledger import seller_id_for_product
from .models import Invoice, InvoiceCounter, ProductTax, ReturnLine, ReturnRequest, SellerProfile

STATE_CODES = {
    "jammu and kashmir": "01", "himachal pradesh": "02", "punjab": "03", "chandigarh": "04", "uttarakhand": "05", "haryana": "06",
    "delhi": "07", "rajasthan": "08", "uttar pradesh": "09", "bihar": "10", "sikkim": "11", "arunachal pradesh": "12", "nagaland": "13",
    "manipur": "14", "mizoram": "15", "tripura": "16", "meghalaya": "17", "assam": "18", "west bengal": "19", "jharkhand": "20",
    "odisha": "21", "chhattisgarh": "22", "madhya pradesh": "23", "gujarat": "24", "dadra and nagar haveli and daman and diu": "26",
    "maharashtra": "27", "karnataka": "29", "goa": "30", "lakshadweep": "31", "kerala": "32", "tamil nadu": "33", "puducherry": "34",
    "andaman and nicobar islands": "35", "telangana": "36", "andhra pradesh": "37", "ladakh": "38",
}
_ALIASES = {"orissa": "odisha", "new delhi": "delhi", "nct of delhi": "delhi", "uttaranchal": "uttarakhand", "pondicherry": "puducherry",
            "j&k": "jammu and kashmir", "dadra and nagar haveli": "dadra and nagar haveli and daman and diu", "daman and diu": "dadra and nagar haveli and daman and diu"}


def state_code(value: str | None) -> str:
    v = (value or "").strip().lower().replace("&", "and")
    if v.isdigit() and len(v) <= 2:
        return v.zfill(2)
    return STATE_CODES.get(_ALIASES.get(v, v), "")


def _r(x) -> int:
    return int(Decimal(x).to_integral_value(ROUND_HALF_UP))


def split_tax(taxable: int, rate: float, intra: bool) -> dict:
    tax = _r(Decimal(taxable) * Decimal(str(rate)) / 100)
    if intra:
        c = tax // 2
        return {"cgst": c, "sgst": tax - c, "igst": 0, "tax": tax}
    return {"cgst": 0, "sgst": 0, "igst": tax, "tax": tax}


def financial_year(d: datetime) -> str:
    y = d.year if d.month >= 4 else d.year - 1
    return f"{y % 100:02d}{(y + 1) % 100:02d}"


def next_number(db: Session, key: str) -> int:
    for _ in range(2):
        if db.get(InvoiceCounter, key) is None:
            db.add(InvoiceCounter(key=key, last=0))
            try:
                db.flush()
            except IntegrityError:
                db.rollback()
                continue
        db.execute(update(InvoiceCounter).where(InvoiceCounter.key == key).values(last=InvoiceCounter.last + 1))
        db.flush()
        db.expire_all()
        return db.get(InvoiceCounter, key).last
    raise RuntimeError("could not allocate invoice number")


def supplier(db: Session, seller_id: int | None) -> dict:
    if seller_id is None:
        return {"seller_id": None, "name": cfg.platform_legal_name, "gstin": cfg.platform_gstin, "state_code": cfg.platform_state_code, "address": cfg.platform_address}
    s, p = db.get(Seller, seller_id), db.scalars(select(SellerProfile).where(SellerProfile.seller_id == seller_id)).first()
    return {"seller_id": seller_id, "name": (p.legal_name if p and p.legal_name else (s.name if s else f"Seller {seller_id}")),
            "gstin": p.gstin if p else "", "state_code": (p.state_code if p and p.state_code else (state_code(p.gstin[:2]) if p and p.gstin else "")),
            "address": p.address if p else ""}


def _alloc(total: int, weights: list[int]) -> list[int]:
    """Split `total` proportionally to weights; rounding remainder goes to the largest weight so the parts always sum to total."""
    w = sum(weights)
    if not w or not total:
        return [0] * len(weights)
    parts = [total * x // w for x in weights]
    parts[weights.index(max(weights))] += total - sum(parts)
    return parts


def _lines(db: Session, order: Order, qty_override: dict[int, int] | None = None) -> dict[int | None, dict]:
    """Group the order's lines by supplier and compute taxable value / GST per line. qty_override = {order_item_id: qty} (credit notes)."""
    items = list(order.items)
    line_vals = [it.unit_price_cents * it.qty for it in items]
    disc = _alloc(order.discount_cents, line_vals)                       # discount shared by value across ALL lines
    groups_total: dict[int | None, int] = {}
    sellers = {it.id: seller_id_for_product(db, it.product_id) for it in items}
    for it, v, d in zip(items, line_vals, disc):
        groups_total[sellers[it.id]] = groups_total.get(sellers[it.id], 0) + v - d
    gids = list(groups_total)
    ship = dict(zip(gids, _alloc(order.shipping_cents, [groups_total[g] for g in gids])))
    buyer_state = state_code((order.shipping_address or {}).get("state"))
    out: dict[int | None, dict] = {}
    for it, v, d in zip(items, line_vals, disc):
        if qty_override is not None and it.id not in qty_override:
            continue
        q = qty_override[it.id] if qty_override is not None else it.qty
        sid = sellers[it.id]
        g = out.setdefault(sid, {"supplier": supplier(db, sid), "lines": [], "shipping_cents": 0})
        intra = bool(buyer_state and g["supplier"]["state_code"] == buyer_state)
        paid_for_line = (v - d) * q // it.qty                            # what the buyer paid for these units (incl. GST in inclusive mode)
        pt = db.scalars(select(ProductTax).where(ProductTax.product_id == it.product_id)).first() if it.product_id else None
        rate = pt.gst_rate_pct if pt else cfg.gst_default_rate
        if cfg.gst_mode == "exclusive":
            taxable = paid_for_line
        else:
            taxable = _r(Decimal(paid_for_line) * 100 / (Decimal(str(rate)) + 100))
        t = split_tax(taxable, rate, intra) if cfg.gst_mode == "exclusive" else _incl(paid_for_line, taxable, rate, intra)
        g["lines"].append({"order_item_id": it.id, "description": it.title, "sku": it.sku, "hsn": pt.hsn if pt else "", "qty": q,
                           "unit_price_cents": it.unit_price_cents, "discount_cents": d * q // it.qty, "taxable_cents": taxable, "rate_pct": rate, **t,
                           "total_cents": taxable + t["tax"]})
    for sid, g in out.items():
        if qty_override is None:
            g["shipping_cents"] = ship.get(sid, 0)
        g["buyer_state_code"] = buyer_state
    return out


def _incl(paid: int, taxable: int, rate: float, intra: bool) -> dict:
    tax = paid - taxable
    if intra:
        c = tax // 2
        return {"cgst": c, "sgst": tax - c, "igst": 0, "tax": tax}
    return {"cgst": 0, "sgst": 0, "igst": tax, "tax": tax}


def _totals(g: dict) -> dict:
    taxable = sum(l["taxable_cents"] for l in g["lines"])
    parts = {k: sum(l[k] for l in g["lines"]) for k in ("cgst", "sgst", "igst", "tax")}
    return {"taxable_cents": taxable, **{f"{k}_cents": v for k, v in parts.items()}, "shipping_cents": g["shipping_cents"],
            "total_cents": taxable + parts["tax"] + g["shipping_cents"]}


def _snapshot(order: Order, g: dict, kind: str, number: str, issued: datetime, ref_invoice: str | None = None) -> dict:
    tot = _totals(g)
    warnings = []
    if cfg.gst_mode == "inclusive" and order.tax_cents:
        warnings.append("Checkout charged extra tax on top of GST-inclusive prices; set TAX_PERCENT=0 so the invoice matches the amount paid.")
    if cfg.gst_mode == "exclusive" and not order.tax_cents:
        warnings.append("GST_MODE=exclusive adds GST on top, but checkout charged no tax, so this invoice total exceeds what the buyer paid. Use inclusive mode or charge tax at checkout.")
    if not g["supplier"]["gstin"]:
        warnings.append("Supplier GSTIN is not set; this document is not a valid GST invoice until it is.")
    if not g["buyer_state_code"]:
        warnings.append("Buyer state not recognised; IGST applied.")
    return {"kind": kind, "number": number, "issued_at": issued.isoformat(), "order_number": order.number, "original_invoice": ref_invoice,
            "supplier": g["supplier"], "buyer": {"name": (order.shipping_address or {}).get("full_name", ""), "address": _addr(order.shipping_address or {}),
                                                  "state_code": g["buyer_state_code"]},
            "place_of_supply": g["buyer_state_code"], "supply_type": "intra-state" if g["supplier"]["state_code"] and g["supplier"]["state_code"] == g["buyer_state_code"] else "inter-state",
            "gst_mode": cfg.gst_mode, "currency": order.currency, "lines": g["lines"], "totals": tot, "warnings": warnings}


def _addr(a: dict) -> str:
    return ", ".join(x for x in (a.get("line1"), a.get("line2"), a.get("city"), a.get("state"), a.get("postal_code")) if x)


def _number(db: Session, kind: str, sid: int | None, when: datetime) -> str:
    prefix = ("S%d" % sid) if sid else "H"
    fy = financial_year(when)
    n = next_number(db, f"{kind}:{prefix}:{fy}")
    return f"{'C' if kind == 'credit_note' else ''}{prefix}/{fy}/{n:06d}"   # <=16 chars as GST requires


def invoices_for_order(db: Session, order: Order) -> list[Invoice]:
    """Issue (once) and return one invoice per supplier. Only for paid / shipped / delivered / refunded orders."""
    if order.status not in ("paid", "shipped", "delivered", "refunded"):
        raise ValueError(f"No invoice for an order that is {order.status}")
    out = []
    for sid, g in _lines(db, order).items():
        key = f"invoice:{order.number}:{sid or 0}:0"
        inv = db.scalars(select(Invoice).where(Invoice.key == key)).first()
        if not inv:
            when = now()
            num = _number(db, "invoice", sid, when)
            snap = _snapshot(order, g, "invoice", num, when)
            inv = Invoice(key=key, number=num, kind="invoice", order_number=order.number, seller_id=sid, total_cents=snap["totals"]["total_cents"], data=snap, issued_at=when)
            db.add(inv)
            db.flush()
        out.append(inv)
    db.commit()
    return out


def issue_credit_notes(db: Session, ret: ReturnRequest) -> list[Invoice]:
    order = db.scalars(select(Order).where(Order.number == ret.order_number)).first()
    qty = {rl.order_item_id: rl.qty for rl in db.scalars(select(ReturnLine).where(ReturnLine.return_id == ret.id))}
    out = []
    for sid, g in _lines(db, order, qty).items():
        key = f"credit_note:{order.number}:{sid or 0}:{ret.id}"
        inv = db.scalars(select(Invoice).where(Invoice.key == key)).first()
        if not inv:
            orig = db.scalars(select(Invoice).where(Invoice.key == f"invoice:{order.number}:{sid or 0}:0")).first()
            when = now()
            num = _number(db, "credit_note", sid, when)
            snap = _snapshot(order, g, "credit_note", num, when, orig.number if orig else None)
            inv = Invoice(key=key, number=num, kind="credit_note", order_number=order.number, seller_id=sid, return_id=ret.id,
                          total_cents=snap["totals"]["total_cents"], data=snap, issued_at=when)
            db.add(inv)
            db.flush()
        out.append(inv)
    db.commit()
    return out


# ------------------------------------------------------------------ rendering
def _m(c: int, cur: str = "INR") -> str:
    return f"{cur} {c / 100:,.2f}"


def render_html(inv: Invoice) -> str:
    d, e = inv.data, html.escape
    title = "Credit Note" if d["kind"] == "credit_note" else "Tax Invoice"
    rows = "".join(
        f"<tr><td>{e(l['description'])}<br><small>{e(l['sku'])}</small></td><td>{e(l['hsn'])}</td><td>{l['qty']}</td><td>{_m(l['taxable_cents'])}</td>"
        f"<td>{l['rate_pct']:g}%</td><td>{_m(l['cgst'])}</td><td>{_m(l['sgst'])}</td><td>{_m(l['igst'])}</td><td>{_m(l['total_cents'])}</td></tr>" for l in d["lines"])
    t, s, b = d["totals"], d["supplier"], d["buyer"]
    warn = "".join(f"<p style='color:#b45309'>&#9888; {e(w)}</p>" for w in d["warnings"])
    return f"""<!doctype html><meta charset="utf-8"><title>{title} {e(d['number'])}</title>
<style>body{{font:14px sans-serif;max-width:900px;margin:24px auto}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccc;padding:6px;text-align:left}}</style>
<h2>{title}</h2><p><b>No:</b> {e(d['number'])} &nbsp; <b>Date:</b> {e(d['issued_at'][:10])} &nbsp; <b>Order:</b> {e(d['order_number'])}
{f"&nbsp; <b>Against invoice:</b> {e(d['original_invoice'])}" if d.get('original_invoice') else ""}</p>
<p><b>Supplier:</b> {e(s['name'])}<br>GSTIN: {e(s['gstin'] or '-')}<br>{e(s['address'])}</p>
<p><b>Bill to / Ship to:</b> {e(b['name'])}<br>{e(b['address'])}<br>Place of supply: {e(d['place_of_supply'] or '-')} ({d['supply_type']})</p>
<table><tr><th>Item</th><th>HSN</th><th>Qty</th><th>Taxable</th><th>Rate</th><th>CGST</th><th>SGST</th><th>IGST</th><th>Total</th></tr>{rows}</table>
<p>Taxable {_m(t['taxable_cents'])} &middot; CGST {_m(t['cgst_cents'])} &middot; SGST {_m(t['sgst_cents'])} &middot; IGST {_m(t['igst_cents'])}
{f"&middot; Shipping {_m(t['shipping_cents'])}" if t['shipping_cents'] else ""}</p><h3>Total {_m(t['total_cents'])}</h3>{warn}"""


def render_pdf(inv: Invoice) -> bytes:
    from io import BytesIO
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    d, st = inv.data, getSampleStyleSheet()
    esc = html.escape
    title = "Credit Note" if d["kind"] == "credit_note" else "Tax Invoice"
    s, b, t = d["supplier"], d["buyer"], d["totals"]
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=24, rightMargin=24, topMargin=24, bottomMargin=24, title=f"{title} {d['number']}")
    P = lambda txt, style="BodyText": Paragraph(txt, st[style])
    story = [P(title, "Title"), P(f"<b>No:</b> {esc(d['number'])} &nbsp; <b>Date:</b> {d['issued_at'][:10]} &nbsp; <b>Order:</b> {esc(d['order_number'])}"
                                + (f" &nbsp; <b>Against:</b> {esc(d['original_invoice'])}" if d.get("original_invoice") else "")),
             P(f"<b>Supplier:</b> {esc(s['name'])} &nbsp; GSTIN: {esc(s['gstin'] or '-')}<br/>{esc(s['address'])}"),
             P(f"<b>Bill/Ship to:</b> {esc(b['name'])}, {esc(b['address'])} &nbsp; <b>Place of supply:</b> {esc(d['place_of_supply'] or '-')} ({d['supply_type']})"), Spacer(1, 8)]
    data = [["Item", "HSN", "Qty", "Taxable", "Rate", "CGST", "SGST", "IGST", "Total"]]
    for l in d["lines"]:
        data.append([P(esc(l["description"])), l["hsn"], l["qty"], _m(l["taxable_cents"]), f"{l['rate_pct']:g}%", _m(l["cgst"]), _m(l["sgst"]), _m(l["igst"]), _m(l["total_cents"])])
    tb = Table(data, repeatRows=1, colWidths=[250, 55, 35, 75, 40, 70, 70, 70, 80])
    tb.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), .4, colors.grey), ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("FONTSIZE", (0, 0), (-1, -1), 8)]))
    story += [tb, Spacer(1, 8), P(f"Taxable {_m(t['taxable_cents'])} | CGST {_m(t['cgst_cents'])} | SGST {_m(t['sgst_cents'])} | IGST {_m(t['igst_cents'])}"
                                  + (f" | Shipping {_m(t['shipping_cents'])}" if t["shipping_cents"] else "")), P(f"<b>Total {_m(t['total_cents'])}</b>", "Heading3")]
    story += [P(f"<font color='#b45309'>Note: {esc(w)}</font>") for w in d["warnings"]]
    doc.build(story)
    return buf.getvalue()
