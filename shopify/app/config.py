"""Settings, read once from the environment (and an optional .env file) at import time."""
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv() -> None:
    p = Path(__file__).resolve().parent.parent / ".env"
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def _env(k: str, d: str = "") -> str:
    return os.getenv(k, d)


def _int(k: str, d: int) -> int:
    try:
        return int(_env(k, str(d)))
    except ValueError:
        return d


def _bool(k: str, d: bool) -> bool:
    return _env(k, str(d)).strip().lower() in ("1", "true", "yes", "on")


def _database_url() -> str:
    url = _env("DATABASE_URL").strip()
    if not url:  # local default; falls back to the temp dir if the app folder is read-only
        for base in (Path.cwd(), Path(tempfile.gettempdir())):
            try:
                f = base / "store.db"
                f.touch()
                return f"sqlite:///{f}"
            except OSError:
                continue
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


@dataclass(frozen=True)
class Settings:
    app_env: str
    database_url: str
    jwt_secret: str
    jwt_minutes: int
    admin_email: str
    admin_password: str
    payment_provider: str
    currency: str
    cors_origins: list
    run_expiry_loop: bool
    unpaid_expiry_minutes: int
    shipping_flat_cents: int
    free_shipping_over_cents: int
    tax_rate_pct: float
    stripe_secret_key: str
    stripe_publishable_key: str
    stripe_webhook_secret: str
    razorpay_key_id: str
    razorpay_key_secret: str
    razorpay_webhook_secret: str
    shopify_api_secret: str
    shopify_api_version: str


def load() -> Settings:
    s = Settings(
        app_env=_env("APP_ENV", "dev").lower(),
        database_url=_database_url(),
        jwt_secret=_env("JWT_SECRET", "dev-only-change-me"),
        jwt_minutes=_int("JWT_MINUTES", 60 * 24 * 7),
        admin_email=_env("ADMIN_EMAIL").strip().lower(),
        admin_password=_env("ADMIN_PASSWORD"),
        payment_provider=_env("PAYMENT_PROVIDER", "mock").lower(),
        currency=_env("CURRENCY", "INR").upper(),
        cors_origins=[o.strip() for o in _env("CORS_ORIGINS", "*").split(",") if o.strip()],
        run_expiry_loop=_bool("RUN_EXPIRY_LOOP", True),
        unpaid_expiry_minutes=_int("UNPAID_EXPIRY_MINUTES", 30),
        shipping_flat_cents=_int("SHIPPING_FLAT_CENTS", 4900),
        free_shipping_over_cents=_int("FREE_SHIPPING_OVER_CENTS", 99900),
        tax_rate_pct=float(_env("TAX_RATE_PCT", "0") or 0),
        stripe_secret_key=_env("STRIPE_SECRET_KEY"),
        stripe_publishable_key=_env("STRIPE_PUBLISHABLE_KEY"),
        stripe_webhook_secret=_env("STRIPE_WEBHOOK_SECRET"),
        razorpay_key_id=_env("RAZORPAY_KEY_ID"),
        razorpay_key_secret=_env("RAZORPAY_KEY_SECRET"),
        razorpay_webhook_secret=_env("RAZORPAY_WEBHOOK_SECRET"),
        shopify_api_secret=_env("SHOPIFY_API_SECRET"),
        shopify_api_version=_env("SHOPIFY_API_VERSION", "2025-07"),
    )
    if s.payment_provider not in ("mock", "stripe", "razorpay"):
        raise RuntimeError("PAYMENT_PROVIDER must be mock, stripe or razorpay")
    if s.app_env == "production":  # refuse to boot with unsafe settings
        problems = []
        if s.jwt_secret == "dev-only-change-me" or len(s.jwt_secret) < 32:
            problems.append("JWT_SECRET must be a random string of 32+ characters")
        if s.database_url.startswith("sqlite"):
            problems.append("DATABASE_URL must be Postgres")
        if s.payment_provider == "mock":
            problems.append("PAYMENT_PROVIDER must be stripe or razorpay")
        if problems:
            raise RuntimeError("Refusing to start in production: " + "; ".join(problems))
    return s


settings = load()
