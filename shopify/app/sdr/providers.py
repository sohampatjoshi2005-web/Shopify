"""Provider adapters. Defaults are safe: mock discovery, dry-run email, mock calendar, dry-run CRM."""
import hashlib, os, time
from collections import deque
import requests


class ProviderError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status, self.msg = status, msg


def env(k: str, d: str = "") -> str:
    return os.getenv(k, d)


COUNTRY = {"united states": "US", "canada": "CA", "india": "IN", "united kingdom": "GB", "germany": "DE", "france": "FR"}

# Explicit demo fixture (.example domains, never real people). Used only when provider=mock.
_FIX = [
    ("Northwind Cloud", "northwind-cloud.example", "Software / SaaS", "US", 850, 60_000_000, ["Salesforce", "HubSpot", "Snowflake"],
     "B2B SaaS revenue platform; scaling outbound and hiring sales operations.", [("Ava Reed", "VP Revenue Operations"), ("Liam Cho", "Chief Revenue Officer")]),
    ("Brightpath Analytics", "brightpath.example", "Software / SaaS", "CA", 320, 22_000_000, ["HubSpot", "AWS"],
     "Analytics SaaS for mid-market teams.", [("Maya Singh", "Head of Sales Operations"), ("Noah Park", "Marketing Manager")]),
    ("Malabar Pay", "malabarpay.example", "Fintech", "IN", 410, 18_000_000, ["Razorpay", "AWS"],
     "Payments fintech expanding across South India.", [("Anil Menon", "Chief Revenue Officer")]),
    ("Helix Health Systems", "helixhealth.example", "Healthcare", "US", 2400, 300_000_000, ["Epic", "Azure"],
     "Hospital software and services.", [("Grace Lin", "VP Sales")]),
    ("Stackwise", "stackwise.example", "Software / SaaS", "GB", 150, 9_000_000, ["Salesforce"],
     "Developer tooling startup.", [("Oliver Hart", "Founder")]),
    ("Orbit Retail", "orbitretail.example", "Retail", "US", 5200, 900_000_000, ["Shopify", "SAP"],
     "National retail chain with an e-commerce arm.", [("Zoe Adams", "Director of Revenue Operations")]),
    ("Cedar Manufacturing", "cedarmfg.example", "Manufacturing", "DE", 1800, 210_000_000, ["SAP"],
     "Industrial components manufacturer.", [("Lukas Brandt", "Head of Sales")]),
    ("Lumen Commerce", "lumencommerce.example", "E-commerce", "IN", 260, 12_000_000, ["Shopify", "Razorpay"],
     "Shopify-based D2C brand group looking for new sales channels and marketplaces.", [("Priya Nair", "Head of E-commerce"), ("Rahul Das", "Founder")]),
    ("Tidal Software", "tidalsoft.example", "Software / SaaS", "US", 4300, 520_000_000, ["Salesforce", "Outreach"],
     "Enterprise SaaS with a large sales team.", [("Ethan Cole", "VP Revenue Operations"), ("Sara Kim", "Sales Operations Manager")]),
    ("Fable Ventures", "fableventures.example", "Financial Services", "US", 45, 5_000_000, [],
     "Boutique investment firm.", [("Ivy Stone", "Partner")]),
]


def _mock_rows() -> list[dict]:
    return [dict(name=n, domain=d, industry=i, country=c, employees=e, revenue_usd=r, tech=t, description=desc, source="mock_fixture",
                 contacts=[dict(name=cn, title=ct, email=f"{cn.split()[0].lower()}@{d}") for cn, ct in cs])
            for n, d, i, c, e, r, t, desc, cs in _FIX]


def _apollo_check(r: requests.Response) -> None:
    if r.status_code == 422:
        raise ProviderError(502, "Apollo returned 422: insufficient credits. Add credits or switch SDR_DISCOVERY_PROVIDER.")
    if r.status_code == 403:
        raise ProviderError(502, "Apollo returned 403: this API key or plan has no access to that endpoint.")
    if r.status_code in (401, 429):
        raise ProviderError(502, f"Apollo returned {r.status_code}: check the API key / rate limits.")
    if r.status_code >= 400:
        raise ProviderError(502, f"Apollo returned {r.status_code}.")


