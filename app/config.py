import os, secrets, logging
from dotenv import load_dotenv
load_dotenv()
log = logging.getLogger("store")


class Settings:
    def __init__(self):
        g = os.getenv
        self.env = g("APP_ENV", "dev")
        self.database_url = g("DATABASE_URL", "sqlite:///./store.db")
        self.cors_origins = [o.strip() for o in g("CORS_ORIGINS", "").split(",") if o.strip()]
        self.admin_email = g("ADMIN_EMAIL", "").lower()
        self.admin_password = g("ADMIN_PASSWORD", "")
        self.currency = g("CURRENCY", "inr").lower()
        self.shipping_flat = int(g("SHIPPING_FLAT_MINOR", "4900"))
        self.free_shipping_over = int(g("FREE_SHIPPING_OVER_MINOR", "99900"))
        self.tax_percent = float(g("TAX_PERCENT", "0"))
        self.unpaid_expiry_minutes = int(g("UNPAID_EXPIRY_MINUTES", "30"))
        self.run_expiry_loop = g("RUN_EXPIRY_LOOP", "true").lower() == "true"
        self.payment_provider = g("PAYMENT_PROVIDER", "mock").lower()
        self.stripe_secret = g("STRIPE_SECRET_KEY", "")
        self.stripe_publishable = g("STRIPE_PUBLISHABLE_KEY", "")
        self.stripe_webhook_secret = g("STRIPE_WEBHOOK_SECRET", "")
        self.razorpay_key_id = g("RAZORPAY_KEY_ID", "")
        self.razorpay_key_secret = g("RAZORPAY_KEY_SECRET", "")
        self.razorpay_webhook_secret = g("RAZORPAY_WEBHOOK_SECRET", "")
        self.smtp_host = g("SMTP_HOST", "")
        self.smtp_port = int(g("SMTP_PORT", "587"))
        self.smtp_user = g("SMTP_USER", "")
        self.smtp_password = g("SMTP_PASSWORD", "")
        self.mail_from = g("MAIL_FROM", "orders@example.com")
        self.jwt_secret = g("JWT_SECRET", "")
        if self.env == "production":
            if not self.jwt_secret:
                raise RuntimeError("JWT_SECRET must be set in production")
            if self.payment_provider == "mock":
                raise RuntimeError("PAYMENT_PROVIDER=mock is not allowed in production")
        if not self.jwt_secret:
            self.jwt_secret = secrets.token_hex(32)
            log.warning("JWT_SECRET not set; using a random one (tokens reset on restart)")


settings = Settings()
