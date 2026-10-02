"""Payment providers behind one interface: mock (dev), Stripe, Razorpay. Plain REST, no SDK needed."""
import hashlib, hmac, logging, time
import requests
from ..config import settings
from ..models import Order

log = logging.getLogger("store")


def create_payment(order: Order) -> tuple[str, dict]:
    """Returns (provider_ref, data the browser needs to complete payment)."""
    p = order.payment_provider
    if p == "stripe":
        r = requests.post("https://api.stripe.com/v1/payment_intents",
                          headers={"Authorization": f"Bearer {settings.stripe_secret}", "Idempotency-Key": order.number},
                          data={"amount": order.total_cents, "currency": order.currency,
                                "automatic_payment_methods[enabled]": "true", "metadata[order_number]": order.number},
                          timeout=15)
        r.raise_for_status()
        j = r.json()
        return j["id"], {"client_secret": j["client_secret"], "publishable_key": settings.stripe_publishable}
    if p == "razorpay":
        r = requests.post("https://api.razorpay.com/v1/orders", auth=(settings.razorpay_key_id, settings.razorpay_key_secret),
                          json={"amount": order.total_cents, "currency": order.currency.upper(), "receipt": order.number}, timeout=15)
        r.raise_for_status()
        j = r.json()
        return j["id"], {"razorpay_order_id": j["id"], "key_id": settings.razorpay_key_id,
                         "amount": order.total_cents, "currency": order.currency.upper()}
    return f"mock_{order.number}", {"confirm_url": f"/payments/mock/{order.number}/confirm"}


def refund(order: Order) -> bool:
    """Full refund. Returns True on success. Never raises."""
    try:
        if order.payment_provider == "stripe" and order.provider_payment_id:
            r = requests.post("https://api.stripe.com/v1/refunds", headers={"Authorization": f"Bearer {settings.stripe_secret}",
                              "Idempotency-Key": f"refund-{order.number}"}, data={"payment_intent": order.provider_payment_id}, timeout=15)
            r.raise_for_status()
            return True
        if order.payment_provider == "razorpay" and order.provider_payment_id:
            r = requests.post(f"https://api.razorpay.com/v1/payments/{order.provider_payment_id}/refund",
                              auth=(settings.razorpay_key_id, settings.razorpay_key_secret),
                              json={"amount": order.total_cents}, timeout=15)
            r.raise_for_status()
            return True
        if order.payment_provider == "mock":
            return True
    except Exception:
        log.exception("refund failed for %s", order.number)
    return False


def verify_stripe(raw: bytes, header: str | None, tolerance: int = 300) -> bool:
    if not settings.stripe_webhook_secret or not header:
        return False
    t, sigs = None, []
    for part in header.split(","):
        k, _, v = part.partition("=")
        if k == "t":
            t = v
        elif k == "v1":
            sigs.append(v)
    try:
        if not t or not sigs or abs(time.time() - int(t)) > tolerance:
            return False
    except ValueError:
        return False
    expected = hmac.new(settings.stripe_webhook_secret.encode(), f"{t}.".encode() + raw, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in sigs)


def verify_razorpay(raw: bytes, header: str | None) -> bool:
    if not settings.razorpay_webhook_secret or not header:
        return False
    expected = hmac.new(settings.razorpay_webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)
