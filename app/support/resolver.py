"""Autonomous agent: customer message -> intent -> try to resolve against the real store -> otherwise ALWAYS ticket + escalate + email.
Logged-in customers only (identity comes from the session, never from the request body). Logged-out visitors can only ask for a
password-reset link."""
import re
from sqlalchemy.orm import Session
from ..models import now
from ..notify import send_email
from ..ext import accounts
from ..ext.config import cfg as ext_cfg
from . import ecommerce_service as shop, sla
from .chatbot import classify_intent
from .config import cfg
from .models import Channel, Escalation, Priority, ResolutionType, Ticket, TicketStatus, Worklog

ORDER_RE = re.compile(r"\bORD-[0-9A-F]{8}\b", re.I)


def _ticket(db: Session, user, channel, subject, description, priority, status, resolution, assigned, order_number=None) -> Ticket:
    t = Ticket(user_id=user.id, customer_email=user.email, customer_name=user.full_name or "", channel=channel, subject=subject[:255],
               description=description, status=status, priority=priority, resolution_type=resolution, assigned_to=assigned,
               order_number=order_number, sla_due_at=sla.compute_sla_due(priority),
               resolved_at=now() if status == TicketStatus.resolved else None)
    db.add(t)
    db.flush()
    return t


def escalate(db: Session, user, channel, subject, description, priority, reason, team="engineering", order_number=None) -> Ticket:
    """Always-create-a-ticket path for anything the bot can't finish."""
    t = _ticket(db, user, channel, subject, description, priority, TicketStatus.escalated, ResolutionType.engineering, None, order_number)
    db.add_all([Worklog(ticket_id=t.id, actor="autonomous_agent", action="created", note="Ticket auto-created after autonomous resolution attempt failed."),
                Worklog(ticket_id=t.id, actor="autonomous_agent", action="escalated", note=reason),
                Escalation(ticket_id=t.id, team=team, reason=reason, notified_email=cfg.escalation_email)])
    db.commit()
    send_email(cfg.escalation_email, f"[Escalation] Ticket {t.id} - {subject}",
               f"Reason: {reason}\nCustomer: {user.email}\nChannel: {channel.value}\nPriority: {priority.value}\nOrder: {order_number or '-'}")
    return t


def _resolved(db: Session, user, channel, subject, description, order_number=None) -> Ticket:
    t = _ticket(db, user, channel, subject, description, Priority.low, TicketStatus.resolved, ResolutionType.autonomous, "autonomous_agent", order_number)
    db.add(Worklog(ticket_id=t.id, actor="autonomous_agent", action="resolved", note="Resolved automatically without human involvement."))
    db.commit()
    return t


def resolve(db: Session, user, message: str, channel: Channel = Channel.chat, order_number: str | None = None,
            anon_email: str | None = None) -> tuple[str, bool, str, Ticket | None]:
    """Returns (intent, resolved, reply, ticket_or_none)."""
    intent = classify_intent(message)

    if intent == "password_reset":
        email = user.email if user else (anon_email or "")
        if not email:
            return intent, False, "Please tell me the email address of your account so I can send a reset link.", None
        accounts.start_reset(db, email)      # rate limited, silent about whether the account exists
        reply = f"If there is an account for {email}, a password reset link is on its way (valid for {ext_cfg.reset_ttl_minutes} minutes)."
        ticket = _resolved(db, user, channel, "Password reset", message) if user else None
        return intent, True, reply, ticket

    if not user:
        return intent, False, "Please log in so I can look up your orders and account.", None

    if intent in ("order_status", "invoice_lookup", "refund_request", "shipping_status"):
        match = ORDER_RE.search(message or "")
        order_number = (order_number or (match.group(0) if match else "")).strip().upper()
        if not order_number:
            return intent, False, "Could you share your order number (it looks like ORD-1A2B3C4D) so I can look that up?", None
        try:
            if intent == "order_status":
                d = shop.get_order_status(db, user, order_number)
                reply = f"Order {d['order_number']} is currently '{d['order_status']}' (shipping: {d['shipping_status']})."
            elif intent == "invoice_lookup":
                d = shop.get_invoice(db, user, order_number)
                if not d["invoices"]:
                    return intent, False, f"There is no invoice for order {d['order_number']} yet ({d.get('note', 'not paid')}).", None
                reply = f"Invoice(s) for order {d['order_number']}: " + ", ".join(f"{i['number']} (PDF: {i['pdf']})" for i in d["invoices"])
            elif intent == "refund_request":
                d = shop.initiate_refund(db, user, order_number, message)
                if not d["success"]:
                    t = escalate(db, user, channel, "Refund request needs review", message, Priority.medium, d["reason"], order_number=order_number)
                    return intent, False, "I couldn't process an automatic refund for this order. I've created a ticket and sent it to our team.", t
                reply = f"Order {d['order_number']} is now '{d['status']}'. If you paid, the refund is on its way (3-5 business days)."
            else:
                d = shop.get_shipping_status(db, user, order_number)
                reply = f"Order {d['order_number']} shipping: {d['summary']}."
            return intent, True, reply, _resolved(db, user, channel, intent.replace("_", " ").title(), message, order_number)
        except shop.OrderNotFound:
            t = escalate(db, user, channel, f"Order lookup failed ({intent})", message, Priority.medium,
                         f"Order number '{order_number}' not found for this customer.", order_number=order_number)
            return intent, False, "I couldn't find that order on your account. I've created a ticket and our team will follow up.", t

    if intent == "api_usage_clarification":
        t = escalate(db, user, channel, "API usage question", message, Priority.medium, "API usage questions require engineering/docs team input.")
        return intent, False, "Let me get our team to help with that. I've opened a ticket for you.", t
    if intent == "marketing":
        return intent, False, "I'll pass this to our marketing team. They'll be in touch!", None

    t = escalate(db, user, channel, "General support request", message, Priority.medium, "Autonomous agent could not classify/resolve this request.")
    return intent, False, "I've created a support ticket for this and our team will get back to you shortly.", t