def apollo_discover(icp: dict, limit: int) -> list[dict]:
    key = env("SDR_DISCOVERY_APOLLO_API_KEY")
    if not key:
        raise ProviderError(503, "SDR_DISCOVERY_APOLLO_API_KEY is not set")
    base, h = env("SDR_DISCOVERY_APOLLO_BASE_URL", "https://api.apollo.io").rstrip("/"), {"x-api-key": key, "Content-Type": "application/json"}
    body = {"page": 1, "per_page": min(limit, 50), "q_organization_keyword_tags": icp.get("industries", []),
            "organization_locations": icp.get("geographies", [])}
    if icp.get("employee_min") is not None and icp.get("employee_max") is not None:
        body["organization_num_employees_ranges"] = [f"{icp['employee_min']},{icp['employee_max']}"]
    r = requests.post(f"{base}/api/v1/mixed_companies/search", json=body, headers=h, timeout=20)
    _apollo_check(r)
    j = r.json()
    orgs = [o for o in (j.get("organizations") or j.get("accounts") or []) if o.get("primary_domain") or o.get("domain")][:limit]
    rows = {}
    for o in orgs:
        d = o.get("primary_domain") or o.get("domain")
        rows[d] = dict(name=o.get("name", d), domain=d, industry=(o.get("industry") or "").title(),
                       country=COUNTRY.get((o.get("country") or "").lower(), (o.get("country") or "")[:2].upper()),
                       employees=o.get("estimated_num_employees"), revenue_usd=o.get("annual_revenue"),
                       tech=o.get("technology_names") or [], description=o.get("short_description") or "", source="apollo", contacts=[])
    if rows and icp.get("persona_titles"):
        pr = requests.post(f"{base}/api/v1/mixed_people/api_search", headers=h, timeout=20,
                           json={"q_organization_domains_list": list(rows), "person_titles": icp["persona_titles"], "per_page": 50})
        _apollo_check(pr)
        for p in pr.json().get("people", []):
            d = (p.get("organization") or {}).get("primary_domain")
            em = p.get("email")
            if d in rows:
                rows[d]["contacts"].append(dict(name=p.get("name") or "Unknown", title=p.get("title") or "",
                                                email=None if not em or "not_unlocked" in em else em))
    return list(rows.values())


def discover(icp: dict, limit: int) -> tuple[list[dict], str]:
    if env("SDR_DISCOVERY_PROVIDER", "mock") == "apollo_api":
        try:
            return apollo_discover(icp, limit), "apollo"
        except (ProviderError, requests.RequestException) as e:
            if env("SDR_DISCOVERY_APOLLO_ALLOW_PUBLIC_FALLBACK").lower() == "true":
                return _mock_rows(), "mock_fixture (apollo fallback)"
            raise e if isinstance(e, ProviderError) else ProviderError(502, "Apollo request failed")
    return _mock_rows(), "mock_fixture"


_sent: deque = deque()


def _rate() -> None:
    lim, t = int(env("SDR_OUTREACH_RATE_PER_MIN", "30")), time.time()
    while _sent and _sent[0] < t - 60:
        _sent.popleft()
    if len(_sent) >= lim:
        raise ProviderError(429, "Outreach rate limit reached, try again in a minute")
    _sent.append(t)


def send_email(to: str, subject: str, body: str, idem: str) -> dict:
    _rate()
    if env("SDR_OUTREACH_PROVIDER", "dry_run") == "brevo":
        key, frm = env("SDR_OUTREACH_BREVO_API_KEY"), env("SDR_OUTREACH_FROM_EMAIL")
        if not key or not frm:
            raise ProviderError(503, "Brevo is not configured")
        payload = {"sender": {"name": env("SDR_OUTREACH_SENDER_NAME", "SDR Team"), "email": frm}, "to": [{"email": to}],
                   "subject": subject, "textContent": body}
        if env("SDR_OUTREACH_REPLY_TO_EMAIL"):
            payload["replyTo"] = {"email": env("SDR_OUTREACH_REPLY_TO_EMAIL")}
        r = requests.post("https://api.brevo.com/v3/smtp/email", json=payload, headers={"api-key": key}, timeout=15)
        if r.status_code >= 400:
            raise ProviderError(502, f"Brevo rejected the send ({r.status_code})")
        return {"id": r.json().get("messageId", ""), "provider": "brevo"}
    return {"id": "dry_" + hashlib.sha1(f"{to}|{subject}|{idem}".encode()).hexdigest()[:12], "provider": "dry_run"}


def create_event(meeting_id: int) -> dict:
    """Mock calendar: deterministic id per meeting (idempotent), fake meet URL on a .example host."""
    eid = hashlib.sha1(f"evt-{meeting_id}".encode()).hexdigest()[:16]
    return {"event_id": eid, "meet_url": f"https://meet.example/{eid}"}


def crm_upsert(meeting_id: int, company: dict, person: dict) -> dict:
    h = lambda k: hashlib.sha1(f"{k}-{meeting_id}".encode()).hexdigest()[:10]
    return {"company": {"id": "co_" + h(company["domain"]), **company}, "person": {"id": "pe_" + h(person["name"]), **person},
            "opportunity": {"id": "op_" + h("opp"), "stage": "meeting_booked"}}


def provider_status() -> dict:
    return {"discovery": env("SDR_DISCOVERY_PROVIDER", "mock"), "outreach": env("SDR_OUTREACH_PROVIDER", "dry_run"),
            "meeting": "mock_calendar", "crm": "dry_run", "llm": "none (deterministic)", "metarank": "local fallback"}
