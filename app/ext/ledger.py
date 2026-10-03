"""Multi-vendor money: per-line split (seller / commission / net), append-only seller ledger, payouts.

Nothing here edits your existing order flow. `sync()` is idempotent and derives everything from order status, so it can run
on demand (admin button, payout run) or on a schedule once you deploy the API."""
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from ..models import Order, aware, now
from ..models_market import ProductSeller, Seller
from .config import cfg
from .models import LedgerEntry, LineSplit, Payout, SellerProfile

SALE_STATES = ("paid", "shipped", "delivered")


def pct_of(cents: int, pct: float) -> int:
    return int((Decimal(cents) * Decimal(str(pct)) / 100).to_integral_value(ROUND_HALF_UP))


def seller_id_for_product(db: Session, product_id: int | None) -> int | None:
    if not product_id:
        return None
    return db.scalar(select(ProductSeller.seller_id).where(ProductSeller.product_id == product_id))


def _share(total: int, weights: list[int]) -> list[int]:
    """Split `total` in proportion to weights; the rounding remainder goes to the largest weight so the parts sum to total."""
    w = sum(weights)
    if not w or not total:
        return [0] * len(weights)
    parts = [total * x // w for x in weights]
    parts[weights.index(max(weights))] += total - sum(parts)
    return parts


def accrue_order(db: Session, order: Order) -> int:
    """Create the split + ledger credit for every not-yet-accrued line of a paid order. Returns lines created."""
    if order.status not in SALE_STATES:
        return 0
    done = set(db.scalars(select(LineSplit.order_item_id).where(LineSplit.order_id == order.id)))
    made, cache = 0, {}
    # EXT_COUPON_FUNDED_BY=seller: sellers share the order discount in proportion to their lines (default: the platform absorbs it)
    disc = _share(order.discount_cents, [i.unit_price_cents * i.qty for i in order.items]) if cfg.coupon_funded_by == "seller" else [0] * len(order.items)
    for it, d in zip(order.items, disc):
        if it.id in done:
            continue
        sid = cache.setdefault(it.product_id, seller_id_for_product(db, it.product_id))
        seller = db.get(Seller, sid) if sid else None
        pct = float(seller.commission_pct if seller and seller.commission_pct is not None else cfg.default_commission_pct) if seller else 0.0
        gross = it.unit_price_cents * it.qty - d
        fee = pct_of(gross, pct)
        db.add(LineSplit(order_item_id=it.id, order_id=order.id, order_number=order.number, product_id=it.product_id, seller_id=sid,
                         qty=it.qty, gross_cents=gross, commission_pct=pct, commission_cents=fee, net_cents=gross - fee))
        if sid:
            db.add(LedgerEntry(seller_id=sid, kind="sale", amount_cents=gross - fee, ref=f"item{it.id}", order_number=order.number,
                               note=f"{it.title} x{it.qty}", available_at=now() + timedelta(days=cfg.payout_hold_days)))
        made += 1
    db.flush()
    return made


def reverse_split(db: Session, split: LineSplit, amount_cents: int, ref: str, note: str = "") -> int:
    """Claw back (part of) a line's net from the seller. Capped at what is still owed; idempotent per `ref`."""
    if not split.seller_id:
        return 0
    amt = min(max(amount_cents, 0), split.net_cents - split.reversed_cents)
    if amt <= 0 or db.scalar(select(LedgerEntry.id).where(LedgerEntry.seller_id == split.seller_id, LedgerEntry.kind == "reversal", LedgerEntry.ref == ref)):
        return 0
    db.add(LedgerEntry(seller_id=split.seller_id, kind="reversal", amount_cents=-amt, ref=ref, order_number=split.order_number, note=note, available_at=now()))
    split.reversed_cents += amt
    db.flush()
    return amt


def reverse_order(db: Session, order: Order) -> int:
    n = 0
    for s in db.scalars(select(LineSplit).where(LineSplit.order_id == order.id)):
        if reverse_split(db, s, s.net_cents - s.reversed_cents, f"refund{s.id}", "Order refunded"):
            n += 1
    return n


def sync(db: Session, limit: int = 500) -> dict:
    """Accrue newly paid orders and reverse refunded ones. Safe to call repeatedly."""
    seen = select(LineSplit.order_id).distinct()
    new = db.scalars(select(Order).where(Order.status.in_(SALE_STATES), Order.id.not_in(seen)).limit(limit)).all()
    accrued = sum(accrue_order(db, o) for o in new)
    refunded = db.scalars(select(Order).where(Order.status == "refunded", Order.id.in_(seen)).limit(limit)).all()
    reversed_ = sum(reverse_order(db, o) for o in refunded)
    db.commit()
    return {"lines_accrued": accrued, "lines_reversed": reversed_}


def balances(db: Session, seller_id: int) -> dict:
    rows = db.execute(select(LedgerEntry.amount_cents, LedgerEntry.available_at).where(LedgerEntry.seller_id == seller_id, LedgerEntry.payout_id.is_(None))).all()
    t = now()
    avail = sum(a for a, at in rows if aware(at) <= t)
    pend = sum(a for a, at in rows if aware(at) > t)
    paid = db.scalar(select(func.coalesce(func.sum(Payout.amount_cents), 0)).where(Payout.seller_id == seller_id, Payout.status == "paid")) or 0
    return {"available_cents": int(avail), "pending_cents": int(pend), "paid_out_cents": int(paid)}


def payout_details_ok(db: Session, seller_id: int) -> bool:
    p = db.scalars(select(SellerProfile).where(SellerProfile.seller_id == seller_id)).first()
    return bool(p and (p.upi_id or (p.bank_account_no and p.bank_ifsc)))


def create_payout(db: Session, seller_id: int, method: str = "manual") -> Payout | None:
    """Bundle every released, unpaid ledger entry into one payout. Negative/zero balances carry forward."""
    t = now()
    rows = [e for e in db.scalars(select(LedgerEntry).where(LedgerEntry.seller_id == seller_id, LedgerEntry.payout_id.is_(None))) if aware(e.available_at) <= t]
    total = sum(e.amount_cents for e in rows)
    if total <= 0 or total < cfg.payout_min_cents:
        return None
    p = Payout(seller_id=seller_id, amount_cents=total, method=method)
    db.add(p)
    db.flush()
    claimed = db.execute(update(LedgerEntry).where(LedgerEntry.id.in_([e.id for e in rows]), LedgerEntry.payout_id.is_(None)).values(payout_id=p.id)).rowcount
    if claimed != len(rows):  # a concurrent run took some of them
        db.rollback()
        return None
    db.commit()
    return p


def run_payouts(db: Session, method: str = "manual") -> dict:
    sync(db)
    made, skipped = [], []
    for s in db.scalars(select(Seller).where(Seller.status == "approved")):
        if not balances(db, s.id)["available_cents"] > 0:
            continue
        if not payout_details_ok(db, s.id):
            skipped.append({"seller_id": s.id, "reason": "no UPI / bank details on file"})
            continue
        p = create_payout(db, s.id, method)
        if p:
            made.append({"payout_id": p.id, "seller_id": s.id, "amount_cents": p.amount_cents})
    return {"created": made, "skipped": skipped}


def mark_paid(db: Session, payout: Payout, reference: str) -> None:
    res = db.execute(update(Payout).where(Payout.id == payout.id, Payout.status == "created").values(status="paid", reference=reference.strip()[:64], paid_at=now()))
    db.commit()
    if res.rowcount != 1:
        raise ValueError(f"Payout is {payout.status}")


def mark_failed(db: Session, payout: Payout, note: str = "") -> None:
    res = db.execute(update(Payout).where(Payout.id == payout.id, Payout.status == "created").values(status="failed", note=note[:255]))
    if res.rowcount != 1:
        db.rollback()
        raise ValueError(f"Payout is {payout.status}")
    db.execute(update(LedgerEntry).where(LedgerEntry.payout_id == payout.id).values(payout_id=None))  # money becomes payable again
    db.commit()
