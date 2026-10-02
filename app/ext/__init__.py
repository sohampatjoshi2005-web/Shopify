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
