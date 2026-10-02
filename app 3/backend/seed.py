"""Dev-only sample data:  python -m backend.seed"""
from datetime import timedelta
from backend.core.config import settings
from backend.core.db import Base, engine, SessionLocal
from backend.models import Order, now

if settings.env == "production":
    raise SystemExit("Refusing to seed in production")
Base.metadata.create_all(engine)
with SessionLocal() as db:
    db.add_all([
        Order(name="#1001", customer_email="alice@example.com", financial_status="paid", fulfillment_status="fulfilled",
              tracking_url="https://track.example.com/1001", total=59.0, placed_at=now() - timedelta(days=5)),
        Order(name="#1002", customer_email="bob@example.com", financial_status="paid", fulfillment_status="unfulfilled",
              total=120.0, placed_at=now() - timedelta(days=60)),
    ])
    db.commit()
print("seeded #1001 (alice, refundable) and #1002 (bob, outside refund window)")
