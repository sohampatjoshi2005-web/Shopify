"""Store recommendations + lead list, built on the store's own users, orders, products, wishlist and cart.
Installed from app/main.py:  from .recommend import install; install(app)"""


def install(app) -> None:
    from . import models  # noqa: F401  (registers the rec_* tables on Base.metadata)
    from .api import ROUTERS
    for r in ROUTERS:
        app.include_router(r)
