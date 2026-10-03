"""Support-desk settings. Read from the environment on every access (so Streamlit secrets and tests can change them at runtime)."""
import os


def _f(k: str, d: float) -> float:
    try:
        return float(os.getenv(k, str(d)))
    except ValueError:
        return d


class _Cfg:
    @property
    def sla_hours_by_priority(self) -> dict:
        return {"critical": _f("SLA_CRITICAL_HOURS", 2), "high": _f("SLA_HIGH_HOURS", 8),
                "medium": _f("SLA_MEDIUM_HOURS", 72), "low": _f("SLA_LOW_HOURS", 72)}

    @property
    def escalation_email(self) -> str:
        return os.getenv("SUPPORT_ESCALATION_EMAIL", "engineering-oncall@company.com")

    @property
    def cag_ttl_minutes(self) -> float:
        return _f("CAG_TTL_MINUTES", 30)


cfg = _Cfg()
