from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from backend.core import auth
from backend.core.auth import require_user
from backend.core.config import settings
from backend.core.db import Base, engine, get_db
from backend.integrations import webhooks
from backend.models import Ticket, Lead, Order
from backend.modules import support, marketing, sdr, recommender

Base.metadata.create_all(engine)  # move to Alembic migrations before real traffic

app = FastAPI(title="Ecommerce AI Platform")
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"])
for r in (auth.router, support.router, marketing.router, sdr.router, recommender.router, webhooks.router):
    app.include_router(r)

@app.get("/health")
def health():
    return {"ok": True, "shopify": "live" if settings.shopify_live else "mock"}

@app.get("/dashboard", dependencies=[Depends(require_user)])
def dashboard(db: Session = Depends(get_db)):
    by_status = dict(db.execute(select(Ticket.status, func.count()).group_by(Ticket.status)).all())
    return {"tickets": by_status, "leads": db.scalar(select(func.count()).select_from(Lead)),
            "orders": db.scalar(select(func.count()).select_from(Order))}

@app.get("/orders", dependencies=[Depends(require_user)])
def orders(db: Session = Depends(get_db)):
    return [{"name": o.name, "email": o.customer_email, "payment": o.financial_status,
             "fulfilment": o.fulfillment_status, "total": o.total, "refunded": o.refunded}
            for o in db.scalars(select(Order).order_by(Order.id.desc()).limit(200))]
