"""Hybrid recommender: collaborative + content-based + rules, with explanations and diversity."""
from collections import defaultdict
from typing import Dict, List, Optional

import numpy as np

from catalogue import (  # noqa: F401  (PRODUCT_CATALOGUE re-exported for backward compatibility)
    EVENT_WEIGHTS, LIFECYCLES, PRODUCT_CATALOGUE, PRODUCT_IDS, PRODUCTS_BY_ID, SEGMENTS,
)
from database import fetch_interactions
from utils import clamp, safe_float

# Mirrors the supplied notebook's hybrid design (0.4 behavioural / 0.3 content / 0.3 rules).
HYBRID_WEIGHTS = {"collab": 0.4, "content": 0.3, "rules": 0.3}
MAX_PER_CATEGORY = 2


def normalise_profile(profile: dict) -> dict:
    """Validate and clean a customer profile; unknown product IDs are dropped, not fatal."""
    segment = str(profile.get("segment", "silver")).lower()
    lifecycle = str(profile.get("lifecycle", "active")).lower()
    history = list(dict.fromkeys(str(h).strip().upper() for h in profile.get("history", []) if str(h).strip()))
    return {
        "segment": segment if segment in SEGMENTS else "silver",
        "lifecycle": lifecycle if lifecycle in LIFECYCLES else "active",
        "churn_risk": clamp(safe_float(profile.get("churn_risk", 0.0)), 0.0, 1.0),
        "history": [h for h in history if h in PRODUCTS_BY_ID],
        "ignored_ids": [h for h in history if h not in PRODUCTS_BY_ID],
    }


def _interaction_matrix(rows: Optional[List[dict]] = None) -> np.ndarray:
    rows = fetch_interactions() if rows is None else rows
    users = sorted({r["user_id"] for r in rows})
    u_idx = {u: i for i, u in enumerate(users)}
    p_idx = {p: i for i, p in enumerate(PRODUCT_IDS)}
    m = np.zeros((len(users), len(PRODUCT_IDS)))
    for r in rows:
        if r["product_id"] in p_idx:
            m[u_idx[r["user_id"]], p_idx[r["product_id"]]] += float(r["weight"])
    return m


def collaborative_scores(history: List[str], rows: Optional[List[dict]] = None) -> Dict[str, float]:
    """User-based CF via cosine similarity; falls back to popularity on cold start."""
    m = _interaction_matrix(rows)
    if m.size == 0:
        return {}
    target = np.array([1.0 if p in history else 0.0 for p in PRODUCT_IDS])
    seen = target > 0

    sims = np.zeros(m.shape[0])
    t_norm = np.linalg.norm(target)
    row_norms = np.linalg.norm(m, axis=1)
    if t_norm > 0:
        valid = row_norms > 0
        sims[valid] = (m[valid] @ target) / (row_norms[valid] * t_norm)
    scores = sims @ m
    if not scores[~seen].any():  # cold start or no overlap: fall back to overall popularity
        scores = m.sum(axis=0)
    scores = np.where(seen, 0.0, scores)

    top = scores.max()
    return {pid: float(s / top) if top > 0 else 0.0 for pid, s in zip(PRODUCT_IDS, scores)}


def content_scores(history: List[str], profile: dict) -> Dict[str, float]:
    p = normalise_profile({**profile, "history": history})
    history_tags = set()
    for pid in p["history"]:
        history_tags |= PRODUCTS_BY_ID[pid]["tags"]

    scores = {}
    for prod in PRODUCT_CATALOGUE:
        score = len(history_tags & prod["tags"]) / max(1, len(prod["tags"]))
        if p["lifecycle"] == "new" and prod["category"] == "onboarding":
            score += 0.30
        if p["lifecycle"] in ("at_risk", "dormant") and prod["category"] == "retention":
            score += 0.35 + 0.15 * p["churn_risk"]
        if p["segment"] in ("gold", "platinum") and prod["category"] in ("upsell", "vip", "enterprise"):
            score += 0.25
        if p["segment"] == "platinum" and prod["category"] == "vip":
            score += 0.20
        scores[prod["product_id"]] = min(1.0, score)
    return scores


