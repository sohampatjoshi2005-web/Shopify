"""Extension settings. Read from the environment on every access, so tests and Streamlit secrets can change them at runtime."""
import os


def _s(k: str, d: str = "") -> str:
    return os.getenv(k, d)


def _i(k: str, d: int) -> int:
    try:
        return int(_s(k, str(d)))
    except ValueError:
        return d


class _Cfg:
    # --- payouts
    @property
    def payout_hold_days(self): return _i("EXT_PAYOUT_HOLD_DAYS", 7)          # sale proceeds become payable after this
    @property
    def payout_min_cents(self): return _i("EXT_PAYOUT_MIN_CENTS", 0)
    @property
    def default_commission_pct(self): return float(_s("EXT_DEFAULT_COMMISSION_PCT", "10"))
    @property
    def coupon_funded_by(self): return _s("EXT_COUPON_FUNDED_BY", "platform").lower()   # platform: seller is paid on the list price | seller: on what the buyer paid
    # --- returns
    @property
    def return_window_days(self): return _i("EXT_RETURN_WINDOW_DAYS", 10)
    # --- GST
    @property
    def gst_mode(self): return _s("GST_MODE", "inclusive").lower()            # inclusive | exclusive
    @property
    def gst_default_rate(self): return float(_s("GST_DEFAULT_RATE", "18"))
    @property
    def platform_legal_name(self): return _s("GST_PLATFORM_NAME", "Store")
    @property
    def platform_gstin(self): return _s("GST_PLATFORM_GSTIN", "")
    @property
    def platform_state_code(self): return _s("GST_PLATFORM_STATE_CODE", "")
    @property
    def platform_address(self): return _s("GST_PLATFORM_ADDRESS", "")
    # --- shipping
    @property
    def shipping_provider(self): return _s("SHIPPING_PROVIDER", "mock").lower()  # mock | shiprocket
    @property
    def default_pickup_pincode(self): return _s("SHIPPING_DEFAULT_PICKUP_PINCODE", "110001")
    @property
    def default_weight_g(self): return _i("SHIPPING_DEFAULT_WEIGHT_G", 500)
    @property
    def shipping_webhook_token(self): return _s("SHIPPING_WEBHOOK_TOKEN", "")
    @property
    def shiprocket_email(self): return _s("SHIPROCKET_EMAIL")
    @property
    def shiprocket_password(self): return _s("SHIPROCKET_PASSWORD")
    # --- search
    @property
    def search_provider(self): return _s("SEARCH_PROVIDER", "sql").lower()    # sql | meilisearch
    @property
    def meili_url(self): return _s("MEILI_URL", "http://localhost:7700").rstrip("/")
    @property
    def meili_key(self): return _s("MEILI_KEY")
    @property
    def meili_index(self): return _s("MEILI_INDEX", "products")
    # --- accounts
    @property
    def app_base_url(self): return _s("APP_BASE_URL", "http://localhost:8501").rstrip("/")
    @property
    def verify_ttl_hours(self): return _i("AUTH_VERIFY_TTL_HOURS", 48)
    @property
    def reset_ttl_minutes(self): return _i("AUTH_RESET_TTL_MINUTES", 60)
    @property
    def tokens_per_hour(self): return _i("AUTH_TOKENS_PER_HOUR", 3)


cfg = _Cfg()
