from datetime import datetime, timedelta
from ..models import aware, now
from .config import cfg
from .models import Priority


def compute_sla_due(priority: Priority, from_time: datetime | None = None) -> datetime:
    """SLA deadline by priority (hours configurable through SLA_*_HOURS env vars)."""
    return (aware(from_time) if from_time else now()) + timedelta(hours=cfg.sla_hours_by_priority[Priority(priority).value])


def is_breached(sla_due_at: datetime, resolved_at: datetime | None = None) -> bool:
    return (aware(resolved_at) if resolved_at else now()) > aware(sla_due_at)
