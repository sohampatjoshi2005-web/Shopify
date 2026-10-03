"""Recommendation settings. Read from the environment on every access (tests and Streamlit secrets can change them at runtime)."""
import os


def _i(k: str, d: int) -> int:
    try:
        return int(os.getenv(k, str(d)))
    except ValueError:
        return d


class _Cfg:
    @property
    def popular_days(self) -> int: return _i("REC_POPULAR_DAYS", 90)        # window for "what is selling"
    @property
    def lapsed_days(self) -> int: return _i("REC_LAPSED_DAYS", 90)          # a customer with no order for this long is "lapsed"
    @property
    def min_ratings(self) -> int: return _i("REC_MIN_RATINGS", 3)           # ratings needed before a star average counts
    @property
    def pool_size(self) -> int: return _i("REC_POOL_SIZE", 200)             # max candidates considered per request


cfg = _Cfg()
