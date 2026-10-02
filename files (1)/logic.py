"""SDR agent logic. Deterministic and explainable: scores are heuristic and labelled calibrated=false."""
import hashlib, json, re
from datetime import timedelta
from statistics import mean
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import now
from . import providers as P
from .models import SdrAccount, SdrCampaign, SdrContact, SdrOutcome, SdrTouch

GEO = {"north america": ["US", "CA"], "united states": ["US"], "usa": ["US"], "canada": ["CA"], "india": ["IN"],
       "europe": ["GB", "DE", "FR"], "united kingdom": ["GB"], "uk": ["GB"], "germany": ["DE"], "france": ["FR"]}
IND = {"saas": "Software / SaaS", "software": "Software / SaaS", "fintech": "Fintech", "healthcare": "Healthcare", "retail": "Retail",
       "e-commerce": "E-commerce", "ecommerce": "E-commerce", "manufacturing": "Manufacturing", "financial services": "Financial Services"}
PERSONA = {"revops": ["Revenue Operations", "RevOps", "Sales Operations"], "revenue operations": ["Revenue Operations", "RevOps", "Sales Operations"],
           "sales operations": ["Sales Operations", "Revenue Operations"], "cro": ["Chief Revenue Officer", "CRO", "VP Sales"],
           "vp sales": ["VP Sales", "Head of Sales"], "founder": ["Founder", "CEO"], "e-commerce head": ["Head of E-commerce", "E-commerce"]}
SUGGEST = {"Software / SaaS": ["RevOps", "CRO"], "Fintech": ["CRO", "VP Sales"], "E-commerce": ["E-commerce Head", "Founder"], "Retail": ["RevOps"]}
W_DEFAULT = {"industry": 0.4, "geography": 0.2, "size": 0.25, "tech": 0.15}
clamp = lambda x, hi=0.95: round(max(0.0, min(hi, x)), 3)


# ---------------- ICP
def _ci(v): return [str(x).strip() for x in (v or []) if str(x).strip()]


def normalize_icp(d: dict) -> dict:
    prompt, low = d.get("prompt") or "", (d.get("prompt") or "").lower()
    ind = [IND.get(i.lower(), i.strip().title()) for i in _ci(d.get("industries"))]
    geo = [g.upper() if len(g) == 2 else None for g in _ci(d.get("geographies"))]
    geo = [g for g in geo if g] + [c for g in _ci(d.get("geographies")) if len(g) != 2 for c in GEO.get(g.lower(), [])]
    pers = _ci(d.get("personas"))
    for k, v in IND.items():
        if re.search(rf"\b{re.escape(k)}\b", low) and v not in ind:
            ind.append(v)
    for k, v in GEO.items():
        hit = re.search(r"\b(US|USA)\b", prompt) if k == "usa" else re.search(rf"\b{re.escape(k)}\b", low)
        geo += [c for c in v if hit and c not in geo]
    if re.search(r"\bUS\b", prompt) and "US" not in geo:
        geo.append("US")
    for k in PERSONA:
        if re.search(rf"\b{re.escape(k)}\b", low) and k not in [p.lower() for p in pers]:
            pers.append(k)
    lo, hi = d.get("employee_min"), d.get("employee_max")
    m = re.search(r"(\d[\d,]*)\s*(?:–|-|to)\s*(\d[\d,]*)\s*employees", prompt)
    if m and lo is None and hi is None:
        lo, hi = int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))
    titles = []
    for p in pers:
        for t in PERSONA.get(p.lower(), [p]):
            if t not in titles:
                titles.append(t)
    return {"industries": list(dict.fromkeys(ind)), "geographies": list(dict.fromkeys(geo)), "employee_min": lo, "employee_max": hi,
            "personas": pers, "persona_titles": titles, "tech": _ci(d.get("tech")), "pain_points": _ci(d.get("pain_points")),
            "signals": _ci(d.get("signals")),
            "exclusions": {"industries": _ci((d.get("exclusions") or {}).get("industries")), "domains": [x.lower() for x in _ci((d.get("exclusions") or {}).get("domains"))]},
            "weights": d.get("weights") or dict(W_DEFAULT)}


def validate_icp(d: dict) -> dict:
    n, errors, warnings = normalize_icp(d), [], []
    if not n["industries"] and not n["geographies"]:
        errors.append("Provide at least one industry or geography (in the prompt or as filters)")
    if n["employee_min"] is not None and n["employee_max"] is not None and n["employee_min"] > n["employee_max"]:
        errors.append("employee_min must be <= employee_max")
    if abs(sum(n["weights"].values()) - 1) > 0.01:
        errors.append("Scoring weights must sum to 1")
    if not n["personas"]:
        warnings.append("No buyer personas given; contacts will be scored on seniority only")
    return {"valid": not errors, "errors": errors, "warnings": warnings, "normalized": n}


