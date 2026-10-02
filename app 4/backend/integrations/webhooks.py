import json
from fastapi import APIRouter, Request, Header, HTTPException, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.core.db import get_db
from backend.integrations.shopify import verify_hmac
from backend.models import Order, WebhookLog, now
from backend.modules.marketing import record_event

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

@router.post("/shopify")
async def shopify_webhook(request: Request, db: Session = Depends(get_db),
                          x_shopify_hmac_sha256: str = Header(None),
                          x_shopify_topic: str = Header(""), x_shopify_webhook_id: str = Header("")):
    raw = await request.body()
    if not verify_hmac(raw, x_shopify_hmac_sha256):
        raise HTTPException(401, "Bad signature")
    if x_shopify_webhook_id:
        if db.get(WebhookLog, x_shopify_webhook_id):
            return {"status": "duplicate"}
        db.add(WebhookLog(webhook_id=x_shopify_webhook_id, topic=x_shopify_topic))
    p = json.loads(raw)
    if x_shopify_topic in ("orders/create", "orders/updated"):
        sid = str(p["id"])
        o = db.scalars(select(Order).where(Order.shopify_order_id == sid)).first() or Order(shopify_order_id=sid, name=p["name"], customer_email="")
        o.name = p["name"]
        o.customer_email = (p.get("email") or "").lower()
        o.financial_status = p.get("financial_status") or "pending"
        o.fulfillment_status = p.get("fulfillment_status") or "unfulfilled"
        o.total = float(p.get("total_price") or 0)
        o.tracking_url = next((f["tracking_url"] for f in p.get("fulfillments", []) if f.get("tracking_url")), o.tracking_url)
        db.add(o)
        if x_shopify_topic == "orders/create" and o.customer_email:
            record_event(db, o.customer_email, "purchase")
    elif x_shopify_topic == "refunds/create":
        o = db.scalars(select(Order).where(Order.shopify_order_id == str(p.get("order_id")))).first()
        if o:
            o.refunded = True
    db.commit()
    return {"status": "ok"}
