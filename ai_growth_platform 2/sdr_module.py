"""AI SDR: synthetic lead enrichment, BANT scoring and approval-first outreach drafts."""
import hashlib
import random
import re
from typing import Dict, List

from utils import clamp, llm_generate

FIRST_NAMES = ["Ava", "Liam", "Maya", "Noah", "Sofia", "Arjun", "Emma", "Daniel", "Priya", "Ethan"]
LAST_NAMES = ["Sharma", "Chen", "Patel", "Brown", "Singh", "Martin", "Wilson", "Gupta", "Taylor", "Lee"]
TITLES = ["VP Sales", "Head of Revenue", "Director of Growth", "RevOps Lead", "VP Business Development",
          "Chief Revenue Officer"]
SIGNALS = ["pricing page visit", "demo request", "content engagement", "new funding", "team expansion"]
DEFAULT_PAINS = ["pipeline efficiency", "data fragmentation"]
BANT_MAX = 25
SECTIONS = ("EMAIL_1", "EMAIL_2", "EMAIL_3", "LINKEDIN")


def _slug(text: str, limit: int = 20) -> str:
    return "".join(ch.lower() for ch in text if ch.isalnum())[:limit] or "company"


def split_pains(pain_points: str) -> List[str]:
    return [p.strip() for p in re.split(r"[;,]", pain_points or "") if p.strip()]


def synthesize_leads(company, industry, persona, geography, pain_points, count=6) -> List[Dict]:
    """Deterministic synthetic enrichment simulator. No real scraping or external lookups.

    Same inputs always give the same leads, and emails are unique within a batch.
    Emails use the reserved .example TLD so they can never reach a real mailbox.
    """
    count = int(clamp(count, 1, 50))
    key = f"{company}|{industry}|{persona}|{geography}"
    rng = random.Random(int(hashlib.sha256(key.encode()).hexdigest(), 16) % (2 ** 32))
    pains = split_pains(pain_points) or DEFAULT_PAINS
    domain = _slug(company)

    leads, seen = [], set()
    attempts = 0
    while len(leads) < count and attempts < count * 20:
        attempts += 1
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        email = f"{first.lower()}.{last.lower()}@{domain}.example"
        if email in seen:
            continue
        seen.add(email)
        i = len(leads)
        leads.append({
            "name": f"{first} {last}",
            "email": email,
            "title": persona if i % 2 == 0 and persona else rng.choice(TITLES),
            "company": company,
            "industry": industry,
            "geography": geography,
            "pain_points": "; ".join(rng.sample(pains, k=min(2, len(pains)))),
            "buying_signal": rng.choice(SIGNALS),
            "source": "synthetic",
        })
    return leads


def score_bant(budget, authority, need, timing) -> int:
    """Each dimension is clamped to 0-25; the total is therefore 0-100."""
    return int(sum(int(clamp(v, 0, BANT_MAX)) for v in (budget, authority, need, timing)))


def qualification_label(score) -> str:
    if score >= 75:
        return "SQL"
    if score >= 55:
        return "MQL"
    if score >= 35:
        return "Nurture"
    return "Disqualified"


_MARKER = re.compile(r"(?im)^[\s#*_]*(EMAIL_[123]|LINKEDIN)[\s*_]*[:\-]?[\s*_]*")


def parse_sequence(text: str):
    """Split an LLM reply into EMAIL_1..3 + LINKEDIN. Returns None if any section is missing."""
    matches = list(_MARKER.finditer(text or ""))
    found = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        found[m.group(1).upper()] = text[m.end():end].strip()
    if all(found.get(k) for k in SECTIONS):
        return {
            "email_step_1": found["EMAIL_1"], "email_step_2": found["EMAIL_2"],
            "email_step_3": found["EMAIL_3"], "linkedin": found["LINKEDIN"],
        }
    return None


def _template_sequence(lead: dict, pain_points: str, sender: str) -> dict:
    first = lead["name"].split()[0]
    main = (split_pains(pain_points) or DEFAULT_PAINS)[0]
    return {
        "email_step_1": (
            f"Subject: A thought on {main}\n\nHi {first},\n\n"
            f"I'm reaching out because {lead['company']} may be working on {pain_points or main}. "
            "If this is a current priority, I can share a short overview of how teams use automation "
            f"to reduce manual work.\n\nBest,\n{sender}"
        ),
        "email_step_2": (
            f"Hi {first},\n\nFollowing up on my note about {main}. Would a 15-minute conversation be "
            f"useful, or is someone else on your team closer to this?\n\nBest,\n{sender}"
        ),
        "email_step_3": (
            f"Hi {first},\n\nI'll close the loop here. If improving {main} becomes a priority, "
            f"I'd be happy to send a concise overview.\n\nBest,\n{sender}"
        ),
        "linkedin": (
            f"Hi {first}, I noticed your role at {lead['company']} and I'm exploring how teams "
            f"approach {main}. Happy to connect."
        ),
    }


def generate_outreach(lead: dict, pain_points: str, client=None, provider="Template / Local",
                      sender_name: str = "[Your name]") -> dict:
    """Return 4 draft messages plus 'source' ('llm'|'template') and 'error' (str|None)."""
    prompt = (
        "Create a concise 3-step B2B outbound sequence and a LinkedIn connection message.\n"
        f"Prospect: {lead['name']}, {lead['title']} at {lead['company']}.\n"
        f"Pain points: {pain_points}.\n"
        "Do not invent customer results, pricing, or facts.\n"
        "Return exactly four sections, each starting on its own line with the label "
        "EMAIL_1:, EMAIL_2:, EMAIL_3:, LINKEDIN:"
    )
    text, error = llm_generate(
        provider, client,
        "You are an SDR copy assistant. Be factual, concise, personalized, and non-deceptive.",
        prompt,
    )
    if text:
        parsed = parse_sequence(text)
        if parsed:
            return {**parsed, "source": "llm", "error": None}
        error = "The model reply could not be split into the 4 sections; used templates instead."

    return {**_template_sequence(lead, pain_points, sender_name), "source": "template", "error": error}
