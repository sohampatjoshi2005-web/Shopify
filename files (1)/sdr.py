"""AI SDR API. Admin-only operator endpoints, plus secret-protected provider webhooks."""
import hmac
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import aware, now
from ..security import admin_user
from ..sdr import logic as L, providers as P
from ..sdr.models import (SdrAccount, SdrCampaign, SdrContact, SdrCrmSync, SdrIcp, SdrMeeting, SdrOutcome, SdrThread, SdrTouch)

router = APIRouter(tags=["sdr"], dependencies=[Depends(admin_user)])
hooks = APIRouter(tags=["sdr-webhooks"])


def get_or_404(db, model, id_, what):
    o = db.get(model, id_)
    if not o:
        raise HTTPException(404, f"{what} not found")
    return o


def pe(fn, *a):
    try:
        return fn(*a)
    except P.ProviderError as e:
        raise HTTPException(e.status, e.msg)


# ---------------------------------------------------------------- ICP
class IcpIn(BaseModel):
    name: str = Field(default="ICP", max_length=128)
    prompt: str | None = None
    industries: list[str] = []
    geographies: list[str] = []
    employee_min: int | None = Field(default=None, ge=0)
    employee_max: int | None = Field(default=None, ge=0)
    personas: list[str] = []
    tech: list[str] = []
    pain_points: list[str] = []
    signals: list[str] = []
    exclusions: dict = {}
    weights: dict | None = None


def _icp(r): return {"id": r.id, "group_id": r.group_id, "version": r.version, "name": r.name, "data": r.data, "created_at": r.created_at.isoformat()}


def _valid(body: IcpIn) -> dict:
    v = L.validate_icp(body.model_dump())
    if not v["valid"]:
        raise HTTPException(422, "; ".join(v["errors"]))
    return v["normalized"]


@router.post("/icp/validate")
def icp_validate(body: IcpIn): return L.validate_icp(body.model_dump())


@router.post("/icp/suggest")
def icp_suggest(body: IcpIn): return L.suggest_icp(body.model_dump())


@router.post("/icp", status_code=201)
def icp_create(body: IcpIn, admin=Depends(admin_user), db: Session = Depends(get_db)):
    r = SdrIcp(name=body.name, data=_valid(body), created_by=admin.email)
    db.add(r); db.flush()
    r.group_id = r.id
    db.commit()
    return _icp(r)


@router.put("/icp/{icp_id}")
def icp_update(icp_id: int, body: IcpIn, admin=Depends(admin_user), db: Session = Depends(get_db)):
    cur = get_or_404(db, SdrIcp, icp_id, "ICP")
    latest = db.scalars(select(SdrIcp).where(SdrIcp.group_id == cur.group_id).order_by(SdrIcp.version.desc())).first()
    r = SdrIcp(group_id=cur.group_id, version=latest.version + 1, name=body.name, data=_valid(body), created_by=admin.email)
    db.add(r); db.commit()  # a new version; history is never mutated
    return _icp(r)


@router.get("/icp")
def icp_list(db: Session = Depends(get_db)):
    latest = {}
    for r in db.scalars(select(SdrIcp).order_by(SdrIcp.version)):
        latest[r.group_id] = r  # later versions overwrite earlier ones
    return [_icp(r) for r in sorted(latest.values(), key=lambda r: -r.id)]


@router.get("/icp/{icp_id}")
def icp_get(icp_id: int, db: Session = Depends(get_db)): return _icp(get_or_404(db, SdrIcp, icp_id, "ICP"))


@router.get("/icp/{icp_id}/versions")
def icp_versions(icp_id: int, db: Session = Depends(get_db)):
    cur = get_or_404(db, SdrIcp, icp_id, "ICP")
    return [_icp(r) for r in db.scalars(select(SdrIcp).where(SdrIcp.group_id == cur.group_id).order_by(SdrIcp.version))]


