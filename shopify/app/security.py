import hashlib
import hmac
import os
import time
import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session
from .config import settings
from .db import get_db
from .models import User

_ITER = 200_000
_bearer = HTTPBearer(auto_error=False)  # we raise 401 ourselves (FastAPI's default would be 403)


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, _ITER)
    return f"pbkdf2_sha256${_ITER}${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, it, salt, dk = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(calc.hex(), dk)
    except Exception:
        return False


def make_token(user: User) -> str:
    return jwt.encode({"sub": str(user.id), "exp": int(time.time()) + settings.jwt_minutes * 60}, settings.jwt_secret, algorithm="HS256")


def current_user(creds: HTTPAuthorizationCredentials | None = Depends(_bearer), db: Session = Depends(get_db)) -> User:
    unauth = HTTPException(401, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    if not creds:
        raise unauth
    try:
        uid = int(jwt.decode(creds.credentials, settings.jwt_secret, algorithms=["HS256"])["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise unauth
    user = db.get(User, uid)
    if not user:
        raise unauth
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if user.role != "admin":  # role is read from the DB on every request, never trusted from the token
        raise HTTPException(403, "Admins only")
    return user
