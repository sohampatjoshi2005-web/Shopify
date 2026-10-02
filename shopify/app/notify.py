import logging

log = logging.getLogger("store")


def send_email(to: str, subject: str, body: str) -> None:
    """Placeholder transport: logs the message. Never raises, so a mail problem can't break an order."""
    try:
        log.info("EMAIL to=%s subject=%s", to, subject)
    except Exception:  # pragma: no cover
        pass