# ---------------------------------------------------------------- discovery / enrichment / intelligence / qualification
def _contact(db, c: SdrContact) -> dict:
    a = db.get(SdrAccount, c.account_id)
    return {"id": c.id, "name": c.name, "title": c.title, "email": c.email, "persona_match": c.persona_match, "suppressed": c.suppressed,
            "account_id": a.id, "account": a.name, "domain": a.domain, "fit_score": a.fit_score,
            "decision": (c.qualification or {}).get("decision"), "priority": (c.intel or {}).get("priority_score")}


def _account(db, a: SdrAccount) -> dict:
    cs = db.scalars(select(SdrContact).where(SdrContact.account_id == a.id)).all()
    return {"id": a.id, "icp_id": a.icp_id, "name": a.name, "domain": a.domain, "source": a.source, "fit_score": a.fit_score, "data": a.data,
            "enriched": a.enrichment is not None, "contacts": [_contact(db, c) for c in cs]}


@router.get("/prospect-discovery/status")
def discovery_status(): return P.provider_status()


class DiscoverIn(BaseModel):
    icp_id: int
    limit: int = Field(default=10, ge=1, le=50)


@router.post("/prospect-discovery/run")
def discovery_run(body: DiscoverIn, db: Session = Depends(get_db)):
    icp = get_or_404(db, SdrIcp, body.icp_id, "ICP")
    accs, provider = pe(L.discover_store, db, icp, body.limit)
    db.commit()
    return {"provider": provider, "accounts": [_account(db, a) for a in accs]}


@router.get("/prospect-discovery/accounts")
def accounts(icp_id: int | None = None, db: Session = Depends(get_db)):
    q = select(SdrAccount).order_by(SdrAccount.fit_score.desc())
    return [_account(db, a) for a in db.scalars(q.where(SdrAccount.icp_id == icp_id) if icp_id else q)]


class ResearchIn(BaseModel):
    account_id: int


def _icp_of(db, a): return db.get(SdrIcp, a.icp_id).data


@router.post("/enrichment/research")
def enrichment_research(body: ResearchIn, db: Session = Depends(get_db)):
    a = get_or_404(db, SdrAccount, body.account_id, "Account")
    a.enrichment = L.enrich(a, _icp_of(db, a))
    db.commit()
    return a.enrichment


@router.get("/enrichment")
def enrichment_list(db: Session = Depends(get_db)):
    return [{"account_id": a.id, "name": a.name, **a.enrichment} for a in db.scalars(select(SdrAccount)) if a.enrichment]


@router.get("/enrichment/{account_id}")
def enrichment_get(account_id: int, db: Session = Depends(get_db)):
    a = get_or_404(db, SdrAccount, account_id, "Account")
    if not a.enrichment:
        raise HTTPException(404, "Not enriched yet")
    return a.enrichment


class ContactIn(BaseModel):
    contact_id: int


def _analyze(db, c: SdrContact) -> dict:
    a = db.get(SdrAccount, c.account_id)
    if not a.enrichment:
        a.enrichment = L.enrich(a, _icp_of(db, a))
    c.intel = L.analyze(a, c, _icp_of(db, a))
    return c.intel


def _qualify(db, c: SdrContact) -> dict:
    if not c.intel:
        _analyze(db, c)
    a = db.get(SdrAccount, c.account_id)
    c.qualification = L.qualify(a, c, _icp_of(db, a))
    return c.qualification


@router.post("/prospect-intelligence/analyze")
def intel_analyze(body: ContactIn, db: Session = Depends(get_db)):
    r = _analyze(db, get_or_404(db, SdrContact, body.contact_id, "Contact"))
    db.commit()
    return r


@router.get("/prospect-intelligence")
def intel_list(db: Session = Depends(get_db)):
    return sorted([{**_contact(db, c), **c.intel["probabilities"], "mode": c.intel["mode"]} for c in db.scalars(select(SdrContact)) if c.intel],
                  key=lambda x: -x["priority"])


@router.get("/prospect-intelligence/{contact_id}")
def intel_get(contact_id: int, db: Session = Depends(get_db)):
    c = get_or_404(db, SdrContact, contact_id, "Contact")
    if not c.intel:
        raise HTTPException(404, "No intelligence yet")
    return c.intel


