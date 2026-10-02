import os, secrets, logging
from dotenv import load_dotenv
load_dotenv()
log = logging.getLogger("app")

class Settings:
    def __init__(self):
        g = os.getenv
        self.env = g("APP_ENV", "dev")
        self.database_url = g("DATABASE_URL", "sqlite:///./app.db")
        self.admin_email = g("ADMIN_EMAIL", "")
        self.admin_password = g("ADMIN_PASSWORD", "")
        self.widget_api_key = g("WIDGET_API_KEY", "")
        self.cors_origins = [o for o in g("CORS_ORIGINS", "").split(",") if o]
        self.engineering_email = g("ENGINEERING_EMAIL", "oncall@example.com")
        self.refund_window_days = int(g("REFUND_WINDOW_DAYS", "30"))
        self.smtp_host = g("SMTP_HOST", "")
        self.smtp_port = int(g("SMTP_PORT", "587"))
        self.smtp_user = g("SMTP_USER", "")
        self.smtp_password = g("SMTP_PASSWORD", "")
        self.mail_from = g("MAIL_FROM", "support@example.com")
        # Shopify (leave blank to run in mock mode against the local orders table)
        self.shop_domain = g("SHOPIFY_STORE_DOMAIN", "")
        self.shop_admin_token = g("SHOPIFY_ADMIN_TOKEN", "")
        self.shop_storefront_token = g("SHOPIFY_STOREFRONT_TOKEN", "")
        self.shop_webhook_secret = g("SHOPIFY_WEBHOOK_SECRET", "")
        self.shop_api_version = g("SHOPIFY_API_VERSION", "2025-07")
        self.jwt_secret = g("JWT_SECRET", "")
        if not self.jwt_secret:
            if self.env == "production":
                raise RuntimeError("JWT_SECRET must be set in production")
            self.jwt_secret = secrets.token_hex(32)
            log.warning("JWT_SECRET not set; using a random one (dev only)")

    @property
    def shopify_live(self):
        return bool(self.shop_domain and self.shop_admin_token)

settings = Settings()
