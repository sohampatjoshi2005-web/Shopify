"""Sample catalog + coupon WELCOME10. Safe to call twice."""
from sqlalchemy import select
from .db import SessionLocal
from .models import Category, Coupon, Product, ProductImage, Variant
from .services.slugs import slugify

_IMG = "https://placehold.co/600x400.png?text="
_CATS = ["Apparel", "Drinkware", "Stickers"]
_PRODUCTS = [
    ("Classic Hoodie", "Merch Co", "Apparel", "Heavyweight cotton hoodie with a soft fleece lining.",
     [("HOOD-S", "S", 129900, 25), ("HOOD-M", "M", 129900, 40), ("HOOD-L", "L", 129900, 30), ("HOOD-XL", "XL", 134900, 12)]),
    ("Logo T-Shirt", "Merch Co", "Apparel", "Everyday 100% cotton tee.", [("TEE-M", "M", 49900, 60), ("TEE-L", "L", 49900, 45)]),
    ("Ceramic Mug", "Merch Co", "Drinkware", "350 ml glazed ceramic mug, dishwasher safe.", [("MUG-WHT", "White", 34900, 80), ("MUG-BLK", "Black", 34900, 4)]),
    ("Sticker Pack", "Merch Co", "Stickers", "Ten weatherproof vinyl stickers.", [("STK-10", "Default", 19900, 200)]),
]


def seed() -> str:
    with SessionLocal() as db:
        if db.scalars(select(Product.id)).first():
            return "Products already exist, nothing loaded."
        cats = {n: Category(name=n, slug=slugify(n)) for n in _CATS}
        db.add_all(cats.values())
        db.flush()
        for title, brand, cat, desc, variants in _PRODUCTS:
            p = Product(title=title, slug=slugify(title), brand=brand, description=desc, category_id=cats[cat].id)
            p.variants = [Variant(sku=s, title=t, price_cents=pr, stock=st) for s, t, pr, st in variants]
            p.images = [ProductImage(url=_IMG + title.replace(" ", "+"), position=0)]
            db.add(p)
        if not db.scalars(select(Coupon.id).where(Coupon.code == "WELCOME10")).first():
            db.add(Coupon(code="WELCOME10", percent_off=10))
        db.commit()
    return f"Loaded {len(_PRODUCTS)} products, {len(_CATS)} categories and coupon WELCOME10."


if __name__ == "__main__":
    from . import main  # noqa: F401  (imports every model so create_all sees all tables)
    from .db import Base, engine
    Base.metadata.create_all(engine)
    print(seed())