class OutcomeIn(BaseModel):
    contact_id: int
    kind: str = Field(pattern="^(reply|unsubscribe|meeting_booked|no_reply)$")


@router.post("/prospect-intelligence/outcomes", status_code=201)
def intel_outcome(body: OutcomeIn, db: Session = Depends(get_db)):
    get_or_404(db, SdrContact, body.contact_id, "Contact")
    L.outcome(db, body.contact_id, body.kind)
    db.commit()
    return {"ok": True}


@router.post("/qualification/evaluate")
def qual_eval(body: ContactIn, db: Session = Depends(get_db)):
    r = _qualify(db, get_or_404(db, SdrContact, body.contact_id, "Contact"))
    db.commit()
    return r


@router.get("/qualification")
def qual_list(db: Session = Depends(get_db)):
    return [{**_contact(db, c), **{k: c.qualification[k] for k in ("score", "ml_probability", "framework_coverage", "next_action")}}
            for c in db.scalars(select(SdrContact)) if c.qualification]


@router.get("/qualification/{contact_id}")
def qual_get(contact_id: int, db: Session = Depends(get_db)):
    c = get_or_404(db, SdrContact, contact_id, "Contact")
    if not c.qualification:
        raise HTTPException(404, "Not qualified yet")
    return c.qualification


# ---------------------------------------------------------------- outreach
def _camp(db, c: SdrCampaign) -> dict:
    ct = db.get(SdrContact, c.contact_id)
    return {"id": c.id, "contact": ct.name, "contact_id": ct.id, "email": ct.email, "account": db.get(SdrAccount, ct.account_id).name, "status": c.status,
            "provider": c.provider, "messages": c.messages, "provider_message_id": c.provider_message_id, "events": c.events,
            "created_at": c.created_at.isoformat(), "sent_at": c.sent_at.isoformat() if c.sent_at else None}


@router.post("/outreach/campaigns", status_code=201)
def camp_create(body: ContactIn, db: Session = Depends(get_db)):
    c = get_or_404(db, SdrContact, body.contact_id, "Contact")
    if not c.qualification:
        _qualify(db, c)
    if c.qualification["decision"] not in ("SQL", "MQL"):
        raise HTTPException(409, f"Prospect is {c.qualification['decision']}: {c.qualification['next_action']}")
    camp, created = L.create_campaign(db, db.get(SdrAccount, c.account_id), c)
    db.commit()
    return {**_camp(db, camp), "created": created}


@router.get("/outreach/campaigns")
def camp_list(status: str | None = None, db: Session = Depends(get_db)):
    q = select(SdrCampaign).order_by(SdrCampaign.id.desc())
    return [_camp(db, c) for c in db.scalars(q.where(SdrCampaign.status == status) if status else q)]


@router.get("/outreach/campaigns/{cid}")
def camp_get(cid: int, db: Session = Depends(get_db)): return _camp(db, get_or_404(db, SdrCampaign, cid, "Campaign"))


def _move(db, cid: int, allowed: tuple, new: str) -> dict:
    c = get_or_404(db, SdrCampaign, cid, "Campaign")
    if c.status not in allowed:
        raise HTTPException(409, f"Can't move a '{c.status}' campaign to '{new}'")
    c.status = new
    if new in ("cancelled", "paused"):
        L.stop_touches(db, c.id, "operator")
    db.commit()
    return _camp(db, c)


@router.post("/outreach/campaigns/{cid}/approve")
def camp_approve(cid: int, db: Session = Depends(get_db)): return _move(db, cid, ("pending_approval",), "approved")


@router.post("/outreach/campaigns/{cid}/pause")
def camp_pause(cid: int, db: Session = Depends(get_db)): return _move(db, cid, ("approved", "sent"), "paused")


@router.post("/outreach/campaigns/{cid}/resume")
def camp_resume(cid: int, db: Session = Depends(get_db)):
    c = get_or_404(db, SdrCampaign, cid, "Campaign")
    return _move(db, cid, ("paused",), "sent" if c.sent_at else "approved")


