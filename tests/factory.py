"""Builds a small marketplace through your REAL service layer (cart -> create_order -> mark_paid).
If your models need extra required columns, adjust here only."""
from app.models import Address, Cart, CartItem, Category, Product, User, Variant
from app.models_market import ProductSeller, Seller
from app.security import hash_password
from app.services import orders as osvc


def user(db, email, role="customer", pw="password1"):
    u = User(email=email, password_hash=hash_password(pw), full_name=email.split("@")[0], role=role)
    db.add(u)
    db.commit()
    return u


def seller(db, name, pct, status="approved"):
    u = user(db, f"{name.lower()}@shop.test")
    s = Seller(user_id=u.id, name=name, slug=name.lower(), status=status, commission_pct=pct)
    db.add(s)
    db.commit()
    return u, s


def product(db, title, price, seller_id=None, stock=10, cat=None):
    p = Product(title=title, slug=title.lower().replace(" ", "-"), description=f"{title} description", category_id=cat.id if cat else None)
    p.variants = [Variant(sku=f"SKU-{title[:6]}-{price}", title="Default", price_cents=price, stock=stock)]
    db.add(p)
    db.commit()
    if seller_id:
        db.add(ProductSeller(product_id=p.id, seller_id=seller_id))
        db.commit()
    return p


def paid_order(db, buyer, lines, state="Karnataka", pin="560001", pay=True, coupon=None, idem=None):
    """lines = [(product, qty)] -> a paid Order created by your real create_order/mark_paid."""
    cart = db.query(Cart).filter_by(user_id=buyer.id).first()
    if not cart:
        cart = Cart(user_id=buyer.id)
        db.add(cart)
        db.commit()
    for p, q in lines:
        db.add(CartItem(cart_id=cart.id, variant_id=p.variants[0].id, qty=q))
    a = Address(user_id=buyer.id, full_name="Buyer One", phone="9999999999", line1="1 MG Road", city="Bengaluru", state=state, postal_code=pin, country="IN")
    db.add(a)
    db.commit()
    db.refresh(cart)
    o = osvc.create_order(db, buyer, a.id, coupon, idem)
    if pay:
        assert osvc.mark_paid(db, o, f"pay_{o.number}", o.total_cents)
    return o
