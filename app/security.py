import base64, hashlib, hmac, os, time
import jwt
from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session
from .config import settings
from .db import get_db
from .models import User, aware


def _b(x: bytes) -> str:
    return base64.b64encode(x).decode()


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${_b(salt)}${_b(dk)}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, salt, dk = stored.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except Exception:
        return False


def create_token(user: User, hours: int = 12) -> str:
    t = int(time.time())
    return jwt.encode({"sub": str(user.id), "role": user.role, "iat": t, "exp": t + hours * 3600},
                      settings.jwt_secret, algorithm="HS256")


make_token = create_token  # alias used by the test-suite


def current_user(authorization: str | None = Header(None), db: Session = Depends(get_db)) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing token")
    try:
        claims = jwt.decode(authorization.split(" ", 1)[1], settings.jwt_secret, algorithms=["HS256"])
        uid, iat = int(claims["sub"]), int(claims.get("iat", 0))
    except (jwt.PyJWTError, KeyError, ValueError, TypeError):
        raise HTTPException(401, "Invalid or expired token")
    user = db.get(User, uid)  # role is re-read from DB, never trusted from the token
    if not user or not user.is_active:
        raise HTTPException(401, "Account disabled")
    if _issued_before_password_change(db, uid, iat):
        raise HTTPException(401, "Session expired, please log in again")
    return user


def _issued_before_password_change(db: Session, uid: int, iat: int) -> bool:
    """A password reset logs out every session that existed before it (tokens carry `iat`)."""
    from .ext.models import UserFlag          # local import: ext depends on this module
    f = db.get(UserFlag, uid)
    changed = aware(f.password_changed_at) if f and f.password_changed_at else None
    return bool(changed and iat < int(changed.timestamp()))


def admin_user(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Admin only")
    return user