@router.post("/outreach/campaigns/{cid}/cancel")
def camp_cancel(cid: int, db: Session = Depends(get_db)): return _move(db, cid, ("pending_approval", "approved", "sent", "paused"), "cancelled")


@router.post("/outreach/campaigns/{cid}/send")
def camp_send(cid: int, db: Session = Depends(get_db)):
    c = get_or_404(db, SdrCampaign, cid, "Campaign")
    if c.status == "sent":
        return _camp(db, c)  # idempotent: never sends twice
    if c.status != "approved":
        raise HTTPException(409, "Campaign must be approved before sending")
    ct = db.get(SdrContact, c.contact_id)
    em = next((m for m in c.messages if m["channel"] == "email"), None)
    if ct.suppressed or not ct.email or not em:
        raise HTTPException(409, "Contact is suppressed or has no email; complete the LinkedIn/phone tasks manually")
    r = pe(P.send_email, ct.email, em["subject"], em["body"], str(c.id))
    c.status, c.provider, c.provider_message_id, c.sent_at = "sent", r["provider"], r["id"], now()
    db.commit()
    return _camp(db, c)


@router.get("/outreach/performance")
def performance(db: Session = Depends(get_db)):
    by = dict(db.execute(select(SdrCampaign.status, func.count()).group_by(SdrCampaign.status)).all())
    sent = sum(by.get(k, 0) for k in ("sent", "replied", "unsubscribed", "meeting_booked", "paused"))
    replies = db.scalar(select(func.count()).select_from(SdrThread)) or 0
    return {"campaigns_by_status": by, "sent": sent, "replies": replies, "reply_rate": round(replies / sent, 3) if sent else 0}


# ---------------------------------------------------------------- follow-up
class PlanIn(BaseModel):
    campaign_id: int
    delays_days: list[int] = Field(default=[3, 7], min_length=1, max_length=5)


def _plan(db, cid): return [{"id": t.id, "step": t.step, "due_at": t.due_at.isoformat(), "status": t.status, "stop_reason": t.stop_reason, "subject": t.subject}
                            for t in db.scalars(select(SdrTouch).where(SdrTouch.campaign_id == cid).order_by(SdrTouch.step))]


@router.post("/follow-up/plans", status_code=201)
def plan_create(body: PlanIn, db: Session = Depends(get_db)):
    c = get_or_404(db, SdrCampaign, body.campaign_id, "Campaign")
    if c.status in ("cancelled", "unsubscribed", "replied", "meeting_booked") or db.scalars(select(SdrTouch.id).where(SdrTouch.campaign_id == c.id)).first():
        raise HTTPException(409, "Campaign is stopped or already has a follow-up plan")
    em = next((m for m in c.messages if m["channel"] == "email"), None)
    if not em:
        raise HTTPException(409, "Campaign has no email message")
    base = aware(c.sent_at) or now()
    for i, d in enumerate(sorted(body.delays_days), 1):
        db.add(SdrTouch(campaign_id=c.id, step=i, due_at=base + timedelta(days=d), subject="Re: " + em["subject"],
                        body=f"Hi, following up on my earlier note (touch {i}). Happy to share specifics if useful.\n\nReply 'unsubscribe' to opt out."))
    db.commit()
    return {"campaign_id": c.id, "touches": _plan(db, c.id)}


@router.get("/follow-up/plans")
def plans(db: Session = Depends(get_db)):
    return [{"campaign_id": cid, "touches": _plan(db, cid)} for cid in db.scalars(select(SdrTouch.campaign_id).distinct())]


@router.get("/follow-up/plans/{cid}")
def plan_get(cid: int, db: Session = Depends(get_db)): return {"campaign_id": cid, "touches": _plan(db, cid)}


@router.post("/follow-up/plans/{cid}/approve")
def plan_approve(cid: int, db: Session = Depends(get_db)):
    n = 0
    for t in db.scalars(select(SdrTouch).where(SdrTouch.campaign_id == cid, SdrTouch.status == "pending_approval")):
        t.status, n = "approved", n + 1
    db.commit()
    return {"approved": n}


