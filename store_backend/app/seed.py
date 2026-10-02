"""Dev sample data:  python -m app.seed"""
from sqlalchemy import select
from .config import settings
from .db import Base, SessionLocal, engine
from .models import Category, Coupon, Product, ProductImage, Variant
from .routers.catalog import slugify

if __name__ == "__main__":
    if settings.env == "production":
        raise SystemExit("Refusing to seed in production")
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        if db.scalars(select(Product.id)).first():
            raise SystemExit("Already seeded")
        apparel = Category(name="Apparel", slug="apparel")
        db.add_all([apparel, Category(name="Drinkware", slug="drinkware"), Category(name="Accessories", slug="accessories")])
        db.flush()
        db.add(Category(name="T-Shirts", slug="t-shirts", parent_id=apparel.id))
        db.flush()
        tees = db.scalars(select(Category).where(Category.slug == "t-shirts")).one()
        db.add_all([
            Product(title="Classic Logo Tee", slug="classic-logo-tee", brand="YourBrand", category_id=tees.id,
                    description="240gsm cotton tee.", variants=[Variant(sku=f"TEE-{c}-{s}", title=f"{s} / {c}", price_cents=79900, stock=25)
                                                                  for c in ("BLK", "WHT") for s in ("S", "M", "L")],
                    images=[ProductImage(url="https://example.com/tee.jpg", alt="Classic Logo Tee")]),
            Product(title="Ceramic Mug", slug="ceramic-mug", brand="YourBrand", category_id=db.scalars(select(Category).where(Category.slug == "drinkware")).one().id,
                    description="350ml, dishwasher safe.", variants=[Variant(sku="MUG-01", price_cents=39900, compare_at_cents=49900, stock=40)]),
            Product(title="Sticker Pack", slug="sticker-pack", brand="YourBrand", category_id=db.scalars(select(Category).where(Category.slug == "accessories")).one().id,
                    description="10 vinyl stickers.", variants=[Variant(sku="STK-10", price_cents=19900, stock=100)]),
        ])
        db.add(Coupon(code="WELCOME10", percent_off=10))
        db.commit()
    print("seeded categories, 3 products, coupon WELCOME10")
