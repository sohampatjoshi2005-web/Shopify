"""Keyword intent classification (whole words only). Swap for an LLM classifier later; the contract is classify_intent(message) -> intent name.
Substrings are NOT enough: 'bill' would match 'billion', 'api' would match 'capital', 'return' fires inside 'returning home'."""
import re

MARKETING_KEYWORDS = ["discount", "coupon", "promo", "newsletter", "unsubscribe", "pricing plans", "demo", "sales", "partnership"]
ORDER_NO = re.compile(r"\bORD-[0-9A-F]{8}\b", re.I)


def _has(text: str, *words: str) -> bool:
    return any(re.search(rf"\b{re.escape(w)}\b", text) for w in words)


def classify_intent(message: str) -> str:
    text = (message or "").lower()
    if _has(text, "password", "passcode", "locked out"):
        return "password_reset"
    if _has(text, "invoice", "invoices", "receipt", "bill", "gst"):
        return "invoice_lookup"
    if _has(text, "refund", "refunds", "return", "returns", "cancel", "cancellation"):
        return "refund_request"
    if _has(text, "shipping", "shipped", "track", "tracking", "delivery", "delivered", "courier", "awb"):
        return "shipping_status"
    if _has(text, "order", "orders", "status") or ORDER_NO.search(message or ""):
        return "order_status"
    if _has(text, "api"):
        return "api_usage_clarification"
    if _has(text, *[k for k in MARKETING_KEYWORDS if " " not in k]) or "pricing plans" in text:
        return "marketing"
    return "unknown"
