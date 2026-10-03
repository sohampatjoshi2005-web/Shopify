import smtplib, logging
from email.message import EmailMessage
from .config import settings
log = logging.getLogger("store")
OUTBOX: list[dict] = []   # last emails sent, kept in memory outside production (tests read verification/reset codes from here)


def send_email(to: str, subject: str, body: str) -> None:
    if settings.env != "production":
        OUTBOX.append({"to": to, "subject": subject, "body": body})
        del OUTBOX[:-50]
    if not settings.smtp_host:
        log.info("EMAIL (no SMTP configured) to=%s subject=%s", to, subject)
        return
    try:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = settings.mail_from, to, subject
        msg.set_content(body)
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as s:
            s.starttls()
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
    except Exception:
        log.exception("email send failed")