@router.post("/follow-up/scheduler/run-due")
@router.post("/outreach/scheduler/run-due")
def run_due(db: Session = Depends(get_db)):
    sent = stopped = 0
    for t in db.scalars(select(SdrTouch).where(SdrTouch.status == "approved", SdrTouch.due_at <= now())):
        c = db.get(SdrCampaign, t.campaign_id)
        ct = db.get(SdrContact, c.contact_id)
        if c.status != "sent" or ct.suppressed or not ct.email:
            t.status, t.stop_reason, stopped = "stopped", "campaign_not_active", stopped + 1
            continue
        pe(P.send_email, ct.email, t.subject, t.body, f"touch-{t.id}")
        t.status, t.sent_at, sent = "sent", now(), sent + 1
    db.commit()
    return {"sent": sent, "stopped": stopped}


# ---------------------------------------------------------------- conversations
class InboundIn(BaseModel):
    campaign_id: int | None = None
    from_email: str | None = None
    body: str = Field(min_length=1, max_length=8000)


def ingest_reply(db: Session, campaign_id: int | None, from_email: str | None, text: str) -> SdrThread:
    camp = db.get(SdrCampaign, campaign_id) if campaign_id else None
    if not camp and from_email:
        camp = db.scalars(select(SdrCampaign).join(SdrContact, SdrContact.id == SdrCampaign.contact_id)
                          .where(SdrContact.email == from_email.lower(), SdrCampaign.sent_at.is_not(None)).order_by(SdrCampaign.id.desc())).first()
    if not camp:
        raise HTTPException(404, "No matching campaign for this reply")
    ct = db.get(SdrContact, camp.contact_id)
    th = db.scalars(select(SdrThread).where(SdrThread.campaign_id == camp.id)).first() or SdrThread(campaign_id=camp.id, contact_id=ct.id, messages=[])
    intent = L.classify(text)
    db.add(th); db.flush()
    th.messages = [*th.messages, {"direction": "in", "body": text, "at": now().isoformat(), "intent": intent}]
    th.intent, th.status = intent, "stopped"
    L.stop_touches(db, camp.id, "unsubscribe" if intent == "unsubscribe" else "reply")  # stop before drafting
    if intent == "unsubscribe":
        ct.suppressed, camp.status = True, "unsubscribed"
        L.outcome(db, ct.id, "unsubscribe")
        th.reply_draft, th.reply_status = "", "none"
    else:
        if camp.status != "meeting_booked":
            camp.status = "replied"
        L.outcome(db, ct.id, "reply")
        draft = L.DRAFT.get(intent, "") if intent != "out_of_office" else ""
        th.reply_draft, th.reply_status = draft.format(n=ct.name.split()[0]), "draft" if draft else "none"
    db.commit()
    return th


def _thread(db, t: SdrThread) -> dict:
    ct = db.get(SdrContact, t.contact_id)
    return {"id": t.id, "campaign_id": t.campaign_id, "contact": ct.name, "intent": t.intent, "status": t.status, "messages": t.messages,
            "reply_draft": t.reply_draft, "reply_status": t.reply_status}


@router.post("/conversations/inbound", status_code=201)
def inbound(body: InboundIn, db: Session = Depends(get_db)): return _thread(db, ingest_reply(db, body.campaign_id, body.from_email, body.body))


@router.get("/conversations")
def conv_list(db: Session = Depends(get_db)): return [_thread(db, t) for t in db.scalars(select(SdrThread).order_by(SdrThread.id.desc()))]


@router.get("/conversations/{tid}")
def conv_get(tid: int, db: Session = Depends(get_db)): return _thread(db, get_or_404(db, SdrThread, tid, "Conversation"))


@router.post("/conversations/{tid}/approve-reply")
def reply_approve(tid: int, db: Session = Depends(get_db)):
    t = get_or_404(db, SdrThread, tid, "Conversation")
    if t.reply_status != "draft":
        raise HTTPException(409, "No draft reply to approve")
    t.reply_status = "approved"
    db.commit()
    return _thread(db, t)


