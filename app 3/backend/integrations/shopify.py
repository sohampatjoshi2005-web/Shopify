"""Shopify seam. Same function shapes as ecommerce_service.py in your support backend.
Mock mode (no Shopify env vars) reads the local orders table so you can develop offline."""
import base64, hashlib, hmac
from datetime import datetime
import requests
from sqlalchemy import select
from backend.core.config import settings
from backend.models import Order

def _admin_url():
    return f"https://{settings.shop_domain}/admin/api/{settings.shop_api_version}/graphql.json"

def verify_hmac(raw_body: bytes, header_hmac: str) -> bool:
    if not settings.shop_webhook_secret or not header_hmac:
        return False
    digest = base64.b64encode(hmac.new(settings.shop_webhook_secret.encode(), raw_body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(digest, header_hmac)

_ORDER_Q = """query($q:String!){ orders(first:1, query:$q){ edges{ node{
 id name email createdAt displayFinancialStatus displayFulfillmentStatus statusPageUrl
 totalPriceSet{ shopMoney{ amount } } fulfillments{ trackingInfo{ number url company } } } } } }"""

def get_order(db, order_name: str):
    """Returns a normalized dict or None. order_name like '#1001'."""
    if settings.shopify_live:
        r = requests.post(_admin_url(), json={"query": _ORDER_Q, "variables": {"q": f"name:{order_name}"}},
                          headers={"X-Shopify-Access-Token": settings.shop_admin_token}, timeout=15)
        r.raise_for_status()
        edges = r.json().get("data", {}).get("orders", {}).get("edges", [])
        if not edges:
            return None
        n = edges[0]["node"]
        tracking = next((t["url"] for f in n["fulfillments"] for t in f["trackingInfo"] if t.get("url")), None)
        fin = (n["displayFinancialStatus"] or "").lower()
        return {"name": n["name"], "email": n["email"] or "", "financial_status": fin,
                "fulfillment_status": (n["displayFulfillmentStatus"] or "").lower(),
                "tracking_url": tracking, "status_url": n.get("statusPageUrl"),
                "refunded": fin in ("refunded",), "total": float(n["totalPriceSet"]["shopMoney"]["amount"]),
                "placed_at": datetime.fromisoformat(n["createdAt"].replace("Z", "+00:00"))}
    o = db.scalars(select(Order).where(Order.name == order_name)).first()
    if not o:
        return None
    return {"name": o.name, "email": o.customer_email, "financial_status": o.financial_status,
            "fulfillment_status": o.fulfillment_status, "tracking_url": o.tracking_url, "status_url": None,
            "refunded": o.refunded, "total": o.total, "placed_at": o.placed_at}

def issue_refund(db, order: dict) -> bool:
    """Mock mode: marks the local order refunded. Live mode: NOT implemented on purpose.
    TODO: compute lines with suggestedRefund, then call refundCreate. Until then the
    resolver escalates refunds to a human, which is the safe default."""
    if settings.shopify_live:
        return False
    o = db.scalars(select(Order).where(Order.name == order["name"])).first()
    if o:
        o.refunded = True
        db.commit()
        return True
    return False

def recover_customer(email: str) -> None:
    """Triggers Shopify's own password-recovery email (Storefront API customerRecover)."""
    if not (settings.shop_domain and settings.shop_storefront_token):
        return
    q = "mutation($e:String!){ customerRecover(email:$e){ customerUserErrors{ message } } }"
    requests.post(f"https://{settings.shop_domain}/api/{settings.shop_api_version}/graphql.json",
                  json={"query": q, "variables": {"e": email}},
                  headers={"X-Shopify-Storefront-Access-Token": settings.shop_storefront_token}, timeout=15)
