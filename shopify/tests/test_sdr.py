import os, tempfile, json
from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.sdr import providers as P

h = lambda t: {"Authorization": f"Bearer {t}"}
PROMPT = "We sell a Revenue Operations platform to SaaS companies in North America with 200-5000 employees. Target RevOps and CRO."


@pytest.fixture(scope="module")
def c():
    with TestClient(app) as cl:
        yield cl


@pytest.fixture(scope="module")
def adm(c):
    return h(c.post("/auth/login", json={"email": "admin@shop.dev", "password": "adminpass123"}).json()["access_token"])


@pytest.fixture(scope="module")
def run(c, adm):
    icp = c.post("/icp", json={"name": "RevOps SaaS", "prompt": PROMPT, "pain_points": ["Manual prospect research"]}, headers=adm).json()
    r = c.post("/sdr/pipeline/run", json={"icp_id": icp["id"], "limit": 5}, headers=adm)
    assert r.status_code == 200, r.text
    return icp, r.json()


def test_auth_and_icp(c, adm):
    assert c.get("/icp").status_code == 401
    u = c.post("/auth/register", json={"email": "u@shop.dev", "password": "password123"}).json()["access_token"]
    assert c.get("/icp", headers=h(u)).status_code == 403
    v = c.post("/icp/validate", json={"prompt": PROMPT}, headers=adm).json()["normalized"]
    assert v["industries"] == ["Software / SaaS"] and set(v["geographies"]) == {"US", "CA"} and (v["employee_min"], v["employee_max"]) == (200, 5000)
    assert "Chief Revenue Officer" in v["persona_titles"] and "RevOps" in v["persona_titles"]
    assert c.post("/icp", json={"name": "bad"}, headers=adm).status_code == 422
    icp = c.post("/icp", json={"name": "v1", "industries": ["fintech"]}, headers=adm).json()
    v2 = c.put(f"/icp/{icp['id']}", json={"name": "v2", "industries": ["fintech"], "geographies": ["IN"]}, headers=adm).json()
    assert v2["version"] == 2 and len(c.get(f"/icp/{icp['id']}/versions", headers=adm).json()) == 2


def test_pipeline_qualifies_and_drafts(c, adm, run):
    icp, r = run
    assert r["provider"] == "mock_fixture" and r["summary"]["prospects_qualified"] >= 1
    names = {x["account"] for x in r["qualification_results"]}
    assert "Northwind Cloud" in names and "Helix Health Systems" not in names  # healthcare is outside the ICP
    top = next(x for x in r["qualification_results"] if x["campaign_id"])
    camp = c.get(f"/outreach/campaigns/{top['campaign_id']}", headers=adm).json()
    assert camp["status"] == "pending_approval" and camp["provider"] == "dry_run"
    assert {m["channel"] for m in camp["messages"]} == {"email", "linkedin", "phone"}
    again = c.post("/sdr/pipeline/run", json={"icp_id": icp["id"], "limit": 5}, headers=adm).json()
    assert len(c.get("/outreach/campaigns", headers=adm).json()) == r["summary"]["campaign_drafts"]  # no duplicate campaigns
    q = c.get(f"/qualification/{top['contact_id']}", headers=adm).json()
    assert q["decision"] in ("SQL", "MQL") and q["missing_information"] and q["mode"] == "heuristic"
    assert c.get(f"/prospect-intelligence/{top['contact_id']}", headers=adm).json()["calibrated"] is False


