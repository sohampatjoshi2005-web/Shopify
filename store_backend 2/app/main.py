import asyncio, logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from .config import settings
from .db import Base, SessionLocal, engine
from .models import User
from .routers import admin, auth, cart, catalog, orders, payments
from .security import hash_password
from .services.orders import expire_unpaid

log = logging.getLogger("store")


def bootstrap_admin():
    if not (settings.admin_email and settings.admin_password):
        return
    with SessionLocal() as db:
        if not db.scalars(select(User).where(User.email == settings.admin_email)).first():
            db.add(User(email=settings.admin_email, password_hash=hash_password(settings.admin_password),
                        full_name="Admin", role="admin"))
            db.commit()
            log.info("created admin %s", settings.admin_email)


async def _expiry_loop():
    while True:
        await asyncio.sleep(300)
        try:
            def run():
                with SessionLocal() as db:
                    return expire_unpaid(db)
            n = await asyncio.to_thread(run)
            if n:
                log.info("expired %s unpaid orders", n)
        except Exception:
            log.exception("expiry loop error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)  # switch to Alembic migrations before real traffic
    bootstrap_admin()
    task = asyncio.create_task(_expiry_loop()) if settings.run_expiry_loop else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="Store API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"])
for r in (auth.router, catalog.router, cart.router, orders.router, payments.router, admin.router):
    app.include_router(r)


@app.get("/health")
def health():
    return {"ok": True, "payments": settings.payment_provider, "currency": settings.currency}
