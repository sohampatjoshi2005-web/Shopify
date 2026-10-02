"""Payment providers. `mock` is complete; Stripe/Razorpay use their REST APIs directly (untested against live accounts)."""
import hashlib
import hmac
import logging
import time
import requests
from ..config import settings

log = logging.getLogger("store")


def create_payment(order) -> tuple[str, dict]:
    """Returns (provider_reference, client_payload). The payload is what the buyer's UI needs to pay."""
    p = settings.payment_provider
    if p == "mock":
        return f"mock_{order.number}", {"provider": "mock", "confirm_url": f"/payments/mock/{order.number}/confirm",
                                         "amount": order.total_cents, "currency": order.currency}
    if p == "stripe":
        r = requests.post("https://api.stripe.com/v1/payment_intents", auth=(settings.stripe_secret_key, ""), timeout=20, data={
            "amount": order.total_cents, "currency": order.currency.lower(), "automatic_payment_methods[enabled]": "true",
            "metadata[order_number]": order.number}, headers={"Idempotency-Key": f"pi-{order.number}"})
        r.raise_for_status()
        j = r.json()
        return j["id"], {"provider": "stripe", "client_secret": j["client_secret"], "publishable_key": settings.stripe_publishable_key}
    r = requests.post("https://api.razorpay.com/v1/orders", auth=(settings.razorpay_key_id, settings.razorpay_key_secret), timeout=20,
                      json={"amount": order.total_cents, "currency": order.currency, "receipt": order.number})
    r.raise_for_status()
    j = r.json()
    return j["id"], {"provider": "razorpay", "order_id": j["id"], "key_id": settings.razorpay_key_id,
                     "amount": order.total_cents, "currency": order.currency}


def refund(order) -> bool:
    """Full refund. Returns True only if the provider confirmed it."""
    try:
        if order.payment_provider == "mock":
            return True
        pid = order.provider_payment_id
        if not pid:
            return False
        if order.payment_provider == "stripe":
            r = requests.post("https://api.stripe.com/v1/refunds", auth=(settings.stripe_secret_key, ""), timeout=20,
                              data={"payment_intent": pid}, headers={"Idempotency-Key": f"rf-{order.number}"})
        else:
            r = requests.post(f"https://api.razorpay.com/v1/payments/{pid}/refund", timeout=20, json={},
                              auth=(settings.razorpay_key_id, settings.razorpay_key_secret))
        return r.status_code < 300
    except requests.RequestException:
        log.exception("refund failed for %s", order.number)
        return False


def verify_stripe(raw: bytes, header: str | None, tolerance: int = 300) -> bool:
    secret = settings.stripe_webhook_secret
    if not secret or not header:
        return False
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    t, sig = parts.get("t"), parts.get("v1")
    if not t or not sig or not t.isdigit() or abs(time.time() - int(t)) > tolerance:
        return False
    want = hmac.new(secret.encode(), f"{t}.".encode() + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, sig)


def verify_razorpay(raw: bytes, header: str | None) -> bool:
    secret = settings.razorpay_webhook_secret
    if not secret or not header:
        return False
    return hmac.compare_digest(hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest(), header)