def suggest_icp(d: dict) -> dict:
    n = normalize_icp(d)
    personas = [p for i in n["industries"] for p in SUGGEST.get(i, [])]
    return {"mode": "deterministic", "personas": list(dict.fromkeys(personas)) or ["RevOps", "CRO"],
            "pain_points": ["Manual prospect research", "Low outbound reply rates", "Poor lead prioritization"],
            "exclusions": ["Competitors", "Existing customers"]}


# ---------------- scoring
def fit_score(a: dict, icp: dict) -> tuple[float, list[str]]:
    w, s, why = icp["weights"], 0.0, []
    if not icp["industries"] or a.get("industry") in icp["industries"]:
        s += w["industry"]; why.append("industry match")
    if not icp["geographies"] or a.get("country") in icp["geographies"]:
        s += w["geography"]; why.append("geography match")
    e, lo, hi = a.get("employees"), icp["employee_min"], icp["employee_max"]
    if lo is None and hi is None:
        s += w["size"]
    elif e is None:
        s += w["size"] * 0.5
    elif (lo or 0) <= e <= (hi if hi is not None else 10**9):
        s += w["size"]; why.append("company size in range")
    elif (lo or 0) * 0.5 <= e <= (hi or 10**9) * 1.5:
        s += w["size"] * 0.4; why.append("company size near range")
    t = {x.lower() for x in icp["tech"]}
    ov = t & {x.lower() for x in a.get("tech", [])}
    if not t or ov:
        s += w["tech"] * (len(ov) / len(t) if t else 1); why.append("tech overlap" if t else "no tech filter")
    return round(100 * s, 1), why


def excluded(a: dict, icp: dict) -> bool:
    """Structured filters are hard constraints. Apollo industry labels are free text, so Apollo rows rely on the search query for industry."""
    if a.get("industry") in icp["exclusions"]["industries"] or a.get("domain", "").lower() in icp["exclusions"]["domains"]:
        return True
    if icp["industries"] and a.get("source") != "apollo" and a.get("industry") not in icp["industries"]:
        return True
    return bool(icp["geographies"] and a.get("country") and a["country"] not in icp["geographies"])


def persona_score(title: str, icp: dict) -> float:
    tl = (title or "").lower()
    if any(p.lower() in tl for p in icp["persona_titles"]):
        return 100.0
    if re.search(r"\b(vp|head|director|chief|founder|ceo|cro|partner)\b", tl):
        return 60.0
    return 35.0 if "manager" in tl else 15.0


def discover_store(db: Session, icp_row, limit: int, min_fit: float = 0) -> tuple[list[SdrAccount], str]:
    icp = icp_row.data
    rows, provider = P.discover(icp, max(limit, 1) * 3)
    out = []
    for r in rows:
        if excluded(r, icp):
            continue
        fs, why = fit_score(r, icp)
        if fs < min_fit:
            continue
        acc = db.scalars(select(SdrAccount).where(SdrAccount.icp_id == icp_row.id, SdrAccount.domain == r["domain"])).first()
        data = {k: v for k, v in r.items() if k != "contacts"} | {"fit_reasons": why}
        if acc:
            acc.fit_score, acc.data = fs, data
        else:
            acc = SdrAccount(icp_id=icp_row.id, domain=r["domain"], name=r["name"], source=r["source"], fit_score=fs, data=data)
            db.add(acc); db.flush()
        for c in r["contacts"]:
            exists = db.scalars(select(SdrContact.id).where(SdrContact.account_id == acc.id, SdrContact.name == c["name"])).first()
            if not exists:
                db.add(SdrContact(account_id=acc.id, name=c["name"], title=c["title"], email=c.get("email"), persona_match=persona_score(c["title"], icp)))
        out.append(acc)
    db.flush()
    return sorted(out, key=lambda a: -a.fit_score)[:limit], provider