@router.post("/conversations/{tid}/send-reply")
def reply_send(tid: int, db: Session = Depends(get_db)):
    t = get_or_404(db, SdrThread, tid, "Conversation")
    ct = db.get(SdrContact, t.contact_id)
    if t.reply_status != "approved":
        raise HTTPException(409, "Reply must be approved before sending")
    if ct.suppressed or not ct.email:
        raise HTTPException(409, "Contact is suppressed or has no email")
    pe(P.send_email, ct.email, "Re: your reply", t.reply_draft, f"reply-{t.id}-{len(t.messages)}")
    t.messages, t.reply_status = [*t.messages, {"direction": "out", "body": t.reply_draft, "at": now().isoformat()}], "sent"
    db.commit()
    return _thread(db, t)


# ---------------------------------------------------------------- meetings + CRM
class MeetingIn(BaseModel):
    thread_id: int
    start_at: datetime
    duration_min: int = Field(default=30, ge=15, le=120)


def _mt(m: SdrMeeting, db) -> dict:
    s = db.scalars(select(SdrCrmSync).where(SdrCrmSync.meeting_id == m.id)).first()
    return {"id": m.id, "thread_id": m.thread_id, "contact": db.get(SdrContact, m.contact_id).name, "status": m.status, "start_at": aware(m.start_at).isoformat(),
            "duration_min": m.duration_min, "event_id": m.provider_event_id, "meet_url": m.meet_url, "reminders": m.reminders, "crm": s.status if s else None}


@router.get("/meetings/status")
def meetings_status(): return {"provider": "mock_calendar", "free_busy": "checked against booked meetings", "reminders": "computed (24h, 1h); no scheduler worker yet"}


@router.post("/meetings", status_code=201)
def meeting_create(body: MeetingIn, db: Session = Depends(get_db)):
    t = get_or_404(db, SdrThread, body.thread_id, "Conversation")
    if t.intent not in ("meeting_request", "interested"):
        raise HTTPException(409, "A meeting needs a reply that asks for one (meeting_request or interested)")
    st = aware(body.start_at) if body.start_at.tzinfo else body.start_at.replace(tzinfo=timezone.utc)
    if st <= now():
        raise HTTPException(422, "start_at must be in the future")
    m = SdrMeeting(thread_id=t.id, contact_id=t.contact_id, start_at=st, duration_min=body.duration_min)
    db.add(m); db.commit()
    return _mt(m, db)


@router.get("/meetings")
def meetings(db: Session = Depends(get_db)): return [_mt(m, db) for m in db.scalars(select(SdrMeeting).order_by(SdrMeeting.id.desc()))]


@router.post("/meetings/{mid}/approve")
def meeting_approve(mid: int, db: Session = Depends(get_db)):
    m = get_or_404(db, SdrMeeting, mid, "Meeting")
    if m.status != "requested":
        raise HTTPException(409, f"Meeting is {m.status}")
    m.status = "approved"
    db.commit()
    return _mt(m, db)


def _sync_crm(db, m: SdrMeeting) -> SdrCrmSync:
    s = db.scalars(select(SdrCrmSync).where(SdrCrmSync.meeting_id == m.id)).first() or SdrCrmSync(meeting_id=m.id)
    db.add(s)
    ct = db.get(SdrContact, m.contact_id)
    a = db.get(SdrAccount, ct.account_id)
    s.attempts = (s.attempts or 0) + 1
    s.records = P.crm_upsert(m.id, {"name": a.name, "domain": a.domain}, {"name": ct.name, "title": ct.title, "email": ct.email})  # same ids on every retry
    s.status = "synced"
    return s