def test_send_gate_followup_reply_meeting_crm(c, adm, run):
    cid = next(x for x in run[1]["qualification_results"] if x["campaign_id"])["campaign_id"]
    assert c.post(f"/outreach/campaigns/{cid}/send", headers=adm).status_code == 409   # not approved
    assert c.post(f"/outreach/campaigns/{cid}/approve", headers=adm).json()["status"] == "approved"
    s = c.post(f"/outreach/campaigns/{cid}/send", headers=adm).json()
    assert s["status"] == "sent" and s["provider_message_id"].startswith("dry_")
    assert c.post(f"/outreach/campaigns/{cid}/send", headers=adm).json()["provider_message_id"] == s["provider_message_id"]  # idempotent
    plan = c.post("/follow-up/plans", json={"campaign_id": cid, "delays_days": [0, 5]}, headers=adm).json()
    assert [t["status"] for t in plan["touches"]] == ["pending_approval"] * 2
    assert c.post("/follow-up/scheduler/run-due", headers=adm).json()["sent"] == 0       # unapproved touches never send
    c.post(f"/follow-up/plans/{cid}/approve", headers=adm)
    assert c.post("/follow-up/scheduler/run-due", headers=adm).json()["sent"] == 1       # only the due one

    th = c.post("/conversations/inbound", json={"campaign_id": cid, "body": "Sounds interesting, can we schedule a call next week?"}, headers=adm).json()
    assert th["intent"] == "meeting_request" and th["reply_status"] == "draft"
    assert [t["status"] for t in c.get(f"/follow-up/plans/{cid}", headers=adm).json()["touches"]] == ["sent", "stopped"]
    assert c.post(f"/conversations/{th['id']}/send-reply", headers=adm).status_code == 409  # needs approval
    c.post(f"/conversations/{th['id']}/approve-reply", headers=adm)
    assert c.post(f"/conversations/{th['id']}/send-reply", headers=adm).json()["reply_status"] == "sent"

    start = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    assert c.post("/meetings", json={"thread_id": th["id"], "start_at": "2020-01-01T10:00:00Z"}, headers=adm).status_code == 422
    m = c.post("/meetings", json={"thread_id": th["id"], "start_at": start}, headers=adm).json()
    assert m["status"] == "requested" and m["event_id"] is None                 # a request is not a booking
    assert c.post(f"/meetings/{m['id']}/book", headers=adm).status_code == 409
    c.post(f"/meetings/{m['id']}/approve", headers=adm)
    b = c.post(f"/meetings/{m['id']}/book", headers=adm).json()
    assert b["status"] == "booked" and b["event_id"] and b["meet_url"] and b["crm"] == "synced" and len(b["reminders"]) == 2
    r1 = c.post(f"/crm/syncs/{m['id']}/retry", headers=adm).json()
    assert r1["records"]["opportunity"]["id"] == c.get("/crm/syncs", headers=adm).json()[0]["records"]["opportunity"]["id"]  # idempotent ids
    assert c.get(f"/outreach/campaigns/{cid}", headers=adm).json()["status"] == "meeting_booked"
    a = c.get("/sdr/analytics", headers=adm).json()
    assert a["meetings_booked"] == 1 and a["replies"] == 1 and a["outcome_labels"] >= 2


def test_unsubscribe_suppresses_and_webhooks(c, adm, run):
    cid = [x["campaign_id"] for x in run[1]["qualification_results"] if x["campaign_id"]][1:2]
    if not cid:
        pytest.skip("only one campaign drafted")
    cid = cid[0]
    c.post(f"/outreach/campaigns/{cid}/approve", headers=adm); sent = c.post(f"/outreach/campaigns/{cid}/send", headers=adm).json()
    assert c.post("/conversations/inbound/brevo", json={"items": []}).status_code == 401
    r = c.post("/conversations/inbound/brevo", headers={"X-Brevo-Inbound-Secret": "insec"},
               json={"items": [{"From": {"Address": sent["email"]}, "RawTextBody": "Please unsubscribe me"}]})
    assert r.json()["processed"] == 1
    assert c.get(f"/outreach/campaigns/{cid}", headers=adm).json()["status"] == "unsubscribed"
    assert c.post(f"/outreach/campaigns/{cid}/send", headers=adm).status_code == 409


def test_apollo_errors_are_explained(c, adm, run, monkeypatch):
    class R:
        def __init__(s, code): s.status_code = code
        def json(s): return {}
    monkeypatch.setenv("SDR_DISCOVERY_PROVIDER", "apollo_api"); monkeypatch.setenv("SDR_DISCOVERY_APOLLO_API_KEY", "k")
    for code, word in ((422, "credits"), (403, "no access")):
        monkeypatch.setattr(P.requests, "post", lambda *a, _c=code, **k: R(_c))
        r = c.post("/prospect-discovery/run", json={"icp_id": run[0]["id"]}, headers=adm)
        assert r.status_code == 502 and word in r.json()["detail"]
    monkeypatch.setenv("SDR_DISCOVERY_APOLLO_ALLOW_PUBLIC_FALLBACK", "true")
    assert "apollo fallback" in c.post("/prospect-discovery/run", json={"icp_id": run[0]["id"]}, headers=adm).json()["provider"]
