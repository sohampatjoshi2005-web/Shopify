"""Recommendation seam. Replace _fallback with your ProductRecommender (MLP + SVR + rules) once it is
wired to real Shopify products/orders. Keep the response shape the same."""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.core.auth import require_user
from backend.core.db import get_db
from backend.models import Lead
from backend.modules.marketing import segment

router = APIRouter(tags=["recommender"])
CATALOG = {"Bronze": ["Starter Bundle", "Best Seller Pack"], "Silver": ["Popular Upgrade", "Seasonal Pick"],
           "Gold": ["Premium Collection", "Loyalty Offer"], "Platinum": ["VIP Early Access", "Premium Collection"]}

def _fallback(seg: str, n: int):
    return [{"product": p, "reason": f"{seg} tier rule"} for p in CATALOG[seg][:n]]

@router.get("/recommend/{email}", dependencies=[Depends(require_user)])
def recommend(email: str, top_n: int = 3, db: Session = Depends(get_db)):
    lead = db.scalars(select(Lead).where(Lead.email == email.lower())).first()
    seg = segment(lead.score if lead else 0)
    return {"email": email, "segment": seg, "recommendations": _fallback(seg, top_n)}
