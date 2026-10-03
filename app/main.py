import asyncio, logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from .config import settings
from .db import Base, SessionLocal, engine
from .models import User
from .routers import admin, auth, cart, catalog, orders, payments, sellers
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
    ensure_schema(engine)
    bootstrap_admin()
    task = asyncio.create_task(_expiry_loop()) if settings.run_expiry_loop else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="Store API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"])
for r in (auth.router, catalog.router, cart.router, orders.router, payments.router, admin.router, sellers.router):
    app.include_router(r)

from .ext import ensure_schema, install  # noqa: E402  store extensions (payouts, returns, GST, shipping, search, wishlist, email verification)
install(app)
from .support import install as install_support  # noqa: E402  support desk: tickets, SLA, autonomous resolver, RAG/CAG
install_support(app)
from .recommend import install as install_recommend  # noqa: E402  recommendations + store-fed lead list
install_recommend(app)


@app.get("/health")
def health():
    return {"ok": True, "payments": settings.payment_provider, "currency": settings.currency}