@router.post("/meetings/{mid}/book")
def meeting_book(mid: int, db: Session = Depends(get_db)):
    m = get_or_404(db, SdrMeeting, mid, "Meeting")
    if m.status == "booked":
        return _mt(m, db)
    if m.status != "approved":
        raise HTTPException(409, "Meeting must be approved before booking")
    s, e = aware(m.start_at), aware(m.start_at) + timedelta(minutes=m.duration_min)
    for o in db.scalars(select(SdrMeeting).where(SdrMeeting.status == "booked", SdrMeeting.id != m.id)):
        if aware(o.start_at) < e and s < aware(o.start_at) + timedelta(minutes=o.duration_min):
            raise HTTPException(409, "That slot is busy")
    ev = P.create_event(m.id)
    if not ev.get("event_id") or not ev.get("meet_url"):
        m.status = "failed"
        db.commit()
        raise HTTPException(502, "Calendar provider did not confirm the event")
    m.provider_event_id, m.meet_url, m.status = ev["event_id"], ev["meet_url"], "booked"
    m.reminders = [(s - timedelta(hours=24)).isoformat(), (s - timedelta(hours=1)).isoformat()]
    t = db.get(SdrThread, m.thread_id)
    camp = db.get(SdrCampaign, t.campaign_id)
    camp.status = "meeting_booked"
    L.stop_touches(db, camp.id, "meeting")
    L.outcome(db, m.contact_id, "meeting_booked")  # only after provider confirmation
    _sync_crm(db, m)
    db.commit()
    return _mt(m, db)


@router.post("/meetings/{mid}/cancel")
def meeting_cancel(mid: int, db: Session = Depends(get_db)):
    m = get_or_404(db, SdrMeeting, mid, "Meeting")
    m.status = "cancelled"
    db.commit()
    return _mt(m, db)


@router.get("/crm/status")
def crm_status(): return {"provider": "dry_run", "creates": "company, person, opportunity after calendar confirmation"}


@router.get("/crm/syncs")
def crm_syncs(db: Session = Depends(get_db)):
    return [{"id": s.id, "meeting_id": s.meeting_id, "status": s.status, "attempts": s.attempts, "records": s.records} for s in db.scalars(select(SdrCrmSync))]


@router.post("/crm/syncs/{mid}/retry")
def crm_retry(mid: int, db: Session = Depends(get_db)):
    m = get_or_404(db, SdrMeeting, mid, "Meeting")
    if m.status != "booked":
        raise HTTPException(409, "Only booked meetings sync to the CRM")
    s = _sync_crm(db, m)
    db.commit()
    return {"id": s.id, "status": s.status, "attempts": s.attempts, "records": s.records}


# ---------------------------------------------------------------- pipeline + analytics
class PipelineIn(BaseModel):
    icp_id: int
    limit: int = Field(default=5, ge=1, le=25)
    contacts_per_account: int = Field(default=2, ge=1, le=5)
    min_fit: float = Field(default=50, ge=0, le=100)


@router.post("/sdr/pipeline/run")
def pipeline(body: PipelineIn, db: Session = Depends(get_db)):
    icp = get_or_404(db, SdrIcp, body.icp_id, "ICP")
    accs, provider = pe(L.discover_store, db, icp, body.limit, body.min_fit)
    results, campaigns, warnings = [], [], []
    for a in accs:
        a.enrichment = L.enrich(a, icp.data)
        cs = db.scalars(select(SdrContact).where(SdrContact.account_id == a.id).order_by(SdrContact.persona_match.desc()).limit(body.contacts_per_account)).all()
        if not cs:
            warnings.append(f"{a.name}: no named contact found, so intelligence and outreach were skipped")
        for c in cs:
            _analyze(db, c)
            q = _qualify(db, c)
            row = {"contact_id": c.id, "contact": c.name, "title": c.title, "account": a.name, "fit_score": a.fit_score, "decision": q["decision"],
                   "score": q["score"], "next_action": q["next_action"], "campaign_id": None}
            if q["decision"] in ("SQL", "MQL"):
                camp, _ = L.create_campaign(db, a, c)
                row["campaign_id"] = camp.id
                campaigns.append(camp.id)
            else:
                warnings.append(f"{c.name} ({a.name}) is {q['decision']}: stopped before campaign creation")
            results.append(row)
    db.commit()
    return {"provider": provider, "qualification_results": results, "warnings": warnings,
            "summary": {"accounts_discovered": len(accs), "accounts_enriched": len(accs), "prospects_qualified": sum(r["decision"] in ("SQL", "MQL") for r in results),
                        "campaign_drafts": len(set(campaigns))}}


