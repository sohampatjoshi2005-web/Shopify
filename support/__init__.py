"""Support desk (tickets, SLA, autonomous resolver, RAG/CAG) built on the store's own users, orders, shipments and invoices.
Installed from app/main.py:  from .support import install; install(app)"""


def install(app) -> None:
    from . import models  # noqa: F401  (registers the sup_* tables on Base.metadata)
    from .api import ROUTERS
    for r in ROUTERS:
        app.include_router(r)
