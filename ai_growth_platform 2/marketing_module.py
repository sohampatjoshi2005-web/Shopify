"""Marketing engine: channel copy, heuristic campaign forecasts, lifecycle messaging."""
import re
from typing import Dict, List

from utils import clamp, llm_generate

CONTENT_CHANNELS = ["Facebook / Instagram Ads", "Google Search Ads", "Email Newsletter", "Blog Outline"]
FORECAST_CHANNELS = ["Meta", "Google", "Email", "Organic"]
INTENT_LEVELS = ["Low", "Medium", "High"]
SEGMENTS = ["High-Value", "Churn-Risk", "First-Time Buyers"]

# Heuristic planning benchmarks (NOT measured data): base CTR %, click->conversion %, cost per click $, revenue per conversion $
BENCHMARKS = {
    "Meta":    {"ctr": 1.15, "cvr": 2.0, "cpc": 1.50, "rpc": 180},
    "Google":  {"ctr": 2.25, "cvr": 3.2, "cpc": 2.40, "rpc": 210},
    "Email":   {"ctr": 3.80, "cvr": 2.8, "cpc": 0.70, "rpc": 240},
    "Organic": {"ctr": 1.00, "cvr": 2.4, "cpc": 0.30, "rpc": 200},
}
INTENT_MULT = {"Low": 0.75, "Medium": 1.0, "High": 1.30}

RISKY_CLAIMS = [
    r"guarantee[ds]?", r"risk[- ]free", r"#\s?1\b", r"best (?:in|on) (?:the )?(?:world|market)",
    r"100\s?%", r"miracle", r"clinically proven", r"instant(?:ly)? results?", r"\bcure[sd]?\b",
]


def find_risky_claims(text: str) -> List[str]:
    """Flag phrases that usually need substantiation before publishing."""
    hits = []
    for pattern in RISKY_CLAIMS:
        hits += [m.group(0) for m in re.finditer(pattern, text or "", flags=re.I)]
    return sorted(set(h.strip() for h in hits), key=str.lower)


def _template(channel, product, audience, objective, tone) -> str:
    templates = {
        "Facebook / Instagram Ads": (
            f"Primary text: Discover {product} built for {audience}. "
            f"Designed for a {tone.lower()} experience and focused on {objective.lower()}.\n\n"
            "Headline: Make your next purchase decision easier.\nCTA: Learn More"
        ),
        "Google Search Ads": (
            f"Headline 1: {product}\nHeadline 2: Built for {audience}\n"
            f"Description: Explore a {tone.lower()} option for {objective.lower()}. "
            "See features, pricing, and details."
        ),
        "Email Newsletter": (
            f"Subject: A new option for {audience}\n\n"
            f"Meet {product}. If {objective.lower()} is on your roadmap, this is a practical place to start.\n\n"
            "CTA: Explore the offer"
        ),
        "Blog Outline": (
            f"# {product}: A practical guide for {audience}\n"
            "1. The customer problem\n2. What to evaluate\n3. How the product fits\n"
            "4. Common mistakes\n5. Implementation checklist\n6. CTA"
        ),
    }
    if channel not in templates:
        raise ValueError(f"Unsupported channel: {channel}")
    return templates[channel]


def generate_content(channel, product, audience, objective, tone, client=None, provider="Template / Local") -> Dict:
    """Return {'text', 'source', 'error', 'risky_claims'}."""
    prompt = (
        f"Create {channel} content for:\nProduct: {product}\nAudience: {audience}\n"
        f"Objective: {objective}\nTone: {tone}\nInclude a clear CTA and avoid unsupported claims."
    )
    text, error = llm_generate(
        provider, client,
        "You are an e-commerce marketing copy assistant. Avoid fabricated statistics and misleading claims.",
        prompt,
    )
    source = "llm" if text else "template"
    text = text or _template(channel, product, audience, objective, tone)
    return {"text": text, "source": source, "error": error, "risky_claims": find_risky_claims(text)}


def predict_campaign_performance(budget, demographic, channel, creative_quality, offer_strength) -> Dict:
    """Heuristic forecast. Linear in budget; use for planning comparisons, not as ground truth."""
    if channel not in BENCHMARKS:
        raise ValueError(f"Unsupported channel: {channel}")
    if demographic not in INTENT_MULT:
        raise ValueError(f"Unsupported audience intent: {demographic}")
    budget = max(0.0, float(budget))
    creative_quality = clamp(creative_quality, 1, 10)
    offer_strength = clamp(offer_strength, 1, 10)

    b = BENCHMARKS[channel]
    quality_mult = 0.75 + creative_quality * 0.05
    offer_mult = 0.80 + offer_strength * 0.04
    ctr = max(0.1, b["ctr"] * INTENT_MULT[demographic] * quality_mult)
    cvr = clamp(b["cvr"] * INTENT_MULT[demographic] * quality_mult * offer_mult, 0.2, 20.0)

    clicks = budget / b["cpc"]
    conversions = clicks * cvr / 100
    revenue = conversions * b["rpc"]
    return {
        "ctr": ctr,
        "conversion_rate": cvr,
        "clicks": clicks,
        "conversions": conversions,
        "cpa": (budget / conversions) if conversions else 0.0,
        "revenue": revenue,
        "roi": ((revenue - budget) / budget) if budget else 0.0,
    }


def compare_channels(budget, demographic, creative_quality, offer_strength) -> List[Dict]:
    return [
        {"channel": ch, **predict_campaign_performance(budget, demographic, ch, creative_quality, offer_strength)}
        for ch in FORECAST_CHANNELS
    ]


def segment_message(segment, context, client=None, provider="Template / Local") -> Dict:
    """Return {'text', 'source', 'error', 'risky_claims'}."""
    prompt = f"Write a short customer message for segment {segment}. Context: {context}. Include one CTA."
    text, error = llm_generate(
        provider, client, "You write lifecycle marketing messages that are clear and respectful.", prompt)
    source = "llm" if text else "template"
    if not text:
        if segment == "High-Value":
            text = (f"Thanks for being a valued customer. Based on your recent activity ({context}), here is an "
                    "option selected for customers exploring more advanced capabilities. Explore your personalized offer.")
        elif segment == "Churn-Risk":
            text = (f"We noticed your recent activity ({context}). If something is getting in the way, here is a "
                    "simple way to get more value from your account. View support and reactivation options.")
        else:
            text = (f"Welcome! Based on your recent activity ({context}), here are a few products and resources "
                    "to help you get started. Explore your recommendations.")
    return {"text": text, "source": source, "error": error, "risky_claims": find_risky_claims(text)}