@router.get("/sdr/status")
def sdr_status(): return {**P.provider_status(), "note": "Scores are heuristic (calibrated=false); outreach never sends without approval."}


@router.get("/sdr/analytics")
def analytics(db: Session = Depends(get_db)):
    cnt = lambda m, *w: db.scalar(select(func.count()).select_from(m).where(*w)) or 0
    dec = {}
    for c in db.scalars(select(SdrContact)):
        if c.qualification:
            dec[c.qualification["decision"]] = dec.get(c.qualification["decision"], 0) + 1
    perf, booked = performance(db), cnt(SdrMeeting, SdrMeeting.status == "booked")
    intents = dict(db.execute(select(SdrThread.intent, func.count()).group_by(SdrThread.intent)).all())
    return {"icps": cnt(SdrIcp), "accounts": cnt(SdrAccount), "contacts": cnt(SdrContact), "qualification": dec, "campaigns": perf["campaigns_by_status"],
            "sent": perf["sent"], "replies": perf["replies"], "reply_rate": perf["reply_rate"], "reply_intents": intents, "meetings_booked": booked,
            "meeting_rate": round(booked / perf["sent"], 3) if perf["sent"] else 0, "unsubscribes": cnt(SdrOutcome, SdrOutcome.kind == "unsubscribe"),
            "outcome_labels": cnt(SdrOutcome)}


# ---------------------------------------------------------------- provider webhooks (shared-secret protected)
def _secret(name: str, got: str | None):
    want = P.env(name)
    if not want or not got or not hmac.compare_digest(want, got):
        raise HTTPException(401, "Bad webhook secret")


@hooks.post("/outreach/events/brevo")
async def brevo_events(request: Request, db: Session = Depends(get_db), x_brevo_webhook_secret: str | None = Header(None)):
    _secret("SDR_OUTREACH_BREVO_WEBHOOK_SECRET", x_brevo_webhook_secret)
    ev = await request.json()
    mid = str(ev.get("message-id") or ev.get("message_id") or "").strip("<>")
    camp = db.scalars(select(SdrCampaign).where(SdrCampaign.provider_message_id.in_([mid, f"<{mid}>"]))).first() if mid else None
    if not camp:
        return {"status": "ignored"}
    kind = str(ev.get("event", ""))
    camp.events = [*camp.events, {"event": kind, "at": now().isoformat()}]
    if kind in ("unsubscribed", "spam", "hardBounce", "hard_bounce", "blocked"):
        db.get(SdrContact, camp.contact_id).suppressed = True
        L.stop_touches(db, camp.id, kind)
    db.commit()
    return {"status": "ok"}


@hooks.post("/conversations/inbound/brevo")
async def brevo_inbound(request: Request, db: Session = Depends(get_db), x_brevo_inbound_secret: str | None = Header(None)):
    _secret("SDR_OUTREACH_BREVO_INBOUND_SECRET", x_brevo_inbound_secret)
    n = 0
    for it in (await request.json()).get("items", []):
        text = it.get("ExtractedMarkdownMessage") or it.get("RawTextBody") or ""
        sender = ((it.get("From") or {}).get("Address") or "").lower()
        if text and sender:
            ingest_reply(db, None, sender, text)
            n += 1
    return {"processed": n}


@hooks.post("/meetings/webhooks/provider")
async def meeting_webhook(request: Request, db: Session = Depends(get_db), x_sdr_webhook_secret: str | None = Header(None)):
    _secret("SDR_MEETING_WEBHOOK_SECRET", x_sdr_webhook_secret)
    ev = await request.json()
    m = db.scalars(select(SdrMeeting).where(SdrMeeting.provider_event_id == str(ev.get("event_id", "")))).first()
    if m and ev.get("type") in ("cancelled", "rescheduled"):
        m.status = "cancelled"
        db.commit()
    return {"status": "ok" if m else "ignored"}
