"""Store extensions: multi-vendor ledger + payouts, returns, GST invoices, shipping, search, wishlist, email verification + password reset.

Install with ONE line in app/main.py, after `app = FastAPI(...)` and BEFORE `Base.metadata.create_all(...)`:

    from .ext import install; install(app)
"""


def install(app) -> None:
    from . import models  # noqa: F401  (registers the x_* tables on Base.metadata)
    from .api import ROUTERS
    from .search import install_hooks
    for r in ROUTERS:
        app.include_router(r)
    install_hooks()


def ensure_schema(engine) -> None:
    """Things create_all() will not do on tables that already exist. Idempotent; call once at startup after create_all."""
    import logging
    from .models import LIVE_SHIPMENT_INDEX
    try:
        LIVE_SHIPMENT_INDEX.create(bind=engine, checkfirst=True)
    except Exception:
        logging.getLogger("store").exception("could not create uq_x_shipments_live (duplicate live shipments already in x_shipments? clean them up and restart)")
