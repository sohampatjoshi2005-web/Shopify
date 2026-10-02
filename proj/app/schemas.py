from datetime import datetime
from pydantic import BaseModel, ConfigDict, EmailStr, Field, computed_field


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(default="", max_length=255)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class AddressIn(BaseModel):
    full_name: str = Field(min_length=1, max_length=255)
    phone: str = Field(min_length=5, max_length=32)
    line1: str = Field(min_length=1, max_length=255)
    line2: str | None = None
    city: str
    state: str
    postal_code: str = Field(min_length=3, max_length=16)
    country: str = Field(default="IN", min_length=2, max_length=2)
    is_default: bool = False


class VariantIn(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    title: str = "Default"
    price_cents: int = Field(gt=0)
    compare_at_cents: int | None = Field(default=None, gt=0)
    stock: int = Field(default=0, ge=0)


class VariantPatch(BaseModel):
    title: str | None = None
    price_cents: int | None = Field(default=None, gt=0)
    compare_at_cents: int | None = Field(default=None, gt=0)
    stock: int | None = Field(default=None, ge=0)
    is_active: bool | None = None


class ProductIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str = ""
    brand: str | None = None
    category_slug: str | None = None
    variants: list[VariantIn] = Field(min_length=1)
    image_urls: list[str] = []


class ProductPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    brand: str | None = None
    category_slug: str | None = None
    is_active: bool | None = None


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    parent_slug: str | None = None


class CartAdd(BaseModel):
    variant_id: int
    qty: int = Field(default=1, ge=1, le=100)


class CartSet(BaseModel):
    qty: int = Field(ge=0, le=100)


class CheckoutIn(BaseModel):
    address_id: int
    coupon_code: str | None = None


class ReviewIn(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str = Field(default="", max_length=4000)


class CouponIn(BaseModel):
    code: str = Field(min_length=3, max_length=64)
    percent_off: int | None = Field(default=None, ge=1, le=100)
    amount_off_cents: int | None = Field(default=None, gt=0)
    min_subtotal_cents: int = Field(default=0, ge=0)
    max_uses: int | None = Field(default=None, gt=0)
    expires_at: datetime | None = None


class ShipIn(BaseModel):
    tracking_number: str = Field(min_length=1, max_length=100)
    carrier: str = Field(default="", max_length=64)


# ---------- outputs ----------
class VariantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    sku: str
    title: str
    price_cents: int
    compare_at_cents: int | None
    stock: int
    is_active: bool


class ImageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    url: str
    alt: str


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    title: str
    slug: str
    description: str
    brand: str | None
    category_id: int | None
    is_active: bool
    rating_avg: float
    rating_count: int
    images: list[ImageOut]
    variants: list[VariantOut]

    @computed_field
    @property
    def price_from_cents(self) -> int | None:
        return min((v.price_cents for v in self.variants if v.is_active), default=None)

    @computed_field
    @property
    def in_stock(self) -> bool:
        return any(v.is_active and v.stock > 0 for v in self.variants)


def public_product(p) -> ProductOut:
    out = ProductOut.model_validate(p)
    out.variants = [v for v in out.variants if v.is_active]
    return out


def order_out(o) -> dict:
    d = {
        "number": o.number, "status": o.status, "currency": o.currency,
        "subtotal_cents": o.subtotal_cents, "discount_cents": o.discount_cents,
        "shipping_cents": o.shipping_cents, "tax_cents": o.tax_cents, "total_cents": o.total_cents,
        "coupon_code": o.coupon_code, "shipping_address": o.shipping_address,
        "tracking_number": o.tracking_number, "carrier": o.carrier,
        "created_at": o.created_at.isoformat(), "paid_at": o.paid_at.isoformat() if o.paid_at else None,
        "items": [{"sku": i.sku, "title": i.title, "unit_price_cents": i.unit_price_cents, "qty": i.qty,
                   "line_total_cents": i.unit_price_cents * i.qty} for i in o.items],
    }
    if o.status == "pending_payment":
        d["payment"] = {"provider": o.payment_provider, **(o.payment_client or {})}
    return d