def rule_scores(profile: dict) -> Dict[str, float]:
    p = normalise_profile(profile)
    scores = {pid: 0.0 for pid in PRODUCT_IDS}

    def boost(values):
        for pid, val in values.items():
            scores[pid] = max(scores[pid], val)

    if p["segment"] == "platinum":
        boost({"P007": 1.0, "P008": 0.95, "P015": 0.90, "P009": 0.85})
    if p["segment"] in ("gold", "platinum") and p["lifecycle"] in ("active", "vip"):
        boost({"P013": 0.80, "P014": 0.75, "P006": 0.70})
    if p["lifecycle"] == "new":
        boost({"P001": 0.90, "P002": 0.85, "P003": 0.75})
    if p["lifecycle"] in ("dormant", "at_risk") or p["churn_risk"] > 0.6:
        boost({"P010": 1.0, "P011": 0.90, "P012": 0.80})
    return scores


def _explain(product, collab, content, rule) -> str:
    contributions = {
        "rules": HYBRID_WEIGHTS["rules"] * rule,
        "collab": HYBRID_WEIGHTS["collab"] * collab,
        "content": HYBRID_WEIGHTS["content"] * content,
    }
    top = max(contributions, key=contributions.get)
    if contributions[top] <= 0:
        return "Low-confidence fallback: little customer or product signal available."
    if top == "rules":
        if product["category"] == "retention":
            return "Retention rule: elevated churn risk or at-risk/dormant lifecycle."
        return "Business rule: matches the customer's segment and lifecycle stage."
    if top == "collab":
        return "Behavioural affinity: customers with similar activity engaged with this item."
    return "Content match: product tags and category align with the customer's history and stage."


def get_personalized_recommendations(profile: dict, top_n: int = 5, exclude_history: bool = True,
                                     interaction_rows: Optional[List[dict]] = None) -> List[dict]:
    p = normalise_profile(profile)
    history = p["history"]
    collab = collaborative_scores(history, interaction_rows)
    content = content_scores(history, p)
    rules = rule_scores(p)

    results = []
    for prod in PRODUCT_CATALOGUE:
        pid = prod["product_id"]
        if exclude_history and pid in history:
            continue
        c, t, r = collab.get(pid, 0.0), content.get(pid, 0.0), rules.get(pid, 0.0)
        hybrid = HYBRID_WEIGHTS["collab"] * c + HYBRID_WEIGHTS["content"] * t + HYBRID_WEIGHTS["rules"] * r
        signals = sum(v > 0 for v in (c, t, r))
        results.append({
            **{k: v for k, v in prod.items() if k != "tags"},
            "tags": ", ".join(sorted(prod["tags"])),
            "collab_score": round(c, 3), "content_score": round(t, 3), "rule_score": round(r, 3),
            "hybrid_score": round(hybrid, 4),
            "match_pct": int(min(1.0, hybrid) * 100),
            "confidence": "High" if signals == 3 and hybrid >= 0.5 else "Medium" if signals >= 2 else "Low",
            "reason": _explain(prod, c, t, r),
        })

    results.sort(key=lambda x: (-x["hybrid_score"], x["product_id"]))

    # Diversity filter: cap per category, then backfill if that left us short of top_n.
    counts, picked, leftovers = defaultdict(int), [], []
    for r in results:
        if counts[r["category"]] < MAX_PER_CATEGORY:
            counts[r["category"]] += 1
            picked.append(r)
        else:
            leftovers.append(r)
    picked = (picked + leftovers[: max(0, top_n - len(picked))])[:top_n]
    for i, r in enumerate(picked, 1):
        r["rank"] = i
    return picked
