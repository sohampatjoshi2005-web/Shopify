"""Keyword intent classification. Swap for an LLM classifier later; the contract is classify_intent(message) -> intent name."""
MARKETING_KEYWORDS = ["discount", "coupon", "promo", "newsletter", "unsubscribe", "pricing plans", "demo", "sales", "partnership"]


def classify_intent(message: str) -> str:
    text = message.lower()
    if any(k in text for k in ["reset password", "forgot password", "password"]):
        return "password_reset"
    if any(k in text for k in ["invoice", "receipt", "bill"]):
        return "invoice_lookup"
    if any(k in text for k in ["refund", "return", "cancel"]):
        return "refund_request"
    if any(k in text for k in ["shipping", "track", "tracking", "delivery"]):
        return "shipping_status"
    if "order status" in text or "where is my order" in text or "order" in text:
        return "order_status"
    if any(k in text for k in ["api usage", "api key", "api"]):
        return "api_usage_clarification"
    if any(k in text for k in MARKETING_KEYWORDS):
        return "marketing"
    return "unknown"