# ---------------- enrichment (deterministic, evidence only from the discovery record)
def enrich(acc: SdrAccount, icp: dict) -> dict:
    a, sig = acc.data, []
    if a.get("tech"):
        sig.append({"type": "tech_stack", "label": "Uses " + ", ".join(a["tech"][:4]), "confidence": 0.8})
    if a.get("revenue_usd"):
        sig.append({"type": "revenue_scale", "label": f"Estimated revenue ${a['revenue_usd'] / 1e6:.0f}M", "confidence": 0.7})
    if a.get("employees"):
        sig.append({"type": "headcount", "label": f"About {a['employees']} employees", "confidence": 0.7})
    desc = (a.get("description") or "").lower()
    for s in icp["signals"]:
        if s.lower() in desc:
            sig.append({"type": "buying_signal", "label": s, "confidence": 0.6})
    for k in ("hiring", "scaling", "expanding", "looking for"):
        if k in desc and not any(x["label"] == k for x in sig):
            sig.append({"type": "buying_signal", "label": k, "confidence": 0.5})
    for x in sig:
        x["source"] = acc.source
    pains = [{"pain": p, "confidence": 0.5 if any(w in desc for w in p.lower().split()[:2]) else 0.3, "kind": "hypothesis"} for p in icp["pain_points"]]
    return {"mode": "deterministic", "summary": f"{acc.name} is a {a.get('industry') or 'company'} business in {a.get('country') or 'n/a'}. {a.get('description', '')}".strip(),
            "signals": sig, "pain_point_hypotheses": pains, "citations": [{"source": acc.source, "ref": acc.domain}], "generated_at": now().isoformat()}


# ---------------- prospect intelligence (heuristic)
CANON = {"tech_stack": "tech_fit", "revenue_scale": "budget_capacity", "headcount": "scale", "buying_signal": "intent"}


def analyze(acc: SdrAccount, c: SdrContact, icp: dict) -> dict:
    enr = acc.enrichment or enrich(acc, icp)
    fit, per = acc.fit_score, c.persona_match
    sigs = [{**s, "canonical": CANON.get(s["type"], s["type"])} for s in enr["signals"]]
    n_int = sum(1 for s in sigs if s["canonical"] == "intent")
    intent = clamp(0.1 + 0.35 * fit / 100 + 0.15 * n_int)
    reply = clamp(0.03 + 0.25 * fit / 100 * per / 100 + 0.1 * intent)
    first, tech = c.name.split()[0], (acc.data.get("tech") or [])
    fact = f"your {tech[0]} setup" if tech else f"{acc.name}'s growth"
    angle = f"Helping {c.title or 'your team'} at {acc.name} prioritize the accounts most likely to reply"
    return {"mode": "heuristic", "calibrated": False, "signals": sigs,
            "probabilities": {"intent": intent, "reply": reply, "meeting": clamp(0.4 * reply + 0.05 * intent),
                              "qualification": clamp(0.1 + 0.4 * fit / 100 + 0.25 * per / 100 + 0.1 * intent)},
            "priority_score": round(0.4 * fit + 0.3 * per + 30 * intent, 1),
            "explanation": f"fit {fit}, persona {per}, {n_int} intent signal(s); weights are hand-set until models are trained on outcomes",
            "personalization": {"email_angle": angle, "linkedin": f"Hi {first}, saw {fact}. Worth comparing notes on outbound prioritization?",
                                "call_opener": f"Hi {first}, quick question about how {acc.name} decides which accounts to call first."}}


# ---------------- qualification (BANT + MEDDIC evidence)
def qualify(acc: SdrAccount, c: SdrContact, icp: dict) -> dict:
    it, a, per = c.intel, acc.data, c.persona_match
    sigs = it["signals"]
    intent_n = sum(1 for s in sigs if s["canonical"] == "intent")
    tech_ov = bool({t.lower() for t in icp["tech"]} & {t.lower() for t in a.get("tech", [])})
    rev, senior = a.get("revenue_usd") or 0, bool(re.search(r"\b(chief|vp|head|founder|ceo|cro|partner)\b", (c.title or "").lower()))
    pains = (acc.enrichment or {}).get("pain_point_hypotheses", [])
    bant = {"budget": (1 if rev >= 10e6 else 0.5 if rev >= 2e6 else 0, f"revenue ~${rev / 1e6:.0f}M" if rev else "revenue unknown"),
            "authority": (1 if per >= 100 else 0.5 if per >= 60 else 0, f"title '{c.title}'"),
            "need": (1 if intent_n or tech_ov else 0.5 if "industry match" in a.get("fit_reasons", []) else 0, "buying signal / tech overlap / industry fit"),
            "timeline": (1 if intent_n else 0, "intent signal" if intent_n else "no timeline evidence")}
    meddic = {"metrics": (0.5 if rev else 0, "firmographics only, no quantified impact"),
              "economic_buyer": (1 if per >= 100 and senior else 0, f"title '{c.title}'"),
              "decision_criteria": (0.5 if tech_ov else 0, "tech stack overlap" if tech_ov else "unknown"),
              "decision_process": (0, "not evidenced"),
              "identified_pain": (1 if any(p["confidence"] >= 0.5 for p in pains) else 0.5 if pains else 0, "pain hypotheses"),
              "champion": (0, "no engagement yet")}
    cov = mean([v[0] for v in bant.values()] + [v[0] for v in meddic.values()])
    prob = it["probabilities"]["qualification"]
    score = round(0.5 * prob + 0.5 * cov, 3)
    decision = "SQL" if score >= 0.7 else "MQL" if score >= 0.5 else "Nurture" if score >= 0.3 else "Disqualified"
    fmt = lambda d: {k: {"score": v[0], "evidence": v[1]} for k, v in d.items()}
    missing = [f"{fw}: {k}" for fw, d in (("BANT", bant), ("MEDDIC", meddic)) for k, v in d.items() if v[0] == 0]
    nxt = {"SQL": "Review and approve outreach", "MQL": "Review and approve outreach", "Nurture": "Hold and re-evaluate when new signals appear",
           "Disqualified": "No outreach"}[decision]
    return {"decision": decision, "score": score, "ml_probability": prob, "framework_coverage": round(cov, 3), "bant": fmt(bant),
            "meddic": fmt(meddic), "missing_information": missing, "next_action": nxt, "mode": "heuristic"}


