from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.core.auth import require_user
from backend.core.db import get_db
from backend.models import Lead

router = APIRouter(prefix="/sdr", tags=["sdr"], dependencies=[Depends(require_user)])

class LeadIn(BaseModel):
    email: str
    name: str | None = None

@router.post("/leads")
def add_lead(body: LeadIn, db: Session = Depends(get_db)):
    email = body.email.lower()
    lead = db.scalars(select(Lead).where(Lead.email == email)).first()
    if not lead:
        lead = Lead(email=email, name=body.name, source="sdr")
        db.add(lead); db.commit()
    return {"email": lead.email, "score": lead.score}

# TODO: plug in your AI SDR pipeline here (enrich -> score -> draft outreach -> send),
# reading/writing the same Lead table so SDR, marketing and support share one customer identity.
