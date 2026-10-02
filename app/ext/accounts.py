"""Email verification, password reset and wishlist helpers. Tokens are random, single-use, stored only as SHA-256 hashes."""
import hashlib
import secrets
from datetime import timedelta
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from ..models import User, aware, now
from ..notify import send_email
from ..security import hash_password
from .config import cfg
from .models import AuthToken, UserFlag


def _h(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def too_many(db: Session, user_id: int, purpose: str) -> bool:
    n = db.scalar(select(func.count()).select_from(AuthToken).where(AuthToken.user_id == user_id, AuthToken.purpose == purpose, AuthToken.created_at > now() - timedelta(hours=1)))
    return (n or 0) >= cfg.tokens_per_hour


def issue(db: Session, user_id: int, purpose: str, ttl: timedelta) -> str:
    raw = secrets.token_urlsafe(32)
    db.add(AuthToken(user_id=user_id, purpose=purpose, token_hash=_h(raw), expires_at=now() + ttl))
    db.commit()
    return raw


def consume(db: Session, raw: str, purpose: str) -> int | None:
    """Return the token's user id and burn it, or None if unknown / used / expired. Atomic: a token can only win once."""
    t = db.scalars(select(AuthToken).where(AuthToken.token_hash == _h(raw or ""), AuthToken.purpose == purpose)).first()
    if not t or t.used_at is not None or aware(t.expires_at) < now():
        return None
    res = db.execute(update(AuthToken).where(AuthToken.id == t.id, AuthToken.used_at.is_(None)).values(used_at=now()))
    db.commit()
    return t.user_id if res.rowcount == 1 else None


def is_verified(db: Session, user_id: int) -> bool:
    f = db.get(UserFlag, user_id)
    return bool(f and f.email_verified_at)


def _flag(db: Session, user_id: int) -> UserFlag:
    f = db.get(UserFlag, user_id)
    if not f:
        f = UserFlag(user_id=user_id)
        db.add(f)
    return f


def send_verification(db: Session, user: User) -> bool:
    if is_verified(db, user.id) or too_many(db, user.id, "verify"):
        return False
    raw = issue(db, user.id, "verify", timedelta(hours=cfg.verify_ttl_hours))
    send_email(user.email, "Verify your email", f"Confirm your email address:\n{cfg.app_base_url}/?verify_token={raw}\n\nOr paste this code in the app: {raw}\n\nThis link expires in {cfg.verify_ttl_hours} hours.")
    return True


def verify(db: Session, raw: str) -> bool:
    uid = consume(db, raw, "verify")
    if uid is None:
        return False
    _flag(db, uid).email_verified_at = now()
    db.commit()
    return True


def start_reset(db: Session, email: str) -> None:
    """Always silent about whether the account exists (no user enumeration)."""
    u = db.scalars(select(User).where(User.email == (email or "").strip().lower())).first()
    if not u or too_many(db, u.id, "reset"):
        return
    raw = issue(db, u.id, "reset", timedelta(minutes=cfg.reset_ttl_minutes))
    send_email(u.email, "Reset your password", f"Use this link to choose a new password:\n{cfg.app_base_url}/?reset_token={raw}\n\nOr paste this code in the app: {raw}\n\n"
                                               f"It expires in {cfg.reset_ttl_minutes} minutes. If you didn't ask for this, ignore this email.")


def finish_reset(db: Session, raw: str, new_password: str) -> bool:
    uid = consume(db, raw, "reset")
    if uid is None:
        return False
    u = db.get(User, uid)
    u.password_hash = hash_password(new_password)
    f = _flag(db, uid)
    f.password_changed_at, f.email_verified_at = now(), f.email_verified_at or now()   # they just proved they control the mailbox
    db.execute(update(AuthToken).where(AuthToken.user_id == uid, AuthToken.purpose == "reset", AuthToken.used_at.is_(None)).values(used_at=now()))  # kill other reset links
    db.commit()
    send_email(u.email, "Your password was changed", "If this wasn't you, reset your password again and contact support.")
    return True