# ---------------- outreach
ACTIVE = ("pending_approval", "approved", "sent", "paused", "replied")


def compose(acc: SdrAccount, c: SdrContact) -> list[dict]:
    p, first = c.intel["personalization"], c.name.split()[0]
    msgs = []
    if c.email:
        msgs.append({"channel": "email", "type": "send", "subject": f"{acc.name}: {p['email_angle'][:60]}",
                     "body": f"Hi {first},\n\n{p['email_angle']}. {acc.enrichment['signals'][0]['label'] if acc.enrichment and acc.enrichment['signals'] else ''}.\n\n"
                             f"Open to a 20-minute call next week?\n\nReply 'unsubscribe' and I will not contact you again."})
    msgs.append({"channel": "linkedin", "type": "human_task", "body": p["linkedin"]})
    msgs.append({"channel": "phone", "type": "human_task", "body": p["call_opener"]})
    return msgs


def create_campaign(db: Session, acc: SdrAccount, c: SdrContact) -> tuple[SdrCampaign, bool]:
    key = hashlib.sha1(json.dumps([c.intel["probabilities"], c.intel["priority_score"]], sort_keys=True).encode()).hexdigest()
    ex = db.scalars(select(SdrCampaign).where(SdrCampaign.contact_id == c.id, SdrCampaign.status.in_(ACTIVE))).first()
    if ex:
        return ex, False
    camp = SdrCampaign(contact_id=c.id, snapshot_key=key, messages=compose(acc, c), provider=P.env("SDR_OUTREACH_PROVIDER", "dry_run"))
    db.add(camp); db.flush()
    return camp, True


def stop_touches(db: Session, campaign_id: int, reason: str) -> int:
    n = 0
    for t in db.scalars(select(SdrTouch).where(SdrTouch.campaign_id == campaign_id, SdrTouch.status.in_(("pending_approval", "approved")))):
        t.status, t.stop_reason, n = "stopped", reason, n + 1
    return n


def outcome(db: Session, contact_id: int, kind: str) -> None:
    if not db.scalars(select(SdrOutcome.id).where(SdrOutcome.contact_id == contact_id, SdrOutcome.kind == kind)).first():
        db.add(SdrOutcome(contact_id=contact_id, kind=kind))


# ---------------- conversation
RULES = [("unsubscribe", r"unsubscribe|remove me|stop (emailing|contacting)|opt.?out"), ("out_of_office", r"out of (the )?office|auto.?reply|on leave|vacation"),
         ("wrong_person", r"wrong person|not the right person|no longer (at|with)|left the company"),
         ("meeting_request", r"(book|schedule|set up|grab|arrange).{0,25}(call|meeting|demo|time)|can we (meet|talk)|calendar|available (on|next|tomorrow)"),
         ("pricing_request", r"pric(e|ing)|\bcost\b|quote|how much"), ("objection", r"not interested|already (use|have)|too expensive|no budget|happy with"),
         ("not_now", r"not now|next quarter|later|circle back|reach out (in|again)"), ("interested", r"interested|sounds good|tell me more|love to learn|\byes\b")]
DRAFT = {"meeting_request": "Great, thanks {n}. Does a 30-minute call work? I can send times that suit you.",
         "interested": "Thanks {n}, glad it resonates. Happy to share a short walkthrough. Would a quick call help?",
         "pricing_request": "Thanks {n}. Pricing depends on team size; a short call lets me give you an accurate quote.",
         "objection": "Understood {n}, thanks for the candour. If anything changes I'm glad to help.",
         "not_now": "No problem {n}. I'll check back later. Tell me a better time if you prefer.",
         "wrong_person": "Thanks {n}. Could you point me to the right person? I appreciate it.",
         "ambiguous": "Thanks {n}. Could you tell me a little more about what would be most useful?"}


def classify(text: str) -> str:
    t = text.lower()
    return next((k for k, rx in RULES if re.search(rx, t)), "ambiguous")
