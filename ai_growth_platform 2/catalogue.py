"""Single source of truth for the product catalogue and behavioural event weights.

Aligned with the supplied MarketingReco notebook (15 products, event-weight concept).
Both the database seeder and the recommendation engine import from here so the
two can never drift apart.
"""

PRODUCT_CATALOGUE = [
    {"product_id": "P001", "name": "Starter Package", "category": "onboarding", "price_tier": "low", "tags": {"new", "website", "content"}},
    {"product_id": "P002", "name": "Welcome Offer", "category": "onboarding", "price_tier": "low", "tags": {"new", "offer", "conversion"}},
    {"product_id": "P003", "name": "Free Trial Upgrade", "category": "conversion", "price_tier": "medium", "tags": {"trial", "demo", "conversion"}},
    {"product_id": "P004", "name": "Smart Watch", "category": "electronics", "price_tier": "medium", "tags": {"electronics", "wearable", "interest"}},
    {"product_id": "P005", "name": "Wireless Earbuds", "category": "electronics", "price_tier": "medium", "tags": {"electronics", "audio", "interest"}},
    {"product_id": "P006", "name": "Premium Subscription", "category": "subscription", "price_tier": "high", "tags": {"subscription", "premium", "engagement"}},
    {"product_id": "P007", "name": "Premium Membership", "category": "vip", "price_tier": "high", "tags": {"vip", "loyalty", "premium"}},
    {"product_id": "P008", "name": "Executive Package", "category": "vip", "price_tier": "high", "tags": {"vip", "enterprise", "premium"}},
    {"product_id": "P009", "name": "Priority Support", "category": "service", "price_tier": "high", "tags": {"support", "vip", "service"}},
    {"product_id": "P010", "name": "20% Discount Bundle", "category": "retention", "price_tier": "medium", "tags": {"retention", "discount", "churn"}},
    {"product_id": "P011", "name": "Reactivation Offer", "category": "retention", "price_tier": "low", "tags": {"retention", "reactivation", "churn"}},
    {"product_id": "P012", "name": "Loyalty Rewards", "category": "retention", "price_tier": "medium", "tags": {"retention", "loyalty", "reward"}},
    {"product_id": "P013", "name": "Pro Analytics Add-on", "category": "upsell", "price_tier": "high", "tags": {"upsell", "analytics", "active"}},
    {"product_id": "P014", "name": "Team Plan", "category": "upsell", "price_tier": "high", "tags": {"upsell", "team", "business"}},
    {"product_id": "P015", "name": "Enterprise Solution", "category": "enterprise", "price_tier": "high", "tags": {"enterprise", "scale", "vip"}},
]

PRODUCT_IDS = [p["product_id"] for p in PRODUCT_CATALOGUE]
PRODUCTS_BY_ID = {p["product_id"]: p for p in PRODUCT_CATALOGUE}

EVENT_WEIGHTS = {
    "purchase_completed": 1.0, "purchase_intent": 0.9, "demo_request": 0.85,
    "cart_abandonment": 0.75, "form_submission": 0.70, "pricing_page_visit": 0.65,
    "add_to_wishlist": 0.60, "product_interest": 0.55, "cta_click": 0.50,
    "email_click": 0.45, "email_open": 0.35,
}

SEGMENTS = ["silver", "gold", "platinum"]
LIFECYCLES = ["new", "active", "vip", "at_risk", "dormant"]
