from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.core.auth import require_user, widget_key
from backend.core.db import get_db
from backend.models import Lead, Event

router = APIRouter(tags=["marketing"])
WEIGHTS = {"page_view": 1, "cta_click": 5, "form_submit": 50, "purchase": 40, "bounce": -10}

def segment(score: int) -> str:
    return "Platinum" if score >= 150 else "Gold" if score >= 90 else "Silver" if score >= 50 else "Bronze"

def record_event(db: Session, email: str, etype: str) -> Lead:
    email = email.lower()
    lead = db.scalars(select(Lead).where(Lead.email == email)).first()
    if not lead:
        lead = Lead(email=email, source="shopify", score=0)
        db.add(lead)
    lead.score = (lead.score or 0) + WEIGHTS.get(etype, 0)
    db.add(Event(email=email, type=etype))
    db.flush()
    return lead

class EventIn(BaseModel):
    email: str
    type: str

@router.post("/marketing/events", dependencies=[Depends(widget_key)])
def track(body: EventIn, db: Session = Depends(get_db)):
    lead = record_event(db, body.email, body.type)
    db.commit()
    return {"email": lead.email, "score": lead.score, "segment": segment(lead.score)}

@router.get("/marketing/leads", dependencies=[Depends(require_user)])
def leads(db: Session = Depends(get_db)):
    return [{"email": l.email, "name": l.name, "source": l.source, "score": l.score, "segment": segment(l.score)}
            for l in db.scalars(select(Lead).order_by(Lead.score.desc()).limit(500))]
